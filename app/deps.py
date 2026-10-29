import jwt
from fastapi import Depends, HTTPException
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from sqlalchemy.orm import Session

from app.db import get_db
from app.errors import Forbidden
from app.models import User
from app.security import read_token

_bearer = HTTPBearer(auto_error=False)


def current_user(creds: HTTPAuthorizationCredentials | None = Depends(_bearer),
                 db: Session = Depends(get_db)) -> User:
    unauthorized = HTTPException(401, "invalid or missing token",
                                 headers={"WWW-Authenticate": "Bearer"})
    if creds is None:
        raise unauthorized
    try:
        claims = read_token(creds.credentials)
        user = db.get(User, int(claims["sub"]))
    except (jwt.PyJWTError, KeyError, ValueError):
        raise unauthorized
    # role and tenant come from the DB, not the token, so changes apply immediately
    if user is None or user.tenant_id != claims.get("tid"):
        raise unauthorized
    return user


def require_roles(*roles: str):
    def check(user: User = Depends(current_user)) -> User:
        if user.role not in roles:
            raise Forbidden(f"requires one of: {', '.join(roles)}")
        return user
    return check
