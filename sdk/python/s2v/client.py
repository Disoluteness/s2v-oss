"""The Script2Video Studio client.

Typical use::

    from s2v import Script2VideoClient

    client = Script2VideoClient("https://studio.internal", api_key="s2v_...")
    video = client.script_to_video("把这段脚本做成 15 秒口播", project_title="新品预热")
    print(video.url)

Everything is built on one HTTP call helper, so behaviour that must be uniform —
authentication, retry, idempotency, error decoding — exists once. The methods
above it are thin: they name the endpoint, shape the body and pick the model.

Two defaults are worth knowing about:

* **Mutating calls carry an ``Idempotency-Key``.** A retry after a timeout will
  not create a second run. Pass ``idempotency_key=...`` to control it per call
  or ``auto_idempotency=False`` on the client to opt out.
* **Retries are conservative.** Only a connection failure or a code the server
  itself marks retryable is retried, and only with the *same* idempotency key —
  a retry that could duplicate work is not a retry, it is a second request.
"""

from __future__ import annotations

import base64
import json
import mimetypes
import os
import time
from collections.abc import Callable, Iterable, Mapping
from pathlib import Path
from typing import Any
from uuid import uuid4

from s2v.errors import ApiError, FinalVideoNotFound, RunWaitTimeout, TransportError
from s2v.models import (
    BLOCKING_RUN_STATUSES,
    FINAL_VIDEO_ASSET_TYPES,
    TERMINAL_RUN_STATUSES,
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
)
from s2v.transport import Response, Transport, UrllibTransport

Json = dict[str, Any]

MUTATING_METHODS = frozenset({"POST", "PUT", "PATCH", "DELETE"})
DEFAULT_POLL_INTERVAL = 3.0
DEFAULT_WAIT_TIMEOUT = 900.0


def _clean_params(params: Mapping[str, Any] | None) -> list[tuple[str, str]]:
    """Drop ``None`` and stringify the rest, so ``False`` is not lost to falsiness."""
    if not params:
        return []
    cleaned: list[tuple[str, str]] = []
    for key, value in params.items():
        if value is None:
            continue
        if isinstance(value, bool):
            cleaned.append((key, "true" if value else "false"))
        elif isinstance(value, (list, tuple)):
            cleaned.extend((key, str(item)) for item in value)
        else:
            cleaned.append((key, str(value)))
    return cleaned


def _encode_query(params: Iterable[tuple[str, str]]) -> str:
    from urllib.parse import urlencode

    pairs = list(params)
    return urlencode(pairs) if pairs else ""


class Script2VideoClient:
    """A client for one Script2Video Studio deployment."""

    def __init__(
        self,
        base_url: str | None = None,
        api_key: str | None = None,
        *,
        actor_id: str | None = None,
        actor_secret: str | None = None,
        timeout: float = 30.0,
        max_retries: int = 2,
        backoff_factor: float = 0.5,
        transport: Transport | None = None,
        default_headers: Mapping[str, str] | None = None,
        auto_idempotency: bool = True,
        user_agent: str = "s2v-python/0.8.0",
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        """Build a client.

        ``base_url`` / ``api_key`` / ``actor_id`` fall back to ``SCRIPT2VIDEO_BASE_URL``,
        ``SCRIPT2VIDEO_API_KEY`` and ``SCRIPT2VIDEO_ACTOR_ID``, so a deployment can
        configure its callers through the environment without code changes.
        """
        self.base_url = (
            base_url
            or os.environ.get("SCRIPT2VIDEO_BASE_URL")
            or "http://127.0.0.1:8080"
        ).rstrip("/")
        self.api_key = api_key or os.environ.get("SCRIPT2VIDEO_API_KEY")
        self.actor_id = actor_id or os.environ.get("SCRIPT2VIDEO_ACTOR_ID")
        self.actor_secret = actor_secret or os.environ.get("SCRIPT2VIDEO_ACTOR_SECRET")
        self.timeout = timeout
        self.max_retries = max(0, max_retries)
        self.backoff_factor = max(0.0, backoff_factor)
        self.auto_idempotency = auto_idempotency
        self.transport: Transport = transport or UrllibTransport()
        self.default_headers = dict(default_headers or {})
        self.user_agent = user_agent
        self._sleep = sleep

    # -- plumbing ----------------------------------------------------------

    def _headers(
        self,
        *,
        has_body: bool,
        extra: Mapping[str, str] | None = None,
    ) -> dict[str, str]:
        headers: dict[str, str] = {
            "Accept": "application/json",
            "User-Agent": self.user_agent,
        }
        if has_body:
            headers["Content-Type"] = "application/json"
        if self.api_key:
            headers["Authorization"] = f"Bearer {self.api_key}"
        elif self.actor_id:
            # The V6.5 trusted-header identity: the fallback when no key is held.
            headers["X-Actor-Id"] = self.actor_id
        if self.actor_secret:
            headers["X-Actor-Secret"] = self.actor_secret
        headers.update(self.default_headers)
        headers.update(extra or {})
        return headers

    def _decode(self, response: Response, *, method: str, path: str) -> Any:
        if response.status >= 400:
            raise self._api_error(response, method=method, path=path)
        if not response.body:
            return None
        try:
            return json.loads(response.body.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            return response.body

    def _api_error(self, response: Response, *, method: str, path: str) -> ApiError:
        detail: Json = {}
        try:
            payload = json.loads(response.body.decode("utf-8")) if response.body else None
        except (UnicodeDecodeError, json.JSONDecodeError):
            payload = None
        if isinstance(payload, dict):
            raw_detail = payload.get("detail")
            if isinstance(raw_detail, dict):
                detail = raw_detail
            elif raw_detail is not None:
                detail = {"message": str(raw_detail)}
        code = str(detail.get("code") or _fallback_code(response.status))
        message = str(detail.get("message") or response.body.decode("utf-8", "replace"))
        return ApiError(
            response.status,
            code,
            message,
            detail=detail,
            method=method,
            path=path,
        )

    def _request_raw(
        self,
        method: str,
        path: str,
        *,
        body: bytes | None = None,
        json_body: Json | None = None,
        params: Mapping[str, Any] | None = None,
        headers: Mapping[str, str] | None = None,
        idempotency_key: str | None | bool = None,
    ) -> Response:
        method = method.upper()
        payload = body
        if json_body is not None:
            payload = json.dumps(json_body).encode("utf-8")

        query = _encode_query(_clean_params(params))
        url = f"{self.base_url}{path}" + (f"?{query}" if query else "")

        sent = self._headers(has_body=payload is not None, extra=headers)
        if method in MUTATING_METHODS:
            key = self._idempotency_key(idempotency_key)
            if key:
                sent["Idempotency-Key"] = key

        attempt = 0
        while True:
            try:
                response = self.transport.send(method, url, sent, payload, self.timeout)
            except TransportError:
                if attempt >= self.max_retries:
                    raise
                self._backoff(attempt, None)
                attempt += 1
                continue
            if response.status < 400 or attempt >= self.max_retries:
                return response
            if not self._is_retryable(response):
                return response
            self._backoff(attempt, response.header("retry-after"))
            attempt += 1

    def _idempotency_key(self, requested: str | None | bool) -> str | None:
        if requested is False:
            return None
        if isinstance(requested, str):
            return requested
        return uuid4().hex if self.auto_idempotency else None

    def _is_retryable(self, response: Response) -> bool:
        if response.status == 429 or response.status >= 500:
            return True
        try:
            payload = json.loads(response.body.decode("utf-8"))
            code = (payload.get("detail") or {}).get("code")
        except (UnicodeDecodeError, json.JSONDecodeError, AttributeError):
            return False
        from s2v.errors import RETRYABLE_CODES

        return bool(code) and code in RETRYABLE_CODES

    def _backoff(self, attempt: int, retry_after: str | None) -> None:
        if retry_after:
            try:
                self._sleep(max(0.0, float(retry_after)))
                return
            except ValueError:
                pass
        self._sleep(self.backoff_factor * (2**attempt))

    def request(
        self,
        method: str,
        path: str,
        *,
        json_body: Json | None = None,
        params: Mapping[str, Any] | None = None,
        headers: Mapping[str, str] | None = None,
        idempotency_key: str | None | bool = None,
    ) -> Any:
        """Call an endpoint that is not wrapped by a named method."""
        response = self._request_raw(
            method,
            path,
            json_body=json_body,
            params=params,
            headers=headers,
            idempotency_key=idempotency_key,
        )
        return self._decode(response, method=method, path=path)

    # -- meta --------------------------------------------------------------

    def health(self) -> Json:
        return self.request("GET", "/api/health") or {}

    def me(self) -> Actor:
        """Who the server thinks this client is, and what it may do."""
        return Actor(self.request("GET", "/api/me") or {})

    def error_codes(self) -> list[Json]:
        """The published failure catalog — useful to validate a branch table."""
        return list(self.request("GET", "/api/error-codes") or [])

    # -- projects ----------------------------------------------------------

    def create_project(
        self,
        title: str,
        *,
        description: str | None = None,
        default_language: str | None = None,
        default_aspect_ratio: str | None = None,
        idempotency_key: str | None | bool = None,
    ) -> Project:
        body: Json = {"title": title}
        if description is not None:
            body["description"] = description
        if default_language is not None:
            body["default_language"] = default_language
        if default_aspect_ratio is not None:
            body["default_aspect_ratio"] = default_aspect_ratio
        return Project(
            self.request(
                "POST",
                "/api/projects",
                json_body=body,
                idempotency_key=idempotency_key,
            )
        )

    def get_project(self, project_id: str) -> Project:
        return Project(self.request("GET", f"/api/projects/{project_id}"))

    def list_projects(self) -> list[Project]:
        return [Project(item) for item in self.request("GET", "/api/projects") or []]

    # -- runs --------------------------------------------------------------

    def create_run(
        self,
        project_id: str,
        raw_script: str,
        *,
        pipeline_key: str | None = None,
        brand_name: str | None = None,
        product_name: str | None = None,
        target_platform: str | None = None,
        target_duration_seconds: int | None = None,
        aspect_ratio: str | None = None,
        style_key: str | None = None,
        cost_budget: float | None = None,
        quality_mode: str | None = None,
        idempotency_key: str | None | bool = None,
        **extra: Any,
    ) -> Run:
        """Create a run. Pass ``**extra`` for fields this client does not know yet."""
        body: Json = {"raw_script": raw_script}
        for key, value in (
            ("pipeline_key", pipeline_key),
            ("brand_name", brand_name),
            ("product_name", product_name),
            ("target_platform", target_platform),
            ("target_duration_seconds", target_duration_seconds),
            ("aspect_ratio", aspect_ratio),
            ("style_key", style_key),
            ("cost_budget", cost_budget),
            ("quality_mode", quality_mode),
        ):
            if value is not None:
                body[key] = value
        body.update(extra)
        return Run(
            self.request(
                "POST",
                f"/api/projects/{project_id}/runs",
                json_body=body,
                idempotency_key=idempotency_key,
            )
        )

    def get_run(self, run_id: str) -> Run:
        return Run(self.request("GET", f"/api/runs/{run_id}"))

    def list_project_runs(self, project_id: str) -> list[Run]:
        return [
            Run(item)
            for item in self.request("GET", f"/api/projects/{project_id}/runs") or []
        ]

    def get_run_board(self, run_id: str) -> Model:
        """The full run view: stages, artifacts, jobs, cost, review state."""
        return Model(self.request("GET", f"/api/runs/{run_id}/board") or {})

    def list_run_artifacts(self, run_id: str, *, current_only: bool = False) -> list[Artifact]:
        return [
            Artifact(item)
            for item in self.request(
                "GET",
                f"/api/runs/{run_id}/artifacts",
                params={"current_only": current_only} if current_only else None,
            )
            or []
        ]

    def list_run_jobs(self, run_id: str) -> list[Job]:
        return [Job(item) for item in self.request("GET", f"/api/runs/{run_id}/jobs") or []]

    def list_run_events(self, run_id: str, *, limit: int | None = None) -> list[Json]:
        return list(
            self.request(
                "GET",
                f"/api/runs/{run_id}/events",
                params={"limit": limit},
            )
            or []
        )

    def submit_review_decision(
        self,
        review_request_id: str,
        decision: str,
        *,
        comment: str | None = None,
        reason_code: str | None = None,
        actor_user_id: str | None = None,
        actor_display_name: str | None = None,
    ) -> Json:
        body: Json = {"decision": decision}
        for key, value in (
            ("comment", comment),
            ("reason_code", reason_code),
            ("actor_user_id", actor_user_id),
            ("actor_display_name", actor_display_name),
        ):
            if value is not None:
                body[key] = value
        return (
            self.request(
                "POST",
                f"/api/review-requests/{review_request_id}/decisions",
                json_body=body,
            )
            or {}
        )

    # -- assets ------------------------------------------------------------

    def upload_asset(
        self,
        run_id: str,
        *,
        asset_type: str,
        filename: str | None = None,
        content: bytes | None = None,
        path: str | Path | None = None,
        mime_type: str | None = None,
        idempotency_key: str | None | bool = None,
        **extra: Any,
    ) -> Asset:
        """Upload bytes to a run.

        Pass exactly one of ``content`` (bytes) or ``path`` (a local file). The
        body is base64 inside JSON because that is the API's upload contract — no
        multipart, so the same call works from any language's stock HTTP stack.
        """
        if (content is None) == (path is None):
            raise ValueError("pass exactly one of 'content' or 'path'")
        if path is not None:
            resolved = Path(path)
            content = resolved.read_bytes()
            filename = filename or resolved.name
            mime_type = mime_type or mimetypes.guess_type(resolved.name)[0]
        assert content is not None
        filename = filename or "upload.bin"
        body: Json = {
            "asset_type": asset_type,
            "filename": filename,
            "content_base64": base64.b64encode(content).decode("ascii"),
        }
        if mime_type is not None:
            body["mime_type"] = mime_type
        body.update(extra)
        return Asset(
            self.request(
                "POST",
                f"/api/runs/{run_id}/assets",
                json_body=body,
                idempotency_key=idempotency_key,
            )
        )

    def upload_person_image(
        self,
        run_id: str,
        *,
        content: bytes | None = None,
        path: str | Path | None = None,
        filename: str | None = None,
        **extra: Any,
    ) -> Asset:
        """Upload the persona image the avatar stage composites onto."""
        return self.upload_asset(
            run_id,
            asset_type="person_image",
            content=content,
            path=path,
            filename=filename,
            **extra,
        )

    def list_run_assets(self, run_id: str) -> list[Asset]:
        return [Asset(item) for item in self.request("GET", f"/api/runs/{run_id}/assets") or []]

    def get_asset(self, asset_id: str) -> Asset:
        return Asset(self.request("GET", f"/api/assets/{asset_id}"))

    def download_asset(self, asset_id: str) -> bytes:
        """Fetch an asset's bytes."""
        response = self._request_raw("GET", f"/api/assets/{asset_id}/download")
        if response.status >= 400:
            raise self._api_error(
                response,
                method="GET",
                path=f"/api/assets/{asset_id}/download",
            )
        return response.body

    # -- lifecycle ---------------------------------------------------------

    def confirm_paid_provider(
        self,
        run_id: str,
        provider: str,
        *,
        accept: bool = True,
        note: str | None = None,
    ) -> Json:
        """Acknowledge the first paid call to a provider on this run (T76)."""
        body: Json = {"provider": provider, "accept": accept}
        if note is not None:
            body["note"] = note
        return self.request("POST", f"/api/runs/{run_id}/provider-consents", json_body=body) or {}

    def start_run(
        self,
        run_id: str,
        *,
        accept_intent_downgrade: bool = False,
        accept_paid_providers: bool = False,
    ) -> Run:
        """Start a run.

        ``accept_paid_providers`` clears the T76 first-use gate for every provider
        the run is waiting on, so a batch job does not have to replay the gate
        logic itself. It does *not* bypass the budget gate — a run over budget is
        still refused, and should be.
        """
        if accept_paid_providers:
            board = self.get_run_board(run_id)
            for provider in board.get("paid_providers_pending") or []:
                self.confirm_paid_provider(run_id, provider)
        return Run(
            self.request(
                "POST",
                f"/api/runs/{run_id}/start",
                json_body={"accept_intent_downgrade": accept_intent_downgrade},
            )
        )

    def wait_run(
        self,
        run_id: str,
        *,
        timeout: float = DEFAULT_WAIT_TIMEOUT,
        poll_interval: float = DEFAULT_POLL_INTERVAL,
        stop_on_review: bool = True,
        raise_on_timeout: bool = False,
        on_progress: Callable[[Run], None] | None = None,
    ) -> Run:
        """Poll until the run settles, then return it.

        Stops at a terminal state (completed / failed / cancelled) and, unless
        ``stop_on_review`` is off, at a human gate (waiting_review / paused) —
        waiting for a person is not a thing polling can fix. A run that *failed*
        is returned, not raised: the caller decides whether a failure is fatal,
        and the run object carries the evidence.
        """
        deadline = time.monotonic() + timeout
        run = self.get_run(run_id)
        while True:
            if on_progress is not None:
                on_progress(run)
            settled = run.status in TERMINAL_RUN_STATUSES or (
                stop_on_review and run.status in BLOCKING_RUN_STATUSES
            )
            if settled:
                return run
            if time.monotonic() >= deadline:
                if raise_on_timeout:
                    raise RunWaitTimeout(run, timeout=timeout)
                return run
            self._sleep(poll_interval)
            run = self.get_run(run_id)

    def get_final_video(self, run_id: str) -> FinalVideo:
        """The run's render output.

        Read from the run board's ``final_outputs``, preferring a rendered master
        over an edited cut. Raises :class:`FinalVideoNotFound` when the run has
        produced nothing yet — which usually just means it has not finished.
        """
        board = self.get_run_board(run_id)
        outputs = [FinalVideo(item) for item in board.get("final_outputs") or []]
        for asset_type in FINAL_VIDEO_ASSET_TYPES:
            for output in outputs:
                if output.asset_type == asset_type:
                    return output
        for output in outputs:
            if (output.mime_type or "").startswith("video/"):
                return output
        raise FinalVideoNotFound(
            f"run {run_id} has no final video yet (status: {board.get('run', {}).get('status')})"
        )

    # -- actions -----------------------------------------------------------

    def retry_action(
        self,
        run_id: str,
        action: str,
        *,
        stage_key: str | None = None,
        stage_run_id: str | None = None,
        manifest_step_key: str | None = None,
        job_id: str | None = None,
        payload: Json | None = None,
    ) -> Json:
        """Run a manifest action — the same repair verbs the Web board offers.

        ``action`` is one of ``dispatch_ready_steps``, ``generate_person_image``,
        ``upload_person_image``, ``generate_tts``, ``analyze_source_media``,
        ``process_job``, ``retry_job``.
        """
        body: Json = {"action": action}
        for key, value in (
            ("stage_key", stage_key),
            ("stage_run_id", stage_run_id),
            ("manifest_step_key", manifest_step_key),
            ("job_id", job_id),
        ):
            if value is not None:
                body[key] = value
        if payload:
            body["payload"] = payload
        return self.request("POST", f"/api/runs/{run_id}/manifest-actions", json_body=body) or {}

    def retry_job(self, job_id: str) -> Json:
        """Retry one background job."""
        return self.request("POST", f"/api/jobs/{job_id}/retry") or {}

    def retry_failed_jobs(self, run_id: str) -> list[Json]:
        """Retry every failed job on a run, oldest first.

        Reads the job list rather than guessing: a run can fail several jobs, and
        retrying them one at a time is what the Web board does.
        """
        failed = [job for job in self.list_run_jobs(run_id) if job.status == "failed"]
        results: list[Json] = []
        for job in failed:
            if job.job_id:
                results.append(self.retry_job(job.job_id))
        return results

    def run_state_action(
        self,
        run_id: str,
        action: str,
        *,
        stage_key: str | None = None,
        artifact_type: str | None = None,
        reason: str | None = None,
        payload: Json | None = None,
    ) -> Json:
        """Pause, resume, cancel, or rerun a stage / artifact."""
        body: Json = {"action": action}
        for key, value in (
            ("stage_key", stage_key),
            ("artifact_type", artifact_type),
            ("reason", reason),
        ):
            if value is not None:
                body[key] = value
        if payload:
            body["payload"] = payload
        return self.request("POST", f"/api/runs/{run_id}/state-actions", json_body=body) or {}

    # -- credentials -------------------------------------------------------

    def create_api_key(
        self,
        name: str,
        *,
        scopes: list[str] | None = None,
        expires_in_days: int | None = None,
    ) -> ApiKey:
        """Issue a credential. Requires ``apikey:manage`` (owner or admin).

        The returned object's ``plaintext`` is the only copy that will ever
        exist — store it, because it cannot be read back.
        """
        body: Json = {"name": name}
        if scopes is not None:
            body["scopes"] = scopes
        if expires_in_days is not None:
            body["expires_in_days"] = expires_in_days
        return ApiKey(self.request("POST", "/api/api-keys", json_body=body))

    def list_api_keys(self, *, status: str | None = None, limit: int = 50, offset: int = 0) -> Page:
        return Page(
            self.request(
                "GET",
                "/api/api-keys",
                params={"status": status, "limit": limit, "offset": offset},
            )
            or {},
            model=ApiKey,
        )

    def revoke_api_key(self, api_key_id: str) -> ApiKey:
        return ApiKey(self.request("POST", f"/api/api-keys/{api_key_id}/revoke"))

    # -- webhooks ----------------------------------------------------------

    def list_webhook_events(self) -> list[Model]:
        """The published event vocabulary, from the open catalog endpoint."""
        data = self.request("GET", "/api/webhook-events") or []
        return [Model(entry) for entry in data]

    def create_webhook_subscription(
        self,
        name: str,
        url: str,
        event_types: list[str],
        *,
        description: str | None = None,
        status: str = "active",
    ) -> WebhookSubscription:
        """Register an endpoint. Requires ``webhook:manage`` (owner or admin).

        Event names are validated against the catalog server-side — an unknown
        one is refused rather than stored, so a typo fails at registration and
        not three weeks later when nothing has arrived.
        """
        body: Json = {"name": name, "url": url, "event_types": event_types, "status": status}
        if description is not None:
            body["description"] = description
        return WebhookSubscription(self.request("POST", "/api/webhooks", json_body=body))

    def list_webhook_subscriptions(
        self,
        *,
        status: str | None = None,
        limit: int = 50,
        offset: int = 0,
    ) -> Page:
        return Page(
            self.request(
                "GET",
                "/api/webhooks",
                params={"status": status, "limit": limit, "offset": offset},
            )
            or {},
            model=WebhookSubscription,
        )

    def get_webhook_subscription(self, subscription_id: str) -> WebhookSubscription:
        return WebhookSubscription(
            self.request("GET", f"/api/webhooks/{subscription_id}") or {}
        )

    def update_webhook_subscription(
        self,
        subscription_id: str,
        *,
        name: str | None = None,
        url: str | None = None,
        event_types: list[str] | None = None,
        description: str | None = None,
        status: str | None = None,
    ) -> WebhookSubscription:
        """Partial update: omitted fields are left alone.

        Re-activating a disabled subscription (``status="active"``) also resets
        the server-side consecutive-failure count, so a fixed endpoint is not
        re-disabled by its history.
        """
        body: Json = {}
        if name is not None:
            body["name"] = name
        if url is not None:
            body["url"] = url
        if event_types is not None:
            body["event_types"] = event_types
        if description is not None:
            body["description"] = description
        if status is not None:
            body["status"] = status
        return WebhookSubscription(
            self.request("PATCH", f"/api/webhooks/{subscription_id}", json_body=body) or {}
        )

    def delete_webhook_subscription(self, subscription_id: str) -> WebhookSubscription:
        """Delete a subscription and its queued deliveries. The event log keeps everything."""
        return WebhookSubscription(
            self.request("DELETE", f"/api/webhooks/{subscription_id}") or {}
        )

    def rotate_webhook_secret(self, subscription_id: str) -> WebhookSubscription:
        """Replace the signing secret. In-flight attempts may carry the old one;
        accept both during the handover window."""
        return WebhookSubscription(
            self.request("POST", f"/api/webhooks/{subscription_id}/rotate-secret") or {}
        )

    def list_webhook_deliveries(
        self,
        subscription_id: str,
        *,
        status: str | None = None,
        limit: int = 50,
        offset: int = 0,
    ) -> Page:
        """The subscription's outbox: every attempt, its response, and its fate.

        ``status`` filters on ``pending`` / ``delivering`` / ``succeeded`` /
        ``failed`` / ``abandoned`` — the last one means attempts ran out and a
        manual :meth:`requeue_webhook_delivery` is the only way it arrives.
        """
        return Page(
            self.request(
                "GET",
                f"/api/webhooks/{subscription_id}/deliveries",
                params={"status": status, "limit": limit, "offset": offset},
            )
            or {},
            model=WebhookDelivery,
        )

    def requeue_webhook_delivery(self, delivery_id: str) -> WebhookDelivery:
        """Put a delivery back in the queue with a fresh attempt count."""
        return WebhookDelivery(
            self.request("POST", f"/api/webhook-deliveries/{delivery_id}/requeue") or {}
        )

    # -- convenience -------------------------------------------------------

    def run_script(
        self,
        project_id: str,
        raw_script: str,
        *,
        wait: bool = True,
        timeout: float = DEFAULT_WAIT_TIMEOUT,
        poll_interval: float = DEFAULT_POLL_INTERVAL,
        accept_intent_downgrade: bool = False,
        accept_paid_providers: bool = True,
        on_progress: Callable[[Run], None] | None = None,
        **options: Any,
    ) -> Run:
        """Create a run and start it — the two calls that always go together."""
        run = self.create_run(project_id, raw_script, **options)
        assert run.run_id is not None
        started = self.start_run(
            run.run_id,
            accept_intent_downgrade=accept_intent_downgrade,
            accept_paid_providers=accept_paid_providers,
        )
        if not wait:
            return started
        return self.wait_run(
            run.run_id,
            timeout=timeout,
            poll_interval=poll_interval,
            on_progress=on_progress,
        )

    def script_to_video(
        self,
        raw_script: str,
        *,
        project_id: str | None = None,
        project_title: str | None = None,
        timeout: float = DEFAULT_WAIT_TIMEOUT,
        poll_interval: float = DEFAULT_POLL_INTERVAL,
        accept_intent_downgrade: bool = False,
        accept_paid_providers: bool = True,
        on_progress: Callable[[Run], None] | None = None,
        **options: Any,
    ) -> FinalVideo:
        """Script in, final video out — the whole job in one call.

        Creates the project when ``project_id`` is omitted (titled from
        ``project_title``), creates the run, starts it, waits, and returns the
        render. Fails loudly if the run produced no video, so a broken pipeline
        is never mistaken for a slow one.
        """
        if project_id is None:
            project = self.create_project(project_title or "Script2Video run")
            project_id = project.project_id
        assert project_id is not None
        run = self.run_script(
            project_id,
            raw_script,
            wait=True,
            timeout=timeout,
            poll_interval=poll_interval,
            accept_intent_downgrade=accept_intent_downgrade,
            accept_paid_providers=accept_paid_providers,
            on_progress=on_progress,
            **options,
        )
        assert run.run_id is not None
        return self.get_final_video(run.run_id)


def _fallback_code(status: int) -> str:
    """Mirror the server's status → code fallback for a body we could not parse."""
    if status == 401:
        return "unauthorized"
    if status == 403:
        return "permission_denied"
    if status == 404:
        return "not_found"
    if status == 405:
        return "method_not_allowed"
    if status == 409:
        return "conflict"
    if status == 422:
        return "validation_error"
    if status == 429:
        return "rate_limited"
    if status >= 500:
        return "internal_error"
    return "bad_request"
