import logging
import os

import psycopg
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

from app import ingest, pricing, webhooks

log = logging.getLogger("uvicorn.error")

LOG_LEVELS = ("DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL")
LOG_FORMAT = "%(asctime)s %(levelname)s %(name)s %(message)s"


def configure_logging() -> None:
    """Show every `app.*` log line at LOG_LEVEL (default INFO). Safe to call more than once.

    uvicorn configures only its own loggers; Python's default (WARNING) hid the INFO lines of app.webhooks, app.pricing
    and the rest, so a duplicate, ignored or stale webhook left no trace. `uvicorn.*` loggers do not propagate to the
    root logger, so adding a root handler cannot print their lines twice. An invalid LOG_LEVEL falls back to INFO.
    """
    raw = os.environ.get("LOG_LEVEL", "INFO")
    name = raw.strip().upper()
    level = name if name in LOG_LEVELS else "INFO"
    logging.basicConfig(level=level, format=LOG_FORMAT)        # does nothing if the root logger already has a handler
    logging.getLogger("app").setLevel(level)                   # so the level holds even then
    if name not in LOG_LEVELS:
        logging.getLogger("app").warning("LOG_LEVEL=%r is not one of %s; using INFO", raw, ", ".join(LOG_LEVELS))


def create_app() -> FastAPI:
    configure_logging()
    app = FastAPI(title="usage-metering")

    @app.exception_handler(psycopg.OperationalError)
    async def database_unavailable(request: Request, exc: psycopg.OperationalError) -> JSONResponse:
        """A database that is down, restarting or out of connections is 503 on every route, not a bare 500.

        The edge already answers 503 when its own read fails; this covers a database that dies after the edge check.
        Nothing the handler did is committed: a connection that fails mid-request has rolled back.
        """
        log.error("database unavailable: %s %s: %s", request.method, request.url.path, exc)
        return JSONResponse(status_code=503, content={"detail": "database unavailable"})

    @app.get("/healthz")
    def healthz() -> dict[str, str]:
        return {"status": "ok"}

    app.include_router(ingest.router)
    app.include_router(webhooks.router)
    app.include_router(pricing.router)
    return app


app = create_app()
