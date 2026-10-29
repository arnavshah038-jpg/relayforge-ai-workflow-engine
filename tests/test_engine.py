import pytest

from app import ai
from app.config import settings
from app.engine import service, steps
from app.engine.runner import advance
from app.engine.service import recover_interrupted
from app.engine.states import InvalidTransition, Status, move
from app.models import AuditLog, StepRun, Workflow
from tests.conftest import REFUND_TICKET, make_user_row, refund_count
from tests.helpers import Crash, ScriptedGateway


@pytest.fixture
def people(db):
    agent = make_user_row(db, "Acme", "agent@acme.test", "agent")
    manager = make_user_row(db, email="mgr@acme.test", role="manager")
    # second user must share the tenant
    manager.tenant_id = agent.tenant_id
    db.commit()
    return agent, manager


def _waiting_workflow(db, agent):
    wf, _ = service.create_workflow(db, agent, dict(REFUND_TICKET), None)
    advance(db, wf.id)
    assert wf.status == Status.WAITING_APPROVAL.value
    return wf


def test_analysis_is_not_repeated_after_a_crash(db, people, monkeypatch):
    agent, manager = people
    calls = []
    real = ai.classify_ticket

    def counting(subject, body):
        calls.append(1)
        return real(subject, body)

    monkeypatch.setattr(steps, "classify_ticket", counting)
    monkeypatch.setattr(steps, "get_gateway", lambda: ScriptedGateway(crash_after_refund=True))

    wf = _waiting_workflow(db, agent)
    service.approve(db, manager, wf.id)
    with pytest.raises(Crash):
        advance(db, wf.id)

    # "restart": new session, healthy provider
    monkeypatch.setattr(steps, "get_gateway", lambda: ScriptedGateway())
    from app.db import SessionLocal
    with SessionLocal() as fresh:
        assert recover_interrupted(fresh) == 1
        done = fresh.get(Workflow, wf.id)
        assert done.status == Status.COMPLETED.value

    assert len(calls) == 1


def test_crash_after_provider_call_does_not_refund_twice(db, people, monkeypatch):
    agent, manager = people
    monkeypatch.setattr(steps, "get_gateway", lambda: ScriptedGateway(crash_after_refund=True))

    wf = _waiting_workflow(db, agent)
    service.approve(db, manager, wf.id)
    with pytest.raises(Crash):
        advance(db, wf.id)

    # provider already took the refund, but we never saved the step result
    assert refund_count() == 1
    assert db.get(Workflow, wf.id).status == Status.RUNNING.value

    monkeypatch.setattr(steps, "get_gateway", lambda: ScriptedGateway())
    from app.db import SessionLocal
    with SessionLocal() as fresh:
        recover_interrupted(fresh)
        run = fresh.query(StepRun).filter_by(workflow_id=wf.id, name="execute").one()
        assert run.status == "done"
        assert run.output["duplicate"] is True
        assert run.attempts == 2

    assert refund_count() == 1


def test_recover_does_not_touch_waiting_or_finished(db, people):
    agent, _ = people
    _waiting_workflow(db, agent)
    assert recover_interrupted(db) == 0


def test_transient_provider_errors_are_retried(db, people, monkeypatch):
    agent, manager = people
    gw = ScriptedGateway(fail_first=2)
    monkeypatch.setattr(steps, "get_gateway", lambda: gw)

    wf = _waiting_workflow(db, agent)
    service.approve(db, manager, wf.id)
    advance(db, wf.id)

    assert db.get(Workflow, wf.id).status == Status.COMPLETED.value
    assert gw.calls == 3
    assert refund_count() == 1
    retries = db.query(AuditLog).filter_by(workflow_id=wf.id, event="step_retry").count()
    assert retries == 2


def test_gives_up_after_max_attempts(db, people, monkeypatch):
    agent, manager = people
    gw = ScriptedGateway(fail_first=100)
    monkeypatch.setattr(steps, "get_gateway", lambda: gw)
    monkeypatch.setattr(settings, "step_max_attempts", 3)

    wf = _waiting_workflow(db, agent)
    service.approve(db, manager, wf.id)
    advance(db, wf.id)

    assert db.get(Workflow, wf.id).status == Status.FAILED.value
    assert gw.calls == 3
    assert refund_count() == 0
    last = db.query(AuditLog).filter_by(workflow_id=wf.id).order_by(AuditLog.id.desc()).first()
    assert last.event == "workflow_failed"


def test_bad_llm_output_is_retried_then_fails(db, people, monkeypatch):
    agent, _ = people
    attempts = []

    def bad_llm(subject, body):
        attempts.append(1)
        return ai.TicketAnalysis(category="payment", urgency="high", action="refund",
                                 refund_amount=10**9, summary="huge refund")

    monkeypatch.setattr(steps, "classify_ticket", bad_llm)
    wf, _ = service.create_workflow(db, agent, dict(REFUND_TICKET), None)
    advance(db, wf.id)

    # the model object itself refuses an absurd amount, so we never get past analyze
    assert db.get(Workflow, wf.id).status == Status.FAILED.value
    assert len(attempts) == settings.step_max_attempts
    assert refund_count() == 0


def test_terminal_states_cannot_be_left(db, people):
    agent, _ = people
    wf, _ = service.create_workflow(db, agent, dict(REFUND_TICKET), None)
    wf.status = Status.COMPLETED.value
    with pytest.raises(InvalidTransition):
        move(wf, Status.RUNNING)


def test_keyword_classifier():
    out = ai._keyword_rules("Refund", "please refund Rs 1,500 for my order")
    assert out.action == "refund" and out.refund_amount == 1500
    # refund asked but no amount, we escalate instead of guessing
    assert ai._keyword_rules("Refund", "I want my money back").action == "escalate"
    assert ai._keyword_rules("Hi", "where is my delivery").action == "reply"


def test_refund_over_limit_is_not_accepted():
    with pytest.raises(ValueError):
        ai.TicketAnalysis(category="payment", urgency="high", action="refund",
                          refund_amount=settings.max_refund + 1, summary="x")
