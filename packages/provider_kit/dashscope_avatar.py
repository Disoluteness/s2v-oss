from __future__ import annotations

import json
import os
import urllib.error
import urllib.request
from collections.abc import Mapping
from pathlib import Path
from typing import Any, Protocol

from dotenv import dotenv_values

from packages.tool_kit.contracts import ToolExecutionResult

DEFAULT_DASHSCOPE_API_BASE_URL = "https://dashscope.aliyuncs.com"
# ``emo-v1`` (悦动人像 EMO) is no longer served by Bailian; ``wan2.2-s2v``
# (万相-数字人) is the replacement for audio-driven talking-head video. The two
# speak different request dialects, so the adapter picks one from the model name.
DEFAULT_DASHSCOPE_AVATAR_MODEL = "wan2.2-s2v"
DEFAULT_DASHSCOPE_AVATAR_DETECT_MODEL = "emo-detect-v1"
DEFAULT_DASHSCOPE_AVATAR_RATIO = "3:4"
DEFAULT_DASHSCOPE_AVATAR_RESOLUTION = "480P"
EMO_PROTOCOL = "emo"
S2V_PROTOCOL = "s2v"
# wan2.2-s2v rejects audio longer than this (both duration and the 15 MB cap are
# enforced provider-side; the duration is the one a long script blows through).
S2V_MAX_AUDIO_SECONDS = 20


def avatar_protocol(model: str | None) -> str:
    """Map a model name to the request dialect it speaks.

    EMO needs a face/ext bbox produced by a separate face-detect call, and takes
    ``style_level``. wan2.2-s2v takes the raw image and infers the crop itself,
    and takes ``resolution`` instead. Any ``emo*`` name stays on the legacy
    dialect so a deployment that pinned EMO keeps working if the model returns.
    """

    value = (model or "").strip().lower()
    return EMO_PROTOCOL if value.startswith(EMO_PROTOCOL) else S2V_PROTOCOL


class HttpJsonTransport(Protocol):
    def request_json(
        self,
        *,
        method: str,
        url: str,
        headers: dict[str, str],
        payload: dict[str, Any] | None = None,
        timeout_seconds: int = 60,
    ) -> dict[str, Any]:
        ...


class UrllibJsonTransport:
    def request_json(
        self,
        *,
        method: str,
        url: str,
        headers: dict[str, str],
        payload: dict[str, Any] | None = None,
        timeout_seconds: int = 60,
    ) -> dict[str, Any]:
        data = None if payload is None else json.dumps(payload, ensure_ascii=False).encode("utf-8")
        request = urllib.request.Request(url, data=data, headers=headers, method=method)
        try:
            with urllib.request.urlopen(request, timeout=timeout_seconds) as response:
                raw = response.read().decode("utf-8")
        except urllib.error.HTTPError as exc:
            error_body = exc.read().decode("utf-8", errors="replace")
            raise RuntimeError(f"DashScope HTTP {exc.code}: {error_body}") from exc
        except urllib.error.URLError as exc:
            raise RuntimeError(f"DashScope request failed: {exc}") from exc
        return json.loads(raw) if raw else {}


class DashScopeAvatarVideoAdapter:
    provider_key = "dashscope"
    tool_key = "dashscope_avatar_video"
    capability = "avatar_video"

    def __init__(
        self,
        *,
        api_key: str | None = None,
        base_url: str | None = None,
        model: str | None = None,
        detect_model: str | None = None,
        ratio: str | None = None,
        resolution: str | None = None,
        transport: HttpJsonTransport | None = None,
        timeout_seconds: int = 60,
    ) -> None:
        env = _load_environment()
        self.api_key = api_key or env.get("DASHSCOPE_API_KEY")
        self.base_url = (base_url or env.get("DASHSCOPE_API_BASE_URL") or DEFAULT_DASHSCOPE_API_BASE_URL).rstrip("/")
        self.model = model or env.get("DASHSCOPE_AVATAR_MODEL") or DEFAULT_DASHSCOPE_AVATAR_MODEL
        self.detect_model = (
            detect_model
            or env.get("DASHSCOPE_AVATAR_DETECT_MODEL")
            or DEFAULT_DASHSCOPE_AVATAR_DETECT_MODEL
        )
        self.ratio = ratio or env.get("DASHSCOPE_AVATAR_RATIO") or DEFAULT_DASHSCOPE_AVATAR_RATIO
        self.resolution = (
            resolution
            or env.get("DASHSCOPE_AVATAR_RESOLUTION")
            or DEFAULT_DASHSCOPE_AVATAR_RESOLUTION
        )
        self.transport = transport or UrllibJsonTransport()
        self.timeout_seconds = timeout_seconds

    def get_capabilities(self) -> list[str]:
        return [self.capability]

    def check_availability(self) -> bool:
        return bool(self.api_key)

    def requires_bbox(self, model: str | None = None) -> bool:
        """Whether the given model needs a face/ext bbox before it can be called.

        Only the EMO dialect does — and only EMO exposes the face-detect call
        that produces one. Callers use this to skip the detection hop entirely on
        s2v models instead of paying for a round trip that has no consumer.
        """

        return avatar_protocol(model or self.model) == EMO_PROTOCOL

    def estimate_cost(self, capability: str, payload: dict[str, Any]) -> float:
        self.validate_input(capability, payload)
        return 0.0

    def validate_input(self, capability: str, payload: dict[str, Any]) -> None:
        if capability != self.capability:
            raise ValueError(f"unsupported capability: {capability}")
        required = ["image_url", "audio_url"]
        missing = [key for key in required if key not in payload or payload[key] in {None, ""}]
        if missing:
            raise ValueError("missing required avatar input: " + ", ".join(missing))
        # A bbox is optional input: only the EMO dialect consumes one, and even
        # there it can be filled in by ``detect_bbox`` during submit. Validate it
        # only when supplied, so s2v callers are free to omit it.
        for key in ["face_bbox", "ext_bbox"]:
            if payload.get(key) is None:
                continue
            value = payload[key]
            if not isinstance(value, list) or len(value) != 4 or not all(isinstance(item, int) for item in value):
                raise ValueError(f"{key} must be a list of four integers")

    def execute(self, capability: str, payload: dict[str, Any]) -> ToolExecutionResult:
        self.validate_input(capability, payload)
        response = self.submit(payload)
        normalized = self.normalize_output(capability, response)
        return ToolExecutionResult(
            success=True,
            provider=self.provider_key,
            tool_key=self.tool_key,
            external_task_id=normalized.get("external_task_id"),
            raw_response_ref=normalized.get("raw_response_ref"),
        )

    def submit(self, payload: dict[str, Any]) -> dict[str, Any]:
        if not self.api_key:
            raise RuntimeError("DASHSCOPE_API_KEY is required for DashScope avatar video")
        if self.requires_bbox(payload.get("model")):
            missing_bbox = [key for key in ["face_bbox", "ext_bbox"] if not payload.get(key)]
            if missing_bbox:
                raise ValueError("missing required avatar input: " + ", ".join(missing_bbox))
        body = self._submit_body(payload)
        return self.transport.request_json(
            method="POST",
            url=f"{self.base_url}/api/v1/services/aigc/image2video/video-synthesis",
            headers={
                "Authorization": f"Bearer {self.api_key}",
                "Content-Type": "application/json",
                "X-DashScope-Async": "enable",
            },
            payload=body,
            timeout_seconds=self.timeout_seconds,
        )

    def detect_bbox(
        self,
        *,
        image_url: str,
        provider_options: dict[str, Any] | None = None,
        model: str | None = None,
    ) -> dict[str, Any]:
        if not self.api_key:
            raise RuntimeError("DASHSCOPE_API_KEY is required for DashScope avatar detection")
        if avatar_protocol(model or self.model) != EMO_PROTOCOL:
            raise ValueError(
                "face detection belongs to the emo dialect; "
                f"{model or self.model} takes the image directly and needs no bbox"
            )
        if not image_url:
            raise ValueError("image_url is required for DashScope avatar detection")
        body = self._detect_body(image_url=image_url, provider_options=provider_options or {})
        raw_response = self.transport.request_json(
            method="POST",
            url=f"{self.base_url}/api/v1/services/aigc/image2video/face-detect",
            headers={
                "Authorization": f"Bearer {self.api_key}",
                "Content-Type": "application/json",
            },
            payload=body,
            timeout_seconds=self.timeout_seconds,
        )
        normalized = self.normalize_detection(raw_response)
        return {**normalized, "raw_response": raw_response}

    def normalize_detection(self, raw_response: dict[str, Any]) -> dict[str, Any]:
        output = raw_response.get("output") or raw_response
        face_bbox = _find_bbox(output, "face_bbox")
        ext_bbox = _find_bbox(output, "ext_bbox")
        if not face_bbox or not ext_bbox:
            raise ValueError("DashScope avatar detection response did not include face_bbox/ext_bbox")
        return {
            "face_bbox": face_bbox,
            "ext_bbox": ext_bbox,
        }

    def get_status(self, external_task_id: str) -> dict[str, Any]:
        if not self.api_key:
            raise RuntimeError("DASHSCOPE_API_KEY is required for DashScope avatar video")
        response = self.transport.request_json(
            method="GET",
            url=f"{self.base_url}/api/v1/tasks/{external_task_id}",
            headers={"Authorization": f"Bearer {self.api_key}"},
            timeout_seconds=self.timeout_seconds,
        )
        return self.normalize_output(self.capability, response)

    def normalize_output(self, capability: str, raw_response: dict[str, Any]) -> dict[str, Any]:
        if capability != self.capability:
            raise ValueError(f"unsupported capability: {capability}")
        output = raw_response.get("output") or raw_response
        task_id = output.get("task_id") or raw_response.get("request_id")
        task_status = output.get("task_status") or output.get("status")
        video_url = _extract_video_url(output)
        error = output.get("message") or raw_response.get("message")
        return {
            "external_task_id": task_id,
            "provider_status": task_status,
            "status": _normalize_status(task_status),
            "video_url": video_url,
            "error_message": error,
            "raw_response": raw_response,
        }

    def cancel(self, external_task_id: str) -> None:
        raise NotImplementedError("DashScope avatar task cancellation is not implemented yet")

    def _submit_body(self, payload: dict[str, Any]) -> dict[str, Any]:
        model = payload.get("model") or self.model
        provider_options = payload.get("provider_options") or {}
        if avatar_protocol(model) == EMO_PROTOCOL:
            input_payload = {
                "image_url": payload["image_url"],
                "audio_url": payload["audio_url"],
                "face_bbox": payload["face_bbox"],
                "ext_bbox": payload["ext_bbox"],
            }
            parameters = provider_options.get("parameters", {})
        else:
            # wan2.2-s2v takes no bbox: it locates the speaker itself. It does
            # take a resolution tier, defaulted from configuration but overridable
            # per call through ``provider_options``.
            input_payload = {
                "image_url": payload["image_url"],
                "audio_url": payload["audio_url"],
            }
            parameters = {
                "resolution": provider_options.get("resolution") or self.resolution,
                **provider_options.get("parameters", {}),
            }
        return {
            "model": model,
            "input": {**input_payload, **provider_options.get("input", {})},
            "parameters": parameters,
        }

    def _detect_body(
        self,
        *,
        image_url: str,
        provider_options: dict[str, Any],
    ) -> dict[str, Any]:
        detect_options = provider_options.get("detect") or {}
        parameters = {
            "ratio": detect_options.get("ratio") or self.ratio,
            **detect_options.get("parameters", {}),
        }
        return {
            "model": detect_options.get("model") or self.detect_model,
            "input": {
                "image_url": image_url,
                **detect_options.get("input", {}),
            },
            "parameters": parameters,
        }


def _load_environment(env_file: Path | None = None) -> dict[str, str]:
    project_root = Path(__file__).resolve().parents[2]
    values = dotenv_values(env_file or project_root / ".env")
    merged: Mapping[str, str | None] = {**values, **os.environ}
    return {key: value for key, value in merged.items() if value is not None}


def _normalize_status(provider_status: str | None) -> str:
    if provider_status in {"SUCCEEDED", "succeeded", "success"}:
        return "succeeded"
    if provider_status in {"FAILED", "failed", "error"}:
        return "failed"
    if provider_status in {"CANCELED", "CANCELLED", "cancelled"}:
        return "cancelled"
    return "running"


def _extract_video_url(output: dict[str, Any]) -> str | None:
    for key in ["video_url", "url", "result_url"]:
        value = output.get(key)
        if isinstance(value, str):
            return value
    results = output.get("results")
    if isinstance(results, dict):
        for key in ["video_url", "url", "result_url"]:
            value = results.get(key)
            if isinstance(value, str):
                return value
    if isinstance(results, list):
        for result in results:
            if isinstance(result, dict):
                value = _extract_video_url(result)
                if value:
                    return value
    return None


def _find_bbox(value: Any, key: str) -> list[int] | None:
    if isinstance(value, dict):
        candidate = value.get(key)
        if _is_bbox(candidate):
            return [int(item) for item in candidate]
        for child in value.values():
            found = _find_bbox(child, key)
            if found:
                return found
    if isinstance(value, list):
        for child in value:
            found = _find_bbox(child, key)
            if found:
                return found
    return None


def _is_bbox(value: Any) -> bool:
    return (
        isinstance(value, list)
        and len(value) == 4
        and all(isinstance(item, int | float) for item in value)
    )
