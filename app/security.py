import functools
from datetime import timedelta

import jwt
from pwdlib import PasswordHash

from app.config import settings
from app.models import User, utcnow

_hasher = PasswordHash.recommended()


def hash_password(raw: str) -> str:
    return _hasher.hash(raw)


def verify_password(raw: str, hashed: str) -> bool:
    try:
        return _hasher.verify(raw, hashed)
    except Exception:
        return False


@functools.lru_cache(maxsize=1)
def _dummy_hash() -> str:
    return hash_password("not-a-real-password")


def burn_hash_time(raw: str) -> None:
    """Login for an unknown email should take as long as one for a known email,
    otherwise response time tells an attacker which emails are registered."""
    verify_password(raw, _dummy_hash())


def make_token(user: User) -> str:
    claims = {
        "sub": str(user.id),
        "tid": user.tenant_id,
        "role": user.role,
        "exp": utcnow() + timedelta(minutes=settings.token_minutes),
    }
    return jwt.encode(claims, settings.secret_key, algorithm="HS256")


def read_token(token: str) -> dict:
    return jwt.decode(token, settings.secret_key, algorithms=["HS256"],
                      options={"require": ["exp", "sub"]})
