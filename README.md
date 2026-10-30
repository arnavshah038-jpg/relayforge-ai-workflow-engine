# relayforge-ai-workflow-engine

[![tests](https://github.com/arnavshah038-jpg/relayforge-ai-workflow-engine/actions/workflows/ci.yml/badge.svg)](https://github.com/arnavshah038-jpg/relayforge-ai-workflow-engine/actions)

**Live demo:** https://relayforge.onrender.com (use the ready-made agent and manager logins; free hosting, so the first load can take about a minute)

A small backend for running multi-step workflows safely. Each workflow is saved in the database
step by step, so a crash, a restart, a retried request or two workers at the same time don't
cause duplicate side effects.

The demo workflow is a customer support ticket: an LLM (or a keyword fallback) reads the ticket,
a manager approves any refund, and then the refund is issued exactly once. It is a learning project,
not a replacement for Temporal or Airflow (see Limitations).

## Flow

```mermaid
flowchart TD
    A[POST /workflows + Idempotency-Key] --> B[analyze: LLM or keyword rules]
    B --> C{action == refund?}
    C -- no --> E[execute: nothing to do]
    C -- yes --> D[waiting_approval]
    D -- manager approves --> F[execute: refund via provider]
    D -- manager rejects --> R[rejected]
    F --> E2[completed]
    E --> E2
    B -. retries exhausted .-> X[failed]
    F -. retries exhausted .-> X
    X -. manager retries .-> F
```

Every state change and every step writes a row to `audit_logs`.

## What it handles

| Problem | How |
|---|---|
| Client retries the same request | `Idempotency-Key` header, unique per tenant. Same key + same body returns the original workflow (200), same key + different body is a 409. Parallel requests with the same key create one workflow |
| Server dies halfway | Step results live in `step_runs`. A sweeper resumes workflows that are `pending`/`running` and that nobody is working on, finished steps are skipped |
| Provider charged us but we crashed before saving | The refund call carries its own key (`wf-<id>-refund`), so the second attempt gets the existing refund back instead of creating a new one |
| Two workers pick up the same workflow | A lease (`locked_by`, `locked_until`) is claimed with one atomic UPDATE and renewed before every attempt. A dead worker's lease expires and someone else takes over |
| Double click on "Approve" | The status change is a compare-and-swap (`UPDATE ... WHERE status = 'waiting_approval'`), exactly one request wins, the others get a 409 |
| A step that kills the process every time | Attempts are saved before the call. After `STEP_MAX_ATTEMPTS` the step is marked failed instead of crash-looping forever |
| Flaky provider or bad LLM output | Retries with exponential backoff and jitter, then `failed`. A manager can retry a failed workflow, finished steps are not repeated |
| LLM suggests something dangerous | Output is validated (allowed actions, refund cap, no NaN), refunds always need a human approval |
| Ticket mentions two amounts | The keyword classifier escalates to a human instead of guessing which one is the refund |
| Prompt injection in the ticket | The system prompt marks the ticket as untrusted, but the real protection is validation + approval + the cap |
| Provider error text containing the API key | LLM errors are stored as the exception class name only, never the message |
| Someone approves their own request | Blocked, the approver has to be a different manager/admin |
| Tenant data leaking | Every query is filtered by the tenant, other tenants get a 404 (same as "does not exist") |
| Forged or stale tokens | Algorithm is pinned, `exp` is required, and role and tenant are re-read from the database on every request |
| Password guessing | Failed logins are counted per IP + email (429 after 5), unknown emails take as long as wrong passwords |
| Weird input | Blank text, NUL characters (Postgres can't store them), oversized ids (32 bit column), unknown status filters are all 422s instead of 500s |
| Weak config | The app refuses to start with a missing or short `SECRET_KEY` |

## Running it

Needs a running PostgreSQL. One-time setup (names are just examples):

```bash
sudo -u postgres psql -c "create user relay with password 'relay' createdb"
sudo -u postgres psql -c "create database relayforge owner relay"
sudo -u postgres psql -c "create database relayforge_test owner relay"
```

Then:

```bash
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt -r requirements-dev.txt
cp .env.example .env          # set SECRET_KEY (32+ chars), check DATABASE_URL
alembic upgrade head
uvicorn app.main:app --reload
```

If you don't want Postgres for a quick try, set `DATABASE_URL=sqlite:///./relayforge.db` and it works the same way.

The web UI is at http://127.0.0.1:8000/ (log in, send tickets, approve, read the audit history). API docs are at http://127.0.0.1:8000/docs. Use the Authorize button with the token from `/auth/login`.

Endpoints:

| Method | Path | Who |
|---|---|---|
| POST | `/auth/signup` | anyone, creates a company and its first admin |
| POST | `/auth/login`, GET `/auth/me` | |
| POST | `/auth/users` | admin |
| POST | `/workflows` | any user (`Idempotency-Key` header recommended) |
| GET | `/workflows`, `/workflows/{id}`, `/workflows/{id}/audit` | any user of the same tenant |
| POST | `/workflows/{id}/approve`, `/reject`, `/retry` | manager or admin |
| GET | `/health` | |

Quick walkthrough with curl:

```bash
B=http://127.0.0.1:8000
curl -X POST $B/auth/signup -H 'content-type: application/json' \
  -d '{"company_name":"Acme Corp","email":"admin@acme.test","password":"password123"}'
# log in, create an agent with POST /auth/users, then as the agent:
curl -X POST $B/workflows -H "authorization: Bearer $AGENT" \
  -H 'Idempotency-Key: ticket-1042-a' -H 'content-type: application/json' \
  -d '{"customer_email":"r@x.com","subject":"Refund","body":"Charged twice, refund Rs 2000"}'
# the workflow stops at waiting_approval, now as the admin or a manager:
curl -X POST $B/workflows/1/approve -H "authorization: Bearer $ADMIN"
curl $B/workflows/1/audit -H "authorization: Bearer $ADMIN"
```

To use a real LLM, put `OPENAI_API_KEY` in `.env`. Without it the keyword classifier is used.

### Crash demo

```bash
bash scripts/crash_demo.sh
```

It starts the server with a provider that always fails, approves a refund, `kill -9`s the server
while the refund step is retrying, starts a new server and checks that the workflow finishes and
the provider recorded exactly one refund. Example output:

```
2. manager approved, the refund step is retrying:
  status: running  steps: [('analyze', 'done', 1), ('approval', 'done', 1), ('execute', 'running', 2)]
3. kill -9 the server
4. start a new server, provider is healthy again
5. picked up and finished after about 5s:
  status: completed  steps: [('analyze', 'done', 1), ('approval', 'done', 1), ('execute', 'done', 3)]
6. refunds the provider recorded for this workflow: 1 (should be 1)
```

The 5 seconds are the demo's short lease (`LEASE_SECONDS=5`), the default is 60.

## Deploying to Render (free plan)

`render.yaml` describes a free web service and a free Postgres. On Render: New > Blueprint, pick the
GitHub repo, apply. The start command runs the migrations, creates the demo data if it is missing and
starts uvicorn. `DEMO_MODE=true` shows two ready-made logins on the login page (agent and manager) and
`SIGNUP_ENABLED=false` stops strangers from creating companies. Leave `OPENAI_API_KEY` unset on a public
demo so nobody can run up your bill.

Free plan facts to keep in mind: the service sleeps after 15 minutes without traffic (the first request
afterwards takes about a minute), and the free database expires after 30 days.

## Tests

```bash
pytest                                   # SQLite in a temp file, nothing to set up
TEST_DATABASE_URL=postgresql+psycopg://relay:relay@localhost:5432/relayforge_test pytest
```

The Postgres run drops and recreates all tables in that database on every test, so the name has
to end with `_test` (the suite refuses to start otherwise). Never point it at your real database.
Tests never call OpenAI, the OpenAI client is replaced by a stub.

| File | What it proves |
|---|---|
| `test_engine.py` | crash after the provider already refunded does not refund twice, the LLM step runs once across restarts, retries and giving up |
| `test_concurrency.py` | parallel creates with one key, double approve, three workers on one workflow, lease takeover, poison step, manual retry |
| `test_approval.py` | approval rules, self-approval blocked, reject, double approve |
| `test_auth_tenancy.py` | roles, tenant isolation, pagination |
| `test_idempotency.py` | idempotency keys per tenant, provider-side dedup |
| `test_ui.py`, `test_demo.py` | the UI page is served, signup switch, demo seed is repeatable and the demo story works |
| `test_edge_cases.py` | input validation, forged tokens, login limiter, classifier amounts, OpenAI stub (key never leaks, garbage output, prompt injection), config checks, sweeper |

The concurrency tests were checked by removing the protection on purpose (no lease, no
compare-and-swap) and watching them fail.

## Layout

```
app/
  engine/states.py    allowed state transitions
  engine/runner.py    leases, step execution, retries, resume logic
  engine/steps.py     the actual step bodies
  engine/service.py   create / approve / reject / retry / sweep
  ai.py               LLM call + keyword fallback + output validation
  gateway.py          fake payment provider (dedupes on key)
  ratelimit.py        login failure limiter
  routers/            auth and workflow endpoints
migrations/           alembic
scripts/crash_demo.sh
tests/
```

## Limitations

- Steps are hard-coded for the ticket workflow. There is no generic "define your own workflow" layer.
- Work runs inside the API process (FastAPI background tasks plus a sweeper loop). Leases make several API processes safe, but a separate worker process and a real queue would scale better.
- Leases compare timestamps written by different servers, so clocks should be roughly in sync (NTP).
- A worker that stalls longer than the lease and then wakes up can still finish its current call. Idempotency keys on the provider side are what protect the outside world in that case.
- Money is stored as a float rounded to 2 decimals, a real system would use integer minor units.
- The login limiter lives in memory, so it is per process and resets on restart. Redis would fix that.
- Agents can see all workflows of their company, not just their own.
- The keyword classifier is deliberately dumb ("I don't want a refund" still looks like a refund request). Approval catches it, but it is not a language model.
- No per-step timeout besides the OpenAI client timeout, no metrics endpoint, no WebSockets.
- Tested on PostgreSQL 16 and SQLite. I have not tried the OpenAI call against the live API from this setup, only with a stub.
- Temporal, Inngest and similar tools solve this properly, with replay, timers, signals and workers. This project exists to understand the ideas behind them.

## Ideas for later

A separate worker using `SKIP LOCKED`, Redis for rate limiting, Docker Compose, a `/metrics` endpoint,
step timeouts, more workflow types.
