from __future__ import annotations

import mimetypes
import os
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from dotenv import dotenv_values

from packages.provider_kit.audio import (
    DEFAULT_DASHSCOPE_API_BASE_URL,
    BinaryTransport,
    JsonTransport,
    UrllibBinaryTransport,
    UrllibJsonTransport,
)

DEFAULT_DASHSCOPE_IMAGE_MODEL = "qwen-image-2.0-pro"
DEFAULT_DASHSCOPE_IMAGE_SIZE = "1024*1024"


class DashScopeImageAdapter:
    provider_key = "dashscope"
    tool_key = "dashscope_image"
    capability = "image_generation"

    def __init__(
        self,
        *,
        api_key: str | None = None,
        base_url: str | None = None,
        model: str | None = None,
        json_transport: JsonTransport | None = None,
        binary_transport: BinaryTransport | None = None,
        timeout_seconds: int = 120,
    ) -> None:
        env = _load_environment()
        self.api_key = api_key or env.get("DASHSCOPE_API_KEY")
        self.base_url = (
            base_url or env.get("DASHSCOPE_API_BASE_URL") or DEFAULT_DASHSCOPE_API_BASE_URL
        ).rstrip("/")
        self.model = model or env.get("DASHSCOPE_IMAGE_MODEL") or DEFAULT_DASHSCOPE_IMAGE_MODEL
        self.size = env.get("DASHSCOPE_IMAGE_SIZE") or DEFAULT_DASHSCOPE_IMAGE_SIZE
        self.json_transport = json_transport or UrllibJsonTransport()
        self.binary_transport = binary_transport or UrllibBinaryTransport()
        self.timeout_seconds = timeout_seconds

    def check_availability(self) -> bool:
        return bool(self.api_key)

    def generate(self, payload: dict[str, Any]) -> dict[str, Any]:
        self.validate_input(payload)
        if not self.api_key:
            raise RuntimeError("DASHSCOPE_API_KEY is required for DashScope image generation")
        body = self._request_body(payload)
        raw_response = self.json_transport.request_json(
            method="POST",
            url=f"{self.base_url}/api/v1/services/aigc/multimodal-generation/generation",
            headers={
                "Authorization": f"Bearer {self.api_key}",
                "Content-Type": "application/json",
            },
            payload=body,
            timeout_seconds=self.timeout_seconds,
        )
        image_url = _extract_image_url(raw_response)
        if not image_url:
            raise ValueError("DashScope image response did not include an image URL")
        content = self.binary_transport.request_bytes(
            method="GET",
            url=image_url,
            timeout_seconds=self.timeout_seconds,
        )
        return {
            "content": content,
            "mime_type": _guess_image_mime_type(image_url),
            "image_url": image_url,
            "raw_response": raw_response,
            "model": body["model"],
            "size": body["parameters"].get("size"),
            "prompt": payload["prompt"],
        }

    def validate_input(self, payload: dict[str, Any]) -> None:
        prompt = payload.get("prompt")
        if not isinstance(prompt, str) or not prompt.strip():
            raise ValueError("prompt is required for DashScope image generation")

    def _request_body(self, payload: dict[str, Any]) -> dict[str, Any]:
        provider_options = payload.get("provider_options") or {}
        parameters = {
            "size": payload.get("size") or self.size,
            "n": int(payload.get("n") or 1),
            "prompt_extend": bool(payload.get("prompt_extend", True)),
            "watermark": bool(payload.get("watermark", False)),
            **provider_options.get("parameters", {}),
        }
        if payload.get("negative_prompt"):
            parameters["negative_prompt"] = payload["negative_prompt"]
        if payload.get("seed") is not None:
            parameters["seed"] = payload["seed"]
        return {
            "model": payload.get("model") or self.model,
            "input": {
                "messages": [
                    {
                        "role": "user",
                        "content": [{"text": payload["prompt"]}],
                    }
                ],
                **provider_options.get("input", {}),
            },
            "parameters": parameters,
        }


def _load_environment(env_file: Path | None = None) -> dict[str, str]:
    project_root = Path(__file__).resolve().parents[2]
    values = dotenv_values(env_file or project_root / ".env")
    merged: Mapping[str, str | None] = {**values, **os.environ}
    return {key: value for key, value in merged.items() if value is not None}


def _extract_image_url(raw_response: dict[str, Any]) -> str | None:
    output = raw_response.get("output") or raw_response
    direct = _find_image_url(output)
    if direct:
        return direct
    return None


def _find_image_url(value: Any) -> str | None:
    if isinstance(value, dict):
        for key in ["image", "image_url", "url"]:
            candidate = value.get(key)
            if isinstance(candidate, str) and candidate.startswith(("http://", "https://")):
                return candidate
        for child in value.values():
            found = _find_image_url(child)
            if found:
                return found
    if isinstance(value, list):
        for child in value:
            found = _find_image_url(child)
            if found:
                return found
    return None


def _guess_image_mime_type(image_url: str) -> str:
    mime_type, _encoding = mimetypes.guess_type(image_url.split("?", 1)[0])
    return mime_type or "image/png"
