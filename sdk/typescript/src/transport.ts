/**
 * HTTP transport.
 *
 * The client talks to the API through this small interface rather than through
 * `fetch` directly, for two reasons:
 *
 *   - a caller (or a test) can swap in a pooled agent, a proxy, or an
 *     in-process shim without the client knowing;
 *   - the default implementation is the only place that knows about Node's
 *     `fetch`, so the client stays testable without a network.
 *
 * A transport performs one HTTP exchange and reports the status, the lower-cased
 * headers and the raw bytes. Bytes, not a string: the same client downloads
 * MP4s and parses JSON, and decoding a video as UTF-8 would corrupt it. Use
 * `bodyText` when the body is known to be text.
 *
 * A transport must **not** throw for a 4xx or 5xx — those are responses, and
 * interpreting them is the client's job. It signals a genuine connection failure
 * with `TransportError`.
 */

import { TransportError } from "./errors.js";

export interface TransportRequest {
  method: string;
  url: string;
  headers: Record<string, string>;
  body?: string;
  timeoutMs: number;
}

export interface TransportResponse {
  status: number;
  headers: Record<string, string>;
  body: Uint8Array;
}

export interface Transport {
  send(request: TransportRequest): Promise<TransportResponse>;
}

const decoder = new TextDecoder("utf-8");

/** Decode a response body as UTF-8, for the responses that are JSON or text. */
export function bodyText(response: TransportResponse): string {
  return decoder.decode(response.body);
}

/** Default transport, built on the global `fetch` (Node 18+). */
export class FetchTransport implements Transport {
  async send(request: TransportRequest): Promise<TransportResponse> {
    const controller = new AbortController();
    const timer = setTimeout(() => controller.abort(), request.timeoutMs);
    try {
      const response = await fetch(request.url, {
        method: request.method,
        headers: request.headers,
        body: request.body,
        signal: controller.signal,
        redirect: "follow",
      });
      const headers: Record<string, string> = {};
      response.headers.forEach((value, key) => {
        headers[key.toLowerCase()] = value;
      });
      return {
        status: response.status,
        headers,
        body: new Uint8Array(await response.arrayBuffer()),
      };
    } catch (error) {
      // An aborted request and a refused connection are the same thing to a
      // caller: the exchange never completed, so retrying may help.
      const message =
        error instanceof Error ? error.message : `request to ${request.url} failed`;
      throw new TransportError(`${request.method} ${request.url} failed: ${message}`, {
        method: request.method,
        path: request.url,
        cause: error,
      });
    } finally {
      clearTimeout(timer);
    }
  }
}
