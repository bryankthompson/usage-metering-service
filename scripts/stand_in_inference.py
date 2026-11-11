#!/usr/bin/env python3
"""A stand-in inference server for the demo scenario. It is NOT the real service and answers with made-up text.

Serves the two routes the adapter's docs describe, with the response shapes of tests/fixtures/seekr_chat.json and
seekr_embeddings.json: POST /v1/inference/chat/completions and POST /v1/inference/embeddings. Token counts vary per
request but are the same for the same --seed and the same order of requests. Response ids are unique within one server run.
Standard library only.

    python3 scripts/stand_in_inference.py [--port 8089] [--seed 1]
"""
import argparse
import json
import random
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

CHAT = "/v1/inference/chat/completions"
EMBEDDINGS = "/v1/inference/embeddings"


class Replies:
    """Builds the response bodies. One lock keeps the counter and the random numbers in request order."""

    def __init__(self, seed: int):
        self.rng = random.Random(seed)
        self.count = 0
        self.lock = threading.Lock()

    def chat(self, model: str) -> dict:
        with self.lock:
            self.count += 1
            tokens_in, tokens_out = self.rng.randint(20, 2000), self.rng.randint(1, 800)
            reply_id = f"chatcmpl-standin-{self.count:06d}"
        return {"id": reply_id, "object": "chat.completion", "created": int(time.time()), "model": model,
                "choices": [{"index": 0, "message": {"role": "assistant", "content": "Stand-in reply."},
                             "finish_reason": "stop"}],
                "usage": {"prompt_tokens": tokens_in, "completion_tokens": tokens_out,
                          "total_tokens": tokens_in + tokens_out}}

    def embeddings(self) -> dict:   # like the real shape, no `model` and no `choices`
        with self.lock:
            self.count += 1
            tokens_in = self.rng.randint(5, 1500)
            reply_id = f"emb-standin-{self.count:06d}"
        return {"id": reply_id, "object": "list", "created": int(time.time()),
                "data": [{"object": "embedding", "index": 0, "embedding": [0.1, 0.2]}],
                "usage": {"prompt_tokens": tokens_in, "completion_tokens": 0, "total_tokens": tokens_in}}


def make_server(port: int, seed: int) -> ThreadingHTTPServer:
    """A server on 127.0.0.1 (port 0 picks a free one). The caller runs serve_forever()."""
    replies = Replies(seed)

    class Handler(BaseHTTPRequestHandler):
        def do_POST(self):
            try:
                body = json.loads(self.rfile.read(int(self.headers.get("Content-Length", 0))))
            except ValueError:
                return self.send_error(400, "body is not JSON")
            if self.path == CHAT:
                payload = replies.chat(str(body.get("model", "stand-in-model")))
            elif self.path == EMBEDDINGS:
                payload = replies.embeddings()
            else:
                return self.send_error(404, "unknown route")
            data = json.dumps(payload).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def log_message(self, *args):
            pass

    return ThreadingHTTPServer(("127.0.0.1", port), Handler)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="stand-in inference server (not the real service)")
    parser.add_argument("--port", type=int, default=8089)
    parser.add_argument("--seed", type=int, default=1)
    args = parser.parse_args(argv)
    try:
        server = make_server(args.port, args.seed)
    except OSError as err:
        print(f"error: cannot listen on 127.0.0.1:{args.port}: {err}", file=sys.stderr)
        return 1
    print(f"STAND-IN SERVER: not the real service (127.0.0.1:{args.port}, seed {args.seed})", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    return 0


if __name__ == "__main__":
    sys.exit(main())
