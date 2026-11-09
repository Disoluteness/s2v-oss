# Deployment manifests

Two ways to run this stack in production, and the runbook that covers both:
[`docs/DEPLOYMENT.md`](../../docs/DEPLOYMENT.md). Read that first — these files are
the machine-readable half of it, and the parts that are judgement calls (ordering,
what to check before and after) are written there rather than in YAML comments
nobody reads until something is on fire.

## Files

| Path | What it is |
| --- | --- |
| [`docker-compose.prod.yml`](../../docker-compose.prod.yml) | Single-host production compose: no published infrastructure ports, a one-shot `migrate` service, four role-split workers, resource limits and log rotation. |
| `k8s/00-namespace.yaml` | The namespace. |
| `k8s/10-config.yaml` | ConfigMap (safe to read) + a **template** for the Secret. |
| `k8s/20-api.yaml` | API Service, Deployment, and the shared storage claim. |
| `k8s/30-workers.yaml` | One Deployment per queue (`provider` / `analysis` / `media` / `render`). |
| `k8s/40-migrate-job.yaml` | The migration Job — the thing that makes an upgrade a procedure. |
| `k8s/50-ingress.yaml` | Ingress with TLS, and the deliberate absence of a `/metrics` route. |
| `k8s/60-autoscaling.yaml` | HPAs and disruption budgets. |

## Choosing between them

**Compose** is the right answer for one host, and one host is the right answer for
an internal studio with a handful of concurrent runs. It is the shorter path to a
working deployment and the shorter path to understanding what is running, because
there is one file and `docker compose ps` is the whole control plane.

**Kubernetes** earns its complexity at the point where you need something Compose
cannot give you: two API replicas behind a load balancer with no dropped requests
during a roll, a worker fleet that scales per queue, and a schema change that is an
object in the cluster instead of a command in someone's shell history. It also
gives secrets a better home than `docker inspect`.

The manifests are plain YAML rather than a Helm chart on purpose. See the header of
`00-namespace.yaml` for the reasoning; the short version is that a chart earns its
keep when there are many deploy shapes, and this deployment has one.

## What you must change before applying

Every placeholder is marked `CHANGE ME` or `REPLACE_WITH_...` in the file. The ones
that are easy to miss and expensive to get wrong:

- **Image references** (`registry.example.com/s2v/studio:0.9.0`) appear in
  `20-api.yaml`, `30-workers.yaml` and `40-migrate-job.yaml`. They must all be the
  same tag — a drifted api/worker pair is the classic "works in the API, fails in
  the worker" bug, and the migration Job must be the same build whose code expects
  the schema it applies.
- **`PUBLIC_BASE_URL`** must equal the ingress host. It is what the platform puts
  in the links it mints (embed sessions, asset links); behind an ingress the `Host`
  header is the internal hop, so a link built from it works for us and for nobody
  else.
- **The storage class** must support `ReadWriteMany` for `OBJECT_STORAGE_BACKEND=local`
  at more than one replica. If it does not, point the deployment at S3/MinIO and
  drop the claim instead of silently running with replicas that cannot see each
  other's assets.
- **Secrets** are not in this repository. Create `s2v-secrets` out of band — the
  header of `10-config.yaml` has the exact command — so no filled-in copy of the
  template ever exists on disk.

## Order of operations

Applying the directory in filename order is close to correct, with two exceptions
worth knowing up front:

1. `10-config.yaml` contains a Secret whose values are placeholders. Create the
   real Secret first (out of band) and then apply the ConfigMap, or the Deployments
   will start against `CHANGE_ME` connection strings.
2. The `migrate` Job must run before the api and worker images are updated. There is
   no manifest-level dependency that expresses this — see the note in
   `40-migrate-job.yaml` — so it is a step in the runbook, not a property of the
   YAML.

After the first rollout, verify with the deployment posture endpoint:

```bash
curl -fsS https://studio.example.com/api/deployment | jq '.migration'
# {"head":"20261109_0023","current":"20261109_0023","pending":[],"up_to_date":true}
```

`up_to_date: false` with a non-empty `pending` means the code shipped ahead of the
schema. That is the one state worth alerting on.
