import math
import shutil
import subprocess

import pytest

from packages.media_analysis_kit import (
    AudioEnergySample,
    FfmpegMediaAnalyzer,
    SceneCut,
    parse_scene_metadata,
    scene_boundaries_from_cuts,
    summarise_audio_energy,
)
from packages.media_analysis_kit.ffmpeg import _window_sample


def test_ffmpeg_media_analyzer_probes_and_extracts_frames(tmp_path) -> None:
    if shutil.which("ffmpeg") is None or shutil.which("ffprobe") is None:
        pytest.skip("ffmpeg and ffprobe are required")

    source_path = tmp_path / "source.mp4"
    _run(
        [
            "ffmpeg",
            "-y",
            "-f",
            "lavfi",
            "-i",
            "testsrc=size=320x180:rate=24:duration=1",
            "-f",
            "lavfi",
            "-i",
            "sine=frequency=440:duration=1",
            "-c:v",
            "libx264",
            "-pix_fmt",
            "yuv420p",
            "-c:a",
            "aac",
            str(source_path),
        ]
    )

    analyzer = FfmpegMediaAnalyzer()
    result = analyzer.analyze(
        source_path,
        output_dir=tmp_path / "frames",
        max_frames=2,
    )

    assert result.has_video is True
    assert result.has_audio is True
    assert result.width == 320
    assert result.height == 180
    assert result.duration_seconds is not None
    assert result.duration_seconds > 0
    assert len(result.frames) == 2
    assert all(frame.path.is_file() for frame in result.frames)


def _run(command: list[str]) -> None:
    completed = subprocess.run(command, check=False, capture_output=True, text=True)
    assert completed.returncode == 0, completed.stderr
# --- pure parsing / reduction units (no FFmpeg needed) -----------------------


def test_parse_scene_metadata_pairs_frames_with_scores() -> None:
    output = (
        "frame:0 pts:0 pts_time:2.5\n"
        "lavfi.scene_score=0.620000\n"
        "frame:1 pts:6000 pts_time:9.5\n"
        "lavfi.scene_score=0.510000"
    )

    cuts = parse_scene_metadata(output)

    assert [cut.timestamp_seconds for cut in cuts] == [2.5, 9.5]
    assert [cut.score for cut in cuts] == [0.62, 0.51]
    assert [cut.index for cut in cuts] == [1, 2]
    assert cuts[0].timestamp_formatted == "00:02"


def test_parse_scene_metadata_keeps_orphan_score_line() -> None:
    # A score without a preceding frame line still describes a cut; dropping
    # it would quietly under-count. It is placed at t=0 where the boundary
    # builder clamps it out of the span list rather than making a zero span.
    cuts = parse_scene_metadata("lavfi.scene_score=0.400000")

    assert len(cuts) == 1
    assert cuts[0].score == 0.4


def test_scene_boundaries_tile_the_whole_source() -> None:
    cuts = [
        SceneCut(index=1, timestamp_seconds=4.0, timestamp_formatted="00:04", score=0.6),
        SceneCut(index=2, timestamp_seconds=9.5, timestamp_formatted="00:09", score=0.5),
    ]

    boundaries = scene_boundaries_from_cuts(cuts, duration_seconds=12.5)

    assert [(b.start_seconds, b.end_seconds) for b in boundaries] == [
        (0.0, 4.0),
        (4.0, 9.5),
        (9.5, 12.5),
    ]
    # Boundaries tile, so adjacent spans share an edge and durations add up.
    assert math.isclose(
        sum(b.duration_seconds for b in boundaries), 12.5, abs_tol=1e-6
    )
    # The first span has no preceding cut to score it.
    assert boundaries[0].confidence is None
    assert boundaries[1].confidence == 0.5


def test_scene_boundaries_drop_degenerate_cuts() -> None:
    cuts = [
        SceneCut(index=1, timestamp_seconds=0.0, timestamp_formatted="00:00", score=0.9),
        SceneCut(index=2, timestamp_seconds=12.5, timestamp_formatted="00:12", score=0.9),
        SceneCut(index=3, timestamp_seconds=5.0, timestamp_formatted="00:05", score=0.4),
    ]

    boundaries = scene_boundaries_from_cuts(cuts, duration_seconds=12.5)

    # A cut at 0 and a cut at the end cannot open a real span.
    assert [(b.start_seconds, b.end_seconds) for b in boundaries] == [
        (0.0, 5.0),
        (5.0, 12.5),
    ]


def test_summarise_audio_energy_classifies_and_aggregates() -> None:
    def sample(start: float, rms_db: float | None) -> AudioEnergySample:
        return AudioEnergySample(
            start_seconds=start,
            end_seconds=start + 0.5,
            rms_db=rms_db,
            peak_db=None,
        )

    samples = [
        sample(0.0, -6.0),  # loud (within 6 dB of peak)
        sample(0.5, -20.0),  # normal
        sample(1.0, -55.0),  # silence (below relative ceiling)
        sample(1.5, -6.0),  # loud again -> third segment
    ]

    summary = summarise_audio_energy(samples, sample_interval_seconds=0.5)

    assert [segment.kind for segment in summary["segments"]] == [
        "loud",
        "normal",
        "silence",
        "loud",
    ]
    assert summary["peak_rms_db"] == -6.0
    assert summary["dynamic_range_db"] == 49.0
    assert summary["silence_seconds"] == 0.5
    assert summary["silence_ratio"] == 0.25
    assert summary["segments_truncated"] is False


def test_summarise_audio_energy_survives_digital_silence_and_truncates() -> None:
    silent = [
        AudioEnergySample(
            start_seconds=start,
            end_seconds=start + 0.5,
            rms_db=None,
            peak_db=None,
        )
        for start in (0.0, 0.5)
    ]

    summary = summarise_audio_energy(silent, sample_interval_seconds=0.5)

    assert summary["segments"] == []
    assert summary["mean_rms_db"] is None
    assert summary["silence_ratio"] == 1.0

    loud = [
        AudioEnergySample(
            start_seconds=index * 0.5,
            end_seconds=index * 0.5 + 0.5,
            rms_db=-10.0 if index % 2 == 0 else -20.0,
            peak_db=None,
        )
        for index in range(6)
    ]
    truncated = summarise_audio_energy(
        loud, sample_interval_seconds=0.5, max_segments=2
    )
    assert len(truncated["segments"]) == 2
    assert truncated["segments_truncated"] is True


def test_window_sample_converts_pcm_to_db() -> None:
    # One full-scale sample followed by digital silence: RMS is well below
    # peak, and digital silence alone decodes to None, not -inf.
    full_scale = _window_sample(
        (32767).to_bytes(2, "little") + b"\x00\x00",
        start_seconds=0.0,
        window_seconds=0.25,
    )
    assert full_scale.peak_db == 0.0
    # RMS of [full scale, silence] is 0.707 amplitude = -3.01 dB.
    assert full_scale.rms_db == pytest.approx(-3.01, abs=0.05)

    silent = _window_sample(b"\x00\x00\x00\x00", start_seconds=1.0, window_seconds=0.25)
    assert silent.rms_db is None
    assert silent.peak_db is None


# --- FFmpeg-backed integration ----------------------------------------------


def test_ffmpeg_scene_detection_finds_hard_color_cut(tmp_path) -> None:
    if shutil.which("ffmpeg") is None or shutil.which("ffprobe") is None:
        pytest.skip("ffmpeg and ffprobe are required")

    source_path = tmp_path / "two_scenes.mp4"
    # Two solid colors with a hard cut between them: the most unambiguous
    # scene change a detector can be asked to find. The sine tone keeps an
    # audio stream present so the same file exercises both new paths.
    _run(
        [
            "ffmpeg",
            "-y",
            "-f",
            "lavfi",
            "-i",
            "color=c=red:size=160x90:rate=10:duration=2",
            "-f",
            "lavfi",
            "-i",
            "color=c=blue:size=160x90:rate=10:duration=2",
            "-f",
            "lavfi",
            "-i",
            "sine=frequency=440:duration=4",
            "-filter_complex",
            "[0:v][1:v]concat=n=2:v=1:a=0[v]",
            "-map",
            "[v]",
            "-map",
            "2:a",
            "-c:v",
            "libx264",
            "-pix_fmt",
            "yuv420p",
            "-c:a",
            "aac",
            str(source_path),
        ]
    )

    analyzer = FfmpegMediaAnalyzer()
    scene_result = analyzer.detect_scene_cuts(
        source_path,
        duration_seconds=4.0,
        threshold=0.30,
    )

    assert scene_result.threshold == 0.30
    # Exactly one hard cut, and the boundaries tile the full four seconds.
    assert len(scene_result.cuts) == 1
    cut = scene_result.cuts[0]
    assert cut.timestamp_seconds == pytest.approx(2.0, abs=0.2)
    assert [(b.start_seconds, b.end_seconds) for b in scene_result.boundaries] == [
        (0.0, cut.timestamp_seconds),
        (cut.timestamp_seconds, 4.0),
    ]

    full = analyzer.analyze(
        source_path,
        output_dir=tmp_path / "frames",
        max_frames=2,
    )
    assert full.scene_detection_threshold == 0.30
    assert len(full.scene_boundaries) == 2

    audio = full.audio_energy
    assert audio is not None
    assert audio.sample_interval_seconds == 0.5
    assert audio.samples, "a 4-second tone should yield 8+ energy windows"
    assert audio.mean_rms_db is not None and audio.mean_rms_db > -60.0
    assert audio.silence_ratio is not None and audio.silence_ratio < 0.5

    # Disabled on purpose: no scene work, no audio work, no misleading warnings.
    minimal = analyzer.analyze(
        source_path,
        output_dir=tmp_path / "frames_min",
        max_frames=1,
        detect_scenes=False,
        analyze_audio=False,
    )
    assert minimal.scene_cuts == []
    assert minimal.audio_energy is None
    assert not any("scene" in warning for warning in minimal.warnings)
    assert not any("energy" in warning for warning in minimal.warnings)
