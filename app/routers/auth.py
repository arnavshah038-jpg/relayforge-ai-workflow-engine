from fastapi import APIRouter, Depends, HTTPException, Request
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.config import settings
from app.db import get_db
from app.deps import current_user, require_roles
from app.errors import Conflict, Forbidden
from app.models import Tenant, User
from app.schemas import LoginIn, NewUserIn, SignupIn, TokenOut, UserOut
from app.ratelimit import FailureLimiter
from app.security import burn_hash_time, hash_password, make_token, verify_password

router = APIRouter(prefix="/auth", tags=["auth"])

login_limiter = FailureLimiter(settings.login_max_failures, settings.login_window_s)


@router.post("/signup", response_model=UserOut, status_code=201)
def signup(body: SignupIn, db: Session = Depends(get_db)):
    """Creates a new company (tenant) with its first admin."""
    if not settings.signup_enabled:
        raise Forbidden("signup is switched off on this server")
    email = body.email.lower()
    if db.scalar(select(User).where(User.email == email)):
        raise Conflict("email already registered")
    tenant = Tenant(name=body.company_name)
    db.add(tenant)
    db.flush()
    user = User(tenant_id=tenant.id, email=email, role="admin",
                password_hash=hash_password(body.password))
    db.add(user)
    try:
        db.commit()
    except IntegrityError:
        db.rollback()
        raise Conflict("email already registered")
    return user


@router.post("/login", response_model=TokenOut)
def login(body: LoginIn, request: Request, db: Session = Depends(get_db)):
    email = body.email.strip().lower()
    client_ip = request.client.host if request.client else "unknown"
    limiter_key = f"{client_ip}|{email}"

    if login_limiter.blocked(limiter_key):
        raise HTTPException(429, "too many failed attempts, try again later")

    user = db.scalar(select(User).where(User.email == email))
    if user is None:
        burn_hash_time(body.password)
    if user is None or not verify_password(body.password, user.password_hash):
        login_limiter.add_failure(limiter_key)
        raise HTTPException(401, "wrong email or password")

    login_limiter.clear(limiter_key)
    return TokenOut(access_token=make_token(user))


@router.get("/me", response_model=UserOut)
def me(user: User = Depends(current_user)):
    return user


@router.post("/users", response_model=UserOut, status_code=201)
def add_user(body: NewUserIn, admin: User = Depends(require_roles("admin")),
             db: Session = Depends(get_db)):
    email = body.email.lower()
    if db.scalar(select(User).where(User.email == email)):
        raise Conflict("email already registered")
    user = User(tenant_id=admin.tenant_id, email=email, role=body.role,
                password_hash=hash_password(body.password))
    db.add(user)
    db.commit()
    return user
