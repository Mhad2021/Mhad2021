"""Presence — employee attendance and activity tracking.

FastAPI application factory: REST API for the desktop agent and dashboards,
server-rendered management UI, and the background monitoring scheduler.
"""
from __future__ import annotations

import logging
import os
import sys
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import APIRouter, FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from starlette.exceptions import HTTPException as StarletteHTTPException
from starlette.middleware.trustedhost import TrustedHostMiddleware

from app.config import settings

logging.basicConfig(
    level=logging.DEBUG if settings.debug else logging.INFO,
    format="%(asctime)s %(levelname)-8s %(name)s: %(message)s",
)
logger = logging.getLogger(__name__)

BASE_DIR = Path(__file__).resolve().parent


def _preflight() -> None:
    """Refuse to start in production with development defaults still in place."""
    if not settings.is_production:
        return

    problems = []
    if "CHANGE-ME" in settings.secret_key or len(settings.secret_key) < 32:
        problems.append("SECRET_KEY must be set to a random value of 32+ characters")
    if not settings.encryption_key:
        problems.append(
            "ENCRYPTION_KEY must be set (generate with: "
            "python -c \"from cryptography.fernet import Fernet; "
            "print(Fernet.generate_key().decode())\")"
        )
    if settings.database_url.startswith("sqlite"):
        problems.append("SQLite is not supported in production — use PostgreSQL")
    if not settings.secure_cookies:
        problems.append("SECURE_COOKIES must be true when serving over HTTPS")

    if problems:
        for problem in problems:
            logger.critical("Startup check failed: %s", problem)
        sys.exit(1)


@asynccontextmanager
async def lifespan(app: FastAPI):
    _preflight()
    logger.info(
        "Starting %s (%s) at %s", settings.app_name, settings.environment, settings.base_url
    )

    from app.scheduler import shutdown, start

    # A multi-worker deployment must run exactly one scheduler, otherwise every
    # worker raises the same alerts. See deploy/README for the worker split.
    if os.getenv("PRESENCE_DISABLE_SCHEDULER", "").lower() not in ("1", "true", "yes"):
        start()
    else:
        logger.info("Scheduler disabled for this process (PRESENCE_DISABLE_SCHEDULER)")

    yield
    shutdown()


def create_app() -> FastAPI:
    app = FastAPI(
        title=f"{settings.app_name} API",
        description=(
            "Employee attendance and activity tracking. Records work-session "
            "status only — never keystroke content, messages, screenshots or files."
        ),
        version="1.0.0",
        lifespan=lifespan,
        docs_url="/api/docs" if not settings.is_production else None,
        redoc_url=None,
        openapi_url="/api/openapi.json" if not settings.is_production else None,
    )

    if settings.trusted_hosts and settings.trusted_hosts != ["*"]:
        app.add_middleware(TrustedHostMiddleware, allowed_hosts=settings.trusted_hosts)
    if settings.cors_origins:
        app.add_middleware(
            CORSMiddleware,
            allow_origins=settings.cors_origins,
            allow_credentials=True,
            allow_methods=["*"],
            allow_headers=["*"],
        )

    # --- API ---------------------------------------------------------------
    from app.api.v1 import agent, alerts, auth, employees, live, reports, settings_api

    api = APIRouter(prefix="/api/v1")
    api.include_router(auth.router)
    api.include_router(agent.router)
    api.include_router(employees.router)
    api.include_router(live.router)
    api.include_router(reports.router)
    api.include_router(alerts.router)
    api.include_router(settings_api.router)
    app.include_router(api)

    # --- Web UI ------------------------------------------------------------
    from app.web.routes import router as web_router

    app.include_router(web_router)

    static_dir = BASE_DIR / "web" / "static"
    if static_dir.exists():
        app.mount("/static", StaticFiles(directory=static_dir), name="static")

    # --- Health ------------------------------------------------------------
    @app.get("/health", include_in_schema=False)
    def health() -> dict:
        from sqlalchemy import text

        from app.database import SessionLocal

        db_ok = True
        db = SessionLocal()
        try:
            db.execute(text("SELECT 1"))
        except Exception:  # noqa: BLE001
            db_ok = False
            logger.exception("Health check: database unreachable")
        finally:
            db.close()

        return {
            "status": "ok" if db_ok else "degraded",
            "database": "up" if db_ok else "down",
            "version": app.version,
            "environment": settings.environment,
        }

    # --- Error handling ----------------------------------------------------
    @app.exception_handler(StarletteHTTPException)
    async def http_exception_handler(request: Request, exc: StarletteHTTPException):
        # Browsers hitting a protected page get a redirect to sign in;
        # API clients get JSON.
        wants_html = "text/html" in request.headers.get("accept", "")
        if exc.status_code == 401 and wants_html and not request.url.path.startswith("/api"):
            return RedirectResponse(
                url=f"/login?next={request.url.path}", status_code=303
            )
        return JSONResponse(
            status_code=exc.status_code,
            content={"detail": exc.detail},
            headers=getattr(exc, "headers", None),
        )

    @app.exception_handler(RequestValidationError)
    async def validation_handler(request: Request, exc: RequestValidationError):
        first = exc.errors()[0] if exc.errors() else {}
        field = ".".join(str(p) for p in first.get("loc", [])[1:]) or "request"
        return JSONResponse(
            status_code=422,
            content={
                "detail": f"{field}: {first.get('msg', 'invalid value')}",
                "errors": exc.errors(),
            },
        )

    return app


app = create_app()
