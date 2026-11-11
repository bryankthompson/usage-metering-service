# Platform adapter (`app/seekrflow.py`)

Turns the `usage` object of an inference response into one usage event for `POST /v1/usage`. The response `id` is the
idempotency key, so a resend is a 200 `duplicate`. Standard library only. **No call to the real service has been made**
(there is no API key); everything is built from the public documentation and tested against hand-written fixtures.

```bash
python3 -m app.seekrflow replay tests/fixtures/seekr_chat.json --tenant tenant-1 [--api http://127.0.0.1:8000]
SEEKR_API_KEY=... python3 -m app.seekrflow live --tenant tenant-1 --prompt "hello"   # calls the service
```

`replay` takes one saved response or a JSON list and prints `id in=N out=N status accepted|duplicate` per event; it exits
non-zero on any error. `live` needs `SEEKR_API_KEY` in the environment, fails without it and never prints it; it has never
been run. `fetch_chat` is tested only against a stand-in `http.server` on 127.0.0.1.
`live --spool FILE` saves the response to FILE before posting, so a refused post can be replayed with `replay FILE`; without it
nothing is written, and a failed post loses that response's usage (the command still exits 1). A rerun with the same FILE
overwrites it. `replay --model M` names the model for a response that has none (embeddings). A redirect from the service is
refused, so the key is never sent to another origin (tested).

Mapping: `tokens_in` = `usage.prompt_tokens`; `tokens_out` = `usage.completion_tokens` (0 for embeddings, which have `data`
and no `choices`); `model` = the response `model` (the caller's argument for embeddings); `occurred_at` = `created` as an
ISO 8601 UTC time. A final streaming chunk maps the same way. A missing or non-integer or negative `usage` count raises
`MissingUsage`; it is never counted as 0.

## Verified from the public docs
1. Base URL `https://flow.seekr.com/v1`, chat at `/inference/chat/completions`, embeddings at `/inference/embeddings`; the
   auth header is `Authorization: YOUR_API_KEY` (no "Bearer"), key in `SEEKR_API_KEY`:
   https://docs.seekr.com/flow/reference/getting-started-with-your-api.md
2. Chat response requires `id`, `created`, `model`, `choices`, `usage`; `created` is an integer, "Unix timestamp, in seconds"
   (confirmed on the page); usage has `prompt_tokens`, `completion_tokens`, `total_tokens`, `prompt_tokens_details`
   (`cached_tokens`). The docs make every usage field optional with default 0; the adapter still refuses a missing one:
   https://docs.seekr.com/flow/reference/route_chat_completion_v1_inference_chat_completions_post.md
3. Embeddings response requires `id`, `created`, `data`, `usage`; `model` is optional:
   https://docs.seekr.com/flow/reference/route_embeddings_v1_inference_embeddings_post.md
4. Model ids (chat `meta-llama/Llama-3.1-8B-Instruct`, `meta-llama/Llama-3.3-70B-Instruct`; embeddings
   `BAAI/bge-large-en-v1.5`) and the final streaming chunk carrying `usage` with `stream_options.include_usage`:
   https://docs.seekr.com/flow/components/models/generative-models.md

## Not verified
- Any live call: none was made, so real response bodies, errors and limits are unseen.
- The attribute path to usage on the `seekrai` SDK objects (the adapter does not use the SDK).
- Any usage or observability API to reconcile against.
- Per-model prices: the pricing page lists size tiers, not models. Prices in the tests are illustrative and not theirs.

## Things to know
- The response does not say which customer made the call, so the caller supplies the tenant.
- Cached prompt tokens are counted inside `prompt_tokens` and billed at the full input price; this service has no
  cached-token price.
- `live` calls the service before it posts usage, and nothing checks entitlement before that call. The edge controls recording
  usage and reading charges, not serving inference: an unpaid or canceled tenant still gets the completion, its usage gets
  402, `live` exits 1, and the usage is lost unless `--spool` was used. A real gate needs an entitlement-read endpoint, which does not exist.
- The fixtures in `tests/fixtures/seekr_*.json` are hand-written from the documented schema, not recorded.
