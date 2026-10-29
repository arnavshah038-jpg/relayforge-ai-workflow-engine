from tests.conftest import PLAIN_TICKET, add_user, auth, login, signup


def test_signup_login_me(client):
    token = signup(client)
    r = client.get("/auth/me", headers=auth(token))
    assert r.status_code == 200
    assert r.json()["role"] == "admin"


def test_wrong_password_and_missing_token(client):
    signup(client)
    r = client.post("/auth/login", json={"email": "admin@acme.test", "password": "nope-nope"})
    assert r.status_code == 401
    assert client.get("/auth/me").status_code == 401
    assert client.get("/auth/me", headers=auth("garbage")).status_code == 401


def test_duplicate_email_rejected(client):
    signup(client)
    r = client.post("/auth/signup", json={"company_name": "Other", "email": "ADMIN@acme.test",
                                          "password": "password123"})
    assert r.status_code == 409


def test_only_admin_can_add_users(client):
    admin = signup(client)
    agent = add_user(client, admin, "agent@acme.test", "agent")
    r = client.post("/auth/users", headers=auth(agent),
                    json={"email": "x@acme.test", "password": "password123", "role": "manager"})
    assert r.status_code == 403


def test_tenants_cannot_see_each_others_workflows(client):
    a = signup(client, "A Corp", "a@a.test")
    b = signup(client, "B Corp", "b@b.test")

    wf_id = client.post("/workflows", headers=auth(a), json=PLAIN_TICKET).json()["id"]

    assert client.get(f"/workflows/{wf_id}", headers=auth(a)).status_code == 200
    assert client.get(f"/workflows/{wf_id}", headers=auth(b)).status_code == 404
    assert client.get(f"/workflows/{wf_id}/audit", headers=auth(b)).status_code == 404
    assert client.get("/workflows", headers=auth(b)).json()["total"] == 0


def test_other_tenant_cannot_approve(client):
    a = signup(client, "A Corp", "a@a.test")
    b = signup(client, "B Corp", "b@b.test")
    wf_id = client.post("/workflows", headers=auth(a), json={
        "customer_email": "c@c.test", "subject": "refund", "body": "refund Rs 500 please"}).json()["id"]
    assert client.post(f"/workflows/{wf_id}/approve", headers=auth(b)).status_code == 404


def test_pagination_and_status_filter(client):
    token = signup(client)
    for i in range(5):
        client.post("/workflows", headers=auth(token), json=PLAIN_TICKET)
    page = client.get("/workflows?limit=2&offset=0", headers=auth(token)).json()
    assert page["total"] == 5 and len(page["items"]) == 2
    done = client.get("/workflows?status=completed", headers=auth(token)).json()
    assert done["total"] == 5
    assert client.get("/workflows?limit=500", headers=auth(token)).status_code == 422
