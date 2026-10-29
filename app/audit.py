from sqlalchemy.orm import Session

from app.middleware import request_id_var
from app.models import AuditLog


def record(db: Session, tenant_id: int, event: str, *, workflow_id: int | None = None,
           actor: str = "system", **detail) -> None:
    detail["request_id"] = request_id_var.get()
    db.add(AuditLog(tenant_id=tenant_id, workflow_id=workflow_id, actor=actor,
                    event=event, detail=detail))
