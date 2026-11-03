import shutil
import subprocess

import pytest

from packages.compose_kit import FfmpegComposeRequest, FfmpegComposeWorker


def test_ffmpeg_compose_worker_burns_subtitles_and_mixes_music(tmp_path) -> None:
    if shutil.which("ffmpeg") is None or shutil.which("ffprobe") is None:
        pytest.skip("ffmpeg and ffprobe are required")

    avatar_path = tmp_path / "avatar.mp4"
    narration_path = tmp_path / "narration.wav"
    music_path = tmp_path / "music.wav"
    subtitle_path = tmp_path / "subtitles.srt"
    output_path = tmp_path / "final.mp4"

    _run(
        [
            "ffmpeg",
            "-y",
            "-f",
            "lavfi",
            "-i",
            "color=c=black:s=320x568:d=2",
            "-f",
            "lavfi",
            "-i",
            "sine=frequency=880:duration=2",
            "-c:v",
            "libx264",
            "-pix_fmt",
            "yuv420p",
            "-c:a",
            "aac",
            str(avatar_path),
        ]
    )
    _run(
        [
            "ffmpeg",
            "-y",
            "-f",
            "lavfi",
            "-i",
            "sine=frequency=660:duration=2",
            str(narration_path),
        ]
    )
    _run(
        [
            "ffmpeg",
            "-y",
            "-f",
            "lavfi",
            "-i",
            "sine=frequency=330:duration=2",
            str(music_path),
        ]
    )
    subtitle_path.write_text(
        "1\n00:00:00,000 --> 00:00:01,500\nHello milk tea\n",
        encoding="utf-8",
    )

    worker = FfmpegComposeWorker()
    supports_subtitles = worker.supports_filter("subtitles")
    result = worker.compose(
        FfmpegComposeRequest(
            avatar_video_path=avatar_path,
            narration_path=narration_path,
            subtitle_path=subtitle_path,
            music_path=music_path,
            output_path=output_path,
            width=360,
            height=640,
            fps=24,
            preset="veryfast",
        )
    )

    assert output_path.is_file()
    assert output_path.stat().st_size > 0
    assert result.width == 360
    assert result.height == 640
    assert result.has_audio is True
    assert str(narration_path) in result.command
    assert result.duration_seconds is not None
    assert result.duration_seconds > 0
    assert result.subtitles_embedded is supports_subtitles
    if supports_subtitles:
        command = " ".join(result.command)
        assert "FontName=Noto Sans CJK SC" in command
    if not supports_subtitles:
        assert result.subtitle_track_added is True
        assert result.warnings == ["ffmpeg_subtitles_filter_unavailable_soft_track_used"]


def _run(command: list[str]) -> None:
    completed = subprocess.run(command, check=False, capture_output=True, text=True)
    assert completed.returncode == 0, completed.stderr
