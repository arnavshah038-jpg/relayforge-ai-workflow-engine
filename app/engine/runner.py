import logging
import random
import time
import uuid
from datetime import timedelta

from pydantic import ValidationError
from sqlalchemy import or_, select, update
from sqlalchemy.orm import Session

from app import audit
from app.config import settings
from app.engine import steps
from app.engine.states import IDLE, Status, move
from app.errors import TransientError
from app.models import StepRun, Workflow, utcnow

log = logging.getLogger("relayforge.runner")

_RETRYABLE = (TransientError, ValidationError)


class StepFailed(Exception):
    def __init__(self, step: str, reason: str):
        super().__init__(f"{step}: {reason}")
        self.step = step
        self.reason = reason


class LeaseLost(Exception):
    """Another worker took the workflow over because our lease ran out."""


def advance(db: Session, workflow_id: int) -> Workflow | None:
    """Push a workflow as far as it can go.

    Safe to call at any time and from several workers: only the holder of the lease runs steps,
    and steps that already finished are never executed again."""
    wf = db.get(Workflow, workflow_id)
    if wf is None:
        return None
    owner = uuid.uuid4().hex[:12]

    # More than one pass only happens when a manager approved while we were still holding
    # the lease, in that case nobody else would have picked the workflow up.
    for _ in range(3):
        db.refresh(wf)
        if Status(wf.status) in IDLE:
            break
        if not _claim(db, workflow_id, owner):
            break  # somebody else is on it
        try:
            _advance_locked(db, wf, owner)
        except LeaseLost:
            log.warning("workflow %s: lease lost, leaving it to the new owner", wf.id)
            break
        finally:
            _release(db, workflow_id, owner)
        db.refresh(wf)
        if wf.status != Status.RUNNING.value:
            break
    return wf


def _claim(db: Session, workflow_id: int, owner: str) -> bool:
    now = utcnow()
    result = db.execute(
        update(Workflow)
        .where(Workflow.id == workflow_id,
               or_(Workflow.locked_until.is_(None), Workflow.locked_until < now))
        .values(locked_by=owner, locked_until=now + timedelta(seconds=settings.lease_seconds),
                updated_at=Workflow.updated_at)
        .execution_options(synchronize_session=False))
    db.commit()
    return result.rowcount == 1


def _heartbeat(db: Session, workflow_id: int, owner: str) -> None:
    now = utcnow()
    result = db.execute(
        update(Workflow)
        .where(Workflow.id == workflow_id, Workflow.locked_by == owner)
        .values(locked_until=now + timedelta(seconds=settings.lease_seconds),
                updated_at=Workflow.updated_at)
        .execution_options(synchronize_session=False))
    db.commit()
    if result.rowcount != 1:
        raise LeaseLost()


def _release(db: Session, workflow_id: int, owner: str) -> None:
    try:
        db.rollback()  # the session may be in a failed transaction if we got here through an error
        db.execute(
            update(Workflow)
            .where(Workflow.id == workflow_id, Workflow.locked_by == owner)
            .values(locked_by=None, locked_until=None, updated_at=Workflow.updated_at)
            .execution_options(synchronize_session=False))
        db.commit()
    except Exception:
        # worst case the lease simply expires on its own
        log.exception("could not release lease on workflow %s", workflow_id)


def _advance_locked(db: Session, wf: Workflow, owner: str) -> None:
    if wf.status == Status.PENDING.value:
        move(wf, Status.RUNNING)
        audit.record(db, wf.tenant_id, "workflow_started", workflow_id=wf.id)
        db.commit()

    try:
        _run_steps(db, wf, owner)
    except StepFailed as exc:
        move(wf, Status.FAILED)
        audit.record(db, wf.tenant_id, "workflow_failed", workflow_id=wf.id,
                     step=exc.step, reason=exc.reason)
        db.commit()
        log.warning("workflow %s failed at %s: %s", wf.id, exc.step, exc.reason)


def _run_steps(db: Session, wf: Workflow, owner: str) -> None:
    analysis = _do_step(db, wf, owner, "analyze", lambda: steps.analyze(wf)).output

    if steps.needs_approval(analysis) and _find_step(db, wf, "approval") is None:
        move(wf, Status.WAITING_APPROVAL)
        audit.record(db, wf.tenant_id, "approval_requested", workflow_id=wf.id,
                     action=analysis["action"], amount=analysis.get("refund_amount"))
        db.commit()
        return

    _do_step(db, wf, owner, "execute", lambda: steps.execute(wf, analysis))

    move(wf, Status.COMPLETED)
    audit.record(db, wf.tenant_id, "workflow_completed", workflow_id=wf.id)
    db.commit()


def _find_step(db: Session, wf: Workflow, name: str) -> StepRun | None:
    return db.scalar(select(StepRun).where(StepRun.workflow_id == wf.id, StepRun.name == name))


def _backoff(attempt: int) -> float:
    delay = min(settings.retry_max_delay_s, settings.retry_base_delay_s * 2 ** (attempt - 1))
    return delay * random.uniform(0.8, 1.2)


def _fail_step(db: Session, wf: Workflow, run: StepRun, reason: str) -> None:
    run.status = "failed"
    run.error = reason[:500]
    audit.record(db, wf.tenant_id, "step_failed", workflow_id=wf.id,
                 step=run.name, attempts=run.attempts, error=run.error)
    db.commit()
    raise StepFailed(run.name, run.error)


def _do_step(db: Session, wf: Workflow, owner: str, name: str, fn) -> StepRun:
    run = _find_step(db, wf, name)
    if run is not None and run.status == "done":
        return run  # finished before a crash or restart, reuse the saved output

    if run is None:
        run = StepRun(workflow_id=wf.id, name=name, status="running", attempts=0)
        db.add(run)
        db.commit()
    elif run.status == "failed":
        raise StepFailed(name, run.error or "failed earlier")

    while True:
        # attempts are saved before the call, so a step that kills the process every time
        # (a poison step) still runs out of attempts instead of crash-looping forever
        if run.attempts >= settings.step_max_attempts:
            _fail_step(db, wf, run, run.error or "gave up, attempts used up (repeated crashes?)")

        _heartbeat(db, wf.id, owner)
        run.attempts += 1
        db.commit()  # also means no write lock is held while fn() talks to the outside world

        try:
            output = fn()
        except Exception as exc:  # a crash (BaseException) is deliberately not caught
            run.error = str(exc)[:500]
            if not isinstance(exc, _RETRYABLE):
                _fail_step(db, wf, run, run.error)
            if run.attempts >= settings.step_max_attempts:
                _fail_step(db, wf, run, run.error)

            audit.record(db, wf.tenant_id, "step_retry", workflow_id=wf.id,
                         step=name, attempt=run.attempts, error=run.error)
            db.commit()
            time.sleep(_backoff(run.attempts))
            continue

        run.status = "done"
        run.output = output
        run.error = None
        run.finished_at = utcnow()
        audit.record(db, wf.tenant_id, "step_done", workflow_id=wf.id,
                     step=name, attempts=run.attempts)
        db.commit()
        return run
