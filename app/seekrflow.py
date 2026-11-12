"""Platform adapter: turn an inference response's `usage` into one usage event for POST /v1/usage.

Built from the public API documentation only; no call to the real service has been made (docs/seekrflow.md).
The response does not say which customer made the call, so the caller supplies the tenant.
Standard library only.

    python -m app.seekrflow replay FILE.json --tenant T [--api URL] [--model M]
    python -m app.seekrflow live --tenant T --prompt TEXT [--api URL] [--spool FILE]     (needs SEEKR_API_KEY)
"""

import argparse
import json
import os
import sys
import urllib.error
import urllib.request
from datetime import datetime, timezone

DEFAULT_BASE = "https://flow.seekr.com/v1"
DEFAULT_MODEL = "meta-llama/Llama-3.1-8B-Instruct"
DEFAULT_API = "http://127.0.0.1:8000"


class MissingUsage(ValueError):
    """The response has no usable `usage`. It is never counted as zero: n/a is not 0."""


def _count(usage: dict, key: str) -> int:
    value = usage.get(key)
    if isinstance(value, bool) or not isinstance(value, int):
        raise MissingUsage(f"usage.{key} is missing or not an integer: {value!r}")
    if value < 0:
        raise MissingUsage(f"usage.{key} is negative: {value}")
    return value


def usage_event(response: dict, tenant_id: str, model: str | None = None) -> dict:
    """The body for POST /v1/usage. `model` is used only when the response has none (embeddings)."""
    usage = response.get("usage")
    if not isinstance(usage, dict):
        raise MissingUsage(f"response {response.get('id')!r} has no usage object")
    tokens_in = _count(usage, "prompt_tokens")
    # An embeddings response has `data` and no `choices`: no output tokens.
    tokens_out = 0 if "choices" not in response and "data" in response else _count(usage, "completion_tokens")
    created = response.get("created")
    if isinstance(created, bool) or not isinstance(created, int):
        raise MissingUsage(f"response created is missing or not an integer: {created!r}")
    model = response.get("model") or model
    if not model:
        raise MissingUsage(
            f"response {response.get('id')!r} has no model (embeddings responses have none): pass --model"
        )
    return {
        "tenant_id": tenant_id,
        "idempotency_key": response["id"],
        "model": model,
        "tokens_in": tokens_in,
        "tokens_out": tokens_out,
        "occurred_at": datetime.fromtimestamp(created, timezone.utc).isoformat(),
    }


class _Refuse(urllib.request.HTTPRedirectHandler):
    """A redirect would carry the API key to another origin: surface it as an HTTP error instead."""

    def redirect_request(self, *args, **kwargs):
        return None


_NO_REDIRECT = urllib.request.build_opener(_Refuse)


def _post_json(url: str, body: dict, headers: dict) -> tuple[int, dict]:
    request = urllib.request.Request(url, json.dumps(body).encode(), {"Content-Type": "application/json", **headers})
    try:
        with _NO_REDIRECT.open(request, timeout=30) as reply:
            return reply.status, json.load(reply)
    except urllib.error.HTTPError as err:  # an answer with an error status; a transport error propagates
        try:
            return err.code, json.load(err)
        except ValueError:  # a redirect or a proxy page has no JSON body: keep the status, drop the body
            return err.code, {}


def post_usage(event: dict, api_base: str, tenant_id: str) -> tuple[int, dict]:
    """POST the event to our intake. 201 accepted and 200 duplicate are both success. No retries."""
    return _post_json(f"{api_base}/v1/usage", event, {"X-Tenant-Id": tenant_id})


def fetch_chat(api_base: str, api_key: str, body: dict) -> dict:
    """POST a chat-completion request. The docs show the key as the bare Authorization value, no "Bearer"."""
    status, reply = _post_json(f"{api_base}/inference/chat/completions", body, {"Authorization": api_key})
    if status != 200:
        raise RuntimeError(f"chat completion answered {status}")  # the reply is left out: it could echo the request
    return reply


def _send(response: dict, tenant: str, api: str, model: str | None = None) -> None:
    event = usage_event(response, tenant, model)
    status, body = post_usage(event, api, tenant)
    if status not in (200, 201):
        raise RuntimeError(f"intake answered {status}: {body}")
    print(f"{event['idempotency_key']} in={event['tokens_in']} out={event['tokens_out']} " f"{status} {body['status']}")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m app.seekrflow")
    sub = parser.add_subparsers(dest="command", required=True)
    replay = sub.add_parser("replay", help="post a saved response (one JSON object or a list)")
    replay.add_argument("file")
    replay.add_argument("--model", help="model to use for a response that omits it (embeddings)")
    live = sub.add_parser("live", help="call the service once and post its usage (needs SEEKR_API_KEY)")
    live.add_argument("--prompt", required=True)
    live.add_argument(
        "--spool",
        help="save the response to this file before posting, so a failed post can be replayed (off by default)",
    )
    for p in (replay, live):
        p.add_argument("--tenant", required=True)
        p.add_argument("--api", default=DEFAULT_API, help=f"our intake (default {DEFAULT_API})")
    live.add_argument("--base", default=DEFAULT_BASE, help=f"the inference API (default {DEFAULT_BASE})")
    args = parser.parse_args(argv)
    try:
        if args.command == "replay":
            with open(args.file) as f:
                saved = json.load(f)
            for response in saved if isinstance(saved, list) else [saved]:
                _send(response, args.tenant, args.api, args.model)
        else:
            key = os.environ.get("SEEKR_API_KEY")
            if not key:
                raise RuntimeError("SEEKR_API_KEY is not set")
            body = {"model": DEFAULT_MODEL, "messages": [{"role": "user", "content": args.prompt}]}
            response = fetch_chat(args.base, key, body)
            if args.spool:  # saved first: a failed post can be replayed, the usage is not lost
                with open(args.spool, "w") as f:
                    json.dump(response, f)
                print(f"saved {args.spool}", file=sys.stderr)
            _send(response, args.tenant, args.api)
    except Exception as err:
        print(f"error: {err}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
