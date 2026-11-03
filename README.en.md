# Script2Video Studio

An AI script-to-video production platform, built as a service: HTTP API + background workers + a web workspace, designed to be called by other systems — internal AI platforms, external partners, automation pipelines.

- A complete Chinese-language avatar-spokesperson pipeline: script → production plan → spoken script → storyboard → media assets → edit plan → composed video
- Everything is an artifact, everything is audited: stage artifacts are versioned with lineage, key decisions land as events and decision logs
- Cost and risk up front: budget reservation, human confirmation before the first paid provider call, quality-intent downgrades must be acknowledged explicitly
- Human-in-the-loop: five review gates, and a finished video can still be rolled back to any stage for a redo

## Positioning and Clean-room Statement

This repository is built from scratch (clean-room). It is inspired by OpenMontage's pipeline philosophy — staged artifacts + human gates — but contains **none** of OpenMontage's source code, pipeline files, schemas, prompts, skills, UI code, or renderer components. The only thing the two projects share is the idea: break AI video production into reviewable stages instead of one black-box call.

Built for:

- Teams that want to run their own digital-human / brand short-video production service
- Platforms that want to offer video production as a capability through API / SDK / iframe embedding
- Internal production systems that need multi-tenancy, permissions, audit trails, quotas, and cost accounting

Not for: an out-of-the-box desktop video editor — this is a server-side platform. Media lives in object storage, rendering runs on FFmpeg workers.

## Core Features

**Pipelines and artifacts**
- 7 pipelines: the formal `avatar_spokesperson` (digital-human spokesperson: brand ads, knowledge narration, product introductions), the compatibility key `spokesperson_ad`, and 5 betas: `animated_explainer`, `source_clip_factory`, `screen_demo`, `hybrid_story`, `localization_dub`
- Pipeline DSL v1: the pipeline definition is snapshotted onto each run, so later file edits never change historical runs
- Artifact Schema Registry: `production_plan`, `spoken_script`, `shot_list`, `preflight_report`, `execution_plan`, `composition_graph` and friends are validated on write and carry `producer` / `lineage` / `derived_from` lineage
- Run state machine: `pause` / `resume` / `cancel` / `rerun_stage` / `rerun_artifact` / `skip_optional_stage`; reruns may be issued from `completed`, and downstream passed gates reopen with an incremented round

**Review and risk gates**
- Five human review gates (plan / script / storyboard / asset_review / final_review) with version-locked approval scopes, append-only decisions, and automatic invalidation on rerun
- Three start gates: quality-intent downgrades need acknowledgement (`intent_downgrade_not_acknowledged`), a plan exceeding `cost_budget` refuses to start (`cost_reservation_exceeds_budget`), and the first paid provider call needs human consent (`paid_provider_confirmation_required`)
- Review requests can mount evidence attachments (referenced, not copied), comments, and tracked action items

**Multi-tenancy and permissions**
- Team roles (owner/admin/member/viewer) are the baseline; project roles only widen, never revoke — the effective set is the union
- 23 permission keys, 7 of them tenant-level (team / API key / webhook / integration / quota / provider credentials / deployment management); an unseated identity gets 403 everywhere
- Every privileged change and every refusal is written to the audit log

**API contract**
- One failure envelope `{"detail": {"code", "message", ...}}`, stable branchable error codes, discoverable via `GET /api/error-codes`
- `Idempotency-Key` replay (same key with a different body is a 409), `Page` pagination envelope, OpenAPI document with 124 paths / 156 operations
- Version frozen at 1.1.0; the compatibility policy separates stable / experimental / not-promised surfaces (`docs/API_STABILITY.md`)

**Integration surfaces**
- REST API + a zero-dependency Python SDK + a TypeScript SDK — both attach idempotency keys automatically, retry only what is retryable, and `waitRun` stops at a review gate instead of spinning
- `s2v` CLI (ships with the Python SDK): `s2v quick "script"` goes from script to downloaded video in one command
- Outbound webhooks: a published event catalog, a transactional outbox, signed at-least-once delivery with exponential backoff, auto-pause and manual replay
- Embeddable workspace: stateless signed tokens (the token lives in the URL fragment and never reaches server logs), per-integration `frame-ancestors` allowlists, and at most the `run:review` write scope
- **Let AI drive video production**: the repo ships `skills/s2v-pipeline/` — a skill for AI tools like Claude Code / Codex (a stdlib-only CLI script plus per-stage reference docs). Install it and say "turn this script into a 15-second spokesperson video", and the AI drives the platform stage by stage, pausing for human confirmation at every step, with rollback to any stage when something looks wrong. See `skills/s2v-pipeline/SKILL.md`

**Assets and media**
- Object storage abstraction (local / MinIO); a project asset library with provenance and license terms, where reuse means "copy + record the source"
- DashScope adapters: TTS (qwen3-tts-flash), image (qwen-image-2.0-pro), digital human (Wanxiang `wan2.2-s2v`, async submit / poll / archive); EMO (`emo-v1`) is retired, and the adapter picks the protocol dialect by model-name prefix
- FFmpeg composition: scale/pad to canvas, SRT subtitle burn-in, narration and BGM mixing; built-in render profiles for Douyin, Xiaohongshu, Bilibili, WeChat Channels, and YouTube Shorts
- Quality gates: ffprobe readability, resolution, FPS, audio loudness, black/frozen-frame risk, subtitle overflow, CJK font readiness, scored 0-100
- Edit tools: `trim` / `concat` / `subtitle_burn_in` / `auto_reframe` / `watermark` / `cover_generation` / `export_versions`

**Workers and operations**
- A DB-lease job queue so multiple workers never consume the same job; role-affinity lanes (`provider` / `analysis` / `media` / `render`); dead-letter states replayable via API; exponential-backoff auto-retries for local tool steps (never for paid provider submissions)
- Prometheus `/metrics` (credential-free scraping, absent from the OpenAPI document), structured JSON logging, `X-Request-Id` on every response
- Team quotas: cost / run-count / token ceilings over `calendar_month` / `rolling_30d` / `lifetime` windows, with hard gates, soft warnings, and per-run overrides
- Optional provider-secret encryption at rest (AES-256-GCM, AAD-bound to tenant and key name) with one-command rotation
- Backup / restore scripts (restore interlocked with maintenance mode), maintenance mode (writes suspended, reads keep answering), and a self-reporting deployment posture (version / migration head / capability switches / table count)
- A production compose file (migrations as a one-shot service, four role-scoped workers) and Kubernetes manifests

## Architecture at a Glance

```text
                ┌────────────────────────────────────────────┐
                │           FastAPI (apps/api)               │
   Web ────────▶  124 routes · permission middleware ·       │◀── SDK / CLI / services
   workspace     idempotency · event log                    │
                └───────┬────────────────────┬───────────────┘
                        │                    │
              ┌─────────▼────────┐  ┌────────▼─────────┐
              │  MySQL 8 (36 t)  │  │  Redis / MinIO   │
              │  runs/artifacts/ │  │  queues / media  │
              │  leases          │  │  objects         │
              └─────────┬────────┘  └────────▲─────────┘
                        │                    │
                ┌───────▼────────────────────┴───────┐
                │        Worker (apps/worker)         │
                │  manifest-step dispatch · provider  │
                │  submit/poll · FFmpeg compose ·     │────▶ DashScope / ElevenLabs / Kling
                │  webhook delivery                   │
                └────────────────────────────────────┘
```

Core concepts:

| Concept | Meaning |
| --- | --- |
| Pipeline | The pipeline definition (`pipelines/*.json`), version-snapshotted onto each run |
| Run | One production pass, advancing stage by stage under the state machine |
| Stage | A stage; stages with `gate: true` park in `waiting_review` for a human |
| Gate | A human review gate: snapshot, version-locked approval scope, append-only decisions |
| Artifact | A stage output (plan / spoken script / storyboard / execution plan / render summary…), versioned with lineage |
| Asset | Media-file metadata; bytes live in object storage, access goes through signed links |
| Job | A background job (digital-human submit/poll, compose, export), consumed off the lease queue |

## Quick Start

Local development (Python ≥ 3.11):

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -e ".[dev]"
cp .env.example .env
make api
```

Open:

```text
http://localhost:8080/api/health
http://localhost:8080/docs
http://localhost:8080/workspace
```

Docker Compose (MySQL 8 + Redis + MinIO + API + Worker):

```bash
cp .env.example .env
docker compose up --build
docker compose exec api alembic upgrade head
```

Inside the compose network the API connects to `mysql:3306`; the host gets MySQL on `3307` and the API on `18080` (avoiding common port conflicts), while Redis and MinIO stay internal. The worker processes queued jobs automatically; for one-off debugging:

```bash
docker compose exec worker python -m apps.worker.background_jobs --once
```

## First Run (Shortest Calling Chain)

> Integrating from another service? Read `docs/API_INTEGRATION.md` first: it gives the calling order (which endpoint before which, and why), how to get an API key, the review-gate loop every run passes through, and the webhook alternative to polling. This section is the shortest path to *something*; that document is the path to *production*.

```bash
### 1. Create a project
curl -X POST http://localhost:8080/api/projects \
  -H "Content-Type: application/json" \
  -d '{"title":"Autumn milk tea ad"}'

### 2. Create a run under the project
curl -X POST http://localhost:8080/api/projects/<project_id>/runs \
  -H "Content-Type: application/json" \
  -d '{
    "pipeline_key":"avatar_spokesperson",
    "raw_script":"秋天第一杯奶茶，还没人给你点？",
    "brand_name":"Example Brand",
    "product_name":"Instant milk tea",
    "target_platform":"douyin",
    "target_duration_seconds":15
  }'

### 3. Preflight → auditable execution plan → start
curl -X POST http://localhost:8080/api/runs/<run_id>/preflight
curl -X POST http://localhost:8080/api/runs/<run_id>/execution-plan
curl -X POST http://localhost:8080/api/runs/<run_id>/start

### 4. Review loop: when the run parks in waiting_review, find the open gate and approve it
curl "http://localhost:8080/api/runs/<run_id>/review-requests?status=open"

curl -X POST http://localhost:8080/api/review-requests/<request_id>/decisions \
  -H "Content-Type: application/json" \
  -d '{"decision":"approve","comment":"OK"}'

### Or one-click stage review (equivalent, backed by the same review kernel)
curl -X POST http://localhost:8080/api/runs/<run_id>/stages/<stage_key>/review \
  -H "Content-Type: application/json" \
  -d '{"decision":"approve","comment":"OK"}'
```

Two things worth knowing immediately:

- **With no provider key configured, a run parking in `waiting_review` is normal** — not a failure. The human is part of the pipeline. The run advances only after a decision is submitted for each open gate.
- `start` has three gates: acknowledge a quality-intent downgrade with `{"accept_intent_downgrade": true}`; a plan whose reserved cost exceeds `cost_budget` refuses to start; the first paid provider call needs `POST /api/runs/<run_id>/provider-consents`.

Not ready to wire real providers? The MVP sample endpoint walks the full script-to-MP4 chain with deterministic local FFmpeg media (the default `local_preview` mode):

```bash
curl -X POST http://localhost:8080/api/mvp/sample-runs \
  -H "Content-Type: application/json" \
  -d '{
    "title":"Autumn milk tea sample",
    "raw_script":"秋天第一杯奶茶，还没人给你点？别等啦，自己冲就好！",
    "brand_name":"Example Brand",
    "product_name":"Instant milk tea",
    "target_duration_seconds":15
  }'
```

A runnable end-to-end example (curl + python3 only) is `examples/curl/run_pipeline.sh`; SDK versions live in `sdk/python/examples/` and `sdk/typescript/examples/`; `examples/README.md` is the map of all three.

## SDK and CLI

```bash
pip install ./sdk/python   # provides both `import s2v` and the `s2v` command
```

```python
from s2v import Script2VideoClient

client = Script2VideoClient(base_url="http://localhost:8080", api_key="s2v_...")
video = client.script_to_video("秋天第一杯奶茶，还没人给你点？", project_title="Launch")
print(video.download_url)
```

```bash
s2v quick "把这段脚本做成 15 秒口播" --download out.mp4
s2v runs create <project_id> "脚本内容" --budget 5 --platform douyin
s2v runs start <run_id> --accept-paid-providers
s2v runs wait <run_id>      # parks at waiting_review, exit 0 — a stop, not a failure
s2v review submit <run_id> approve --note "OK"
s2v video download <run_id> -o final.mp4
```

Exit codes are part of the contract: `0` success, `1` failure, `2` usage, `3` wait timed out, `4` run settled on `failed`.

## Digital-Human Pipeline Notes

The adapter is `dashscope_avatar_video`, with an async lifecycle: submit → poll → archive as an asset → FFmpeg compose.

- Default model `wan2.2-s2v` (Wanxiang digital human), which locates the speaker itself — no face bounding boxes; `provider_options.resolution` selects 480P / 720P
- **Hard constraint: audio must be under 20 seconds and 15 MB** — split longer narration before submitting
- The retired `emo-v1` model uses the legacy protocol (with a face-detection hop); the adapter routes by the `emo*` model-name prefix, so setting `DASHSCOPE_AVATAR_MODEL` back to an emo name restores it
- When locally stored person images / audio must be fetched by the provider, `PUBLIC_BASE_URL` is required — the platform mints temporary signed asset links and hands absolute URLs to the provider

Submit from existing assets (the platform signs the temporary links):

```bash
curl -X POST http://localhost:8080/api/runs/<run_id>/avatar-tasks/from-assets \
  -H "Content-Type: application/json" \
  -d '{"person_image_asset_id":"<asset_id>","audio_asset_id":"<asset_id>"}'
```

For non-blocking execution, enqueue the same work as a background job (the worker submits, polls, archives, and composes automatically):

```bash
curl -X POST http://localhost:8080/api/runs/<run_id>/jobs/avatar-from-assets \
  -H "Content-Type: application/json" \
  -d '{"person_image_asset_id":"<asset_id>","audio_asset_id":"<asset_id>"}'
```

## Configuration Cheat Sheet

The full annotated list is `.env.example`; the ones you will touch first:

| Variable | Purpose |
| --- | --- |
| `DATABASE_URL` / `REDIS_URL` | MySQL and Redis connections (compose defaults point at the bundled services) |
| `OBJECT_STORAGE_BACKEND` | `local` (default) or MinIO/S3 |
| `PUBLIC_BASE_URL` | The externally reachable address providers use to fetch local assets; embed links build on it too |
| `DASHSCOPE_API_KEY` | TTS / image / digital-human provider key (when absent the related tools simply show as unavailable, no errors) |
| `DASHSCOPE_AVATAR_MODEL` / `DASHSCOPE_AVATAR_RESOLUTION` | Digital-human model (default `wan2.2-s2v`) and resolution tier (default 480P) |
| `SCRIPT2VIDEO_PLANNER_PROVIDER` | `rule_based` (default, deterministic and free) or `dashscope` (qwen-plus) |
| `ACTOR_TRUST_MODE` | `open` (default, trusts the gateway-injected identity header) or `secret` (switch before exposing the API) |
| `API_KEY_PEPPER` | API-key hash salt — **changing it invalidates every issued key**; set once, keep stable |
| `SECRET_ENCRYPTION_KEY` | Optional: provider-secret encryption at rest (AES-256-GCM) |
| `EMBED_TOKEN_SECRET` | Optional: embed-token signing key; must be set before onboarding external platforms |

Note on compose: inside `environment:` interpolation, **shell environment variables take precedence over the `.env` file** — if a `.env` change does not reach the container, `unset` the leftover variable and `docker compose up -d --force-recreate`. See `docs/DEPLOYMENT.md` §2.

## Planner and Providers

The planner defaults to deterministic local JSON generation (free and reproducible). To switch to DashScope:

```bash
SCRIPT2VIDEO_PLANNER_PROVIDER=dashscope
DASHSCOPE_API_KEY=your-key
DASHSCOPE_LLM_MODEL=qwen-plus
```

Provider keys come from three sources: `.env` variables, database-managed provider credentials (responses carry masked previews only; requires `provider:manage`), and provider callbacks (`POST /api/provider-callbacks/<provider>`, optional HMAC signature). The preflight report declares each stage `ready` / `degraded` / `blocked` against tool dependencies, naming the missing environment variables, binaries, and fallbacks.

## Deployment and Operations

```bash
### Production stack: infrastructure ports unpublished, migrations as a one-shot
# migrate service, four role-scoped workers
docker compose -f docker-compose.prod.yml up -d --build

### Pre-upgrade preflight (reads the database and configuration itself; exit 0/1/2, --json available)
python scripts/deployment_preflight.py

### Post-deploy smoke: pure-HTTP walk of the review kernel and every versioned
# capability surface (390 checks by default)
docker compose exec -T api python scripts/verify_review_loop.py

### Deployment posture self-report: version, migration head, capability switches,
# table count, maintenance switch
curl http://localhost:18080/api/deployment

### Maintenance mode: writes suspended, reads keep answering
curl -X POST localhost:18080/api/deployment/maintenance \
  -H 'Content-Type: application/json' -d '{"enabled": true}'
```

Backups are `scripts/backup.sh` (dump + manifest + sha256; `.env` is deliberately **not** included; retention only reports unless you pass `--prune`); restores are `scripts/restore.sh`, which refuses to run unless maintenance mode is on. The full upgrade order, rollback paths, and secret-rotation blast radius are in `docs/DEPLOYMENT.md` (a 12-section runbook, including what it deliberately does not cover: no HA database, no cross-region DR, no KMS).

Tests:

```bash
pytest                 # 811 tests
make lint              # ruff
```

## Repository Layout

```text
apps/api/         FastAPI application (routes, middleware, domain logic)
apps/worker/      Background-job worker (queue consumption, provider polling, FFmpeg)
apps/web/         Built-in web workspace (FastAPI pages, no frontend build step)
packages/         Domain packages: provider adapters, permissions, tool contracts
pipelines/        Pipeline DSL definitions (7)
tool_catalog/     Runtime tool catalog (26 tools)
sdk/python/       Python SDK (zero-dependency) + the s2v CLI
sdk/typescript/   TypeScript SDK
examples/         End-to-end examples for curl / Python / TypeScript
skills/           s2v-pipeline: a pipeline skill for AI tools like Claude Code / Codex
scripts/          backup.sh / restore.sh / deployment_preflight.py / verify_review_loop.py
infra/            Dockerfiles, production compose, K8s manifests
docs/             Architecture decisions, API stability contract, deployment runbook, version plans
tests/            811 pytest tests
```

## Documentation Index

| Document | Contents |
| --- | --- |
| `docs/API_INTEGRATION.md` | Integration guide for external services: calling order, API keys, the review loop, the webhook alternative |
| `docs/API_STABILITY.md` | Version compatibility policy: stable / experimental / not-promised surfaces |
| `docs/DEPLOYMENT.md` | 12-section deployment runbook: upgrades, rollback, backup/restore, key rotation |
| `docs/DECISIONS.md` | Architecture decision records (ADRs) |
| `CHANGELOG.md` | Version history |
| `docs/FULL_VERSION_ROADMAP.md` | Per-version acceptance notes |
| `README.md` | Chinese README |

## Known Limitations

- With no provider key configured, digital-human / TTS / image tools show `unavailable` in preflight and runs park at review gates — by design, not a fault
- Digital-human cost estimation currently returns 0: the `cost_budget` gate gives no protection for digital-human calls (quota dimensions are unaffected)
- `wan2.2-s2v` requires audio under 20 seconds and 15 MB; the platform does not auto-split yet — callers must pre-split long scripts
- `/api/assets/{id}/download` may return 500 for wav files (use the `/api/assets/{id}/content` signed link instead)
- Provider 4xx errors are currently classified as `input.invalid_or_missing`; the taxonomy needs finer granularity
- The HTML/Canvas preview is a preview renderer; final MP4 encoding always goes through FFmpeg

## Version and License

Current version **1.1.0** (the frozen-version promise is in force, see `docs/API_STABILITY.md`). The database has 36 tables at migration head `20261012_0022`.

Released under the [Apache License 2.0](LICENSE).

Clean-room acknowledgement: thanks to the OpenMontage project for sharing its pipeline design ideas publicly — staged artifacts, human gates, auditable execution plans. This project's implementation was built from scratch and copies none of its code or assets.
