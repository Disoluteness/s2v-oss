from __future__ import annotations

import json
import os
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Mapping
from pathlib import Path
from typing import Any, Protocol

from dotenv import dotenv_values

DEFAULT_DASHSCOPE_API_BASE_URL = "https://dashscope.aliyuncs.com"
DEFAULT_DASHSCOPE_TTS_MODEL = "qwen3-tts-flash"
DEFAULT_DASHSCOPE_ASR_MODEL = "qwen3-asr-flash"
DEFAULT_ELEVENLABS_API_BASE_URL = "https://api.elevenlabs.io"
DEFAULT_ELEVENLABS_MUSIC_ENDPOINT = "/v1/music"


class JsonTransport(Protocol):
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


class BinaryTransport(Protocol):
    def request_bytes(
        self,
        *,
        method: str,
        url: str,
        headers: dict[str, str] | None = None,
        payload: dict[str, Any] | None = None,
        timeout_seconds: int = 60,
    ) -> bytes:
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
            raise RuntimeError(f"HTTP {exc.code}: {error_body}") from exc
        except urllib.error.URLError as exc:
            raise RuntimeError(f"HTTP request failed: {exc}") from exc
        return json.loads(raw) if raw else {}


class UrllibBinaryTransport:
    def request_bytes(
        self,
        *,
        method: str,
        url: str,
        headers: dict[str, str] | None = None,
        payload: dict[str, Any] | None = None,
        timeout_seconds: int = 60,
    ) -> bytes:
        data = None if payload is None else json.dumps(payload, ensure_ascii=False).encode("utf-8")
        request = urllib.request.Request(url, data=data, headers=headers or {}, method=method)
        try:
            with urllib.request.urlopen(request, timeout=timeout_seconds) as response:
                return response.read()
        except urllib.error.HTTPError as exc:
            error_body = exc.read().decode("utf-8", errors="replace")
            raise RuntimeError(f"HTTP {exc.code}: {error_body}") from exc
        except urllib.error.URLError as exc:
            raise RuntimeError(f"HTTP request failed: {exc}") from exc


class DashScopeTtsAdapter:
    provider_key = "dashscope"
    tool_key = "dashscope_tts"
    capability = "tts"

    def __init__(
        self,
        *,
        api_key: str | None = None,
        base_url: str | None = None,
        model: str | None = None,
        json_transport: JsonTransport | None = None,
        binary_transport: BinaryTransport | None = None,
        timeout_seconds: int = 60,
    ) -> None:
        env = _load_environment()
        self.api_key = api_key or env.get("DASHSCOPE_API_KEY")
        self.base_url = (
            base_url or env.get("DASHSCOPE_API_BASE_URL") or DEFAULT_DASHSCOPE_API_BASE_URL
        ).rstrip("/")
        self.model = model or env.get("DASHSCOPE_TTS_MODEL") or DEFAULT_DASHSCOPE_TTS_MODEL
        self.json_transport = json_transport or UrllibJsonTransport()
        self.binary_transport = binary_transport or UrllibBinaryTransport()
        self.timeout_seconds = timeout_seconds

    def check_availability(self) -> bool:
        return bool(self.api_key)

    def synthesize(self, payload: dict[str, Any]) -> dict[str, Any]:
        self.validate_input(payload)
        if not self.api_key:
            raise RuntimeError("DASHSCOPE_API_KEY is required for DashScope TTS")
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
        audio_url = _extract_audio_url(raw_response)
        if not audio_url:
            raise ValueError("DashScope TTS response did not include output.audio.url")
        content = self.binary_transport.request_bytes(
            method="GET",
            url=audio_url,
            timeout_seconds=self.timeout_seconds,
        )
        return {
            "content": content,
            "mime_type": "audio/wav",
            "audio_url": audio_url,
            "raw_response": raw_response,
            "model": body["model"],
            "voice": body["input"]["voice"],
            "language_type": body["input"]["language_type"],
        }

    def validate_input(self, payload: dict[str, Any]) -> None:
        text = payload.get("text")
        if not isinstance(text, str) or not text.strip():
            raise ValueError("text is required for DashScope TTS")
        if len(text) > 600:
            raise ValueError("text must be 600 characters or fewer for DashScope TTS")

    def _request_body(self, payload: dict[str, Any]) -> dict[str, Any]:
        provider_options = payload.get("provider_options") or {}
        return {
            "model": payload.get("model") or self.model,
            "input": {
                "text": payload["text"],
                "voice": payload.get("voice") or "Cherry",
                "language_type": payload.get("language_type") or "Auto",
                **provider_options.get("input", {}),
            },
            "parameters": provider_options.get("parameters", {}),
        }


class ElevenLabsMusicAdapter:
    provider_key = "elevenlabs"
    tool_key = "elevenlabs_music"
    capability = "music_generation"

    def __init__(
        self,
        *,
        api_key: str | None = None,
        base_url: str | None = None,
        endpoint: str | None = None,
        binary_transport: BinaryTransport | None = None,
        timeout_seconds: int = 120,
    ) -> None:
        env = _load_environment()
        self.api_key = api_key or env.get("ELEVENLABS_API_KEY")
        self.base_url = (
            base_url or env.get("ELEVENLABS_API_BASE_URL") or DEFAULT_ELEVENLABS_API_BASE_URL
        ).rstrip("/")
        self.endpoint = endpoint or env.get("ELEVENLABS_MUSIC_ENDPOINT") or DEFAULT_ELEVENLABS_MUSIC_ENDPOINT
        self.binary_transport = binary_transport or UrllibBinaryTransport()
        self.timeout_seconds = timeout_seconds

    def check_availability(self) -> bool:
        return bool(self.api_key)

    def compose(self, payload: dict[str, Any]) -> dict[str, Any]:
        self.validate_input(payload)
        if not self.api_key:
            raise RuntimeError("ELEVENLABS_API_KEY is required for ElevenLabs music")
        body = self._request_body(payload)
        output_format = payload.get("output_format") or "mp3_44100_128"
        query = urllib.parse.urlencode({"output_format": output_format})
        content = self.binary_transport.request_bytes(
            method="POST",
            url=f"{self.base_url}{self.endpoint}?{query}",
            headers={
                "xi-api-key": self.api_key,
                "Content-Type": "application/json",
            },
            payload=body,
            timeout_seconds=self.timeout_seconds,
        )
        return {
            "content": content,
            "mime_type": "audio/mpeg",
            "request": body,
            "output_format": output_format,
        }

    def validate_input(self, payload: dict[str, Any]) -> None:
        prompt = payload.get("prompt")
        if not isinstance(prompt, str) or not prompt.strip():
            raise ValueError("prompt is required for ElevenLabs music")
        length = int(payload.get("music_length_ms") or 0)
        if length < 10_000 or length > 300_000:
            raise ValueError("music_length_ms must be between 10000 and 300000")

    def _request_body(self, payload: dict[str, Any]) -> dict[str, Any]:
        provider_options = payload.get("provider_options") or {}
        body = {
            "prompt": payload["prompt"],
            "music_length_ms": payload.get("music_length_ms") or 15_000,
            "force_instrumental": bool(payload.get("force_instrumental", True)),
            **provider_options,
        }
        if payload.get("model_id"):
            body["model_id"] = payload["model_id"]
        return body


class DashScopeAsrAdapter:
    """Speech-to-text over the already-integrated DashScope credential.

    This is deliberately *not* a new provider: it reuses the same
    ``DASHSCOPE_API_KEY`` the TTS and image adapters use, so a deployment that
    can already speak can also listen without new secrets. One honest
    constraint shapes the whole contract: DashScope fetches the audio itself,
    so the caller must supply a URL the provider can reach. A local storage
    path is not that — the caller (``analyze_source_media``) treats "no
    reachable URL" as a degraded transcript, never as a silent empty string.
    """

    provider_key = "dashscope"
    tool_key = "dashscope_asr"
    capability = "speech_to_text"

    def __init__(
        self,
        *,
        api_key: str | None = None,
        base_url: str | None = None,
        model: str | None = None,
        json_transport: JsonTransport | None = None,
        timeout_seconds: int = 120,
    ) -> None:
        env = _load_environment()
        self.api_key = api_key or env.get("DASHSCOPE_API_KEY")
        self.base_url = (
            base_url or env.get("DASHSCOPE_API_BASE_URL") or DEFAULT_DASHSCOPE_API_BASE_URL
        ).rstrip("/")
        self.model = model or env.get("DASHSCOPE_ASR_MODEL") or DEFAULT_DASHSCOPE_ASR_MODEL
        self.json_transport = json_transport or UrllibJsonTransport()
        self.timeout_seconds = timeout_seconds

    def check_availability(self) -> bool:
        return bool(self.api_key)

    def transcribe(self, payload: dict[str, Any]) -> dict[str, Any]:
        self.validate_input(payload)
        if not self.api_key:
            raise RuntimeError("DASHSCOPE_API_KEY is required for DashScope ASR")
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
        text = _extract_transcript_text(raw_response)
        if text is None:
            raise ValueError("DashScope ASR response did not include transcript text")
        return {
            "text": text,
            # The engine returns plain text without word timestamps. We do not
            # synthesize segment times from silence detection here: invented
            # timestamps look precise and are wrong. Consumers that need
            # alignment get the audio energy timeline instead, which carries
            # its own provenance.
            "segments": [],
            "model": body["model"],
            "raw_response": raw_response,
        }

    def validate_input(self, payload: dict[str, Any]) -> None:
        audio_url = payload.get("audio_url")
        if not isinstance(audio_url, str) or not audio_url.strip():
            raise ValueError("audio_url is required for DashScope ASR")

    def _request_body(self, payload: dict[str, Any]) -> dict[str, Any]:
        provider_options = payload.get("provider_options") or {}
        content: list[dict[str, Any]] = [
            {"type": "audio", "audio": payload["audio_url"]},
            {
                "type": "text",
                "text": payload.get("instruction")
                or "Transcribe the audio verbatim.",
            },
        ]
        if payload.get("language"):
            content.append(
                {"type": "text", "text": f"Target language: {payload['language']}"}
            )
        return {
            "model": payload.get("model") or self.model,
            "input": {
                "messages": [{"role": "user", "content": content}],
                **provider_options.get("input", {}),
            },
            "parameters": provider_options.get("parameters", {}),
        }


def _load_environment(env_file: Path | None = None) -> dict[str, str]:
    project_root = Path(__file__).resolve().parents[2]
    values = dotenv_values(env_file or project_root / ".env")
    merged: Mapping[str, str | None] = {**values, **os.environ}
    return {key: value for key, value in merged.items() if value is not None}


def _extract_transcript_text(raw_response: dict[str, Any]) -> str | None:
    """Pull the transcript text out of a multimodal-generation response.

    The content node has shipped as a plain string and as a typed part list
    depending on model and API revision, so both shapes are accepted. An empty
    result returns ``None`` (not ``""``) so the caller can distinguish "the
    model said nothing" from "the response had no transcript at all".
    """
    output = raw_response.get("output") or raw_response
    choices = output.get("choices") or []
    message = (choices[0].get("message") if choices else None) or {}
    content = message.get("content")
    if isinstance(content, str):
        return content.strip() or None
    if isinstance(content, list):
        parts = [
            part["text"]
            for part in content
            if isinstance(part, dict) and isinstance(part.get("text"), str)
        ]
        text = "".join(parts).strip()
        return text or None
    return None


def _extract_audio_url(raw_response: dict[str, Any]) -> str | None:
    output = raw_response.get("output") or raw_response
    audio = output.get("audio")
    if isinstance(audio, dict) and isinstance(audio.get("url"), str):
        return audio["url"]
    if isinstance(audio, str):
        return audio
    for key in ["audio_url", "url"]:
        value = output.get(key)
        if isinstance(value, str):
            return value
    return None
