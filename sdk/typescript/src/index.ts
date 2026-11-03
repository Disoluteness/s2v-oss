/**
 * Script2Video Studio TypeScript/Node SDK (V7.2).
 *
 * A dependency-free client for the platform's HTTP API. It covers the whole
 * script-to-video path::
 *
 *     import { Script2VideoClient } from "s2v-client";
 *
 *     const client = new Script2VideoClient({ baseUrl: "https://studio.internal", apiKey: "s2v_..." });
 *     const video = await client.scriptToVideo("把这段脚本做成 15 秒口播", { projectTitle: "新品预热" });
 *     console.log(video.url);
 *
 * — and the individual steps underneath it, so an internal platform can plug in
 * at whatever level it needs: create a run, upload the persona image, start it,
 * watch it, fetch the render, or retry a failed stage.
 */

export { Script2VideoClient } from "./client.js";
export type {
  ClientOptions,
  CreateRunOptions,
  RequestOptions,
  StartRunOptions,
  UploadAssetOptions,
  WaitRunOptions,
} from "./client.js";
export { DEFAULT_POLL_INTERVAL_MS, DEFAULT_WAIT_TIMEOUT_MS } from "./client.js";

export {
  ApiError,
  FinalVideoNotFound,
  RETRYABLE_CODES,
  RunWaitTimeout,
  Script2VideoError,
  TransportError,
} from "./errors.js";
export type { ApiErrorOptions, JsonRecord } from "./errors.js";

export {
  Actor,
  ApiKey,
  Artifact,
  Asset,
  BLOCKING_RUN_STATUSES,
  FINAL_VIDEO_ASSET_TYPES,
  FinalVideo,
  Job,
  Model,
  Page,
  Project,
  Run,
  Stage,
  TERMINAL_RUN_STATUSES,
  WebhookDelivery,
  WebhookSubscription,
} from "./models.js";

export { FetchTransport, bodyText } from "./transport.js";
export type { Transport, TransportRequest, TransportResponse } from "./transport.js";

export {
  ATTEMPT_HEADER,
  DELIVERY_ID_HEADER,
  EVENT_ID_HEADER,
  EVENT_TYPE_HEADER,
  SIGNATURE_HEADER,
  SIGNATURE_TOLERANCE_SECONDS,
  computeSignature,
  parseSignatureHeader,
  verifyWebhookSignature,
} from "./webhooks.js";

export const VERSION = "0.8.0";
