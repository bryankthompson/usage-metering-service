"""A request body size cap that runs before the app reads or parses the body, or runs any dependency.

FastAPI parses the body before it resolves dependencies, so on POST /v1/usage a caller with no credentials made the API
buffer the whole body before the edge could answer 401 (a 600 MB body took the process from 56 MiB to 915 MiB). This pure
ASGI guard answers 413 from the Content-Length header without reading anything, and counts the stream as it arrives for a
body that sends no (or a wrong) Content-Length, so memory stays near the cap. The webhook has its own cap (webhooks.raw_body).
"""
from starlette.exceptions import HTTPException
from starlette.responses import JSONResponse
from starlette.types import ASGIApp, Receive, Scope, Send

TOO_LARGE = "request body too large"


class BodyLimit:
    def __init__(self, app: ASGIApp, limits: dict[str, int]) -> None:
        self.app = app
        self.limits = limits                      # path -> largest body in bytes; any other path is not limited here

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        limit = self.limits.get(scope["path"]) if scope["type"] == "http" else None
        if limit is None:
            await self.app(scope, receive, send)
            return
        declared = dict(scope["headers"]).get(b"content-length", b"")
        if declared.isdigit() and int(declared) > limit:
            await JSONResponse({"detail": TOO_LARGE}, status_code=413, headers={"Connection": "close"})(scope, receive, send)
            return
        seen = 0

        async def counted() -> dict:
            nonlocal seen
            message = await receive()
            if message["type"] == "http.request":
                seen += len(message.get("body", b""))
                if seen > limit:                  # FastAPI re-raises an HTTPException from the body read: a 413, not a 400
                    raise HTTPException(status_code=413, detail=TOO_LARGE)
            return message

        await self.app(scope, counted, send)
