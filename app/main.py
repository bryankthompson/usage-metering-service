import logging

import psycopg
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

from app import ingest, pricing, webhooks

log = logging.getLogger("uvicorn.error")


def create_app() -> FastAPI:
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
