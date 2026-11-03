/**
 * Exceptions raised by the Script2Video client.
 *
 * The server answers every failure with one envelope (V7.1):
 *
 *     {"detail": {"code": "...", "message": "...", ...details}}
 *
 * `ApiError` is that envelope as an exception. `code` is the stable field to
 * branch on — it comes from the catalog the server publishes at
 * `GET /api/error-codes` — while `message` is for humans and may change.
 */

export type JsonRecord = Record<string, unknown>;

/** Codes where retrying the *identical* request can succeed, mirroring the
 * server's published catalog. */
export const RETRYABLE_CODES: ReadonlySet<string> = new Set([
  "rate_limited",
  "internal_error",
  "bad_gateway",
  "service_unavailable",
  "idempotency_in_progress",
]);

/** Base class for everything this client throws. */
export class Script2VideoError extends Error {
  constructor(message: string) {
    super(message);
    this.name = new.target.name;
  }
}

/**
 * The request never produced an HTTP response (DNS, refused, timeout).
 *
 * Kept distinct from `ApiError` on purpose: "the server said no" and "the server
 * was never reached" need different handling, and conflating them makes a retry
 * policy impossible to reason about.
 */
export class TransportError extends Script2VideoError {
  readonly method?: string;
  readonly path?: string;

  constructor(message: string, options: { method?: string; path?: string; cause?: unknown } = {}) {
    super(message);
    this.method = options.method;
    this.path = options.path;
    if (options.cause !== undefined) {
      (this as { cause?: unknown }).cause = options.cause;
    }
  }
}

export interface ApiErrorOptions {
  detail?: JsonRecord;
  method?: string;
  path?: string;
  retryable?: boolean;
  cause?: unknown;
}

/** A structured failure returned by the API. */
export class ApiError extends Script2VideoError {
  readonly statusCode: number;
  readonly code: string;
  readonly detail: JsonRecord;
  readonly method?: string;
  readonly path?: string;
  readonly retryable: boolean;

  constructor(statusCode: number, code: string, message: string, options: ApiErrorOptions = {}) {
    super(`HTTP ${statusCode} ${code}: ${message}`);
    this.statusCode = statusCode;
    this.code = code;
    this.detail = { ...(options.detail ?? {}) };
    this.method = options.method;
    this.path = options.path;
    this.retryable = options.retryable ?? RETRYABLE_CODES.has(code);
    if (options.cause !== undefined) {
      (this as { cause?: unknown }).cause = options.cause;
    }
    this.message = message;
  }

  /** Alias for `statusCode`; reads better at a glance. */
  get status(): number {
    return this.statusCode;
  }

  toJSON(): JsonRecord {
    return {
      statusCode: this.statusCode,
      code: this.code,
      message: this.message,
      ...this.detail,
    };
  }
}

/** The run has no render output yet — usually because it is not finished. */
export class FinalVideoNotFound extends Script2VideoError {}

/** `waitRun` gave up before the run reached a stopping state. */
export class RunWaitTimeout<T> extends Script2VideoError {
  readonly run: T;
  readonly timeout: number;

  constructor(run: T, timeout: number) {
    const status = (run as { status?: string } | null | undefined)?.status;
    super(`run did not settle within ${timeout}s (last status: ${status ?? "unknown"})`);
    this.run = run;
    this.timeout = timeout;
  }
}
