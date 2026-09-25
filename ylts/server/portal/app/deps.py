"""Shared FastAPI dependencies."""
from __future__ import annotations

from typing import Iterator, Optional

from fastapi import Depends, HTTPException, Request
from sqlalchemy import select
from sqlalchemy.orm import Session

from .db import AccessToken, User, utcnow
from .security import Crypto, token_hash


def get_db(request: Request) -> Iterator[Session]:
    yield from request.app.state.db.session()


def get_crypto(request: Request) -> Crypto:
    return request.app.state.crypto


def client_ip(request: Request) -> str:
    # Caddy sets X-Forwarded-For; only trust it because the portal is not exposed directly.
    fwd = request.headers.get("x-forwarded-for", "")
    if fwd:
        return fwd.split(",")[0].strip()
    return request.client.host if request.client else ""


class ApiError(HTTPException):
    """RustDesk clients expect {"error": "..."} bodies."""


def bearer_token(request: Request) -> str:
    auth = request.headers.get("authorization", "")
    if auth.lower().startswith("bearer "):
        return auth[7:].strip()
    return ""


def optional_api_user(request: Request, s: Session = Depends(get_db)) -> Optional[User]:
    tok = bearer_token(request)
    if not tok:
        return None
    row = s.scalar(select(AccessToken).where(AccessToken.token_hash == token_hash(tok)))
    now = utcnow()
    if not row or row.expires_at <= now or not row.user.active:
        return None
    if (now - row.last_used).total_seconds() > 300:
        row.last_used = now
    request.state.access_token = row
    return row.user


def api_user(user: Optional[User] = Depends(optional_api_user)) -> User:
    if user is None:
        raise ApiError(status_code=401, detail="Invalid token")
    return user
