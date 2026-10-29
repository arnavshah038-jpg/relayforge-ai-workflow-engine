"""Creates the demo company, two logins and two sample tickets.

Run as:  python -m app.seed_demo
Does nothing unless DEMO_MODE=true, and nothing if the demo company already exists,
so it is safe to run on every start."""
from sqlalchemy import select

from app.config import settings
from app.db import SessionLocal
from app.demo import DEMO_ACCOUNTS, DEMO_COMPANY, SAMPLE_TICKETS
from app.engine import service
from app.engine.runner import advance
from app.models import Tenant, User
from app.security import hash_password


def seed() -> bool:
    if not settings.demo_mode:
        return False
    with SessionLocal() as db:
        if db.scalar(select(Tenant).where(Tenant.name == DEMO_COMPANY)):
            return False

        tenant = Tenant(name=DEMO_COMPANY)
        db.add(tenant)
        db.flush()
        users = {}
        for acc in DEMO_ACCOUNTS:
            users[acc["role"]] = User(tenant_id=tenant.id, email=acc["email"], role=acc["role"],
                                      password_hash=hash_password(acc["password"]))
            db.add(users[acc["role"]])
        db.commit()

        for key, email, subject, body in SAMPLE_TICKETS:
            wf, _ = service.create_workflow(
                db, users["agent"], {"customer_email": email, "subject": subject, "body": body}, key)
            advance(db, wf.id)
    return True


if __name__ == "__main__":
    print("demo data created" if seed() else "demo data skipped (not demo mode, or already there)")
