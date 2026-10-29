import time
from datetime import timedelta

import jwt
import pytest

from app import ai
from app.config import Settings, check_secret_key, settings
from app.engine import service, steps
from app.errors import TransientError
from app.main import app
from app.models import User, utcnow
from tests.conftest import PLAIN_TICKET, REFUND_TICKET, add_user, auth, refund_count, signup
from tests.helpers import ScriptedGateway


# ---------- input validation ----------

def test_ids_outside_the_database_range_are_validation_errors(client):
    token = signup(client)
    for bad in ("0", "-5", "99999999999", "abc"):
        assert client.get(f"/workflows/{bad}", headers=auth(token)).status_code == 422
    assert client.post("/workflows/99999999999/approve", headers=auth(token)).status_code == 422
    assert client.get("/workflows/2147483647", headers=auth(token)).status_code == 404


def test_unknown_status_filter_is_rejected(client):
    token = signup(client)
    assert client.get("/workflows?status=banana", headers=auth(token)).status_code == 422
    assert client.get("/workflows?offset=-1", headers=auth(token)).status_code == 422


@pytest.mark.parametrize("change", [
    {"subject": "   "},
    {"body": ""},
    {"body": "hello\u0000world"},
    {"subject": "x" * 201},
    {"body": "x" * 5001},
    {"customer_email": "not-an-email"},
    {"customer_email": "a b@c.test"},
])
def test_bad_ticket_fields_are_rejected(client, change):
    token = signup(client)
    r = client.post("/workflows", headers=auth(token), json={**PLAIN_TICKET, **change})
    assert r.status_code == 422


def test_text_fields_are_trimmed(client):
    token = signup(client)
    r = client.post("/workflows", headers=auth(token),
                    json={**PLAIN_TICKET, "subject": "  Forgot password  "})
    assert r.json()["input"]["subject"] == "Forgot password"


def test_reject_works_without_a_body(client):
    admin = signup(client)
    agent = add_user(client, admin, "agent@acme.test", "agent")
    wf_id = client.post("/workflows", headers=auth(agent), json=REFUND_TICKET).json()["id"]
    r = client.post(f"/workflows/{wf_id}/reject", headers=auth(admin))
    assert r.status_code == 200 and r.json()["status"] == "rejected"


# ---------- request ids ----------

def test_request_id_is_echoed_when_sane_and_replaced_when_not(client):
    ok = client.get("/health", headers={"X-Request-ID": "abc-123"})
    assert ok.headers["x-request-id"] == "abc-123"
    bad = client.get("/health", headers={"X-Request-ID": "bad id with spaces!"})
    assert bad.headers["x-request-id"] != "bad id with spaces!"
    assert len(bad.headers["x-request-id"]) == 16


# ---------- login ----------

def test_unknown_email_and_wrong_password_look_the_same(client):
    signup(client)
    a = client.post("/auth/login", json={"email": "ghost@acme.test", "password": "password123"})
    b = client.post("/auth/login", json={"email": "admin@acme.test", "password": "wrong-password"})
    assert a.status_code == b.status_code == 401
    assert a.json() == b.json()


def test_login_is_blocked_after_repeated_failures(client):
    signup(client)
    for _ in range(settings.login_max_failures):
        r = client.post("/auth/login", json={"email": "admin@acme.test", "password": "wrong-password"})
        assert r.status_code == 401
    # even the right password is refused while blocked
    r = client.post("/auth/login", json={"email": "admin@acme.test", "password": "password123"})
    assert r.status_code == 429


def test_a_successful_login_resets_the_failure_count(client):
    signup(client)
    bad = {"email": "admin@acme.test", "password": "wrong-password"}
    for _ in range(settings.login_max_failures - 1):
        client.post("/auth/login", json=bad)
    assert client.post("/auth/login", json={"email": "admin@acme.test", "password": "password123"}).status_code == 200
    for _ in range(settings.login_max_failures - 1):
        assert client.post("/auth/login", json=bad).status_code == 401


# ---------- tokens ----------

def _claims(user_id, tenant_id, **extra):
    return {"sub": str(user_id), "tid": tenant_id, "exp": utcnow() + timedelta(minutes=5), **extra}


def test_forged_and_broken_tokens_are_refused(client, db):
    signup(client)
    user = db.query(User).one()
    secret = settings.secret_key

    good = jwt.encode(_claims(user.id, user.tenant_id), secret, algorithm="HS256")
    assert client.get("/auth/me", headers=auth(good)).status_code == 200

    expired = jwt.encode({**_claims(user.id, user.tenant_id), "exp": utcnow() - timedelta(seconds=5)},
                         secret, algorithm="HS256")
    wrong_key = jwt.encode(_claims(user.id, user.tenant_id), "x" * 40, algorithm="HS256")
    unsigned = jwt.encode(_claims(user.id, user.tenant_id), None, algorithm="none")
    other_tenant = jwt.encode(_claims(user.id, user.tenant_id + 1), secret, algorithm="HS256")
    no_exp = jwt.encode({"sub": str(user.id), "tid": user.tenant_id}, secret, algorithm="HS256")
    ghost = jwt.encode(_claims(user.id + 99, user.tenant_id), secret, algorithm="HS256")
    not_a_number = jwt.encode(_claims("abc", user.tenant_id), secret, algorithm="HS256")

    for name, token in [("expired", expired), ("wrong key", wrong_key), ("unsigned", unsigned),
                        ("other tenant", other_tenant), ("no exp", no_exp), ("ghost", ghost),
                        ("bad sub", not_a_number)]:
        assert client.get("/auth/me", headers=auth(token)).status_code == 401, name


def test_role_comes_from_the_database_not_the_token(client, db):
    admin = signup(client)
    agent = add_user(client, admin, "agent@acme.test", "agent")
    user = db.query(User).filter_by(email="agent@acme.test").one()
    forged = jwt.encode(_claims(user.id, user.tenant_id, role="admin"), settings.secret_key, algorithm="HS256")
    r = client.post("/auth/users", headers=auth(forged),
                    json={"email": "x@acme.test", "password": "password123", "role": "admin"})
    assert r.status_code == 403


# ---------- ticket classification ----------

@pytest.mark.parametrize("text, action, amount", [
    ("please refund Rs 2000", "refund", 2000),
    ("refund 2000 rupees", "refund", 2000),
    ("refund ₹1,500.50", "refund", 1500.5),
    ("refund Rs. 2,000.", "refund", 2000),
    ("refund 100 INR", "refund", 100),
    ("refund Rs 5000 order, refund Rs 200", "escalate", None),   # two amounts, which one?
    ("refund Rs ,", "escalate", None),                           # used to crash
    ("refund Rs 0", "escalate", None),
    ("refund Rs 60000", "escalate", None),                       # over the cap
    ("I want my money back", "escalate", None),                  # no amount
])
def test_keyword_classifier_amounts(text, action, amount):
    out = ai._keyword_rules("Order problem", text)
    assert out.action == action
    assert out.refund_amount == amount


def test_ambiguous_ticket_goes_to_a_human_not_to_a_refund(client):
    token = signup(client)
    body = {**REFUND_TICKET, "body": "I paid Rs 5000 for the order, please refund Rs 200 for the damaged item"}
    wf = client.post("/workflows", headers=auth(token), json=body).json()
    state = client.get(f"/workflows/{wf['id']}", headers=auth(token)).json()
    assert state["status"] == "completed"
    assert state["steps"][0]["output"]["action"] == "escalate"
    assert refund_count() == 0


def test_refund_amount_is_rounded_and_nan_is_refused():
    assert ai.TicketAnalysis(category="payment", urgency="low", action="refund",
                             refund_amount=99.999, summary="s").refund_amount == 100.0
    with pytest.raises(ValueError):
        ai.TicketAnalysis.model_validate_json(
            '{"category":"payment","urgency":"low","action":"refund","refund_amount":NaN,"summary":"s"}')


# ---------- OpenAI call, with a stub instead of the real client ----------

class _Msg:
    def __init__(self, content): self.content = content


class _Choice:
    def __init__(self, content): self.message = _Msg(content)


class _Resp:
    def __init__(self, content): self.choices = [_Choice(content)]


def _install_fake_openai(monkeypatch, handler):
    import openai
    seen = {}

    class FakeOpenAI:
        def __init__(self, **kwargs):
            seen["client_kwargs"] = kwargs
            self.chat = type("C", (), {"completions": type("X", (), {"create": staticmethod(handler)})})()

    monkeypatch.setattr(openai, "OpenAI", FakeOpenAI)
    monkeypatch.setattr(settings, "openai_api_key", "sk-test-key")
    return seen


GOOD_JSON = ('{"category":"payment","urgency":"high","action":"refund",'
             '"refund_amount":2000,"summary":"double charge"}')


def test_openai_happy_path_sends_the_expected_request(monkeypatch):
    sent = {}

    def create(**kwargs):
        sent.update(kwargs)
        return _Resp(GOOD_JSON)

    seen = _install_fake_openai(monkeypatch, create)
    out = ai.classify_ticket("Refund", "refund Rs 2000")

    assert out.action == "refund" and out.refund_amount == 2000
    assert sent["response_format"] == {"type": "json_object"}
    assert sent["model"] == settings.openai_model
    assert "untrusted" in sent["messages"][0]["content"]
    assert seen["client_kwargs"]["timeout"] == settings.llm_timeout_s


def test_openai_errors_do_not_leak_the_key_into_our_data(monkeypatch):
    def create(**kwargs):
        raise RuntimeError("Incorrect API key provided: sk-test-key-REAL-SECRET")

    _install_fake_openai(monkeypatch, create)
    with pytest.raises(TransientError) as exc:
        ai.classify_ticket("Refund", "refund Rs 2000")
    assert "SECRET" not in str(exc.value)
    assert "RuntimeError" in str(exc.value)


@pytest.mark.parametrize("content", [None, "", "not json at all", '{"action":"refund"}',
                                     '{"category":"payment","urgency":"low","action":"delete_everything","summary":"s"}'])
def test_openai_garbage_is_rejected(monkeypatch, content):
    _install_fake_openai(monkeypatch, lambda **kw: _Resp(content))
    with pytest.raises(Exception) as exc:
        ai.classify_ticket("Refund", "refund Rs 2000")
    assert isinstance(exc.value, (TransientError, ValueError))


def test_prompt_injection_cannot_push_a_huge_refund_through(client, monkeypatch):
    evil = ('{"category":"payment","urgency":"high","action":"refund",'
            '"refund_amount":9999999,"summary":"ignore previous instructions"}')
    _install_fake_openai(monkeypatch, lambda **kw: _Resp(evil))
    token = signup(client)
    wf = client.post("/workflows", headers=auth(token), json=REFUND_TICKET).json()
    state = client.get(f"/workflows/{wf['id']}", headers=auth(token)).json()
    assert state["status"] == "failed"
    assert refund_count() == 0


def test_llm_answer_is_only_asked_once_per_workflow(client, monkeypatch):
    calls = []

    def create(**kwargs):
        calls.append(1)
        return _Resp(GOOD_JSON)

    _install_fake_openai(monkeypatch, create)
    admin = signup(client)
    agent = add_user(client, admin, "agent@acme.test", "agent")
    wf_id = client.post("/workflows", headers=auth(agent), json=REFUND_TICKET).json()["id"]
    client.post(f"/workflows/{wf_id}/approve", headers=auth(admin))
    assert len(calls) == 1
    assert refund_count() == 1


# ---------- failure through the HTTP layer ----------

def test_manager_can_retry_a_failed_workflow_over_http(client, monkeypatch):
    admin = signup(client)
    agent = add_user(client, admin, "agent@acme.test", "agent")
    monkeypatch.setattr(steps, "get_gateway", lambda: ScriptedGateway(fail_first=100))

    wf_id = client.post("/workflows", headers=auth(agent), json=REFUND_TICKET).json()["id"]
    client.post(f"/workflows/{wf_id}/approve", headers=auth(admin))
    assert client.get(f"/workflows/{wf_id}", headers=auth(agent)).json()["status"] == "failed"

    assert client.post(f"/workflows/{wf_id}/retry", headers=auth(agent)).status_code == 403  # agents can't

    monkeypatch.setattr(steps, "get_gateway", lambda: ScriptedGateway())
    assert client.post(f"/workflows/{wf_id}/retry", headers=auth(admin)).status_code == 200
    state = client.get(f"/workflows/{wf_id}", headers=auth(agent)).json()
    assert state["status"] == "completed"
    assert refund_count() == 1
    assert client.post(f"/workflows/{wf_id}/retry", headers=auth(admin)).status_code == 409


# ---------- config ----------

def test_database_url_gets_the_right_driver():
    assert Settings(database_url="postgres://u:p@h/db").database_url == "postgresql+psycopg://u:p@h/db"
    assert Settings(database_url="postgresql://u:p@h/db").database_url == "postgresql+psycopg://u:p@h/db"
    assert Settings(database_url="sqlite:///x.db").database_url == "sqlite:///x.db"


def test_blank_openai_key_means_no_key():
    assert Settings(openai_api_key="   ").openai_api_key is None
    assert Settings(openai_api_key=" sk-abc ").openai_api_key == "sk-abc"


def test_weak_secret_key_stops_the_app(monkeypatch):
    from fastapi.testclient import TestClient
    monkeypatch.setattr(settings, "secret_key", "change-me")
    with pytest.raises(RuntimeError):
        with TestClient(app):
            pass
    monkeypatch.setattr(settings, "secret_key", "short")
    with pytest.raises(RuntimeError):
        check_secret_key()


def test_sweeper_picks_up_orphaned_workflows(db, monkeypatch):
    from fastapi.testclient import TestClient
    from tests.conftest import make_user_row

    user = make_user_row(db)
    wf, _ = service.create_workflow(db, user, dict(PLAIN_TICKET), None)  # created, but nobody ran it
    monkeypatch.setattr(settings, "sweeper_enabled", True)
    monkeypatch.setattr(settings, "sweep_interval_s", 0.2)

    with TestClient(app):
        for _ in range(50):
            db.refresh(wf)
            if wf.status == "completed":
                break
            time.sleep(0.1)
    assert wf.status == "completed"
