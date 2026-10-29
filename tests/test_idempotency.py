from app.gateway import PaymentGateway
from tests.conftest import PLAIN_TICKET, REFUND_TICKET, auth, refund_count, signup


def test_same_key_returns_same_workflow(client):
    token = signup(client)
    headers = {**auth(token), "Idempotency-Key": "ticket-1042-abc"}

    first = client.post("/workflows", headers=headers, json=PLAIN_TICKET)
    second = client.post("/workflows", headers=headers, json=PLAIN_TICKET)

    assert first.status_code == 201
    assert second.status_code == 200
    assert first.json()["id"] == second.json()["id"]
    assert client.get("/workflows", headers=auth(token)).json()["total"] == 1


def test_same_key_with_different_body_is_a_conflict(client):
    token = signup(client)
    headers = {**auth(token), "Idempotency-Key": "ticket-1042-abc"}
    client.post("/workflows", headers=headers, json=PLAIN_TICKET)
    r = client.post("/workflows", headers=headers, json=REFUND_TICKET)
    assert r.status_code == 409


def test_same_key_in_two_tenants_does_not_clash(client):
    a = signup(client, "A Corp", "a@a.test")
    b = signup(client, "B Corp", "b@b.test")
    ra = client.post("/workflows", headers={**auth(a), "Idempotency-Key": "shared-key-1"}, json=PLAIN_TICKET)
    rb = client.post("/workflows", headers={**auth(b), "Idempotency-Key": "shared-key-1"}, json=PLAIN_TICKET)
    assert ra.status_code == rb.status_code == 201
    assert ra.json()["id"] != rb.json()["id"]


def test_short_key_is_rejected(client):
    token = signup(client)
    r = client.post("/workflows", headers={**auth(token), "Idempotency-Key": "x"}, json=PLAIN_TICKET)
    assert r.status_code == 422


def test_provider_refund_is_deduplicated_by_key():
    gw = PaymentGateway()
    first = gw.refund("wf-1-refund", 100)
    second = gw.refund("wf-1-refund", 100)
    assert first["duplicate"] is False
    assert second["duplicate"] is True
    assert first["refund_id"] == second["refund_id"]
    assert refund_count() == 1
