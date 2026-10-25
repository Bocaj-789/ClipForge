"""FastAPI application factory (run with uvicorn's --factory flag: app.main:create_app)."""

from __future__ import annotations

import logging
import secrets
import sys
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from urllib.parse import urlsplit

from fastapi import APIRouter, FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import FileResponse, JSONResponse, Response
from starlette.exceptions import HTTPException as StarletteHTTPException

from app.api.routes import auth as auth_routes
from app.api.routes import channels, clips, inbox, jobs, logs, settings, sources, system
from app.config import REPO_ROOT, Config, get_config
from app.core import auth
from app.core.errors import AppError, ErrorCode
from app.core.security import PathContainmentError
from app.db.migrate import upgrade_to_head
from app.db.session import init_engine, session_scope
from app.logging_config import setup_logging

log = logging.getLogger("clipforge.api")
FRONTEND_DIST = REPO_ROOT / "frontend" / "dist"

CSP = (
    "default-src 'self'; img-src 'self' data: https://i.ytimg.com; media-src 'self'; "
    "style-src 'self' 'unsafe-inline'; script-src 'self'; connect-src 'self'; frame-ancestors 'none'; "
    "base-uri 'none'; form-action 'self'; object-src 'none'"
)


def _error(status: int, code: str, message: str, request: Request) -> JSONResponse:
    rid = getattr(request.state, "request_id", None)
    return JSONResponse({"error": {"code": code, "message": message, "request_id": rid}}, status_code=status)


def create_app(cfg: Config | None = None, *, configure_logging: bool = True) -> FastAPI:
    cfg = cfg or get_config()

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        cfg.ensure_dirs()
        if configure_logging:
            setup_logging(cfg.logs_dir, "api")
        upgrade_to_head(cfg.database_url)
        init_engine(cfg.database_url)
        with session_scope() as db:
            if not auth.user_exists(db):
                token = app.state.setup_token.issue(cfg.secrets_dir)
                # Printed straight to the local terminal, deliberately not via the logger.
                print(
                    f"\n  ClipForge första start – installationskod: {token}\n"
                    f"  (finns även i {cfg.secrets_dir / 'setup-token.txt'})\n",
                    file=sys.stderr,
                    flush=True,
                )
        yield

    app = FastAPI(
        title="ClipForge",
        version="0.1.0",
        lifespan=lifespan,
        docs_url="/api/docs" if cfg.dev_mode else None,
        redoc_url=None,
        openapi_url="/api/openapi.json" if cfg.dev_mode else None,
    )
    app.state.cfg = cfg
    app.state.setup_token = auth.SetupToken()
    app.state.login_limiter = auth.RateLimiter(limit=5, window_seconds=60)
    app.state.setup_limiter = auth.RateLimiter(limit=5, window_seconds=300)

    @app.middleware("http")
    async def security(request: Request, call_next):  # type: ignore[no-untyped-def]
        request.state.request_id = request.headers.get("x-request-id", "")[:64] or secrets.token_hex(8)
        if request.method not in ("GET", "HEAD", "OPTIONS"):
            origin = request.headers.get("origin")
            host = request.headers.get("host", "")
            if origin and urlsplit(origin).netloc != host:
                return _error(403, ErrorCode.FORBIDDEN, "Anrop från annan webbplats nekades.", request)
        response: Response = await call_next(request)
        response.headers["X-Request-ID"] = request.state.request_id
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["X-Frame-Options"] = "DENY"
        response.headers["Referrer-Policy"] = "no-referrer"
        response.headers["Permissions-Policy"] = "camera=(), microphone=(), geolocation=()"
        response.headers.setdefault("Content-Security-Policy", CSP)
        if request.url.path.startswith("/api/") and "cache-control" not in response.headers:
            response.headers["Cache-Control"] = "no-store"
        return response

    @app.exception_handler(AppError)
    async def app_error(request: Request, exc: AppError) -> JSONResponse:
        return _error(exc.status_code, exc.code, exc.safe_message, request)

    @app.exception_handler(PathContainmentError)
    async def path_error(request: Request, exc: PathContainmentError) -> JSONResponse:
        return _error(400, ErrorCode.INVALID_INPUT, "Ogiltig sökväg.", request)

    @app.exception_handler(RequestValidationError)
    async def validation_error(request: Request, exc: RequestValidationError) -> JSONResponse:
        first = exc.errors()[0] if exc.errors() else {}
        loc = ".".join(str(x) for x in first.get("loc", []) if x not in ("body", "query"))
        return _error(
            422, ErrorCode.INVALID_INPUT, f"Ogiltig indata ({loc}): {first.get('msg', '')}"[:300], request
        )

    @app.exception_handler(StarletteHTTPException)
    async def http_error(request: Request, exc: StarletteHTTPException) -> JSONResponse:
        code = ErrorCode.NOT_FOUND if exc.status_code == 404 else ErrorCode.INVALID_INPUT
        return _error(
            exc.status_code, code, "Hittades inte." if exc.status_code == 404 else str(exc.detail), request
        )

    @app.exception_handler(Exception)
    async def unhandled(request: Request, exc: Exception) -> JSONResponse:
        log.exception("unhandled error on %s %s", request.method, request.url.path)
        return _error(500, ErrorCode.INTERNAL, "Internt fel. Se loggen för detaljer.", request)

    api = APIRouter(prefix="/api/v1")
    for r in (
        system.router,
        auth_routes.router,
        inbox.router,
        jobs.router,
        clips.router,
        sources.router,
        channels.router,
        settings.router,
        logs.router,
    ):
        api.include_router(r)
    app.include_router(api)

    @app.get("/{full_path:path}", include_in_schema=False)
    async def spa(full_path: str) -> Response:
        if full_path.startswith("api/"):
            raise StarletteHTTPException(404)
        dist = FRONTEND_DIST.resolve()
        if not (dist / "index.html").is_file():
            return JSONResponse(
                {"message": "Frontend är inte byggd. Kör: cd frontend && npm run build"}, status_code=503
            )
        candidate = (dist / full_path).resolve()
        if full_path and dist in candidate.parents and candidate.is_file():
            headers = (
                {"Cache-Control": "public, max-age=31536000, immutable"}
                if full_path.startswith("assets/")
                else {}
            )
            return FileResponse(candidate, headers=headers)
        return FileResponse(dist / "index.html", headers={"Cache-Control": "no-cache"})

    return app
