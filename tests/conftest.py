import os
import tempfile

# must happen before app modules are imported.
# Default is a throwaway SQLite file. Set TEST_DATABASE_URL to run the same tests on PostgreSQL,
# the tables in that database are dropped and recreated for every test.
_test_url = os.environ.get("TEST_DATABASE_URL")
if _test_url:
    if not _test_url.split("?")[0].endswith("_test"):
        raise RuntimeError("TEST_DATABASE_URL must point to a database whose name ends with _test")
    os.environ["DATABASE_URL"] = _test_url
else:
    _tmp = tempfile.mkdtemp(prefix="relayforge-test-")
    os.environ["DATABASE_URL"] = f"sqlite:///{_tmp}/test.db"
os.environ["SECRET_KEY"] = "test-secret-key-for-relayforge-tests-only"
os.environ["OPENAI_API_KEY"] = ""          # tests never talk to OpenAI
os.environ["RETRY_BASE_DELAY_S"] = "0"
os.environ["SWEEPER_ENABLED"] = "false"
os.environ["GATEWAY_FAILURE_RATE"] = "0"

import pytest
from fastapi.testclient import TestClient
from pwdlib import PasswordHash
from pwdlib.hashers.argon2 import Argon2Hasher

from app import security
from app.db import Base, SessionLocal, engine
from app.main import app
from app.models import GatewayRefund, Tenant, User

# cheap argon2 settings so the suite stays fast and light on RAM
security._hasher = PasswordHash((Argon2Hasher(time_cost=1, memory_cost=1024, parallelism=1),))


@pytest.fixture(autouse=True)
def fresh_db():
    Base.metadata.drop_all(engine)
    Base.metadata.create_all(engine)
    yield


@pytest.fixture(autouse=True)
def reset_login_limiter():
    from app.routers.auth import login_limiter
    login_limiter.reset()
    yield


@pytest.fixture
def client():
    return TestClient(app)


@pytest.fixture
def db():
    with SessionLocal() as session:
        yield session


def auth(token: str) -> dict:
    return {"Authorization": f"Bearer {token}"}


def signup(client, company="Acme", email="admin@acme.test", password="password123") -> str:
    r = client.post("/auth/signup", json={"company_name": company, "email": email, "password": password})
    assert r.status_code == 201, r.text
    return login(client, email, password)


def login(client, email, password="password123") -> str:
    r = client.post("/auth/login", json={"email": email, "password": password})
    assert r.status_code == 200, r.text
    return r.json()["access_token"]


def add_user(client, admin_token, email, role) -> str:
    r = client.post("/auth/users", headers=auth(admin_token),
                    json={"email": email, "password": "password123", "role": role})
    assert r.status_code == 201, r.text
    return login(client, email)


REFUND_TICKET = {
    "customer_email": "ravi@example.com",
    "subject": "Refund please",
    "body": "I was charged twice, please refund Rs 2000 for order 1042.",
}
PLAIN_TICKET = {
    "customer_email": "sita@example.com",
    "subject": "Forgot my password",
    "body": "I cannot login to my account.",
}


def refund_count() -> int:
    with SessionLocal() as s:
        return s.query(GatewayRefund).count()


def make_user_row(db, tenant_name="T", email="u@t.test", role="agent", tenant=None) -> User:
    tenant = tenant or Tenant(name=tenant_name)
    db.add(tenant)
    db.flush()
    user = User(tenant_id=tenant.id, email=email, role=role, password_hash="x")
    db.add(user)
    db.commit()
    return user
