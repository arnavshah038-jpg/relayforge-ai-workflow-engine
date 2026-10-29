from tests.conftest import PLAIN_TICKET, REFUND_TICKET, add_user, auth, refund_count, signup


def _events(client, token, wf_id):
    return [e["event"] for e in client.get(f"/workflows/{wf_id}/audit", headers=auth(token)).json()]


def test_refund_waits_for_approval(client):
    admin = signup(client)
    agent = add_user(client, admin, "agent@acme.test", "agent")

    wf = client.post("/workflows", headers=auth(agent), json=REFUND_TICKET).json()
    state = client.get(f"/workflows/{wf['id']}", headers=auth(agent)).json()

    assert state["status"] == "waiting_approval"
    assert refund_count() == 0
    assert "approval_requested" in _events(client, agent, wf["id"])


def test_manager_approves_and_refund_happens_once(client):
    admin = signup(client)
    agent = add_user(client, admin, "agent@acme.test", "agent")
    manager = add_user(client, admin, "mgr@acme.test", "manager")

    wf_id = client.post("/workflows", headers=auth(agent), json=REFUND_TICKET).json()["id"]
    r = client.post(f"/workflows/{wf_id}/approve", headers=auth(manager))
    assert r.status_code == 200

    state = client.get(f"/workflows/{wf_id}", headers=auth(agent)).json()
    assert state["status"] == "completed"
    assert refund_count() == 1
    assert [s["name"] for s in state["steps"]] == ["analyze", "approval", "execute"]

    trail = _events(client, agent, wf_id)
    assert trail[0] == "workflow_created"
    assert "approved" in trail and trail[-1] == "workflow_completed"


def test_agent_cannot_approve(client):
    admin = signup(client)
    agent = add_user(client, admin, "agent@acme.test", "agent")
    wf_id = client.post("/workflows", headers=auth(agent), json=REFUND_TICKET).json()["id"]
    assert client.post(f"/workflows/{wf_id}/approve", headers=auth(agent)).status_code == 403
    assert refund_count() == 0


def test_requester_cannot_approve_own_workflow(client):
    admin = signup(client)
    manager = add_user(client, admin, "mgr@acme.test", "manager")
    wf_id = client.post("/workflows", headers=auth(manager), json=REFUND_TICKET).json()["id"]
    assert client.post(f"/workflows/{wf_id}/approve", headers=auth(manager)).status_code == 403
    assert refund_count() == 0


def test_approving_twice_is_a_conflict(client):
    admin = signup(client)
    agent = add_user(client, admin, "agent@acme.test", "agent")
    wf_id = client.post("/workflows", headers=auth(agent), json=REFUND_TICKET).json()["id"]
    assert client.post(f"/workflows/{wf_id}/approve", headers=auth(admin)).status_code == 200
    assert client.post(f"/workflows/{wf_id}/approve", headers=auth(admin)).status_code == 409
    assert refund_count() == 1


def test_reject_stops_the_workflow(client):
    admin = signup(client)
    agent = add_user(client, admin, "agent@acme.test", "agent")
    wf_id = client.post("/workflows", headers=auth(agent), json=REFUND_TICKET).json()["id"]

    r = client.post(f"/workflows/{wf_id}/reject", headers=auth(admin), json={"reason": "looks fake"})
    assert r.json()["status"] == "rejected"
    assert refund_count() == 0
    assert client.post(f"/workflows/{wf_id}/approve", headers=auth(admin)).status_code == 409


def test_non_refund_ticket_needs_no_approval(client):
    token = signup(client)
    wf = client.post("/workflows", headers=auth(token), json=PLAIN_TICKET).json()
    state = client.get(f"/workflows/{wf['id']}", headers=auth(token)).json()
    assert state["status"] == "completed"
    assert refund_count() == 0
