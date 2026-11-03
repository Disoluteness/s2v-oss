/**
 * Read-only views over the API's JSON objects.
 *
 * Every value the client returns is a `Model` subclass: the fields the API
 * documents are exposed as real getters with the right types, and anything the
 * server adds later is still reachable through `model.raw`. That combination is
 * deliberate — a client that predates a server-side addition keeps working, and
 * one that wants the new field does not need a client release to see it.
 */

import type { JsonRecord } from "./errors.js";

/** Statuses after which a run will not change again on its own. */
export const TERMINAL_RUN_STATUSES: ReadonlySet<string> = new Set([
  "completed",
  "failed",
  "cancelled",
]);

/** A run parked on a human: not finished, but nothing happens without one. */
export const BLOCKING_RUN_STATUSES: ReadonlySet<string> = new Set([
  "waiting_review",
  "paused",
]);

/** Asset types that count as a deliverable, most preferred first. */
export const FINAL_VIDEO_ASSET_TYPES: readonly string[] = ["rendered_video", "edited_video"];

/** A thin view over a JSON object, with typed accessors for known fields. */
export class Model {
  readonly raw: JsonRecord;

  constructor(data: JsonRecord | null | undefined) {
    this.raw = { ...(data ?? {}) };
  }

  get(key: string): unknown {
    return this.raw[key];
  }

  str(key: string): string | undefined {
    const value = this.raw[key];
    return typeof value === "string" ? value : undefined;
  }

  num(key: string): number | undefined {
    const value = this.raw[key];
    return typeof value === "number" ? value : undefined;
  }

  bool(key: string): boolean | undefined {
    const value = this.raw[key];
    return typeof value === "boolean" ? value : undefined;
  }

  list(key: string): JsonRecord[] {
    const value = this.raw[key];
    return Array.isArray(value) ? (value as JsonRecord[]) : [];
  }

  toJSON(): JsonRecord {
    return { ...this.raw };
  }
}

export class Project extends Model {
  get projectId(): string | undefined {
    return this.str("project_id");
  }
  get title(): string | undefined {
    return this.str("title");
  }
}

export class Stage extends Model {
  get stageKey(): string | undefined {
    return this.str("stage_key");
  }
  get status(): string | undefined {
    return this.str("status");
  }
  get requiresHumanReview(): boolean {
    return this.bool("requires_human_review") ?? false;
  }
}

export class Run extends Model {
  get runId(): string | undefined {
    return this.str("run_id");
  }
  get projectId(): string | undefined {
    return this.str("project_id");
  }
  get status(): string | undefined {
    return this.str("status");
  }
  get currentStageKey(): string | undefined {
    return this.str("current_stage_key");
  }
  get pipelineKey(): string | undefined {
    return this.str("pipeline_key");
  }
  get stages(): Stage[] {
    return this.list("stages").map((item) => new Stage(item));
  }
  get isTerminal(): boolean {
    const status = this.status;
    return status !== undefined && TERMINAL_RUN_STATUSES.has(status);
  }

  /** True when the run will not progress without review or a resume. */
  get isBlockedOnHuman(): boolean {
    const status = this.status;
    return status !== undefined && BLOCKING_RUN_STATUSES.has(status);
  }
}

export class Asset extends Model {
  get assetId(): string | undefined {
    return this.str("asset_id");
  }
  get runId(): string | undefined {
    return this.str("run_id");
  }
  get assetType(): string | undefined {
    return this.str("asset_type");
  }
  get mimeType(): string | undefined {
    return this.str("mime_type");
  }
  get storageUrl(): string | undefined {
    return this.str("storage_url");
  }
}

export class Artifact extends Model {
  get artifactId(): string | undefined {
    return this.str("artifact_id");
  }
  get artifactType(): string | undefined {
    return this.str("artifact_type");
  }
  get runId(): string | undefined {
    return this.str("run_id");
  }
  get payload(): JsonRecord {
    const value = this.raw["payload"];
    return typeof value === "object" && value !== null ? (value as JsonRecord) : {};
  }
}

export class Job extends Model {
  get jobId(): string | undefined {
    return this.str("job_id");
  }
  get runId(): string | undefined {
    return this.str("run_id");
  }
  get status(): string | undefined {
    return this.str("status");
  }
  get jobType(): string | undefined {
    return this.str("job_type");
  }
}

/**
 * The render output of a run, as reported by the run board.
 *
 * `downloadUrl` is a signed, time-limited link, so it is worth re-reading the
 * board rather than caching this object for long.
 */
export class FinalVideo extends Model {
  get assetId(): string | undefined {
    return this.str("asset_id");
  }
  get assetType(): string | undefined {
    return this.str("asset_type");
  }
  get mimeType(): string | undefined {
    return this.str("mime_type");
  }
  get downloadUrl(): string | undefined {
    return this.str("download_url");
  }
  /** Alias for `downloadUrl`; the link is the thing callers want. */
  get url(): string | undefined {
    return this.downloadUrl;
  }
  get durationMs(): number | undefined {
    return this.num("duration_ms");
  }
  get width(): number | undefined {
    return this.num("width");
  }
  get height(): number | undefined {
    return this.num("height");
  }
  get fileSize(): number | undefined {
    return this.num("file_size");
  }
}

export class Actor extends Model {
  get actorId(): string | undefined {
    return this.str("actor_id");
  }
  get source(): string | undefined {
    return this.str("source");
  }
  get permissions(): string[] {
    const value = this.raw["permissions"];
    return Array.isArray(value) ? value.map(String) : [];
  }
  can(permission: string): boolean {
    return this.permissions.includes(permission);
  }
}

export class ApiKey extends Model {
  get apiKeyId(): string | undefined {
    return this.str("api_key_id");
  }
  get name(): string | undefined {
    return this.str("name");
  }
  get keyPrefix(): string | undefined {
    return this.str("key_prefix");
  }
  get status(): string | undefined {
    return this.str("status");
  }
  get scopes(): string[] | null {
    const value = this.raw["scopes"];
    return Array.isArray(value) ? value.map(String) : null;
  }
  /** The credential itself — present only on the creation response. */
  get plaintext(): string | undefined {
    return this.str("api_key");
  }
}

/** A registered endpoint. `secret` is shown on every read on purpose: it
 * authenticates the platform *to* the receiver, so there is nothing to hide
 * from the person who just read it back. */
export class WebhookSubscription extends Model {
  get subscriptionId(): string | undefined {
    return this.str("subscription_id");
  }
  get name(): string | undefined {
    return this.str("name");
  }
  get url(): string | undefined {
    return this.str("url");
  }
  /** Key for `verifyWebhookSignature` on received deliveries. */
  get secret(): string | undefined {
    return this.str("secret");
  }
  get eventTypes(): string[] {
    const value = this.raw["event_types"];
    return Array.isArray(value) ? value.map(String) : [];
  }
  get status(): string | undefined {
    return this.str("status");
  }
  get consecutiveFailures(): number {
    return this.num("consecutive_failures") ?? 0;
  }
}

/** One queued or attempted delivery, for debugging and manual replay. */
export class WebhookDelivery extends Model {
  get deliveryId(): string | undefined {
    return this.str("delivery_id");
  }
  get subscriptionId(): string | undefined {
    return this.str("subscription_id");
  }
  get eventId(): string | undefined {
    return this.str("event_id");
  }
  get eventType(): string | undefined {
    return this.str("event_type");
  }
  get runId(): string | undefined {
    return this.str("run_id");
  }
  get status(): string | undefined {
    return this.str("status");
  }
  get attempt(): number {
    return this.num("attempt") ?? 0;
  }
  get maxAttempts(): number {
    return this.num("max_attempts") ?? 0;
  }
  get errorMessage(): string | undefined {
    return this.str("error_message");
  }
  get responseStatus(): number | undefined {
    return this.num("response_status");
  }
}

/**
 * A page of results, with items optionally wrapped in a model.
 *
 * The `model` argument converts each entry on the way in, so `page.items` holds
 * real objects (`ApiKey`, ...) rather than raw records while the envelope's own
 * fields stay untouched.
 */
export class Page<T = JsonRecord> extends Model {
  constructor(
    data: JsonRecord | null | undefined,
    model?: new (item: JsonRecord) => T,
  ) {
    super(data);
    if (model) {
      const raw = this.raw["items"];
      this.raw["items"] = (Array.isArray(raw) ? raw : []).map(
        (item) => new model(item as JsonRecord),
      );
    }
  }

  get items(): T[] {
    const value = this.raw["items"];
    return (Array.isArray(value) ? value : []) as T[];
  }

  get total(): number {
    return this.num("total") ?? 0;
  }

  get limit(): number {
    return this.num("limit") ?? 0;
  }

  get offset(): number {
    return this.num("offset") ?? 0;
  }

  get hasMore(): boolean {
    return this.offset + this.items.length < this.total;
  }

  [Symbol.iterator](): Iterator<T> {
    return this.items[Symbol.iterator]();
  }
}
