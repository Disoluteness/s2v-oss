# Script2Video Studio — Python SDK

A dependency-free client for the platform's HTTP API (V7.1 contract, V7.2 SDK).
It speaks the same API the Web Studio uses, so an internal AI platform or Web
platform can drive script-to-video without touching provider keys.

- **Zero runtime dependencies** — the default transport is `urllib`.
- **One failure shape** — every error is an `ApiError` carrying the server's
  stable `code`, so a caller can branch on `api_error.code` instead of guessing
  from a status number.
- **Retry-safe by default** — mutating calls carry an `Idempotency-Key`, and a
  retry reuses it, so a timeout cannot create a second run.

The import name is `s2v`; the distribution is `s2v-client`.

## Install

```bash
pip install ./sdk/python          # from this repository
# or, once published:  pip install s2v-client
```

## Configure

```python
from s2v import Script2VideoClient

client = Script2VideoClient(
    base_url="https://studio.internal",   # or $SCRIPT2VIDEO_BASE_URL
    api_key="s2v_...",                    # or $SCRIPT2VIDEO_API_KEY
)
```

Two ways to identify the caller, and they coexist:

| Credential | Header | When to use |
|---|---|---|
| API key | `Authorization: Bearer s2v_...` | Anything outside the deployment's own trust boundary. The key inherits the owner's permissions and can be scoped narrower. |
| Trusted actor header | `X-Actor-Id` | Calls already behind the internal gateway / SSO proxy. Set with `actor_id="alice"`. |

A key always wins over a header. A request with neither falls back to the
deployment's default actor, which is what keeps existing unauthenticated callers
working.

## Issue a key

A key is issued by an owner or admin (`apikey:manage`) and belongs to whoever
asked for it. The plaintext appears once, in the creation response, and is never
recoverable — only a salted digest is stored.

```python
created = client.create_api_key("batch-pipeline", scopes=["run:read", "run:create"])
print(created.plaintext)          # s2v_... — store this now
print(created.key_prefix)         # s2v_1a2b3c4d5e6f — safe to log

client.revoke_api_key(created.api_key_id)
```

`scopes` may only narrow the owner's own permissions. Asking for more is refused
(`permission_denied`) rather than silently trimmed, because a credential that
behaves unlike the request that created it is worse than an error.

## The whole job in one call

```python
video = client.script_to_video(
    "开场用一个反常识的问题抓住注意力，然后给出一个可验证的数据，最后收尾。",
    project_title="新品预热",
    accept_paid_providers=True,
)
print(video.url)            # signed, time-limited download link
print(video.duration_ms)
```

`script_to_video` creates the project (if you did not pass `project_id`), creates
the run, starts it, waits, and returns the render.

## The individual steps

Same flow, one call at a time — the level to plug in at if you want your own
scheduling, progress reporting or UI.

```python
project = client.create_project("新品预热")

run = client.create_run(
    project.project_id,
    "开场三秒用一个反常识的问题……",
    pipeline_key="avatar_spokesperson",
    target_platform="douyin",
    target_duration_seconds=15,
    quality_mode="provider_avatar",     # declare intent; the plan flags any downgrade
    cost_budget=8.0,
)

# Supply the persona image the avatar stage composites onto.
client.upload_person_image(run.run_id, path="avatar.png")

client.start_run(run.run_id, accept_paid_providers=True)

finished = client.wait_run(
    run.run_id,
    timeout=900,
    on_progress=lambda r: print(r.status, r.current_stage_key),
)

if finished.status == "failed":
    for result in client.retry_failed_jobs(run.run_id):
        print("retried", result.get("job_id"))

video = client.get_final_video(run.run_id)
open("out.mp4", "wb").write(client.download_asset(video.asset_id))
```

### `wait_run` semantics

It stops — and returns — when the run reaches a state polling cannot change:

- `completed` / `failed` / `cancelled` — terminal
- `waiting_review` / `paused` — waiting for a **human**

A failed run is *returned*, not raised, so the caller decides whether that is
fatal. Pass `raise_on_timeout=True` to get `RunWaitTimeout` instead of the last
observed run when the deadline passes.

## Human review

```python
for request in client.request("GET", f"/api/runs/{run.run_id}/review-requests") or []:
    client.submit_review_decision(
        request["review_request_id"],
        "approve_with_changes",
        comment="把第三镜的节奏压紧一点",
    )
```

## Error handling

```python
from s2v import ApiError, TransportError

try:
    client.create_run(project_id, "...")
except ApiError as exc:
    print(exc.status, exc.code)       # 403 permission_denied
    print(exc.retryable)              # False — retrying changes nothing
    print(exc.detail)                 # the full server envelope
except TransportError as exc:
    print("never reached the server:", exc)
```

`TransportError` and `ApiError` are deliberately separate: "the server said no"
and "the server was never reached" need different handling.

The full catalog is discoverable at runtime rather than documented by hand:

```python
for entry in client.error_codes():
    print(entry["code"], entry["status_code"], entry["retryable"], entry["description"])
```

## Retries and idempotency

`max_retries` (default 2) applies only to a connection failure, a `429`, a `5xx`,
or a code the server marks retryable. Every retry reuses the same
`Idempotency-Key`, so a duplicate is impossible by construction.

```python
client = Script2VideoClient(base_url=..., api_key=..., max_retries=4, backoff_factor=1.0)

# Pin the key yourself when your own job queue already has one.
client.create_run(project_id, script, idempotency_key=f"job-{job_id}")

# Or turn it off for a call where a duplicate is harmless.
client.create_run(project_id, script, idempotency_key=False)
```

## Custom transport

Anything with a `send(method, url, headers, body, timeout) -> Response` works, so
you can reuse a connection pool or wrap calls in your own telemetry:

```python
from s2v import Script2VideoClient, Response


class MyTransport:
    def send(self, method, url, headers, body, timeout):
        ...  # return Response(status, lowercase_headers, body)
        return Response(200, {}, b"{}")


client = Script2VideoClient(base_url=..., transport=MyTransport())
```

## What is covered

| Area | Methods |
|---|---|
| Meta | `health`, `me`, `error_codes` |
| Projects | `create_project`, `get_project`, `list_projects` |
| Runs | `create_run`, `get_run`, `list_project_runs`, `get_run_board`, `list_run_artifacts`, `list_run_jobs`, `list_run_events` |
| Assets | `upload_asset`, `upload_person_image`, `list_run_assets`, `get_asset`, `download_asset` |
| Lifecycle | `confirm_paid_provider`, `start_run`, `wait_run`, `get_final_video` |
| Actions | `retry_action`, `retry_job`, `retry_failed_jobs`, `run_state_action` |
| Review | `submit_review_decision` |
| Credentials | `create_api_key`, `list_api_keys`, `revoke_api_key` |
| Convenience | `run_script`, `script_to_video` |

Any endpoint without a named method is still reachable:

```python
client.request("GET", "/api/render-profiles", params={"limit": 10})
```

## Webhooks

Register an endpoint, then verify what arrives:

```python
from s2v import Script2VideoClient, verify_webhook_signature, SIGNATURE_HEADER

client = Script2VideoClient(base_url="http://localhost:8080", api_key="s2v_...")

subscription = client.create_webhook_subscription(
    "ops-hook", "https://ops.internal/hooks/s2v", ["run.completed", "stage.waiting_review"],
)
print(subscription.secret)  # shown on every read; key for verification

# ... inside your HTTP handler, with the RAW request bytes:
if verify_webhook_signature(
    subscription.secret,
    header=request.headers.get(SIGNATURE_HEADER),
    body=raw_body,
):
    payload = json.loads(raw_body)          # dedupe on payload["delivery_id"] —
    ...                                     # delivery is at-least-once
```

Verification rejects tampered bodies and replayed deliveries with one check:
the signature's timestamp is inside the signed string, so anything older than
five minutes (the default tolerance) is stale. Operations:

- `list_webhook_events()` — the published vocabulary, no subscription needed.
- `create_webhook_subscription` / `list_webhook_subscriptions` /
  `get_webhook_subscription` / `update_webhook_subscription` /
  `delete_webhook_subscription`.
- `rotate_webhook_secret(id)` — new secret; accept both during the handover.
- `list_webhook_deliveries(id)` — every attempt, its response and its fate
  (`pending` / `delivering` / `succeeded` / `failed` / `abandoned`).
- `requeue_webhook_delivery(delivery_id)` — manual replay with a fresh attempt
  count; for an `abandoned` delivery this is the only way it ever arrives.

## Command line (`s2v`)

Installing the package also installs the `s2v` command (V7.5) — the SDK's shell
for local debugging and server ops. Same client underneath, so auth, retries,
idempotency and error decoding behave exactly as in the library:

```bash
pip install ./sdk/python                       # provides both `import s2v` and `s2v`

s2v config set base-url https://studio.internal
s2v config set api-key s2v_...                 # stored 0600 in ~/.config/s2v/config.json
s2v whoami                                     # sanity-check the credential

s2v quick "把这段脚本做成 15 秒口播" --download out.mp4
                                               # script in, video out: project + run +
                                               # start + wait + render + download

# or step by step:
s2v projects create "新品预热"
s2v runs create <project_id> "脚本内容" --budget 5 --platform douyin
s2v assets upload <run_id> persona.png --type person_image
s2v runs start <run_id> --accept-paid-providers
s2v runs wait <run_id>                         # stops at waiting_review; exit 0
s2v review submit <run_id> approve --note "OK"
s2v jobs list --run <run_id>
s2v jobs retry <job_id>
s2v video download <run_id> -o final.mp4
```

Configuration precedence: flags (`--base-url`, `--api-key`) beat the
`S2V_BASE_URL` / `S2V_API_KEY` environment variables, which beat the config
file. Every command takes `--json` (where marked) for scripts that parse
output, and the exit codes are part of the contract:

| Code | Meaning |
|---|---|
| 0 | success |
| 1 | failure (HTTP error, transport error, anything else) |
| 2 | usage error |
| 3 | `runs wait` / `quick` timed out |
| 4 | the run settled on `failed` |

`waiting_review` is a *stop*, not a failure — a person is the blocking
resource, so the process exits 0 and says so on stderr.

## Examples


- `examples/script_to_video.py` — the one-call path.
- `examples/upload_and_retry.py` — step-by-step, with a persona image and a
  failed-job retry.
