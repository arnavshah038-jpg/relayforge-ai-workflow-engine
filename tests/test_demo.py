from app import seed_demo
from app.config import settings
from app.db import SessionLocal
from app.demo import DEMO_ACCOUNTS
from app.models import Workflow
from tests.conftest import auth, login, refund_count


def test_signup_can_be_switched_off(client, monkeypatch):
    monkeypatch.setattr(settings, "signup_enabled", False)
    r = client.post("/auth/signup", json={"company_name": "Acme", "email": "a@acme.test", "password": "password123"})
    assert r.status_code == 403


def test_public_config_hides_demo_accounts_unless_demo_mode(client, monkeypatch):
    assert client.get("/config").json() == {"signup_enabled": True, "demo_accounts": []}
    monkeypatch.setattr(settings, "demo_mode", True)
    monkeypatch.setattr(settings, "signup_enabled", False)
    cfg = client.get("/config").json()
    assert cfg["signup_enabled"] is False
    assert [a["email"] for a in cfg["demo_accounts"]] == [a["email"] for a in DEMO_ACCOUNTS]


def test_seed_does_nothing_outside_demo_mode():
    assert seed_demo.seed() is False


def test_seed_creates_working_demo_and_is_repeatable(client, monkeypatch):
    monkeypatch.setattr(settings, "demo_mode", True)
    assert seed_demo.seed() is True
    assert seed_demo.seed() is False  # second start changes nothing

    with SessionLocal() as db:
        statuses = sorted(w.status for w in db.query(Workflow))
    assert statuses == ["completed", "waiting_approval"]

    # the whole demo story works: manager approves the agent's refund
    agent = login(client, "agent@demo.test", "demo-pass-123")
    manager = login(client, "manager@demo.test", "demo-pass-123")
    waiting = client.get("/workflows?status=waiting_approval", headers=auth(agent)).json()["items"][0]
    assert client.post(f"/workflows/{waiting['id']}/approve", headers=auth(agent)).status_code == 403
    assert client.post(f"/workflows/{waiting['id']}/approve", headers=auth(manager)).status_code == 200
    assert refund_count() == 1
