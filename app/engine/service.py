import hashlib
import json
import logging

from sqlalchemy import func, or_, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app import audit
from app.db import SessionLocal
from app.engine.runner import advance
from app.engine.states import InvalidTransition, Status, can_move
from app.errors import Conflict, Forbidden, NotFound
from app.models import AuditLog, StepRun, User, Workflow, utcnow

log = logging.getLogger("relayforge.service")


def _hash(payload: dict) -> str:
    return hashlib.sha256(json.dumps(payload, sort_keys=True).encode()).hexdigest()


def _find_by_key(db: Session, tenant_id: int, key: str) -> Workflow | None:
    return db.scalar(select(Workflow).where(
        Workflow.tenant_id == tenant_id, Workflow.idempotency_key == key))


def create_workflow(db: Session, user: User, payload: dict, key: str | None) -> tuple[Workflow, bool]:
    """Returns (workflow, created). created is False when the idempotency key was seen before."""
    digest = _hash(payload)

    if key:
        existing = _find_by_key(db, user.tenant_id, key)
        if existing:
            return _same_request_or_conflict(existing, digest), False

    wf = Workflow(tenant_id=user.tenant_id, created_by=user.id, status=Status.PENDING.value,
                  input=payload, idempotency_key=key, request_hash=digest)
    db.add(wf)
    try:
        db.flush()
    except IntegrityError:
        # two requests with the same key raced and the other one won
        db.rollback()
        existing = _find_by_key(db, user.tenant_id, key) if key else None
        if existing is None:
            raise
        return _same_request_or_conflict(existing, digest), False

    audit.record(db, wf.tenant_id, "workflow_created", workflow_id=wf.id,
                 actor=f"user:{user.id}", idempotency_key=key)
    db.commit()
    return wf, True


def _same_request_or_conflict(existing: Workflow, digest: str) -> Workflow:
    if existing.request_hash != digest:
        raise Conflict("this Idempotency-Key was already used with a different request body")
    return existing


def get_workflow(db: Session, user: User, workflow_id: int) -> Workflow:
    wf = db.scalar(select(Workflow).where(
        Workflow.id == workflow_id, Workflow.tenant_id == user.tenant_id))
    if wf is None:
        raise NotFound("workflow not found")  # same answer for "doesn't exist" and "not yours"
    return wf


def list_workflows(db: Session, user: User, status: Status | None, limit: int, offset: int):
    q = select(Workflow).where(Workflow.tenant_id == user.tenant_id)
    if status:
        q = q.where(Workflow.status == status.value)
    total = db.scalar(select(func.count()).select_from(q.subquery()))
    rows = db.scalars(q.order_by(Workflow.id.desc()).limit(limit).offset(offset)).all()
    return rows, total


def audit_trail(db: Session, user: User, workflow_id: int) -> list[AuditLog]:
    wf = get_workflow(db, user, workflow_id)
    return list(db.scalars(select(AuditLog).where(AuditLog.workflow_id == wf.id)
                           .order_by(AuditLog.id).limit(1000)))


def _switch_status(db: Session, wf: Workflow, expected: Status, new: Status) -> None:
    """Compare-and-swap on the status column. When two requests race (double click on
    Approve, say) exactly one of them sees rowcount 1, the other gets a 409."""
    if not can_move(expected, new):
        raise InvalidTransition(f"cannot go from {expected.value} to {new.value}")
    result = db.execute(
        update(Workflow)
        .where(Workflow.id == wf.id, Workflow.status == expected.value)
        .values(status=new.value)
        .execution_options(synchronize_session=False))
    if result.rowcount != 1:
        db.rollback()
        raise Conflict("the workflow changed state while handling this request, check its status")


def approve(db: Session, user: User, workflow_id: int) -> Workflow:
    wf = get_workflow(db, user, workflow_id)
    if wf.status != Status.WAITING_APPROVAL.value:
        raise Conflict(f"workflow is {wf.status}, nothing to approve")
    if wf.created_by == user.id:
        raise Forbidden("you cannot approve a workflow you started yourself")

    _switch_status(db, wf, Status.WAITING_APPROVAL, Status.RUNNING)
    db.add(StepRun(workflow_id=wf.id, name="approval", status="done", attempts=1,
                   output={"approved_by": user.id}, finished_at=utcnow()))
    audit.record(db, wf.tenant_id, "approved", workflow_id=wf.id, actor=f"user:{user.id}")
    db.commit()
    db.refresh(wf)
    return wf


def reject(db: Session, user: User, workflow_id: int, reason: str | None) -> Workflow:
    wf = get_workflow(db, user, workflow_id)
    if wf.status != Status.WAITING_APPROVAL.value:
        raise Conflict(f"workflow is {wf.status}, nothing to reject")
    _switch_status(db, wf, Status.WAITING_APPROVAL, Status.REJECTED)
    audit.record(db, wf.tenant_id, "rejected", workflow_id=wf.id,
                 actor=f"user:{user.id}", reason=reason)
    db.commit()
    db.refresh(wf)
    return wf


def retry(db: Session, user: User, workflow_id: int) -> Workflow:
    """Manual retry of a failed workflow. Finished steps stay finished, only the failed one runs again."""
    wf = get_workflow(db, user, workflow_id)
    if wf.status != Status.FAILED.value:
        raise Conflict(f"workflow is {wf.status}, only failed workflows can be retried")
    _switch_status(db, wf, Status.FAILED, Status.RUNNING)
    db.execute(update(StepRun)
               .where(StepRun.workflow_id == wf.id, StepRun.status == "failed")
               .values(status="running", attempts=0, error=None)
               .execution_options(synchronize_session=False))
    audit.record(db, wf.tenant_id, "retried", workflow_id=wf.id, actor=f"user:{user.id}")
    db.commit()
    db.refresh(wf)
    return wf


def run_in_background(workflow_id: int) -> None:
    with SessionLocal() as db:
        try:
            advance(db, workflow_id)
        except Exception:
            # status stays running, the sweeper will pick it up again
            log.exception("background run of workflow %s blew up", workflow_id)


def recover_interrupted(db: Session) -> int:
    """Finds workflows that are pending/running but that nobody is working on
    (process died, restart, background task lost) and continues them."""
    now = utcnow()
    ids = db.scalars(select(Workflow.id).where(
        Workflow.status.in_([Status.PENDING.value, Status.RUNNING.value]),
        or_(Workflow.locked_until.is_(None), Workflow.locked_until < now))
        .order_by(Workflow.id).limit(100)).all()
    for wf_id in ids:
        try:
            advance(db, wf_id)
        except Exception:
            db.rollback()
            log.exception("could not resume workflow %s", wf_id)
    return len(ids)


def sweep_once() -> int:
    with SessionLocal() as db:
        return recover_interrupted(db)
