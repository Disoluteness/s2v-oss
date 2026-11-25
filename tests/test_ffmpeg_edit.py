import shutil
import subprocess

import pytest

from packages.edit_kit import FfmpegEditRequest, FfmpegEditWorker


def test_ffmpeg_edit_worker_trims_reframes_watermarks_and_extracts_cover(tmp_path) -> None:
    if shutil.which("ffmpeg") is None or shutil.which("ffprobe") is None:
        pytest.skip("ffmpeg and ffprobe are required")

    source = tmp_path / "source.mp4"
    _make_video(source, duration=1.2)
    worker = FfmpegEditWorker()

    trim = worker.run(
        FfmpegEditRequest(
            operation="trim",
            source_paths=[source],
            output_path=tmp_path / "trim.mp4",
            start_seconds=0.2,
            duration_seconds=0.5,
        )
    )
    reframe = worker.run(
        FfmpegEditRequest(
            operation="auto_reframe",
            source_paths=[source],
            output_path=tmp_path / "reframe.mp4",
            width=180,
            height=320,
            fps=12,
        )
    )
    watermark = worker.run(
        FfmpegEditRequest(
            operation="watermark",
            source_paths=[source],
            output_path=tmp_path / "watermark.mp4",
            width=180,
            height=320,
            watermark_text="DEMO",
        )
    )
    cover = worker.run(
        FfmpegEditRequest(
            operation="cover_generation",
            source_paths=[source],
            output_path=tmp_path / "cover.jpg",
            cover_time_seconds=0.1,
            width=180,
            height=320,
        )
    )

    assert trim.output_path.is_file()
    assert trim.asset_type == "edited_video"
    assert trim.duration_seconds is not None
    assert trim.duration_seconds <= 0.8
    assert reframe.width == 180
    assert reframe.height == 320
    assert watermark.width == 180
    assert watermark.height == 320
    assert cover.asset_type == "cover_image"
    assert cover.mime_type == "image/jpeg"
    assert cover.width == 180
    assert cover.height == 320


def test_ffmpeg_edit_worker_concats_and_burns_subtitles_when_supported(tmp_path) -> None:
    if shutil.which("ffmpeg") is None or shutil.which("ffprobe") is None:
        pytest.skip("ffmpeg and ffprobe are required")

    first = tmp_path / "first.mp4"
    second = tmp_path / "second.mp4"
    subtitle = tmp_path / "subtitle.srt"
    _make_video(first, duration=0.5)
    _make_video(second, duration=0.5, color="blue")
    subtitle.write_text(
        "1\n00:00:00,000 --> 00:00:00,500\nHello\n",
        encoding="utf-8",
    )
    worker = FfmpegEditWorker()

    concat = worker.run(
        FfmpegEditRequest(
            operation="concat",
            source_paths=[first, second],
            output_path=tmp_path / "concat.mp4",
            width=160,
            height=284,
            fps=12,
        )
    )
    assert concat.width == 160
    assert concat.height == 284
    assert concat.duration_seconds is not None
    assert concat.duration_seconds >= 0.8

    if not worker.supports_filter("subtitles"):
        pytest.skip("ffmpeg subtitles filter is unavailable")
    burned = worker.run(
        FfmpegEditRequest(
            operation="subtitle_burn_in",
            source_paths=[first],
            subtitle_path=subtitle,
            output_path=tmp_path / "burned.mp4",
            width=160,
            height=284,
        )
    )
    assert burned.output_path.is_file()
    assert burned.metadata["subtitle_burned"] is True


def test_ffmpeg_edit_worker_colors_loudness_and_pip(tmp_path) -> None:
    """P5 enhancement operations: grade, normalize, and overlay all run locally."""

    if shutil.which("ffmpeg") is None or shutil.which("ffprobe") is None:
        pytest.skip("ffmpeg and ffprobe are required")
    if not FfmpegEditWorker().supports_filter("loudnorm"):
        pytest.skip("ffmpeg loudnorm filter is unavailable")

    base = tmp_path / "base.mp4"
    overlay = tmp_path / "overlay.mp4"
    _make_video(base, duration=0.8, color="red")
    _make_video(overlay, duration=0.8, color="blue")
    worker = FfmpegEditWorker()

    graded = worker.run(
        FfmpegEditRequest(
            operation="color_grade",
            source_paths=[base],
            output_path=tmp_path / "graded.mp4",
            grade_preset="warm",
            grade_strength=0.6,
        )
    )
    assert graded.output_path.is_file()
    assert graded.asset_type == "edited_video"
    assert graded.has_audio is True
    assert graded.metadata["grade_preset"] == "warm"

    normalized = worker.run(
        FfmpegEditRequest(
            operation="loudness_normalize",
            source_paths=[base],
            output_path=tmp_path / "normalized.mp4",
            target_loudness_lufs=-16.0,
        )
    )
    assert normalized.output_path.is_file()
    assert normalized.has_audio is True
    assert normalized.metadata["target_loudness_lufs"] == -16.0

    pip = worker.run(
        FfmpegEditRequest(
            operation="picture_in_picture",
            source_paths=[base, overlay],
            output_path=tmp_path / "pip.mp4",
            pip_position="bottom_right",
            pip_scale=0.3,
            pip_margin=16,
        )
    )
    assert pip.output_path.is_file()
    assert pip.width == 160
    assert pip.height == 284
    assert pip.metadata["pip_position"] == "bottom_right"


def test_ffmpeg_edit_worker_rejects_unknown_preset_and_single_source_pip(tmp_path) -> None:
    """Bad inputs fail with a value error before any ffmpeg invocation."""

    if shutil.which("ffmpeg") is None or shutil.which("ffprobe") is None:
        pytest.skip("ffmpeg and ffprobe are required")

    source = tmp_path / "source.mp4"
    _make_video(source, duration=0.4)
    worker = FfmpegEditWorker()

    with pytest.raises(ValueError, match="unknown grade preset"):
        worker.run(
            FfmpegEditRequest(
                operation="color_grade",
                source_paths=[source],
                output_path=tmp_path / "bad.mp4",
                grade_preset="nope",
            )
        )
    with pytest.raises(ValueError, match="two source paths"):
        worker.run(
            FfmpegEditRequest(
                operation="picture_in_picture",
                source_paths=[source],
                output_path=tmp_path / "pip.mp4",
            )
        )
    assert not (tmp_path / "bad.mp4").exists()
    assert not (tmp_path / "pip.mp4").exists()


def _make_video(path, *, duration: float, color: str = "red") -> None:
    completed = subprocess.run(
        [
            "ffmpeg",
            "-y",
            "-f",
            "lavfi",
            "-i",
            f"color=c={color}:s=160x284:d={duration}:r=12",
            "-f",
            "lavfi",
            "-i",
            f"sine=frequency=440:duration={duration}",
            "-shortest",
            "-c:v",
            "libx264",
            "-pix_fmt",
            "yuv420p",
            "-c:a",
            "aac",
            str(path),
        ],
        check=False,
        capture_output=True,
        text=True,
    )
    assert completed.returncode == 0, completed.stderr
