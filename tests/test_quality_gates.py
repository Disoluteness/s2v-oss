import shutil
import subprocess

import pytest

from packages.quality_gate_kit import QualityGateEvaluationRequest, QualityGateRunner


def test_quality_gate_runner_reports_missing_media(tmp_path) -> None:
    runner = QualityGateRunner(ffprobe_path="ffprobe")

    report = runner.evaluate(
        QualityGateEvaluationRequest(media_path=tmp_path / "missing.mp4")
    )

    assert report["status"] == "failed"
    assert report["quality_score"] == 75
    assert report["checks"][0]["check_key"] == "media_file_exists"
    assert report["human_review"]["required"] is True


def test_quality_gate_runner_checks_valid_video_against_graph(tmp_path) -> None:
    if shutil.which("ffmpeg") is None or shutil.which("ffprobe") is None:
        pytest.skip("ffmpeg and ffprobe are required")

    media_path = tmp_path / "render.mp4"
    _run(
        [
            "ffmpeg",
            "-y",
            "-f",
            "lavfi",
            "-i",
            "testsrc2=size=320x568:rate=24:duration=1",
            "-f",
            "lavfi",
            "-i",
            "sine=frequency=660:duration=1",
            "-shortest",
            "-c:v",
            "libx264",
            "-pix_fmt",
            "yuv420p",
            "-c:a",
            "aac",
            str(media_path),
        ]
    )
    graph = {
        "canvas": {
            "width": 320,
            "height": 568,
            "fps": 24,
            "style": {
                "caption_style": {
                    "font_family": "Noto Sans CJK SC",
                    "font_size": 24,
                    "max_width_ratio": 0.9,
                }
            },
        },
        "captions": {
            "style": {
                "font_family": "Noto Sans CJK SC",
                "font_size": 24,
                "max_width_ratio": 0.9,
            },
            "segments": [{"caption_id": "c1", "text": "短字幕"}],
        },
        "audio_mix": {"tracks": [{"track_id": "track_audio_narration"}]},
        "render_targets": [
            {
                "runtime": "ffmpeg",
                "settings": {"width": 320, "height": 568, "fps": 24},
            }
        ],
    }
    runner = QualityGateRunner()

    report = runner.evaluate(
        QualityGateEvaluationRequest(
            media_path=media_path,
            render_asset={
                "provider": "ffmpeg",
                "tool_key": "ffmpeg_compose",
                "asset_metadata": {},
            },
            composition_graph=graph,
            checks=[
                "ffprobe_valid",
                "resolution",
                "frame_rate",
                "audio_presence",
                "subtitle_bounds",
                "cjk_font",
                "provider_output",
            ],
        )
    )

    assert report["status"] == "passed"
    assert report["quality_score"] == 100
    assert report["media"]["width"] == 320
    assert report["media"]["height"] == 568
    check_statuses = {check["check_key"]: check["status"] for check in report["checks"]}
    assert check_statuses["resolution"] == "passed"
    assert check_statuses["audio_presence"] == "passed"
    assert check_statuses["cjk_font"] == "passed"


def _run(command: list[str]) -> None:
    completed = subprocess.run(command, check=False, capture_output=True, text=True)
    assert completed.returncode == 0, completed.stderr
