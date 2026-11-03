/**
 * The Script2Video Studio client.
 *
 * Typical use:
 *
 *     import { Script2VideoClient } from "s2v-client";
 *
 *     const client = new Script2VideoClient({ baseUrl: "https://studio.internal", apiKey: "s2v_..." });
 *     const video = await client.scriptToVideo("把这段脚本做成 15 秒口播", { projectTitle: "新品预热" });
 *     console.log(video.url);
 *
 * Everything is built on one HTTP call helper, so behaviour that must be
 * uniform — authentication, retry, idempotency, error decoding — exists once.
 *
 * Two defaults are worth knowing about:
 *
 *   - **Mutating calls carry an `Idempotency-Key`.** A retry after a timeout
 *     will not create a second run. Pass `idempotencyKey` per call or
 *     `autoIdempotency: false` to opt out.
 *   - **Retries are conservative.** Only a connection failure or a code the
 *     server itself marks retryable is retried, and only with the *same*
 *     idempotency key — a retry that could duplicate work is not a retry, it is
 *     a second request.
 */

import {
  ApiError,
  FinalVideoNotFound,
  RunWaitTimeout,
  TransportError,
  type JsonRecord,
} from "./errors.js";
import {
  Actor,
  ApiKey,
  Artifact,
  Asset,
  FinalVideo,
  Job,
  Model,
  Page,
  Project,
  Run,
  WebhookDelivery,
  WebhookSubscription,
} from "./models.js";
import { FetchTransport, bodyText, type Transport, type TransportResponse } from "./transport.js";

const MUTATING_METHODS = new Set(["POST", "PUT", "PATCH", "DELETE"]);
export const DEFAULT_POLL_INTERVAL_MS = 3000;
export const DEFAULT_WAIT_TIMEOUT_MS = 900_000;

export interface ClientOptions {
  baseUrl?: string;
  apiKey?: string;
  /** V6.5 trusted-header identity, used when no API key is held. */
  actorId?: string;
  actorSecret?: string;
  timeoutMs?: number;
  maxRetries?: number;
  backoffFactor?: number;
  backoffBaseMs?: number;
  transport?: Transport;
  defaultHeaders?: Record<string, string>;
  autoIdempotency?: boolean;
  userAgent?: string;
  sleep?: (ms: number) => Promise<void>;
}

export interface RequestOptions {
  jsonBody?: JsonRecord;
  params?: Record<string, unknown>;
  headers?: Record<string, string>;
  /** `false` disables the header for this call; a string pins it. */
  idempotencyKey?: string | false;
}

export interface CreateRunOptions extends JsonRecord {
  pipelineKey?: string;
  brandName?: string;
  productName?: string;
  targetPlatform?: string;
  targetDurationSeconds?: number;
  aspectRatio?: string;
  styleKey?: string;
  costBudget?: number;
  qualityMode?: string;
  idempotencyKey?: string | false;
}

export interface UploadAssetOptions {
  assetType: string;
  filename?: string;
  content?: Uint8Array | string;
  mimeType?: string;
  idempotencyKey?: string | false;
  [key: string]: unknown;
}

export interface WaitRunOptions {
  timeoutMs?: number;
  pollIntervalMs?: number;
  stopOnReview?: boolean;
  raiseOnTimeout?: boolean;
  onProgress?: (run: Run) => void;
}

export interface StartRunOptions {
  acceptIntentDowngrade?: boolean;
  acceptPaidProviders?: boolean;
}

function defaultEnv(): Record<string, string | undefined> {
  const holder = globalThis as {
    process?: { env?: Record<string, string | undefined> };
  };
  return holder.process?.env ?? {};
}

/** Base64 without assuming a runtime: `Buffer` where present, `btoa` otherwise. */
function toBase64(input: Uint8Array | string): string {
  const bytes = typeof input === "string" ? new TextEncoder().encode(input) : input;
  const holder = globalThis as {
    Buffer?: { from(data: Uint8Array): { toString(encoding: string): string } };
  };
  if (holder.Buffer) {
    return holder.Buffer.from(bytes).toString("base64");
  }
  let binary = "";
  for (let index = 0; index < bytes.length; index += 1) {
    binary += String.fromCharCode(bytes[index] as number);
  }
  return btoa(binary);
}

function encodeQuery(params: Record<string, unknown> | undefined): string {
  if (!params) {
    return "";
  }
  const search = new URLSearchParams();
  for (const [key, value] of Object.entries(params)) {
    if (value === undefined || value === null) {
      continue;
    }
    if (Array.isArray(value)) {
      for (const item of value) {
        search.append(key, String(item));
      }
    } else {
      search.append(key, String(value));
    }
  }
  const encoded = search.toString();
  return encoded ? `?${encoded}` : "";
}

function fallbackCode(status: number): string {
  switch (status) {
    case 401:
      return "unauthorized";
    case 403:
      return "permission_denied";
    case 404:
      return "not_found";
    case 405:
      return "method_not_allowed";
    case 409:
      return "conflict";
    case 422:
      return "validation_error";
    case 429:
      return "rate_limited";
    default:
      return status >= 500 ? "internal_error" : "bad_request";
  }
}

export class Script2VideoClient {
  readonly baseUrl: string;
  readonly apiKey?: string;
  readonly actorId?: string;
  readonly actorSecret?: string;
  readonly timeoutMs: number;
  readonly maxRetries: number;
  readonly backoffFactor: number;
  readonly backoffBaseMs: number;
  readonly autoIdempotency: boolean;
  readonly transport: Transport;
  readonly defaultHeaders: Record<string, string>;
  readonly userAgent: string;

  private readonly sleep: (ms: number) => Promise<void>;

  constructor(options: ClientOptions = {}) {
    const env = defaultEnv();
    this.baseUrl = (
      options.baseUrl ??
      env["SCRIPT2VIDEO_BASE_URL"] ??
      "http://127.0.0.1:8080"
    ).replace(/\/+$/, "");
    this.apiKey = options.apiKey ?? env["SCRIPT2VIDEO_API_KEY"];
    this.actorId = options.actorId ?? env["SCRIPT2VIDEO_ACTOR_ID"];
    this.actorSecret = options.actorSecret ?? env["SCRIPT2VIDEO_ACTOR_SECRET"];
    this.timeoutMs = options.timeoutMs ?? 30_000;
    this.maxRetries = Math.max(0, options.maxRetries ?? 2);
    this.backoffFactor = Math.max(0, options.backoffFactor ?? 1);
    this.backoffBaseMs = Math.max(0, options.backoffBaseMs ?? 500);
    this.autoIdempotency = options.autoIdempotency ?? true;
    this.transport = options.transport ?? new FetchTransport();
    this.defaultHeaders = { ...(options.defaultHeaders ?? {}) };
    this.userAgent = options.userAgent ?? "s2v-node/0.8.0";
    this.sleep =
      options.sleep ?? ((ms: number) => new Promise<void>((resolve) => setTimeout(resolve, ms)));
  }

  // -- plumbing ----------------------------------------------------------

  private buildHeaders(
    hasBody: boolean,
    extra?: Record<string, string>,
  ): Record<string, string> {
    const headers: Record<string, string> = {
      Accept: "application/json",
      "User-Agent": this.userAgent,
    };
    if (hasBody) {
      headers["Content-Type"] = "application/json";
    }
    if (this.apiKey) {
      headers["Authorization"] = `Bearer ${this.apiKey}`;
    } else if (this.actorId) {
      headers["X-Actor-Id"] = this.actorId;
    }
    if (this.actorSecret) {
      headers["X-Actor-Secret"] = this.actorSecret;
    }
    return { ...headers, ...this.defaultHeaders, ...(extra ?? {}) };
  }

  private idempotencyValue(requested: string | false | undefined): string | undefined {
    if (requested === false) {
      return undefined;
    }
    if (typeof requested === "string") {
      return requested;
    }
    return this.autoIdempotency ? randomKey() : undefined;
  }

  private isRetryable(response: TransportResponse): boolean {
    if (response.status === 429 || response.status >= 500) {
      return true;
    }
    const parsed = parseJson(bodyText(response));
    const detail = parsed?.["detail"];
    const code = isRecord(detail) ? detail["code"] : undefined;
    return typeof code === "string" && RETRYABLE_LOOKUP.has(code);
  }

  private async backoff(attempt: number, retryAfter?: string): Promise<void> {
    if (retryAfter) {
      const seconds = Number(retryAfter);
      if (Number.isFinite(seconds) && seconds >= 0) {
        await this.sleep(seconds * 1000);
        return;
      }
    }
    await this.sleep(this.backoffBaseMs * this.backoffFactor * 2 ** attempt);
  }

  private async rawRequest(
    method: string,
    path: string,
    body: string | undefined,
    params: Record<string, unknown> | undefined,
    headers: Record<string, string> | undefined,
    idempotencyKey: string | false | undefined,
  ): Promise<TransportResponse> {
    const upper = method.toUpperCase();
    const url = `${this.baseUrl}${path}${encodeQuery(params)}`;
    const sent = this.buildHeaders(body !== undefined, headers);
    if (MUTATING_METHODS.has(upper)) {
      const key = this.idempotencyValue(idempotencyKey);
      if (key) {
        sent["Idempotency-Key"] = key;
      }
    }

    let attempt = 0;
    for (;;) {
      let response: TransportResponse;
      try {
        response = await this.transport.send({
          method: upper,
          url,
          headers: sent,
          body,
          timeoutMs: this.timeoutMs,
        });
      } catch (error) {
        if (!(error instanceof TransportError) || attempt >= this.maxRetries) {
          throw error;
        }
        await this.backoff(attempt);
        attempt += 1;
        continue;
      }
      if (response.status < 400 || attempt >= this.maxRetries) {
        return response;
      }
      if (!this.isRetryable(response)) {
        return response;
      }
      await this.backoff(attempt, response.headers["retry-after"]);
      attempt += 1;
    }
  }

  private toApiError(
    response: TransportResponse,
    method: string,
    path: string,
  ): ApiError {
    const text = bodyText(response);
    const parsed = parseJson(text);
    let detail: JsonRecord = {};
    if (parsed) {
      const raw = parsed["detail"];
      if (isRecord(raw)) {
        detail = raw;
      } else if (raw !== undefined && raw !== null) {
        detail = { message: String(raw) };
      }
    }
    const code = typeof detail["code"] === "string" ? detail["code"] : fallbackCode(response.status);
    const message = typeof detail["message"] === "string" ? detail["message"] : text;
    return new ApiError(response.status, code, message, { detail, method, path });
  }

  private decode<T>(response: TransportResponse, method: string, path: string): T {
    if (response.status >= 400) {
      throw this.toApiError(response, method, path);
    }
    if (response.body.length === 0) {
      return undefined as T;
    }
    const text = bodyText(response);
    const parsed = parseJson(text);
    return (parsed ?? text) as T;
  }

  /** Call an endpoint that is not wrapped by a named method. */
  async request<T = JsonRecord>(
    method: string,
    path: string,
    options: RequestOptions = {},
  ): Promise<T> {
    const body = options.jsonBody === undefined ? undefined : JSON.stringify(options.jsonBody);
    const response = await this.rawRequest(
      method,
      path,
      body,
      options.params,
      options.headers,
      options.idempotencyKey,
    );
    return this.decode<T>(response, method, path);
  }

  // -- meta --------------------------------------------------------------

  async health(): Promise<JsonRecord> {
    return (await this.request<JsonRecord>("GET", "/api/health")) ?? {};
  }

  /** Who the server thinks this client is, and what it may do. */
  async me(): Promise<Actor> {
    return new Actor(await this.request<JsonRecord>("GET", "/api/me"));
  }

  /** The published failure catalog — useful to validate a branch table. */
  async errorCodes(): Promise<JsonRecord[]> {
    return (await this.request<JsonRecord[]>("GET", "/api/error-codes")) ?? [];
  }

  // -- projects ----------------------------------------------------------

  async createProject(
    title: string,
    options: {
      description?: string;
      defaultLanguage?: string;
      defaultAspectRatio?: string;
    } = {},
  ): Promise<Project> {
    const body: JsonRecord = { title };
    if (options.description !== undefined) body["description"] = options.description;
    if (options.defaultLanguage !== undefined) body["default_language"] = options.defaultLanguage;
    if (options.defaultAspectRatio !== undefined) {
      body["default_aspect_ratio"] = options.defaultAspectRatio;
    }
    return new Project(await this.request<JsonRecord>("POST", "/api/projects", { jsonBody: body }));
  }

  async getProject(projectId: string): Promise<Project> {
    return new Project(await this.request<JsonRecord>("GET", `/api/projects/${projectId}`));
  }

  async listProjects(): Promise<Project[]> {
    const items = (await this.request<JsonRecord[]>("GET", "/api/projects")) ?? [];
    return items.map((item) => new Project(item));
  }

  // -- runs --------------------------------------------------------------

  /** Create a run. Unknown option keys are forwarded as extra run fields. */
  async createRun(
    projectId: string,
    rawScript: string,
    options: CreateRunOptions = {},
  ): Promise<Run> {
    const { idempotencyKey, ...rest } = options;
    const mapping: Record<string, string> = {
      pipelineKey: "pipeline_key",
      brandName: "brand_name",
      productName: "product_name",
      targetPlatform: "target_platform",
      targetDurationSeconds: "target_duration_seconds",
      aspectRatio: "aspect_ratio",
      styleKey: "style_key",
      costBudget: "cost_budget",
      qualityMode: "quality_mode",
    };
    const body: JsonRecord = { raw_script: rawScript };
    for (const [key, value] of Object.entries(rest)) {
      if (value === undefined) {
        continue;
      }
      body[mapping[key] ?? key] = value;
    }
    return new Run(
      await this.request<JsonRecord>("POST", `/api/projects/${projectId}/runs`, {
        jsonBody: body,
        idempotencyKey,
      }),
    );
  }

  async getRun(runId: string): Promise<Run> {
    return new Run(await this.request<JsonRecord>("GET", `/api/runs/${runId}`));
  }

  async listProjectRuns(projectId: string): Promise<Run[]> {
    const items = (await this.request<JsonRecord[]>("GET", `/api/projects/${projectId}/runs`)) ?? [];
    return items.map((item) => new Run(item));
  }

  /** The full run view: stages, artifacts, jobs, cost, review state. */
  async getRunBoard(runId: string): Promise<Model> {
    return new Model(await this.request<JsonRecord>("GET", `/api/runs/${runId}/board`));
  }

  async listRunArtifacts(runId: string, options: { currentOnly?: boolean } = {}): Promise<Artifact[]> {
    const items =
      (await this.request<JsonRecord[]>("GET", `/api/runs/${runId}/artifacts`, {
        params: options.currentOnly ? { current_only: true } : undefined,
      })) ?? [];
    return items.map((item) => new Artifact(item));
  }

  async listRunJobs(runId: string): Promise<Job[]> {
    const items = (await this.request<JsonRecord[]>("GET", `/api/runs/${runId}/jobs`)) ?? [];
    return items.map((item) => new Job(item));
  }

  async listRunEvents(runId: string, options: { limit?: number } = {}): Promise<JsonRecord[]> {
    return (
      (await this.request<JsonRecord[]>("GET", `/api/runs/${runId}/events`, {
        params: options.limit === undefined ? undefined : { limit: options.limit },
      })) ?? []
    );
  }

  async submitReviewDecision(
    reviewRequestId: string,
    decision: string,
    options: {
      comment?: string;
      reasonCode?: string;
      actorUserId?: string;
      actorDisplayName?: string;
    } = {},
  ): Promise<JsonRecord> {
    const body: JsonRecord = { decision };
    if (options.comment !== undefined) body["comment"] = options.comment;
    if (options.reasonCode !== undefined) body["reason_code"] = options.reasonCode;
    if (options.actorUserId !== undefined) body["actor_user_id"] = options.actorUserId;
    if (options.actorDisplayName !== undefined) {
      body["actor_display_name"] = options.actorDisplayName;
    }
    return (
      (await this.request<JsonRecord>(
        "POST",
        `/api/review-requests/${reviewRequestId}/decisions`,
        { jsonBody: body },
      )) ?? {}
    );
  }

  // -- assets ------------------------------------------------------------

  /**
   * Upload bytes to a run.
   *
   * The body is base64 inside JSON because that is the API's upload contract —
   * no multipart — so the same call works from any runtime's stock HTTP stack.
   * Read the file yourself (`await readFile(path)`) and pass the bytes; the SDK
   * stays free of a Node-only file dependency.
   */
  async uploadAsset(runId: string, options: UploadAssetOptions): Promise<Asset> {
    const { assetType, filename, content, mimeType, idempotencyKey, ...extra } = options;
    if (content === undefined) {
      throw new TypeError("uploadAsset requires 'content' (Uint8Array or string)");
    }
    const body: JsonRecord = {
      asset_type: assetType,
      filename: filename ?? "upload.bin",
      content_base64: toBase64(content),
      ...extra,
    };
    if (mimeType !== undefined) {
      body["mime_type"] = mimeType;
    }
    return new Asset(
      await this.request<JsonRecord>("POST", `/api/runs/${runId}/assets`, {
        jsonBody: body,
        idempotencyKey,
      }),
    );
  }

  /** Upload the persona image the avatar stage composites onto. */
  async uploadPersonImage(
    runId: string,
    options: Omit<UploadAssetOptions, "assetType"> & { content: Uint8Array | string },
  ): Promise<Asset> {
    return this.uploadAsset(runId, { ...options, assetType: "person_image" });
  }

  async listRunAssets(runId: string): Promise<Asset[]> {
    const items = (await this.request<JsonRecord[]>("GET", `/api/runs/${runId}/assets`)) ?? [];
    return items.map((item) => new Asset(item));
  }

  async getAsset(assetId: string): Promise<Asset> {
    return new Asset(await this.request<JsonRecord>("GET", `/api/assets/${assetId}`));
  }

  /** Fetch an asset's bytes. */
  async downloadAsset(assetId: string): Promise<Uint8Array> {
    const path = `/api/assets/${assetId}/download`;
    const response = await this.rawRequest("GET", path, undefined, undefined, undefined, undefined);
    if (response.status >= 400) {
      throw this.toApiError(response, "GET", path);
    }
    return response.body;
  }

  // -- lifecycle ---------------------------------------------------------

  /** Acknowledge the first paid call to a provider on this run (T76). */
  async confirmPaidProvider(
    runId: string,
    provider: string,
    options: { accept?: boolean; note?: string } = {},
  ): Promise<JsonRecord> {
    const body: JsonRecord = { provider, accept: options.accept ?? true };
    if (options.note !== undefined) {
      body["note"] = options.note;
    }
    return (
      (await this.request<JsonRecord>("POST", `/api/runs/${runId}/provider-consents`, {
        jsonBody: body,
      })) ?? {}
    );
  }

  /**
   * Start a run.
   *
   * `acceptPaidProviders` clears the T76 first-use gate for every provider the
   * run is waiting on. It does *not* bypass the budget gate — a run over budget
   * is still refused, and should be.
   */
  async startRun(runId: string, options: StartRunOptions = {}): Promise<Run> {
    if (options.acceptPaidProviders) {
      const board = await this.getRunBoard(runId);
      const pending = board.get("paid_providers_pending");
      for (const provider of Array.isArray(pending) ? pending : []) {
        await this.confirmPaidProvider(runId, String(provider));
      }
    }
    return new Run(
      await this.request<JsonRecord>("POST", `/api/runs/${runId}/start`, {
        jsonBody: { accept_intent_downgrade: options.acceptIntentDowngrade ?? false },
      }),
    );
  }

  /**
   * Poll until the run settles, then return it.
   *
   * Stops at a terminal state (completed / failed / cancelled) and, unless
   * `stopOnReview` is off, at a human gate (waiting_review / paused) — waiting
   * for a person is not a thing polling can fix. A run that *failed* is
   * returned, not thrown: the caller decides whether a failure is fatal.
   */
  async waitRun(runId: string, options: WaitRunOptions = {}): Promise<Run> {
    const timeout = options.timeoutMs ?? DEFAULT_WAIT_TIMEOUT_MS;
    const pollInterval = options.pollIntervalMs ?? DEFAULT_POLL_INTERVAL_MS;
    const stopOnReview = options.stopOnReview ?? true;
    const deadline = Date.now() + timeout;

    let run = await this.getRun(runId);
    for (;;) {
      options.onProgress?.(run);
      const settled = run.isTerminal || (stopOnReview && run.isBlockedOnHuman);
      if (settled) {
        return run;
      }
      if (Date.now() >= deadline) {
        if (options.raiseOnTimeout) {
          throw new RunWaitTimeout(run, timeout / 1000);
        }
        return run;
      }
      await this.sleep(pollInterval);
      run = await this.getRun(runId);
    }
  }

  /**
   * The run's render output.
   *
   * Read from the run board's `final_outputs`, preferring a rendered master over
   * an edited cut. Throws `FinalVideoNotFound` when the run has produced nothing
   * yet — which usually just means it has not finished.
   */
  async getFinalVideo(runId: string): Promise<FinalVideo> {
    const board = await this.getRunBoard(runId);
    const outputs = board.list("final_outputs").map((item) => new FinalVideo(item));
    for (const assetType of ["rendered_video", "edited_video"]) {
      const match = outputs.find((output) => output.assetType === assetType);
      if (match) {
        return match;
      }
    }
    const byMime = outputs.find((output) => (output.mimeType ?? "").startsWith("video/"));
    if (byMime) {
      return byMime;
    }
    const run = board.get("run");
    const status = isRecord(run) ? run["status"] : undefined;
    throw new FinalVideoNotFound(`run ${runId} has no final video yet (status: ${String(status)})`);
  }

  // -- actions -----------------------------------------------------------

  /**
   * Run a manifest action — the same repair verbs the Web board offers.
   *
   * `action` is one of `dispatch_ready_steps`, `generate_person_image`,
   * `upload_person_image`, `generate_tts`, `analyze_source_media`,
   * `process_job`, `retry_job`.
   */
  async retryAction(
    runId: string,
    action: string,
    options: {
      stageKey?: string;
      stageRunId?: string;
      manifestStepKey?: string;
      jobId?: string;
      payload?: JsonRecord;
    } = {},
  ): Promise<JsonRecord> {
    const body: JsonRecord = { action };
    if (options.stageKey !== undefined) body["stage_key"] = options.stageKey;
    if (options.stageRunId !== undefined) body["stage_run_id"] = options.stageRunId;
    if (options.manifestStepKey !== undefined) {
      body["manifest_step_key"] = options.manifestStepKey;
    }
    if (options.jobId !== undefined) body["job_id"] = options.jobId;
    if (options.payload !== undefined) body["payload"] = options.payload;
    return (
      (await this.request<JsonRecord>("POST", `/api/runs/${runId}/manifest-actions`, {
        jsonBody: body,
      })) ?? {}
    );
  }

  /** Retry one background job. */
  async retryJob(jobId: string): Promise<JsonRecord> {
    return (await this.request<JsonRecord>("POST", `/api/jobs/${jobId}/retry`)) ?? {};
  }

  /**
   * Retry every failed job on a run, oldest first.
   *
   * Reads the job list rather than guessing: a run can fail several jobs, and
   * retrying them one at a time is what the Web board does.
   */
  async retryFailedJobs(runId: string): Promise<JsonRecord[]> {
    const jobs = await this.listRunJobs(runId);
    const results: JsonRecord[] = [];
    for (const job of jobs) {
      if (job.status === "failed" && job.jobId) {
        results.push(await this.retryJob(job.jobId));
      }
    }
    return results;
  }

  /** Pause, resume, cancel, or rerun a stage / artifact. */
  async runStateAction(
    runId: string,
    action: string,
    options: {
      stageKey?: string;
      artifactType?: string;
      reason?: string;
      payload?: JsonRecord;
    } = {},
  ): Promise<JsonRecord> {
    const body: JsonRecord = { action };
    if (options.stageKey !== undefined) body["stage_key"] = options.stageKey;
    if (options.artifactType !== undefined) body["artifact_type"] = options.artifactType;
    if (options.reason !== undefined) body["reason"] = options.reason;
    if (options.payload !== undefined) body["payload"] = options.payload;
    return (
      (await this.request<JsonRecord>("POST", `/api/runs/${runId}/state-actions`, {
        jsonBody: body,
      })) ?? {}
    );
  }

  // -- credentials -------------------------------------------------------

  /**
   * Issue a credential. Requires `apikey:manage` (owner or admin).
   *
   * The returned key's `plaintext` is the only copy that will ever exist — store
   * it, because it cannot be read back.
   */
  async createApiKey(
    name: string,
    options: { scopes?: string[]; expiresInDays?: number } = {},
  ): Promise<ApiKey> {
    const body: JsonRecord = { name };
    if (options.scopes !== undefined) body["scopes"] = options.scopes;
    if (options.expiresInDays !== undefined) body["expires_in_days"] = options.expiresInDays;
    return new ApiKey(await this.request<JsonRecord>("POST", "/api/api-keys", { jsonBody: body }));
  }

  async listApiKeys(
    options: { status?: string; limit?: number; offset?: number } = {},
  ): Promise<Page<ApiKey>> {
    const page = await this.request<JsonRecord>("GET", "/api/api-keys", {
      params: {
        status: options.status,
        limit: options.limit ?? 50,
        offset: options.offset ?? 0,
      },
    });
    return new Page<ApiKey>(page, ApiKey);
  }

  async revokeApiKey(apiKeyId: string): Promise<ApiKey> {
    return new ApiKey(
      await this.request<JsonRecord>("POST", `/api/api-keys/${apiKeyId}/revoke`),
    );
  }

  // -- webhooks ----------------------------------------------------------

  /** The published event vocabulary, from the open catalog endpoint. */
  async listWebhookEvents(): Promise<Model[]> {
    const data = (await this.request<JsonRecord[]>("GET", "/api/webhook-events")) ?? [];
    return data.map((entry) => new Model(entry));
  }

  /** Register an endpoint. Requires `webhook:manage` (owner or admin).
   *
   * Event names are validated against the catalog server-side — an unknown one
   * is refused rather than stored, so a typo fails at registration and not
   * three weeks later when nothing has arrived. */
  async createWebhookSubscription(
    name: string,
    url: string,
    eventTypes: string[],
    options: { description?: string; status?: string } = {},
  ): Promise<WebhookSubscription> {
    const body: JsonRecord = { name, url, event_types: eventTypes, status: options.status ?? "active" };
    if (options.description !== undefined) body["description"] = options.description;
    return new WebhookSubscription(
      await this.request<JsonRecord>("POST", "/api/webhooks", { jsonBody: body }),
    );
  }

  async listWebhookSubscriptions(
    options: { status?: string; limit?: number; offset?: number } = {},
  ): Promise<Page<WebhookSubscription>> {
    const page = await this.request<JsonRecord>("GET", "/api/webhooks", {
      params: {
        status: options.status,
        limit: options.limit ?? 50,
        offset: options.offset ?? 0,
      },
    });
    return new Page<WebhookSubscription>(page, WebhookSubscription);
  }

  async getWebhookSubscription(subscriptionId: string): Promise<WebhookSubscription> {
    return new WebhookSubscription(
      await this.request<JsonRecord>("GET", `/api/webhooks/${subscriptionId}`),
    );
  }

  /** Partial update: omitted fields are left alone.
   *
   * Re-activating a disabled subscription (`status: "active"`) also resets the
   * server-side consecutive-failure count, so a fixed endpoint is not
   * re-disabled by its history. */
  async updateWebhookSubscription(
    subscriptionId: string,
    options: {
      name?: string;
      url?: string;
      eventTypes?: string[];
      description?: string;
      status?: string;
    } = {},
  ): Promise<WebhookSubscription> {
    const body: JsonRecord = {};
    if (options.name !== undefined) body["name"] = options.name;
    if (options.url !== undefined) body["url"] = options.url;
    if (options.eventTypes !== undefined) body["event_types"] = options.eventTypes;
    if (options.description !== undefined) body["description"] = options.description;
    if (options.status !== undefined) body["status"] = options.status;
    return new WebhookSubscription(
      await this.request<JsonRecord>("PATCH", `/api/webhooks/${subscriptionId}`, { jsonBody: body }),
    );
  }

  /** Delete a subscription and its queued deliveries. The event log keeps everything. */
  async deleteWebhookSubscription(subscriptionId: string): Promise<WebhookSubscription> {
    return new WebhookSubscription(
      await this.request<JsonRecord>("DELETE", `/api/webhooks/${subscriptionId}`),
    );
  }

  /** Replace the signing secret. In-flight attempts may carry the old one;
   * accept both during the handover window. */
  async rotateWebhookSecret(subscriptionId: string): Promise<WebhookSubscription> {
    return new WebhookSubscription(
      await this.request<JsonRecord>("POST", `/api/webhooks/${subscriptionId}/rotate-secret`),
    );
  }

  /** The subscription's outbox: every attempt, its response, and its fate.
   *
   * `status` filters on `pending` / `delivering` / `succeeded` / `failed` /
   * `abandoned` — the last one means attempts ran out and a manual
   * `requeueWebhookDelivery` is the only way it arrives. */
  async listWebhookDeliveries(
    subscriptionId: string,
    options: { status?: string; limit?: number; offset?: number } = {},
  ): Promise<Page<WebhookDelivery>> {
    const page = await this.request<JsonRecord>(
      "GET",
      `/api/webhooks/${subscriptionId}/deliveries`,
      {
        params: {
          status: options.status,
          limit: options.limit ?? 50,
          offset: options.offset ?? 0,
        },
      },
    );
    return new Page<WebhookDelivery>(page, WebhookDelivery);
  }

  /** Put a delivery back in the queue with a fresh attempt count. */
  async requeueWebhookDelivery(deliveryId: string): Promise<WebhookDelivery> {
    return new WebhookDelivery(
      await this.request<JsonRecord>("POST", `/api/webhook-deliveries/${deliveryId}/requeue`),
    );
  }

  // -- convenience -------------------------------------------------------

  /** Create a run and start it — the two calls that always go together. */
  async runScript(
    projectId: string,
    rawScript: string,
    options: CreateRunOptions &
      StartRunOptions & {
        wait?: boolean;
        timeoutMs?: number;
        pollIntervalMs?: number;
        onProgress?: (run: Run) => void;
      } = {},
  ): Promise<Run> {
    const {
      acceptIntentDowngrade,
      acceptPaidProviders,
      wait = true,
      timeoutMs,
      pollIntervalMs,
      onProgress,
      ...runOptions
    } = options;
    const run = await this.createRun(projectId, rawScript, runOptions);
    if (!run.runId) {
      throw new Error("server returned a run without a run_id");
    }
    const started = await this.startRun(run.runId, {
      acceptIntentDowngrade,
      acceptPaidProviders: acceptPaidProviders ?? true,
    });
    if (!wait) {
      return started;
    }
    return this.waitRun(run.runId, { timeoutMs, pollIntervalMs, onProgress });
  }

  /**
   * Script in, final video out — the whole job in one call.
   *
   * Creates the project when `projectId` is omitted (titled from
   * `projectTitle`), creates the run, starts it, waits, and returns the render.
   * Fails loudly if the run produced no video, so a broken pipeline is never
   * mistaken for a slow one.
   */
  async scriptToVideo(
    rawScript: string,
    options: CreateRunOptions &
      StartRunOptions & {
        projectId?: string;
        projectTitle?: string;
        timeoutMs?: number;
        pollIntervalMs?: number;
        onProgress?: (run: Run) => void;
      } = {},
  ): Promise<FinalVideo> {
    const {
      projectId: givenProjectId,
      projectTitle,
      timeoutMs,
      pollIntervalMs,
      onProgress,
      ...rest
    } = options;
    let projectId = givenProjectId;
    if (!projectId) {
      const project = await this.createProject(projectTitle ?? "Script2Video run");
      projectId = project.projectId;
    }
    if (!projectId) {
      throw new Error("could not resolve a project id");
    }
    const run = await this.runScript(projectId, rawScript, {
      ...rest,
      wait: true,
      timeoutMs,
      pollIntervalMs,
      onProgress,
    });
    if (!run.runId) {
      throw new Error("server returned a run without a run_id");
    }
    return this.getFinalVideo(run.runId);
  }
}

function isRecord(value: unknown): value is JsonRecord {
  return typeof value === "object" && value !== null && !Array.isArray(value);
}

function parseJson(text: string): JsonRecord | undefined {
  if (!text) {
    return undefined;
  }
  try {
    const parsed: unknown = JSON.parse(text);
    return isRecord(parsed) ? parsed : undefined;
  } catch {
    return undefined;
  }
}

function randomKey(): string {
  const holder = globalThis as { crypto?: { randomUUID(): string } };
  if (holder.crypto?.randomUUID) {
    return holder.crypto.randomUUID();
  }
  return `${Date.now().toString(16)}${Math.random().toString(16).slice(2, 12)}`;
}

const RETRYABLE_LOOKUP: ReadonlySet<string> = new Set([
  "rate_limited",
  "internal_error",
  "bad_gateway",
  "service_unavailable",
  "idempotency_in_progress",
]);
