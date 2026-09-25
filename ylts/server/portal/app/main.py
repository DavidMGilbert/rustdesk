"""YLTS Portal: permissions and API server for the YLTS Remote Support stack."""
from __future__ import annotations

import asyncio
import contextlib
import logging
from pathlib import Path

from fastapi import FastAPI, HTTPException, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse, PlainTextResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from sqlalchemy import select
from starlette.middleware.sessions import SessionMiddleware

from . import admin, agent_api, rustdesk_api
from .config import Settings, settings as default_settings
from .db import Database, User, audit
from .permissions import expire_grants
from .security import Crypto, hash_password

log = logging.getLogger("ylts.portal")


def bootstrap(db: Database, cfg: Settings) -> None:
    db.create_all()
    with db.Session() as s:
        if s.scalar(select(User).limit(1)) is None:
            if not cfg.bootstrap_admin_password:
                log.warning("No users exist. Set BOOTSTRAP_ADMIN_PASSWORD to create the first admin.")
                return
            s.add(User(username=cfg.bootstrap_admin_user.lower(), display_name="Administrator",
                       password_hash=hash_password(cfg.bootstrap_admin_password), is_admin=True))
            audit(s, "user.bootstrap", detail=cfg.bootstrap_admin_user)
            s.commit()
            log.warning("Created first admin user '%s'. Change its password and enable 2FA.",
                        cfg.bootstrap_admin_user)


async def _maintenance(app: FastAPI) -> None:
    while True:
        try:
            with app.state.db.Session() as s:
                if expire_grants(s, app.state.crypto):
                    s.commit()
        except Exception:  # keep the loop alive
            log.exception("maintenance failed")
        await asyncio.sleep(60)


def create_app(cfg: Settings | None = None) -> FastAPI:
    cfg = cfg or default_settings
    cfg.validate()
    cfg.data_dir.mkdir(parents=True, exist_ok=True)

    @contextlib.asynccontextmanager
    async def lifespan(app: FastAPI):
        bootstrap(app.state.db, cfg)
        task = asyncio.create_task(_maintenance(app))
        yield
        task.cancel()

    app = FastAPI(title="YLTS Portal", docs_url=None, redoc_url=None, openapi_url=None, lifespan=lifespan)
    app.state.db = Database(cfg.db_url)
    app.state.crypto = Crypto(cfg.encryption_key)
    app.state.settings = cfg
    app.add_middleware(SessionMiddleware, secret_key=cfg.secret_key, session_cookie="ylts_portal",
                       max_age=12 * 3600, same_site="lax", https_only=cfg.secure_cookies)

    @app.middleware("http")
    async def security_headers(request: Request, call_next):
        resp = await call_next(request)
        resp.headers.setdefault("X-Content-Type-Options", "nosniff")
        resp.headers.setdefault("Referrer-Policy", "same-origin")
        if not request.url.path.startswith(("/api/", "/agent/")):
            resp.headers.setdefault("X-Frame-Options", "DENY")
            resp.headers.setdefault("Content-Security-Policy",
                                    "default-src 'self'; img-src 'self' data:; style-src 'self'; "
                                    "script-src 'self'; frame-ancestors 'none'; form-action 'self'")
        return resp

    @app.exception_handler(admin.NeedLogin)
    async def need_login(request: Request, exc: admin.NeedLogin):
        return RedirectResponse("/login", status_code=303)

    @app.exception_handler(HTTPException)
    async def http_error(request: Request, exc: HTTPException):
        if request.url.path.startswith(("/api/", "/agent/")):
            return JSONResponse({"error": str(exc.detail)}, status_code=exc.status_code)
        return PlainTextResponse(f"{exc.status_code}: {exc.detail}", status_code=exc.status_code)

    @app.exception_handler(RequestValidationError)
    async def validation_error(request: Request, exc: RequestValidationError):
        return JSONResponse({"error": "Invalid request"}, status_code=422)

    @app.get("/healthz")
    def healthz():
        return {"ok": True}

    app.include_router(rustdesk_api.router)
    app.include_router(agent_api.router)
    app.include_router(admin.router)
    app.mount("/static", StaticFiles(directory=str(Path(__file__).parent / "static")), name="static")
    return app


def app_factory() -> FastAPI:  # used by uvicorn --factory
    logging.basicConfig(level=logging.INFO)
    return create_app()
