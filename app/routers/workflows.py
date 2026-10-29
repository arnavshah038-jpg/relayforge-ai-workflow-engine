from typing import Annotated

from fastapi import APIRouter, BackgroundTasks, Depends, Header, Path, Query, Response
from sqlalchemy.orm import Session

from app.db import get_db
from app.deps import current_user, require_roles
from app.engine import service
from app.engine.states import Status
from app.models import User
from app.schemas import (AuditOut, RejectIn, TicketIn, WorkflowOut, WorkflowPage)

router = APIRouter(prefix="/workflows", tags=["workflows"])

# ids are a 32 bit integer column on Postgres, anything bigger would be a database error (500)
WorkflowId = Annotated[int, Path(ge=1, le=2_147_483_647)]


@router.post("", response_model=WorkflowOut, status_code=201)
def create(body: TicketIn, response: Response, tasks: BackgroundTasks,
           idempotency_key: Annotated[str | None, Header(min_length=8, max_length=100)] = None,
           user: User = Depends(current_user), db: Session = Depends(get_db)):
    wf, created = service.create_workflow(db, user, body.model_dump(), idempotency_key)
    if created:
        tasks.add_task(service.run_in_background, wf.id)
    else:
        response.status_code = 200  # replay of an earlier request
    return wf


@router.get("", response_model=WorkflowPage)
def list_all(status: Status | None = None, limit: int = Query(20, ge=1, le=100),
             offset: int = Query(0, ge=0),
             user: User = Depends(current_user), db: Session = Depends(get_db)):
    rows, total = service.list_workflows(db, user, status, limit, offset)
    return WorkflowPage(items=rows, total=total, limit=limit, offset=offset)


@router.get("/{workflow_id}", response_model=WorkflowOut)
def get_one(workflow_id: WorkflowId, user: User = Depends(current_user), db: Session = Depends(get_db)):
    return service.get_workflow(db, user, workflow_id)


@router.get("/{workflow_id}/audit", response_model=list[AuditOut])
def audit_log(workflow_id: WorkflowId, user: User = Depends(current_user), db: Session = Depends(get_db)):
    return service.audit_trail(db, user, workflow_id)


@router.post("/{workflow_id}/approve", response_model=WorkflowOut)
def approve(workflow_id: WorkflowId, tasks: BackgroundTasks,
            user: User = Depends(require_roles("manager", "admin")),
            db: Session = Depends(get_db)):
    wf = service.approve(db, user, workflow_id)
    tasks.add_task(service.run_in_background, wf.id)
    return wf


@router.post("/{workflow_id}/reject", response_model=WorkflowOut)
def reject(workflow_id: WorkflowId, body: RejectIn | None = None,
           user: User = Depends(require_roles("manager", "admin")),
           db: Session = Depends(get_db)):
    return service.reject(db, user, workflow_id, body.reason if body else None)


@router.post("/{workflow_id}/retry", response_model=WorkflowOut)
def retry(workflow_id: WorkflowId, tasks: BackgroundTasks,
          user: User = Depends(require_roles("manager", "admin")),
          db: Session = Depends(get_db)):
    wf = service.retry(db, user, workflow_id)
    tasks.add_task(service.run_in_background, wf.id)
    return wf
