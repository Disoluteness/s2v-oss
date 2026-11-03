import base64
import shutil
import subprocess

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from apps.api.app import db_models  # noqa: F401
from apps.api.app.db import Base
from apps.api.app.db_store import DatabaseStore
from apps.api.app.models import MvpSampleRequest, RunStatus
from apps.api.app.mvp_sample import MvpSampleRunner
from packages.agent_runner.planner import SpokespersonAdPlanner
from packages.agent_runner.runner import RuleBasedAgentRunner
from packages.compose_kit import FfmpegComposeWorker


class FakeTtsAdapter:
    provider_key = "fake_tts"
    tool_key = "fake_tts"

    def __init__(self, content: bytes) -> None:
        self.content = content

    def check_availability(self) -> bool:
        return True

    def synthesize(self, payload: dict) -> dict:
        return {
            "content": self.content,
            "mime_type": "audio/wav",
            "audio_url": "https://example.test/narration.wav",
            "model": payload.get("model"),
            "voice": payload.get("voice"),
            "language_type": payload.get("language_type"),
        }


class FakeImageAdapter:
    provider_key = "fake_image"
    tool_key = "fake_image"

    def check_availability(self) -> bool:
        return True

    def generate(self, payload: dict) -> dict:
        return {
            "content": b"person image bytes",
            "mime_type": "image/png",
            "image_url": "https://example.test/generated-person.png",
            "model": payload.get("model") or "fake-image-model",
            "size": payload.get("size") or "1024*1024",
            "prompt": payload.get("prompt"),
        }


def test_mvp_sample_runner_creates_completed_run_and_vertical_mp4(tmp_path) -> None:
    if shutil.which("ffmpeg") is None or shutil.which("ffprobe") is None:
        pytest.skip("ffmpeg and ffprobe are required")

    engine = create_engine(f"sqlite:///{tmp_path / 'mvp.db'}")
    Base.metadata.create_all(engine)
    session = Session(engine)
    store = DatabaseStore(
        session,
        agent_runner=RuleBasedAgentRunner(planner=SpokespersonAdPlanner()),
    )
    store.storage.root = tmp_path / "storage"

    result = MvpSampleRunner(store).run(
        MvpSampleRequest(
            title="MVP sample",
            raw_script="秋天第一杯奶茶，还没人给你点？别等啦，自己冲就好！",
            brand_name="茶小朵",
            product_name="冲泡奶茶",
            target_duration_seconds=5,
            width=180,
            height=320,
            fps=12,
            crf=30,
            include_music=True,
        )
    )

    final_video = tmp_path / "final.mp4"
    final_video.write_bytes(store.storage.read_bytes(result.render_asset.storage_url))
    probe = FfmpegComposeWorker().probe(final_video)
    video_stream = next(stream for stream in probe["streams"] if stream["codec_type"] == "video")
    current_artifact_types = {
        artifact.artifact_type
        for artifact in store.list_artifacts(result.run.run_id, current_only=True) or []
    }
    events = store.list_events(result.run.run_id)
    session.close()

    assert result.run.status == RunStatus.COMPLETED
    assert result.run.pipeline_key == "avatar_spokesperson"
    assert result.render_asset.asset_type == "rendered_video"
    assert result.render_asset.mime_type == "video/mp4"
    assert result.render_summary.payload["output"]["width"] == 180
    assert result.render_summary.payload["output"]["height"] == 320
    assert result.asset_manifest.payload["external_provider_calls"] is False
    assert result.publish_package.payload["render_asset_id"] == result.render_asset.asset_id
    assert result.final_video_url.startswith("/api/assets/")
    assert result.workspace_url == f"/workspace?run_id={result.run.run_id}"
    assert set(result.assets) == {"avatar_video", "subtitle", "music", "rendered_video"}
    assert result.assets["avatar_video"].asset_metadata["quality_profile"] in {
        "storyboard_preview_v2",
        "shape_preview_v2",
    }
    assert result.assets["music"].asset_metadata["quality_profile"] == "soft_chord_bed_v2"
    assert int(video_stream["width"]) == 180
    assert int(video_stream["height"]) == 320
    assert {
        "intake_brief",
        "production_plan",
        "spoken_script",
        "shot_list",
        "asset_manifest",
        "edit_plan",
        "render_summary",
        "publish_package",
    }.issubset(current_artifact_types)
    assert events is not None
    assert events[-1].event_type == "mvp.sample_completed"


def test_mvp_sample_runner_provider_audio_adds_narration_track(tmp_path) -> None:
    if shutil.which("ffmpeg") is None or shutil.which("ffprobe") is None:
        pytest.skip("ffmpeg and ffprobe are required")

    narration_bytes = _wav_bytes(tmp_path / "narration.wav")
    engine = create_engine(f"sqlite:///{tmp_path / 'provider-audio.db'}")
    Base.metadata.create_all(engine)
    session = Session(engine)
    store = DatabaseStore(
        session,
        agent_runner=RuleBasedAgentRunner(planner=SpokespersonAdPlanner()),
        tts_adapter=FakeTtsAdapter(narration_bytes),
    )
    store.storage.root = tmp_path / "storage"

    result = MvpSampleRunner(store).run(
        MvpSampleRequest(
            title="Provider audio MVP sample",
            raw_script="秋天第一杯奶茶，还没人给你点？别等啦，自己冲就好！",
            brand_name="茶小朵",
            product_name="冲泡奶茶",
            target_duration_seconds=5,
            width=180,
            height=320,
            fps=12,
            crf=30,
            include_music=False,
            quality_mode="provider_audio",
        )
    )

    render_inputs = result.render_summary.payload["inputs"]
    session.close()

    assert result.asset_manifest.payload["external_provider_calls"] is True
    assert "narration_audio" in result.assets
    assert result.assets["narration_audio"].provider == "fake_tts"
    assert render_inputs["narration_audio_asset_id"] == result.assets["narration_audio"].asset_id


def test_mvp_sample_runner_provider_avatar_queues_background_job(tmp_path) -> None:
    if shutil.which("ffmpeg") is None or shutil.which("ffprobe") is None:
        pytest.skip("ffmpeg and ffprobe are required")

    narration_bytes = _wav_bytes(tmp_path / "avatar-narration.wav")
    engine = create_engine(f"sqlite:///{tmp_path / 'provider-avatar.db'}")
    Base.metadata.create_all(engine)
    session = Session(engine)
    store = DatabaseStore(
        session,
        agent_runner=RuleBasedAgentRunner(planner=SpokespersonAdPlanner()),
        tts_adapter=FakeTtsAdapter(narration_bytes),
    )
    store.storage.root = tmp_path / "storage"

    result = MvpSampleRunner(store).run(
        MvpSampleRequest(
            title="Provider avatar MVP sample",
            raw_script="秋天第一杯奶茶，还没人给你点？别等啦，自己冲就好！",
            brand_name="茶小朵",
            product_name="冲泡奶茶",
            target_duration_seconds=5,
            width=180,
            height=320,
            fps=12,
            crf=30,
            include_music=False,
            quality_mode="provider_avatar",
            person_image={
                "url": "https://example.test/person.png",
                "mime_type": "image/png",
                "description": "数字人口播人物参考",
            },
        )
    )

    jobs = store.list_background_jobs(result.run.run_id)
    session.close()

    assert result.asset_manifest.payload["mode"] == "provider_avatar"
    assert result.asset_manifest.payload["external_provider_calls"] is True
    assert result.avatar_job is not None
    assert jobs is not None
    assert [job.job_id for job in jobs] == [result.avatar_job.job_id]
    assert result.avatar_job.request_payload["person_image_url"] == "https://example.test/person.png"
    assert result.avatar_job.request_payload["audio_url"] == "https://example.test/narration.wav"
    assert result.publish_package.payload["avatar_job"]["job_id"] == result.avatar_job.job_id


def test_mvp_sample_runner_provider_avatar_generates_missing_person_image(tmp_path) -> None:
    if shutil.which("ffmpeg") is None or shutil.which("ffprobe") is None:
        pytest.skip("ffmpeg and ffprobe are required")

    narration_bytes = _wav_bytes(tmp_path / "generated-avatar-narration.wav")
    engine = create_engine(f"sqlite:///{tmp_path / 'generated-provider-avatar.db'}")
    Base.metadata.create_all(engine)
    session = Session(engine)
    store = DatabaseStore(
        session,
        agent_runner=RuleBasedAgentRunner(planner=SpokespersonAdPlanner()),
        image_adapter=FakeImageAdapter(),
        tts_adapter=FakeTtsAdapter(narration_bytes),
    )
    store.storage.root = tmp_path / "storage"

    result = MvpSampleRunner(store).run(
        MvpSampleRequest(
            title="Provider generated avatar MVP sample",
            raw_script="秋天第一杯奶茶，还没人给你点？别等啦，自己冲就好！",
            brand_name="茶小朵",
            product_name="冲泡奶茶",
            target_duration_seconds=5,
            width=180,
            height=320,
            fps=12,
            crf=30,
            include_music=False,
            quality_mode="provider_avatar",
        )
    )

    person_asset = result.assets["person_image"]
    session.close()

    assert person_asset.provider == "fake_image"
    assert person_asset.asset_metadata["role"] == "person_image"
    assert person_asset.asset_metadata["source"] == "dashscope_generated_avatar_reference"
    assert person_asset.asset_metadata["source_image_url"] == (
        "https://example.test/generated-person.png"
    )
    assert result.avatar_job is not None
    assert result.avatar_job.request_payload["person_image_asset_id"] == person_asset.asset_id
    assert result.avatar_job.request_payload["person_image_url"] is None


def test_mvp_sample_runner_records_reference_assets(tmp_path) -> None:
    if shutil.which("ffmpeg") is None or shutil.which("ffprobe") is None:
        pytest.skip("ffmpeg and ffprobe are required")

    engine = create_engine(f"sqlite:///{tmp_path / 'reference-assets.db'}")
    Base.metadata.create_all(engine)
    session = Session(engine)
    store = DatabaseStore(
        session,
        agent_runner=RuleBasedAgentRunner(planner=SpokespersonAdPlanner()),
    )
    store.storage.root = tmp_path / "storage"

    result = MvpSampleRunner(store).run(
        MvpSampleRequest(
            title="Reference asset MVP sample",
            raw_script="秋天第一杯奶茶，还没人给你点？别等啦，自己冲就好！",
            brand_name="茶小朵",
            product_name="冲泡奶茶",
            target_duration_seconds=5,
            width=180,
            height=320,
            fps=12,
            crf=30,
            include_music=False,
            product_image={
                "filename": "product.png",
                "content_base64": base64.b64encode(b"product image bytes").decode("ascii"),
                "mime_type": "image/png",
                "description": "冲泡奶茶产品图",
            },
            person_image={
                "url": "https://example.test/person.png",
                "mime_type": "image/png",
                "description": "数字人口播人物参考",
            },
        )
    )

    references = result.asset_manifest.payload["reference_assets"]
    product_asset = result.assets["product_image"]
    session.close()

    assert product_asset.asset_type == "reference_image"
    assert product_asset.asset_metadata["role"] == "product_image"
    assert product_asset.asset_metadata["description"] == "冲泡奶茶产品图"
    assert any(
        reference["role"] == "product_image"
        and reference["source"] == "uploaded_asset"
        and reference["asset_id"] == product_asset.asset_id
        for reference in references
    )
    assert any(
        reference["role"] == "person_image"
        and reference["source"] == "external_url"
        and reference["url"] == "https://example.test/person.png"
        for reference in references
    )
    assert result.assets["avatar_video"].asset_metadata["reference_asset_roles"] == [
        "product_image"
    ]
    assert result.assets["avatar_video"].asset_metadata["external_reference_count"] == 1


def _wav_bytes(path) -> bytes:
    completed = subprocess.run(
        [
            "ffmpeg",
            "-y",
            "-f",
            "lavfi",
            "-i",
            "sine=frequency=660:duration=5:sample_rate=44100",
            "-c:a",
            "pcm_s16le",
            str(path),
        ],
        check=False,
        capture_output=True,
        text=True,
    )
    assert completed.returncode == 0, completed.stderr
    return path.read_bytes()
