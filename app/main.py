from fastapi import FastAPI

from app import ingest, pricing, webhooks


def create_app() -> FastAPI:
    app = FastAPI(title="usage-metering")

    @app.get("/healthz")
    def healthz() -> dict[str, str]:
        return {"status": "ok"}

    app.include_router(ingest.router)
    app.include_router(webhooks.router)
    app.include_router(pricing.router)
    return app


app = create_app()
