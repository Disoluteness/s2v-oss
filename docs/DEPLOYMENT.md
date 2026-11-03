# Deployment runbook

How to install, upgrade, back up, restore and verify a Script2Video Studio
deployment. Pair this with the manifests in `infra/deploy/` (see that directory's
README for the file map) and `docs/CURRENT_CONTEXT.md` §12 for the environment
variables each version added.

This document is written for the person who gets paged. Where a step is a
judgement call rather than a command, it says so and gives the reasoning, because
the failure mode of a runbook that only lists commands is that the operator
follows it exactly and still cannot tell whether it worked.

---

## 1. Topology

```
                    ┌──────────────┐
   callers ────────►│  ingress /   │
   (SDK, browser,   │  reverse     │
    host platform)  │  proxy       │
                    └──────┬───────┘
                           │  /api, /embed, /metrics(内部)
                    ┌──────▼───────┐        ┌──────────────┐
                    │  api × 1..n  │◄──────►│    MySQL     │
                    └──────┬───────┘        └──────▲───────┘
                           │                        │
                    ┌──────▼───────┐                │
                    │  Redis       │         ┌──────┴───────┐
                    └──────────────┘         │  worker × n  │
                                             │  provider /  │
                    ┌──────────────┐         │  analysis /  │
                    │  object      │◄────────┤  media /     │
                    │  storage     │         │  render      │
                    └──────────────┘         └──────────────┘
```

Three properties of this shape drive everything below.

**The API is stateless; the workers are not interchangeable.** Any API replica can
serve any request, which is what makes a rolling upgrade of the API a non-event.
Workers claim work from a queue that lives in `background_jobs` as a *column*
(V8.1), not a separate service, so there is no broker to operate — but each worker
is bound to one queue and therefore to one kind of work.

**The database is the only source of truth that matters.** Assets live in object
storage, but the rows that describe them (and every run, event, audit entry and
spend ledger line) live in MySQL. A restore that gets the database right and the
storage wrong is a degraded deployment; the reverse is an empty one.

**Two things are held outside the database and both are load-bearing.** The
`SECRET_ENCRYPTION_KEY` decrypts provider credentials (V8.4); losing it while
keeping `encrypted_secrets` makes those credentials unrecoverable. `API_KEY_PEPPER`
salts every issued API key; changing it invalidates all of them. Neither is in a
backup archive on purpose — see §6 — and both must be stored somewhere that is not
the deployment host.

---

## 2. Before you deploy

Run the pre-flight against the target database. It answers the questions that
decide whether the deployment can serve at all, and it does so before anything is
opened to traffic:

```bash
# from the host
python scripts/deployment_preflight.py --database-url "$DATABASE_URL"

# or inside the image, against the compose network
docker compose -f docker-compose.prod.yml run --rm \
  --entrypoint python migrate scripts/deployment_preflight.py
```

Exit code 0 means nothing failed. Warnings are expected on a fresh install (no
backup recorded yet, `actor_trust_mode=open` on a trusted network) and are not
failures. The two failures actually worth stopping for:

- **`asset_link_secret` FAIL.** `APP_ENV` says this is not a development
  environment, and `ASSET_LINK_SECRET` is still `dev-only-change-me`. Every mint
  of `/api/assets/{id}/access-link` will return 503 until it is set — by design
  (V8.4): a public secret signs links anyone can forge, and a hard refusal is the
  only version of that warning anyone reads.
- **`schema_revision` FAIL.** The database is not on the revision the shipped code
  expects. Apply §4 before starting the API.

Everything configurable, and what each variable does, is in `.env.example` — which
is the canonical list. The ones that have no safe default and therefore must be
set by hand on a real host:

| Variable | Why it has no default |
| --- | --- |
| `DATABASE_URL` | It points at your database. |
| `ASSET_LINK_SECRET` | The default is public; a link signed with it is forgeable. |
| `PUBLIC_BASE_URL` | Links the platform mints must carry the address callers use, not the internal hop. |
| `API_KEY_PEPPER` | Optional, but the salt is baked into every key digest at issue time (V7.2). |
| `EMBED_TOKEN_SECRET` | Optional, but the default signs embed links anyone can forge (V7.4). |

### Editing `.env` after the first install

`.env` reaches the containers two ways, and they do not have equal weight:

- `env_file: .env` copies the file's values in;
- the `environment:` block in the compose file *interpolates* `${VAR:-default}`
  — and Compose resolves that against the **shell environment first**, the
  `.env` file second. `environment:` in turn outranks `env_file:`.

So a variable exported in your shell silently outranks the file. A variable
exported once — for example while probing a provider with
`set -a; source .env; set +a` — stays exported for the rest of that session and
pins the value for every later `docker compose` call, while `$EDITOR .env`
appears to do nothing. It does not even produce a visible symptom: if the
resolved configuration is identical to what the running containers already
have, `up -d` does not recreate them, so nothing you observe changes. That is
why an edit to `.env` alone cannot be confirmed by watching compose's output.

```bash
# what the shell is injecting (highest precedence — the usual culprit)
env | grep '^DASHSCOPE'

# what Compose resolves after interpolation (the truth)
docker compose config | grep DASHSCOPE

# what the process actually reads
docker compose exec -T api printenv DASHSCOPE_AVATAR_MODEL
```

`docker compose config` echoing the *old* value is the signature of shell
shadowing: `unset VAR`, then re-run. To force a config-only change through
regardless of how the config hash compares:

```bash
docker compose up -d --force-recreate --no-deps api worker
```

**Do not** shortcut this by prefixing the variable onto the command
(`VAR=value docker compose up -d`). That fixes the one invocation; the next
`up` re-interpolates the shell's stale value and silently reverts you.

Verify config-only changes from inside the container, not from compose's
output. Neither §2's pre-flight nor §9's gate can catch this class of mistake:
they read state and behaviour, and a wrong provider model or endpoint name is
neither.

---

## 3. First install (single host, compose)

```bash
git clone <repo> && cd <repo>
cp .env.example .env
$EDITOR .env                     # at minimum: DATABASE_URL, ASSET_LINK_SECRET,
                                 # MYSQL_PASSWORD, MYSQL_ROOT_PASSWORD,
                                 # MINIO_ROOT_USER, MINIO_ROOT_PASSWORD

docker compose -f docker-compose.prod.yml up -d --build
```

What the compose file does for you that the development one does not:

- Runs `alembic upgrade head` as a one-shot `migrate` service and gates `api` and
  every worker on `service_completed_successfully`. Two replicas racing a schema
  change is a real way to corrupt one; a dependency edge makes it impossible.
- Publishes no infrastructure ports. MySQL, Redis and the MinIO console are
  reachable only on the compose network.
- Splits the worker into four role-bound services (`provider`, `analysis`,
  `media`, `render`).
- Caps log files and container resources.

Then verify (§9) and open the doors.

### Kubernetes

Apply `infra/deploy/k8s/` in filename order, after creating the Secret out of band
and editing the `CHANGE ME` values. Two steps are the operator's, not the YAML's:

1. **Create the real Secret before applying `10-config.yaml`.** The template in
   that file has placeholder values, and a Deployment that starts against
   `CHANGE_ME` connection strings fails in a way that looks like a database outage.
2. **Run the migration Job before rolling the images.** Kubernetes has no
   "Deployment depends on a Job" primitive; see the note in `40-migrate-job.yaml`.

---

## 4. Migration runbook

Every release that changes the schema says so in `docs/TASK_BOARD.md`, and the
migration files are the record. The procedure:

```
1. backup          scripts/backup.sh --record https://studio.example.com \
                                    --token "$S2V_TOKEN" --dir /srv/backups
2. freeze writes   POST /api/deployment/maintenance {"enabled": true}
3. apply schema    compose: docker compose -f docker-compose.prod.yml run --rm migrate
                   k8s:     kubectl -n s2v-studio delete job s2v-migrate
                            kubectl -n s2v-studio apply -f infra/deploy/k8s/40-migrate-job.yaml
                            kubectl -n s2v-studio logs -f job/s2v-migrate
4. roll the code   compose: docker compose -f docker-compose.prod.yml up -d --build api worker-*
                   k8s:     kubectl -n s2v-studio set image deploy/s2v-api api=<image>
                            (and every worker Deployment)
5. verify          GET /api/deployment  →  migration.current == migration.head,
                                           pending == [], up_to_date == true
6. resume writes   POST /api/deployment/maintenance {"enabled": false}
```

**Step 3 before step 4, always.** New code against an old schema fails; old code
against a new schema usually does not. Alembic migrations in this project are
additive for exactly that reason — a rollback of the *code* is a supported
operation, which is what §7 relies on.

**Why step 2 exists.** It is tempting to skip on a "small" migration. The reason
not to is that a migration which takes longer than expected is indistinguishable,
from the caller's side, from an outage — except that in the first case the
deployment can still tell you what is going on. Writes are refused with 503
`maintenance_mode`; reads, the deployment endpoints, and provider callbacks keep
working (§8 explains why callbacks are deliberately exempt).

**If step 3 fails**, the schema is at whatever revision the failing migration left
it. Read the Job/container logs for the revision id, then:

```bash
# inspect what the code expects vs what exists
python scripts/deployment_preflight.py --database-url "$DATABASE_URL"
# roll back one revision, fix, retry
docker compose -f docker-compose.prod.yml run --rm migrate alembic downgrade -1
```

Each migration in this repository is round-trip verified (`upgrade → downgrade →
upgrade`) when it lands, so a `downgrade -1` is a supported operation rather than a
hope.

---

## 5. Backup

```bash
scripts/backup.sh --record https://studio.example.com \
  --token "$S2V_TOKEN" --dir /srv/backups --keep 14
```

`--record` takes the deployment **base URL**, not a flag — the script appends
`/api/deployment/backups` to it.

It dumps MySQL (`--single-transaction`, so no writer lock), archives the storage
directory, writes a `MANIFEST` and a `.sha256`, and — with `--record` — tells the
deployment a backup happened, so `GET /api/deployment` can report how old the last
one is.

Two things it deliberately does not do:

- **It does not back up `.env`.** That file holds `SECRET_ENCRYPTION_KEY` and
  `API_KEY_PEPPER`. Storing it inside the archive it protects converts a lost key
  into a lost key *and* a copy of it sitting on the backup share. Back it up
  separately, somewhere with different access control — and treat losing it as the
  same severity as losing the database.
- **It does not delete anything unless you pass `--prune`.** Without it, archives
  beyond `--keep` are listed but left alone. A retention typo should not be able to
  remove data, and the cost of the conservative default is disk space.

Schedule it from cron or a systemd timer. A missed backup is survivable right up
until the first bad migration, and the pre-flight warns when the last recorded one
is older than the retention window.

---

## 6. Restore

```bash
# 1. freeze writes first — restore.sh checks this and refuses without it
curl -fsS -X POST https://studio.example.com/api/deployment/maintenance \
  -H 'Content-Type: application/json' \
  -H "Authorization: Bearer $S2V_TOKEN" -d '{"enabled": true}'

# 2. restore
scripts/restore.sh --archive /srv/backups/s2v-backup-20261012T031500Z.tar.gz \
  --yes --base-url https://studio.example.com --token "$S2V_TOKEN"
```

The maintenance check is a real interlock, not a formality: restoring while the
application is writing is a race, and the switch that stops those writes is exactly
what V8.5 added so that "I am about to do something dangerous to this database" is
a question the deployment can answer instead of a thing an operator is trusted to
remember. `--skip-maintenance-check` exists for restoring into a stopped or scratch
deployment and should not be used against a live one.

**The database side is destructive; the storage side is additive.** The dump drops
and recreates the tables it contains. The storage archive is extracted over the
existing directory, so files that arrived after the backup stay — a clean-slate
storage restore means restoring into a freshly created directory.

After the restore, the schema is only as new as the archive. Finish the procedure:

```bash
docker compose -f docker-compose.prod.yml run --rm migrate   # apply anything newer
python scripts/deployment_preflight.py --database-url "$DATABASE_URL"
# spot-check a run board, then:
curl -fsS -X POST .../api/deployment/maintenance -d '{"enabled": false}'
```

**Do this before you need it.** A restore path that has never been exercised is a
plan, not a capability. The cheapest rehearsal is a scratch deployment restored
from the newest archive, once a quarter.

---

## 7. Rollback

Two different things get called "rollback", and they have different costs.

**Rolling back the code** (the common case) does not touch the schema:

```bash
docker compose -f docker-compose.prod.yml up -d --build api worker-*   # with the previous tag
# or: kubectl -n s2v-studio set image deploy/s2v-api api=<previous-image>
```

This is safe because migrations here are additive: an older code version reading a
newer schema ignores the columns it does not know about. It stops being safe the
day a migration drops or renames something, which is why those are called out
loudly in the release notes.

**Rolling back the schema** is a real migration and needs the same freeze as §4:

```
1. freeze writes             POST /api/deployment/maintenance {"enabled": true}
2. roll back the code        (as above) — the old code must expect the old schema
3. alembic downgrade <rev>   via the migrate service/Job
4. verify                    GET /api/deployment → current == head
5. resume writes             POST /api/deployment/maintenance {"enabled": false}
```

If a release added data a downgrade would discard, the honest choice is a restore
from the pre-upgrade backup (§6) rather than a downgrade. Decide which before you
start, not after.

---

## 8. Maintenance mode

```bash
# on
curl -fsS -X POST https://studio.example.com/api/deployment/maintenance \
  -H 'Content-Type: application/json' -H "Authorization: Bearer $S2V_TOKEN" \
  -d '{"enabled": true, "message": "database migration in progress"}'

# off
curl -fsS -X POST .../api/deployment/maintenance -d '{"enabled": false}'
```

What it does: refuses every mutating request with 503 `maintenance_mode` (marked
retryable in the error catalog — a window ends, so a client that gave up
permanently would have to be restarted by hand). What it does not do: take the
deployment dark. Reads keep working, so the UI renders and the boards load; the
probe answers; and `/api/deployment` answers, which matters because it is where you
find out you are in maintenance mode.

Three writes stay open, and each is deliberate:

- **`/api/deployment`** — the endpoint that turns maintenance *off* is a write to a
  deployment path. Refusing it would make the switch a one-way door.
- **`/api/provider-callbacks`** — a callback carries the result of a *paid* provider
  job. Refusing it does not postpone work, it discards work that has already been
  billed and that the platform cannot regenerate. The endpoint is authenticated by
  HMAC signature (V8.4) either way, so exempting it opens no door that was closed.
- **`/api/auth`** (V1.1) — maintenance suspends *tenant writes*, not the ability to
  prove who you are. A repair window is exactly when a human should sign in as
  themselves instead of riding the default actor, and refusing logout would make
  ending a session depend on the deployment being healthy. The prefix match is on
  path segments, so lookalike paths inherit nothing.

The switch lives in the database, not in `.env`. That is the whole point: the
moment you need it is the moment you cannot restart the deployment to pick up a
config change. `PATCH /api/deployment` and `POST /api/deployment/maintenance`
require `deployment:manage` (tenant owner or admin); `GET /api/deployment` requires
`deployment:read`, which team members hold — being able to check "is this box up to
date, and is it in maintenance" is ordinary self-service.

---

## 9. Verification

**Deployment posture** — the first thing to read when something is off:

```bash
curl -fsS https://studio.example.com/api/deployment | jq
```

```json
{
  "settings": { "maintenance_mode": false, "last_backup_at": "2026-10-12T03:15:00", ... },
  "app_version": "1.1.0",
  "environment": "production",
  "migration": { "head": "20261012_0022", "current": "20261012_0022",
                 "pending": [], "up_to_date": true },
  "capabilities": { "secret_encryption": true, "provider_callback_signature": true,
                    "asset_link_secret_secure": true, "structured_logs": true,
                    "webhook_dispatch": true },
  "table_count": 36
}
```

`table_count` counts every table the connected schema has, **including** Alembic's own
`alembic_version` bookkeeping row-table — that is why it reads 36 while
`scripts/deployment_preflight.py` (which excludes it) reports 35 application tables. Both
numbers are right; they answer different questions.

`migration.up_to_date: false` with a non-empty `pending` is the one state worth
alerting on: code shipped ahead of the schema. `capabilities` is booleans only, by
design — a posture endpoint that echoed values would be a credential leak waiting
for a screenshot.

**The regression gate.** Every version's acceptance criteria are encoded in one
HTTP-only script:

```bash
docker compose -f docker-compose.prod.yml exec -T api python scripts/verify_review_loop.py
```

It is re-runnable against the same database (fixed slugs are reused, idempotent
operations stay idempotent), so it can be run after every upgrade rather than
against a fresh environment. **390 checks** by default, **394** when
`SECRET_ENCRYPTION_KEY` and `PROVIDER_CALLBACK_SECRET` are both configured — §23
adapts to the deployment instead of demanding it adopt optional features. It is
also adaptive in the other direction: §11 skips three "intent downgrade" checks on
a host that has real provider keys, so a production box reports **three fewer**
than a bare one. The number to trust is *zero failures*, not the total.

**Metrics and logs.**

```bash
curl -fsS http://<api>:8080/metrics | head          # Prometheus text; unauthenticated by design
docker compose logs -f --tail=100 api               # LOG_FORMAT=json → one JSON object per line
```

`/metrics` is deliberately outside `/api` and outside the permission contract: its
consumer is a scraper, not an API caller. Keep it off the public edge — the ingress
in `infra/deploy/k8s/50-ingress.yaml` does not route it.

---

## 10. Troubleshooting

| Symptom | First thing to check | Likely cause |
| --- | --- | --- |
| `GET /api/deployment` → `migration.up_to_date: false`, `pending` non-empty | Did the `migrate` service/Job actually run? | Image rolled forward before the schema. Run §4 step 3. |
| Every write → 503 `maintenance_mode` | `GET /api/deployment` → `settings.maintenance_mode` | Someone left maintenance mode on, possibly a previous restore. §8, `enabled: false`. |
| Asset link minting → 503 `insecure_secret_configuration` | `capabilities.asset_link_secret_secure` | `ASSET_LINK_SECRET` is still the development default outside a dev environment. Set it. (Verification still works — only minting is refused.) |
| Provider calls fail with an authentication error after a key rotation | `GET /api/provider-credentials` → `secret_source`, `encrypted` | `SECRET_ENCRYPTION_KEY` changed but credentials were not re-sealed. `POST /api/provider-credentials/{id}/rotate` per credential. |
| Jobs sit in `queued` and nothing processes them | `WORKER_ROLE` on each worker; `GET /api/jobs/{id}` | The queue has no worker bound to it. `provider`/`analysis`/`media`/`render` each need one. |
| Jobs stuck in `running` | `started_at`, `lease_expires_at` | The reaper fails them after `WORKER_JOB_TIMEOUT_SECONDS` (default 1800s); `waiting_provider` is exempt. |
| An upload → 413 or 415 with no obvious reason | `ASSET_UPLOAD_MAX_BYTES` / the file's actual bytes | 413 is the decoded-size cap (256 MiB default); 415 is a denylisted type or a magic-number mismatch (declared mp4, actually an ELF). |
| Webhooks stop arriving | `GET /api/webhooks/{id}` → `status`, `consecutive_failures` | Auto-disabled after `WEBHOOK_DISABLE_AFTER_FAILURES` (default 20). Fix the endpoint, then `PATCH` status back to `active` — that also resets the counter. |
| Edited `.env`, rebuilt, restarted — behaviour is unchanged (still the old provider model / endpoint) | `docker compose config` for what Compose resolved, `docker compose exec -T api printenv VAR` for what the process reads | An exported shell variable outranks `.env` in Compose interpolation, and an unchanged resolved config means `up -d` never recreates the container. `unset` it, then `docker compose up -d --force-recreate api worker`. §2, "Editing `.env` after the first install". |
| API replicas restart under load | `docker compose logs`, or `kubectl describe pod` | Container memory limit. `worker-media`/`worker-render` are the CPU/high-memory ones; tune the limits rather than removing them. |

Deeper analysis is in the per-project panel, `GET /api/projects/{id}/observability`
(V8.2): cost, latency percentiles, job counts by queue and status, and recent error
events.

---

## 11. Secret rotation

| Secret | Blast radius | Procedure |
| --- | --- | --- |
| `SECRET_ENCRYPTION_KEY` | Provider credentials stop decrypting until re-sealed | Set the new key → restart → `POST /api/provider-credentials/{id}/rotate` for each credential (idempotent; also seals any pre-V8.4 inline secret). Under maintenance mode until every credential reports `secret_source: vault`. |
| `API_KEY_PEPPER` | **Every issued API key stops working** | Re-issue keys, distribute, restart. This is the emergency lever: it is the only way to revoke keys whose plaintext you no longer have. |
| `EMBED_TOKEN_SECRET` | **Every embed link in flight stops verifying** | Restart; re-mint links for host platforms. Also the emergency lever for the same reason. |
| `ASSET_LINK_SECRET` | Existing signed asset links stop verifying after their TTL | Restart. Old links keep working until they expire (verification is not refused, only minting). |
| `PROVIDER_CALLBACK_SECRET` | Unsigned/mis-signed callbacks → 401 | Update the upstream's signing config first, then the deployment, or callbacks fail in the gap. |
| Webhook subscription secret | That one subscriber | Self-service: `POST /api/webhooks/{id}/rotate-secret`, then update the subscriber. |

---

## 12. What this runbook does not cover

Stated so the gaps are visible rather than discovered:

- **No high-availability database.** MySQL is a single instance in the compose
  file. Real HA is a managed service or a cluster with its own failover story, and
  bolting one on inside a deployment runbook would hide that it is a separate
  decision.
- **No disaster recovery across regions.** One host, one database, one storage
  location. The backup procedure gets you a copy; where that copy lives is your
  organisation's call.
- **No key management service.** `SECRET_ENCRYPTION_KEY` comes from the
  environment, so whoever holds the environment holds the ciphertext (V8.4 says so
  explicitly). A KMS/HSM integration is a different project with a different threat
  model.
- **No automatic failover of workers.** A dead worker means its queue drains
  slower, not that work is lost — jobs are claimed with a lease and reaped when it
  expires — but nothing restarts it except the container runtime.
