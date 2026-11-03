# Script2Video Studio — TypeScript / Node SDK

A dependency-free client for the platform's HTTP API (V7.1 contract, V7.2 SDK).
It speaks the same API the Web Studio uses, so an internal AI platform or Web
platform can drive script-to-video without touching provider keys.

- **No runtime dependencies** — built on the global `fetch` (Node 18+).
- **One failure shape** — every error is an `ApiError` carrying the server's
  stable `code`, so a caller can branch on `error.code` instead of guessing from
  a status number.
- **Retry-safe by default** — mutating calls carry an `Idempotency-Key`, and a
  retry reuses it, so a timeout cannot create a second run.
- **Ships compiled** — `dist/` is the build output and is committed, so the
  package is usable without a TypeScript toolchain.

## Install

```bash
npm install ./sdk/typescript
```

## Build

```bash
cd sdk/typescript
npm install
npm run build        # tsc -> dist/  (npm run typecheck for a dry run)
```

## Configure

```ts
import { Script2VideoClient } from "s2v-client";

const client = new Script2VideoClient({
  baseUrl: "https://studio.internal",   // or $SCRIPT2VIDEO_BASE_URL
  apiKey: "s2v_...",                    // or $SCRIPT2VIDEO_API_KEY
});
```

Two ways to identify the caller, and they coexist:

| Credential | Header | When to use |
|---|---|---|
| API key | `Authorization: Bearer s2v_...` | Anything outside the deployment's own trust boundary. The key inherits the owner's permissions and can be scoped narrower. |
| Trusted actor header | `X-Actor-Id` | Calls already behind the internal gateway / SSO proxy. Set with `actorId: "alice"`. |

A key always wins over a header. A request with neither falls back to the
deployment's default actor, which is what keeps existing unauthenticated callers
working.

## Issue a key

A key is issued by an owner or admin (`apikey:manage`) and belongs to whoever
asked for it. The plaintext appears once, in the creation response, and is never
recoverable — only a salted digest is stored.

```ts
const created = await client.createApiKey("batch-pipeline", {
  scopes: ["run:read", "run:create"],
});
console.log(created.plaintext);   // s2v_... — store this now
console.log(created.keyPrefix);   // s2v_1a2b3c4d5e6f — safe to log

await client.revokeApiKey(created.apiKeyId!);
```

`scopes` may only narrow the owner's own permissions. Asking for more is refused
(`permission_denied`) rather than silently trimmed, because a credential that
behaves unlike the request that created it is worse than an error.

## The whole job in one call

```ts
const video = await client.scriptToVideo(
  "开场用一个反常识的问题抓住注意力，然后给出一个可验证的数据，最后收尾。",
  { projectTitle: "新品预热", acceptPaidProviders: true },
);

console.log(video.url);           // signed, time-limited download link
console.log(video.durationMs);
```

`scriptToVideo` creates the project (if you did not pass `projectId`), creates the
run, starts it, waits, and returns the render.

## The individual steps

```ts
import { readFile, writeFile } from "node:fs/promises";

const project = await client.createProject("新品预热");

const run = await client.createRun(project.projectId!, "开场三秒用一个反常识的问题……", {
  pipelineKey: "avatar_spokesperson",
  targetPlatform: "douyin",
  targetDurationSeconds: 15,
  qualityMode: "provider_avatar",   // declare intent; the plan flags any downgrade
  costBudget: 8,
});

// Supply the persona image the avatar stage composites onto. Reading the file
// is the caller's job, which keeps this client runtime-agnostic.
await client.uploadPersonImage(run.runId!, {
  filename: "avatar.png",
  content: await readFile("avatar.png"),
});

await client.startRun(run.runId!, { acceptPaidProviders: true });

const finished = await client.waitRun(run.runId!, {
  timeoutMs: 900_000,
  onProgress: (r) => console.log(r.status, r.currentStageKey),
});

if (finished.status === "failed") {
  for (const result of await client.retryFailedJobs(run.runId!)) {
    console.log("retried", result["job_id"]);
  }
}

const video = await client.getFinalVideo(run.runId!);
await writeFile("out.mp4", await client.downloadAsset(video.assetId!));
```

### `waitRun` semantics

It stops — and returns — when the run reaches a state polling cannot change:

- `completed` / `failed` / `cancelled` — terminal
- `waiting_review` / `paused` — waiting for a **human**

A failed run is *returned*, not thrown, so the caller decides whether that is
fatal. Pass `raiseOnTimeout: true` to get `RunWaitTimeout` instead of the last
observed run when the deadline passes.

## Human review

```ts
const requests = await client.request<Record<string, unknown>[]>(
  "GET",
  `/api/runs/${run.runId}/review-requests`,
);

await client.submitReviewDecision(String(requests[0]?.["review_request_id"]), "approve_with_changes", {
  comment: "把第三镜的节奏压紧一点",
});
```

## Error handling

```ts
import { ApiError, TransportError } from "s2v-client";

try {
  await client.createRun(projectId, "...");
} catch (error) {
  if (error instanceof ApiError) {
    console.log(error.status, error.code);   // 403 permission_denied
    console.log(error.retryable);            // false — retrying changes nothing
    console.log(error.detail);               // the full server envelope
  } else if (error instanceof TransportError) {
    console.log("never reached the server:", error.message);
  }
}
```

`TransportError` and `ApiError` are deliberately separate: "the server said no"
and "the server was never reached" need different handling.

The full catalog is discoverable at runtime rather than documented by hand:

```ts
for (const entry of await client.errorCodes()) {
  console.log(entry.code, entry.status_code, entry.retryable, entry.description);
}
```

## Retries and idempotency

`maxRetries` (default 2) applies only to a connection failure, a `429`, a `5xx`,
or a code the server marks retryable. Every retry reuses the same
`Idempotency-Key`, so a duplicate is impossible by construction.

```ts
const client = new Script2VideoClient({
  baseUrl: "https://studio.internal",
  apiKey: "s2v_...",
  maxRetries: 4,
  backoffFactor: 1.5,
});

// Pin the key yourself when your own job queue already has one.
await client.createRun(projectId, script, { idempotencyKey: `job-${jobId}` });

// Or turn it off for a call where a duplicate is harmless.
await client.createRun(projectId, script, { idempotencyKey: false });
```

## Custom transport

Anything implementing `send(request) => Promise<TransportResponse>` works, so you
can reuse an agent, a proxy, or your own telemetry:

```ts
import { Script2VideoClient, type Transport, type TransportRequest, type TransportResponse } from "s2v-client";

class MyTransport implements Transport {
  async send(request: TransportRequest): Promise<TransportResponse> {
    // ...perform the call, return { status, headers, body: Uint8Array }
    return { status: 200, headers: {}, body: new Uint8Array() };
  }
}

const client = new Script2VideoClient({ baseUrl: "...", transport: new MyTransport() });
```

The body is `Uint8Array`, not `string`, because the same client downloads MP4s —
decoding a video as UTF-8 would corrupt it. Use the exported `bodyText(response)`
helper when a response is known to be text.

## What is covered

| Area | Methods |
|---|---|
| Meta | `health`, `me`, `errorCodes` |
| Projects | `createProject`, `getProject`, `listProjects` |
| Runs | `createRun`, `getRun`, `listProjectRuns`, `getRunBoard`, `listRunArtifacts`, `listRunJobs`, `listRunEvents` |
| Assets | `uploadAsset`, `uploadPersonImage`, `listRunAssets`, `getAsset`, `downloadAsset` |
| Lifecycle | `confirmPaidProvider`, `startRun`, `waitRun`, `getFinalVideo` |
| Actions | `retryAction`, `retryJob`, `retryFailedJobs`, `runStateAction` |
| Review | `submitReviewDecision` |
| Credentials | `createApiKey`, `listApiKeys`, `revokeApiKey` |
| Convenience | `runScript`, `scriptToVideo` |

Any endpoint without a named method is still reachable:

```ts
await client.request("GET", "/api/render-profiles", { params: { limit: 10 } });
```

## Webhooks

Register an endpoint, then verify what arrives:

```ts
import { Script2VideoClient, verifyWebhookSignature, SIGNATURE_HEADER } from "s2v-client";

const client = new Script2VideoClient({ baseUrl: "http://localhost:8080", apiKey: "s2v_..." });

const subscription = await client.createWebhookSubscription(
  "ops-hook", "https://ops.internal/hooks/s2v", ["run.completed", "stage.waiting_review"],
);
console.log(subscription.secret); // shown on every read; key for verification

// ... inside your HTTP handler, with the RAW request bytes:
if (verifyWebhookSignature(subscription.secret, {
  header: req.headers["x-s2v-signature"],
  body: rawBody,
})) {
  const payload = JSON.parse(new TextDecoder().decode(rawBody)); // dedupe on
  ...                                                            // payload.delivery_id
}
```

Verification rejects tampered bodies and replayed deliveries with one check:
the signature's timestamp is inside the signed string, so anything older than
five minutes (the default tolerance) is stale. Operations:

- `listWebhookEvents()` — the published vocabulary, no subscription needed.
- `createWebhookSubscription` / `listWebhookSubscriptions` /
  `getWebhookSubscription` / `updateWebhookSubscription` /
  `deleteWebhookSubscription`.
- `rotateWebhookSecret(id)` — new secret; accept both during the handover.
- `listWebhookDeliveries(id)` — every attempt, its response and its fate
  (`pending` / `delivering` / `succeeded` / `failed` / `abandoned`).
- `requeueWebhookDelivery(deliveryId)` — manual replay with a fresh attempt
  count; for an `abandoned` delivery this is the only way it ever arrives.

## Examples


- `examples/script-to-video.mjs` — the one-call path against a local stack.
- `examples/upload-and-retry.mjs` — step-by-step, with a persona image and a
  failed-job retry.
