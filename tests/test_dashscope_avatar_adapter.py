from typing import Any

import pytest

from packages.provider_kit import DashScopeAvatarVideoAdapter
from packages.provider_kit.dashscope_avatar import avatar_protocol


class FakeTransport:
    def __init__(self, responses: list[dict[str, Any]]) -> None:
        self.responses = responses
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
        return self.responses.pop(0)


def _adapter(**kwargs: Any) -> DashScopeAvatarVideoAdapter:
    return DashScopeAvatarVideoAdapter(
        api_key="test-key",
        transport=FakeTransport(responses=[]),
        **kwargs,
    )


def test_avatar_protocol_follows_the_model_family() -> None:
    """The two dialects are told apart by name, which is what keeps EMO usable.

    ``emo-v1`` was retired from Bailian, so the default moved to ``wan2.2-s2v``;
    an ``emo*`` name has to keep selecting the legacy request shape in case the
    model is re-listed or a deployment pins it.
    """

    assert avatar_protocol("wan2.2-s2v") == "s2v"
    assert avatar_protocol("emo-v1") == "emo"
    assert avatar_protocol("EMO-DETECT-V1") == "emo"
    assert avatar_protocol("  emo-v1  ") == "emo"
    assert avatar_protocol(None) == "s2v"

    assert _adapter().requires_bbox() is False
    assert _adapter().requires_bbox("emo-v1") is True


def test_dashscope_avatar_adapter_submits_async_task() -> None:
    transport = FakeTransport(
        responses=[{"output": {"task_id": "task_123", "task_status": "PENDING"}}]
    )
    adapter = DashScopeAvatarVideoAdapter(
        api_key="test-key",
        transport=transport,
        model="emo-v1",
    )

    raw = adapter.submit(
        {
            "image_url": "https://example.com/person.png",
            "audio_url": "https://example.com/voice.wav",
            "face_bbox": [10, 20, 110, 160],
            "ext_bbox": [0, 0, 180, 220],
        }
    )
    normalized = adapter.normalize_output("avatar_video", raw)

    assert normalized["external_task_id"] == "task_123"
    assert normalized["status"] == "running"
    assert transport.calls[0]["method"] == "POST"
    assert transport.calls[0]["headers"]["X-DashScope-Async"] == "enable"
    assert transport.calls[0]["payload"]["model"] == "emo-v1"
    assert transport.calls[0]["payload"]["input"]["face_bbox"] == [10, 20, 110, 160]


def test_dashscope_avatar_adapter_submits_wan_s2v_without_bbox() -> None:
    """wan2.2-s2v takes the raw image plus a resolution tier — never a bbox.

    Sending the EMO shape (or a stray face_bbox) is rejected provider side, so
    the body shape is the part worth pinning.
    """

    transport = FakeTransport(
        responses=[{"output": {"task_id": "task_s2v", "task_status": "PENDING"}}]
    )
    adapter = DashScopeAvatarVideoAdapter(api_key="test-key", transport=transport)

    raw = adapter.submit(
        {
            "image_url": "https://example.com/person.png",
            "audio_url": "https://example.com/voice.wav",
        }
    )

    body = transport.calls[0]["payload"]
    assert body["model"] == "wan2.2-s2v"
    assert body["input"] == {
        "image_url": "https://example.com/person.png",
        "audio_url": "https://example.com/voice.wav",
    }
    assert body["parameters"] == {"resolution": "480P"}
    assert raw["output"]["task_id"] == "task_s2v"


def test_dashscope_avatar_adapter_submits_s2v_without_the_detection_hop() -> None:
    """``submit`` must not demand a bbox on an s2v model.

    The bbox check used to be unconditional, which made every s2v call fail
    before it reached the provider.
    """

    transport = FakeTransport(
        responses=[{"output": {"task_id": "task_s2v", "task_status": "PENDING"}}]
    )
    adapter = DashScopeAvatarVideoAdapter(api_key="test-key", transport=transport)

    assert adapter.requires_bbox() is False
    adapter.submit(
        {
            "image_url": "https://example.com/person.png",
            "audio_url": "https://example.com/voice.wav",
        }
    )
    assert transport.calls[0]["payload"]["input"].keys() == {"image_url", "audio_url"}


def test_dashscope_avatar_adapter_lets_a_call_override_resolution() -> None:
    transport = FakeTransport(
        responses=[{"output": {"task_id": "task_s2v", "task_status": "PENDING"}}]
    )
    adapter = DashScopeAvatarVideoAdapter(
        api_key="test-key",
        transport=transport,
        resolution="480P",
    )

    adapter.submit(
        {
            "image_url": "https://example.com/person.png",
            "audio_url": "https://example.com/voice.wav",
            "provider_options": {"resolution": "720P"},
        }
    )

    assert transport.calls[0]["payload"]["parameters"] == {"resolution": "720P"}


def test_dashscope_avatar_adapter_polls_and_extracts_video_url() -> None:
    transport = FakeTransport(
        responses=[
            {
                "output": {
                    "task_id": "task_123",
                    "task_status": "SUCCEEDED",
                    # wan2.2-s2v nests the finished file under `results`, unlike
                    # EMO which returned a flat `video_url`.
                    "results": {"video_url": "https://example.com/result.mp4"},
                }
            }
        ]
    )
    adapter = DashScopeAvatarVideoAdapter(api_key="test-key", transport=transport)

    status = adapter.get_status("task_123")

    assert status["status"] == "succeeded"
    assert status["video_url"] == "https://example.com/result.mp4"
    assert transport.calls[0]["method"] == "GET"
    assert transport.calls[0]["url"].endswith("/api/v1/tasks/task_123")


def test_dashscope_avatar_adapter_detects_avatar_bboxes() -> None:
    transport = FakeTransport(
        responses=[
            {
                "output": {
                    "results": [
                        {
                            "face_bbox": [12, 24, 180, 260],
                            "ext_bbox": [0, 0, 240, 320],
                        }
                    ]
                }
            }
        ]
    )
    adapter = DashScopeAvatarVideoAdapter(
        api_key="test-key",
        transport=transport,
        model="emo-v1",
    )

    detected = adapter.detect_bbox(image_url="https://example.com/person.png")

    assert detected["face_bbox"] == [12, 24, 180, 260]
    assert detected["ext_bbox"] == [0, 0, 240, 320]
    assert transport.calls[0]["method"] == "POST"
    assert transport.calls[0]["url"].endswith("/api/v1/services/aigc/image2video/face-detect")
    assert transport.calls[0]["payload"]["model"] == "emo-detect-v1"
    assert transport.calls[0]["payload"]["input"]["image_url"] == "https://example.com/person.png"
    assert transport.calls[0]["payload"]["parameters"]["ratio"] == "3:4"


def test_dashscope_avatar_adapter_refuses_bbox_detection_on_s2v() -> None:
    """There is no face-detect step for s2v, so asking for one is a bug.

    Failing loudly beats returning a bbox nobody consumes — or silently posting
    to a face-detect endpoint that no longer serves the model.
    """

    adapter = _adapter()

    with pytest.raises(ValueError, match="face detection belongs to the emo dialect"):
        adapter.detect_bbox(image_url="https://example.com/person.png")
