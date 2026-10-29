import asyncio
import logging
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.responses import FileResponse, JSONResponse
from sqlalchemy import text
from sqlalchemy.exc import SQLAlchemyError

from app.config import check_secret_key, settings
from app.db import SessionLocal
from app.demo import DEMO_ACCOUNTS
from app.engine.service import sweep_once
from app.errors import DomainError
from app.middleware import new_request_id, request_id_var, setup_logging
from app.routers import auth, workflows

log = logging.getLogger("relayforge")


async def _sweeper():
    """Every so often, continue workflows that nobody is working on. This is also what
    resumes things after a restart: the first sweep runs right at startup."""
    while True:
        try:
            n = await asyncio.to_thread(sweep_once)
            if n:
                log.info("sweeper picked up %d workflow(s)", n)
        except SQLAlchemyError:
            log.exception("sweeper could not reach the database (did you run alembic upgrade head?)")
        except Exception:
            log.exception("sweeper failed")
        await asyncio.sleep(settings.sweep_interval_s)


@asynccontextmanager
async def lifespan(_app: FastAPI):
    setup_logging()
    check_secret_key()
    task = asyncio.create_task(_sweeper()) if settings.sweeper_enabled else None
    try:
        yield
    finally:
        if task:
            task.cancel()


app = FastAPI(title="RelayForge", version="0.2.0", lifespan=lifespan)


@app.middleware("http")
async def request_id_middleware(request: Request, call_next):
    rid = new_request_id(request.headers.get("x-request-id"))
    token = request_id_var.set(rid)
    try:
        response = await call_next(request)
    finally:
        request_id_var.reset(token)
    response.headers["X-Request-ID"] = rid
    return response


@app.exception_handler(DomainError)
async def domain_error_handler(_req: Request, exc: DomainError):
    return JSONResponse(status_code=exc.status_code, content={"detail": exc.message})


@app.get("/", include_in_schema=False)
def ui():
    return FileResponse(Path(__file__).parent / "static" / "index.html")


@app.get("/config", include_in_schema=False)
def public_config():
    # the page uses this to hide the signup tab and to offer the demo logins
    accounts = [{"email": a["email"], "password": a["password"], "label": a["label"]}
                for a in DEMO_ACCOUNTS] if settings.demo_mode else []
    return {"signup_enabled": settings.signup_enabled, "demo_accounts": accounts}


@app.get("/health", tags=["ops"])
def health():
    try:
        with SessionLocal() as db:
            db.execute(text("SELECT 1"))
    except SQLAlchemyError:
        return JSONResponse(status_code=503, content={"status": "db_unavailable"})
    return {"status": "ok"}


app.include_router(auth.router)
app.include_router(workflows.router)
