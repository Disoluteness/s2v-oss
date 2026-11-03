from typing import Any

from packages.provider_kit.image import DashScopeImageAdapter


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
                "headers": headers,
                "payload": payload,
                "timeout_seconds": timeout_seconds,
            }
        )
        return self.content


def test_dashscope_image_adapter_generates_and_downloads_image() -> None:
    json_transport = FakeJsonTransport(
        {
            "output": {
                "choices": [
                    {
                        "message": {
                            "content": [
                                {"image": "https://example.com/generated-person.png"}
                            ]
                        }
                    }
                ]
            }
        }
    )
    binary_transport = FakeBinaryTransport(b"image bytes")
    adapter = DashScopeImageAdapter(
        api_key="test-key",
        json_transport=json_transport,
        binary_transport=binary_transport,
    )

    result = adapter.generate(
        {
            "prompt": "front-facing spokesperson portrait",
            "model": "qwen-image-2.0-pro",
            "size": "1024*1024",
        }
    )

    assert result["content"] == b"image bytes"
    assert result["image_url"] == "https://example.com/generated-person.png"
    assert result["mime_type"] == "image/png"
    assert json_transport.calls[0]["method"] == "POST"
    assert json_transport.calls[0]["payload"]["model"] == "qwen-image-2.0-pro"
    assert json_transport.calls[0]["payload"]["parameters"]["size"] == "1024*1024"
    assert binary_transport.calls[0]["url"] == "https://example.com/generated-person.png"
