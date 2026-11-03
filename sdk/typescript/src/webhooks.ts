/**
 * Receiver-side verification for inbound webhook deliveries.
 *
 * A subscriber's first job is to trust nothing: check that the body it received
 * is the body the platform signed, and that the signature is fresh.
 *
 *     import { verifyWebhookSignature, SIGNATURE_HEADER } from "s2v-client";
 *
 *     const ok = verifyWebhookSignature(subscription.secret, {
 *       header: req.headers["x-s2v-signature"],
 *       body: rawRequestBody, // the raw bytes, before any JSON parsing
 *     });
 *     if (!ok) return res.status(403).end();
 *
 * Verification covers two failures at once. **Tampering**: the digest is an
 * HMAC-SHA256 over `"{t}." + body` keyed by the subscription's secret, so any
 * change to the body invalidates it. **Replay**: the timestamp `t` is inside
 * the signed string, so a receiver that rejects signatures older than
 * `toleranceSeconds` cannot be fed a captured delivery later.
 *
 * Deduplication is *not* part of verification. Delivery is at-least-once, so
 * the same event can legitimately arrive more than once; compare
 * `X-S2V-Delivery-Id` (or the body's `delivery_id`) against what you have
 * already processed.
 */

import { createHmac, timingSafeEqual } from "node:crypto";

/** Header carrying `t=<unix>,v1=<hex digest>`. One header rather than two so
 * the timestamp cannot be swapped independently of the digest it is bound to. */
export const SIGNATURE_HEADER = "X-S2V-Signature";

/** Set on every delivery; the stable key for receiver-side deduplication. */
export const DELIVERY_ID_HEADER = "X-S2V-Delivery-Id";

/** The originating event's id in the platform's event log. */
export const EVENT_ID_HEADER = "X-S2V-Event-Id";

/** The published event name (same vocabulary as `listWebhookEvents`). */
export const EVENT_TYPE_HEADER = "X-S2V-Event-Type";

/** 1-based attempt counter; a first delivery carries "1". */
export const ATTEMPT_HEADER = "X-S2V-Attempt";

/** How far a signature timestamp may be from the receiver's clock. Matches the
 * platform's own default. */
export const SIGNATURE_TOLERANCE_SECONDS = 300;

const SCHEME = "v1";

export function parseSignatureHeader(
  header: string | null | undefined,
): { timestamp: number; digest: string } | null {
  if (!header) return null;
  const fields = new Map<string, string>();
  for (const part of header.split(",")) {
    const index = part.indexOf("=");
    if (index <= 0) continue;
    const key = part.slice(0, index).trim();
    const value = part.slice(index + 1).trim();
    if (key && value) fields.set(key, value);
  }
  const timestampText = fields.get("t");
  const digest = fields.get(SCHEME);
  if (!timestampText || !digest) return null;
  const timestamp = Number(timestampText);
  if (!Number.isFinite(timestamp)) return null;
  return { timestamp, digest };
}

function signingString(timestamp: number, body: Uint8Array): Buffer {
  return Buffer.concat([Buffer.from(`${timestamp}.`, "utf8"), Buffer.from(body)]);
}

/** The `X-S2V-Signature` value the platform would send for this body. Exposed
 * so a receiver can build its own test fixtures without a live platform. */
export function computeSignature(
  secret: string,
  options: { timestamp: number; body: Uint8Array },
): string {
  const digest = createHmac("sha256", secret).update(signingString(options.timestamp, options.body)).digest("hex");
  return `t=${options.timestamp},${SCHEME}=${digest}`;
}

export interface VerifyOptions {
  /** The raw `X-S2V-Signature` header value from the delivery. */
  header: string | null | undefined;
  /** The raw request bytes, before any JSON parsing. */
  body: Uint8Array;
  /** How far the signature timestamp may be from the receiver's clock. */
  toleranceSeconds?: number;
  /** Freeze the clock in tests. Defaults to `Date.now()`. */
  now?: Date;
}

export function verifyWebhookSignature(secret: string, options: VerifyOptions): boolean {
  const parsed = parseSignatureHeader(options.header);
  if (parsed === null) return false;
  const now = options.now ?? new Date();
  const age = Math.abs(now.getTime() / 1000 - parsed.timestamp);
  if (age > (options.toleranceSeconds ?? SIGNATURE_TOLERANCE_SECONDS)) return false;
  const expected = createHmac("sha256", secret)
    .update(signingString(parsed.timestamp, options.body))
    .digest("hex");
  const a = Buffer.from(expected, "utf8");
  const b = Buffer.from(parsed.digest, "utf8");
  return a.length === b.length && timingSafeEqual(a, b);
}
