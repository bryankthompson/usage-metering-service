# Code tour: five Python features, where they are in this code

For a reader who has written Python for about a year. Each item says what the feature is, where it is used here, and what
would break without it. Line numbers are for the tree this was written on; `grep -n` the quoted text if they have moved.

## 1. A decorator: `@app.get("/healthz")` (`app/main.py:46`)
A decorator is a function that takes the function written under it and registers or wraps it. This one tells FastAPI
"answer GET /healthz by calling `healthz`", and gives the function back unchanged.
Without it, `healthz` is an ordinary function nobody calls, and `GET /healthz` answers 404.
The routes `@router.post("/v1/usage")` (`app/ingest.py:116`) and `@router.post("/v1/stripe/webhook")` (`app/webhooks.py:210`)
work the same way; `app.include_router(...)` in `main.py` is what attaches them to the app.

## 2. A context manager: `with connect() as conn:` (`app/edge.py:52`)
`with` runs setup, your block, then cleanup, even if the block raises. A psycopg connection used this way commits if the
block ends cleanly, rolls back if it raises, and always closes the connection.
Without the `with` (a bare `conn = connect()`), the connection stays open until Python gets around to collecting it, and a
burst of requests can use up Postgres's connection slots. `app/ingest.py:128` carries a comment about exactly that.

## 3. Dependency injection: `Depends` (`app/ingest.py:120`)
`tenant: str = Depends(require_entitlement)` tells FastAPI "before calling this handler, call `require_entitlement`, and pass
me what it returns as `tenant`". If that function raises an `HTTPException` (401, 402 or 503 here), the handler never runs.
Without `Depends`, `tenant` becomes a plain query parameter the caller types in. Anyone could name any tenant, and
nothing would check the entitlement. `app/pricing.py:161` and `app/webhooks.py:211` use it too.

## 4. `raise ... from` (`app/edge.py:57`)
`raise _deny(503, ...) from err` raises a new exception while recording the old one (`err`, the database error) as its
cause, so the traceback reads "the above exception was the direct cause of the following one". Use `from None` to hide the old
one on purpose, as `app/ingest.py:107` does for an overflow the caller does not need to see.
Without `from err`, the caller still gets the same 503. Python chains the two anyway, but the log wording becomes
"during handling of the above exception, another exception occurred", which reads like a bug in the handler.

## 5. Star-unpack: `(hour, *result)` (`app/rollup.py:100`)
`*result` splices the items of an iterable into the tuple being built. Here `result` is `(rows written, events counted)`,
so `(hour, *result)` is `(hour, rows, events)`, one flat tuple of three.
Without the star, the tuple is `(hour, (rows, events))`. The loop at `app/rollup.py:110`, `for hour, rows, events in closed`,
would then raise `ValueError: not enough values to unpack (expected 3, got 2)`.
The same idea with two stars merges dicts: `{"a": f, **{name: g for name in names}}` in `app/webhooks.py:179`.
