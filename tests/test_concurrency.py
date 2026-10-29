import threading
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta

import pytest

from app.config import settings
from app.db import SessionLocal
from app.engine import service, steps
from app.engine.runner import advance
from app.engine.service import recover_interrupted
from app.engine.states import Status
from app.errors import Conflict
from app.models import StepRun, User, Workflow, utcnow
from tests.conftest import REFUND_TICKET, make_user_row, refund_count
from tests.helpers import ScriptedGateway


def run_parallel(fns):
    """Starts all callables at the same moment and returns ('ok', value) or ('err', exception) for each."""
    barrier = threading.Barrier(len(fns))

    def wrap(fn):
        def inner():
            barrier.wait()
            try:
                return "ok", fn()
            except Exception as exc:
                return "err", exc
        return inner

    with ThreadPoolExecutor(len(fns)) as pool:
        futures = [pool.submit(wrap(fn)) for fn in fns]
        return [f.result() for f in futures]


@pytest.fixture
def people(db):
    agent = make_user_row(db, "Acme", "agent@acme.test", "agent")
    manager = make_user_row(db, email="mgr@acme.test", role="manager")
    manager.tenant_id = agent.tenant_id
    db.commit()
    return agent, manager


def waiting_workflow(db, agent):
    wf, _ = service.create_workflow(db, agent, dict(REFUND_TICKET), None)
    advance(db, wf.id)
    assert wf.status == Status.WAITING_APPROVAL.value
    return wf


def test_parallel_requests_with_the_same_key_create_one_workflow(people):
    agent, _ = people

    def attempt():
        with SessionLocal() as s:
            wf, created = service.create_workflow(s, s.get(User, agent.id), dict(REFUND_TICKET), "same-key-0001")
            return wf.id, created

    results = run_parallel([attempt] * 8)

    assert all(kind == "ok" for kind, _ in results), results
    assert len({value[0] for _, value in results}) == 1
    assert sum(1 for _, value in results if value[1]) == 1  # exactly one request really created it
    with SessionLocal() as s:
        assert s.query(Workflow).count() == 1


def test_double_click_on_approve_only_works_once(db, people, monkeypatch):
    agent, manager = people
    wf = waiting_workflow(db, agent)

    # keep every request between "I checked the status" and "I change it" for a moment,
    # otherwise the threads tend to run one after the other and the race never happens
    original = service.get_workflow

    def slow_get(*args, **kwargs):
        found = original(*args, **kwargs)
        time.sleep(0.3)
        return found

    monkeypatch.setattr(service, "get_workflow", slow_get)

    def approve():
        with SessionLocal() as s:
            return service.approve(s, s.get(User, manager.id), wf.id).id

    results = run_parallel([approve, approve, approve])

    assert [kind for kind, _ in results].count("ok") == 1
    errors = [value for kind, value in results if kind == "err"]
    assert len(errors) == 2 and all(isinstance(e, Conflict) for e in errors)
    with SessionLocal() as s:
        assert s.query(StepRun).filter_by(workflow_id=wf.id, name="approval").count() == 1


def test_two_workers_never_run_the_same_workflow_together(db, people, monkeypatch):
    agent, manager = people
    gateway = ScriptedGateway(delay=0.4)
    monkeypatch.setattr(steps, "get_gateway", lambda: gateway)

    wf = waiting_workflow(db, agent)
    service.approve(db, manager, wf.id)

    def worker():
        with SessionLocal() as s:
            advance(s, wf.id)

    results = run_parallel([worker, worker, worker])

    assert all(kind == "ok" for kind, _ in results), results
    assert gateway.calls == 1
    assert refund_count() == 1
    with SessionLocal() as s:
        assert s.get(Workflow, wf.id).status == Status.COMPLETED.value
        assert s.query(StepRun).filter_by(workflow_id=wf.id, name="execute").count() == 1


def test_live_lease_blocks_others_and_expired_lease_is_taken_over(db, people, monkeypatch):
    agent, manager = people
    gateway = ScriptedGateway()
    monkeypatch.setattr(steps, "get_gateway", lambda: gateway)

    wf = waiting_workflow(db, agent)
    service.approve(db, manager, wf.id)

    # a worker that is alive and holds the lease
    wf.locked_by, wf.locked_until = "other-worker", utcnow() + timedelta(seconds=60)
    db.commit()
    advance(db, wf.id)
    assert recover_interrupted(db) == 0
    assert gateway.calls == 0
    assert db.get(Workflow, wf.id).status == Status.RUNNING.value

    # the same worker died: its lease runs out and somebody else finishes the job
    wf.locked_until = utcnow() - timedelta(seconds=1)
    db.commit()
    assert recover_interrupted(db) == 1
    db.refresh(wf)
    assert wf.status == Status.COMPLETED.value
    assert wf.locked_by is None
    assert refund_count() == 1


def test_step_that_keeps_killing_the_worker_is_given_up_on(db, people, monkeypatch):
    agent, manager = people
    gateway = ScriptedGateway()
    monkeypatch.setattr(steps, "get_gateway", lambda: gateway)

    wf = waiting_workflow(db, agent)
    service.approve(db, manager, wf.id)
    # as if the process had died inside this step max_attempts times already
    db.add(StepRun(workflow_id=wf.id, name="execute", status="running",
                   attempts=settings.step_max_attempts))
    db.commit()

    advance(db, wf.id)

    db.refresh(wf)
    assert wf.status == Status.FAILED.value
    assert gateway.calls == 0


def test_failed_workflow_can_be_retried_and_still_refunds_once(db, people, monkeypatch):
    agent, manager = people
    monkeypatch.setattr(steps, "get_gateway", lambda: ScriptedGateway(fail_first=100))

    wf = waiting_workflow(db, agent)
    service.approve(db, manager, wf.id)
    advance(db, wf.id)
    assert db.get(Workflow, wf.id).status == Status.FAILED.value
    assert refund_count() == 0

    # a failed workflow is left alone until a human retries it
    assert recover_interrupted(db) == 0

    healthy = ScriptedGateway()
    monkeypatch.setattr(steps, "get_gateway", lambda: healthy)
    service.retry(db, manager, wf.id)
    advance(db, wf.id)

    db.refresh(wf)
    assert wf.status == Status.COMPLETED.value
    assert healthy.calls == 1
    assert refund_count() == 1
    run = db.query(StepRun).filter_by(workflow_id=wf.id, name="execute").one()
    assert run.attempts == 1  # the counter started fresh


def test_only_failed_workflows_can_be_retried(db, people):
    agent, manager = people
    wf = waiting_workflow(db, agent)
    with pytest.raises(Conflict):
        service.retry(db, manager, wf.id)
