from typing import Any

import pytest

from packages.provider_kit import (
    DashScopeAsrAdapter,
    DashScopeTtsAdapter,
    ElevenLabsMusicAdapter,
)


class FakeJsonTransport:
    def __init__(self, response: dict[str, Any]) -> None:
        self.response = response
        self.calls: list[dict[str, Any]] = []

    def request_json(
        self,
        *,
        method: str,
        url: str,
        headers: dict[str, str],
        payload: dict[str, Any] | None = None,
        timeout_seconds: int = 60,
    ) -> dict[str, Any]:
        self.calls.append(
            {
                "method": method,
                "url": url,
                "headers": headers,
                "payload": payload,
                "timeout_seconds": timeout_seconds,
            }
        )
        return self.response


class FakeBinaryTransport:
    def __init__(self, content: bytes) -> None:
        self.content = content
        self.calls: list[dict[str, Any]] = []

    def request_bytes(
        self,
        *,
        method: str,
        url: str,
        headers: dict[str, str] | None = None,
        payload: dict[str, Any] | None = None,
        timeout_seconds: int = 60,
    ) -> bytes:
        self.calls.append(
            {
                "method": method,
                "url": url,
                "headers": headers or {},
                "payload": payload,
                "timeout_seconds": timeout_seconds,
            }
        )
        return self.content


def test_dashscope_tts_adapter_generates_and_downloads_audio() -> None:
    json_transport = FakeJsonTransport(
        {"output": {"audio": {"url": "https://example.com/voice.wav"}}}
    )
    binary_transport = FakeBinaryTransport(b"wav bytes")
    adapter = DashScopeTtsAdapter(
        api_key="test-key",
        json_transport=json_transport,
        binary_transport=binary_transport,
    )

    result = adapter.synthesize({"text": "秋天第一杯奶茶", "voice": "Cherry"})

    assert result["content"] == b"wav bytes"
    assert result["audio_url"] == "https://example.com/voice.wav"
    assert json_transport.calls[0]["payload"]["model"] == "qwen3-tts-flash"
    assert json_transport.calls[0]["payload"]["input"]["voice"] == "Cherry"
    assert binary_transport.calls[0]["url"] == "https://example.com/voice.wav"


def test_elevenlabs_music_adapter_generates_audio_bytes() -> None:
    binary_transport = FakeBinaryTransport(b"mp3 bytes")
    adapter = ElevenLabsMusicAdapter(
        api_key="test-key",
        binary_transport=binary_transport,
    )

    result = adapter.compose(
        {
            "prompt": "Warm guitar background music, no vocals",
            "music_length_ms": 15000,
            "force_instrumental": True,
        }
    )

    assert result["content"] == b"mp3 bytes"
    assert result["mime_type"] == "audio/mpeg"
    assert binary_transport.calls[0]["url"].endswith("/v1/music?output_format=mp3_44100_128")
    assert binary_transport.calls[0]["payload"]["force_instrumental"] is True


def test_dashscope_asr_adapter_transcribes_via_reachable_url() -> None:
    json_transport = FakeJsonTransport(
        {
            "output": {
                "choices": [
                    {
                        "message": {
                            "content": [
                                {"type": "text", "text": "大家好，欢迎观看本期视频。"}
                            ]
                        }
                    }
                ]
            }
        }
    )
    adapter = DashScopeAsrAdapter(
        api_key="test-key",
        json_transport=json_transport,
    )

    result = adapter.transcribe({"audio_url": "https://example.com/audio.mp4"})

    assert result["text"] == "大家好，欢迎观看本期视频。"
    # The engine returns plain text; we do not invent segment timestamps.
    assert result["segments"] == []
    assert result["model"] == "qwen3-asr-flash"
    call = json_transport.calls[0]
    assert call["headers"]["Authorization"] == "Bearer test-key"
    content = call["payload"]["input"]["messages"][0]["content"]
    assert content[0] == {"type": "audio", "audio": "https://example.com/audio.mp4"}
    assert content[1]["type"] == "text"


def test_dashscope_asr_adapter_accepts_string_content_and_language_hint() -> None:
    json_transport = FakeJsonTransport(
        {
            "output": {
                "choices": [{"message": {"content": "  hello world  "}}],
            }
        }
    )
    adapter = DashScopeAsrAdapter(
        api_key="test-key",
        model="qwen3-asr-flash",
        json_transport=json_transport,
    )

    result = adapter.transcribe(
        {
            "audio_url": "https://example.com/audio.mp4",
            "language": "zh-CN",
        }
    )

    assert result["text"] == "hello world"
    content = json_transport.calls[0]["payload"]["input"]["messages"][0]["content"]
    assert content[-1]["text"].startswith("Target language: zh-CN")


def test_dashscope_asr_adapter_requires_audio_url_and_key() -> None:
    adapter = DashScopeAsrAdapter(api_key=None, json_transport=FakeJsonTransport({}))

    assert adapter.check_availability() is False
    with pytest.raises(ValueError, match="audio_url is required"):
        adapter.transcribe({})
    with pytest.raises(RuntimeError, match="DASHSCOPE_API_KEY is required"):
        adapter.transcribe({"audio_url": "https://example.com/audio.mp4"})


def test_dashscope_asr_adapter_surfaces_empty_transcript_as_error() -> None:
    adapter = DashScopeAsrAdapter(
        api_key="test-key",
        json_transport=FakeJsonTransport({"output": {"choices": []}}),
    )

    with pytest.raises(ValueError, match="did not include transcript text"):
        adapter.transcribe({"audio_url": "https://example.com/audio.mp4"})
