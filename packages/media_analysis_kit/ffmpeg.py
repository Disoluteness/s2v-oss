from __future__ import annotations

import array
import json
import math
import re
import shutil
import subprocess
import sys
from collections.abc import Iterator
from pathlib import Path
from typing import Any

from pydantic import BaseModel, Field

# --- scene (shot) boundary detection ----------------------------------------
# FFmpeg's ``scene`` score is a 0..1 distance between consecutive frames. 0.30 is
# the value the FFmpeg documentation's own example uses; it is a *detector knob*,
# not a truth, so it is a parameter and the value actually used is echoed back in
# the report. Raising it returns fewer, more certain cuts.
DEFAULT_SCENE_THRESHOLD = 0.30
DEFAULT_MAX_SCENE_CUTS = 200

# --- audio energy analysis ---------------------------------------------------
# One window per half second is the resolution at which "there is a pause here"
# and "this sentence is shouted" are both legible, while keeping a one-hour
# source at ~7200 sample rows.
DEFAULT_AUDIO_WINDOW_SECONDS = 0.5
# Decoding to mono 8 kHz s16 keeps the whole analysis streaming and cheap: we
# never need fidelity, only the envelope. The PCM is read in chunks and reduced
# window by window, so memory does not scale with source length.
AUDIO_SAMPLE_RATE = 8000
AUDIO_BYTES_PER_SAMPLE = 2

# Silence is decided *relatively* (a quiet passage in a quiet recording is not
# silence) with an absolute floor so a file that is entirely silent does not end
# up wholly classified as "normal".
SILENCE_ABSOLUTE_DB = -50.0
SILENCE_RELATIVE_MARGIN_DB = 40.0
LOUD_RELATIVE_MARGIN_DB = 6.0

_SCENE_FRAME_RE = re.compile(r"pts_time:(-?[0-9]+(?:\.[0-9]+)?)")
_SCENE_SCORE_RE = re.compile(r"lavfi\.scene_score=([0-9]+(?:\.[0-9]+)?)")


class MediaFrameSample(BaseModel):
    path: Path
    timestamp_seconds: float
    timestamp_formatted: str
    width: int | None = None
    height: int | None = None


class SceneCut(BaseModel):
    """One detected scene change, as FFmpeg reported it."""

    index: int
    timestamp_seconds: float
    timestamp_formatted: str
    score: float | None = None


class SceneBoundary(BaseModel):
    """A span between two scene changes.

    The first boundary starts at 0 and the last one ends at the asset duration,
    so the boundaries always tile the whole source. That is the property clip
    selection needs: every candidate window has to be a subrange of exactly one
    of these, never a range that spans a cut.
    """

    index: int
    start_seconds: float
    end_seconds: float
    duration_seconds: float
    confidence: float | None = None
    detection_method: str = "ffmpeg_scene_detection"


class SceneDetectionResult(BaseModel):
    cuts: list[SceneCut] = Field(default_factory=list)
    boundaries: list[SceneBoundary] = Field(default_factory=list)
    detection_method: str = "ffmpeg_scene_detection"
    threshold: float = DEFAULT_SCENE_THRESHOLD
    warnings: list[str] = Field(default_factory=list)
    commands: list[list[str]] = Field(default_factory=list)


class AudioEnergySample(BaseModel):
    start_seconds: float
    end_seconds: float
    rms_db: float | None = None
    peak_db: float | None = None


class AudioEnergySegment(BaseModel):
    kind: str  # silence | loud | normal
    start_seconds: float
    end_seconds: float
    duration_seconds: float
    peak_rms_db: float | None = None


class AudioEnergyAnalysis(BaseModel):
    sample_interval_seconds: float
    duration_seconds: float | None = None
    samples: list[AudioEnergySample] = Field(default_factory=list)
    segments: list[AudioEnergySegment] = Field(default_factory=list)
    segments_truncated: bool = False
    mean_rms_db: float | None = None
    peak_rms_db: float | None = None
    dynamic_range_db: float | None = None
    silence_seconds: float | None = None
    silence_ratio: float | None = None
    warnings: list[str] = Field(default_factory=list)
    commands: list[list[str]] = Field(default_factory=list)


class FfmpegMediaAnalysisResult(BaseModel):
    source_path: Path
    probe: dict[str, Any]
    duration_seconds: float | None = None
    width: int | None = None
    height: int | None = None
    frame_rate: float | None = None
    video_codec: str | None = None
    audio_codecs: list[str] = Field(default_factory=list)
    has_video: bool
    has_audio: bool
    frames: list[MediaFrameSample] = Field(default_factory=list)
    scene_cuts: list[SceneCut] = Field(default_factory=list)
    scene_boundaries: list[SceneBoundary] = Field(default_factory=list)
    scene_detection_threshold: float | None = None
    audio_energy: AudioEnergyAnalysis | None = None
    warnings: list[str] = Field(default_factory=list)
    commands: list[list[str]] = Field(default_factory=list)


class FfmpegMediaAnalyzer:
    def __init__(
        self,
        *,
        ffmpeg_path: str | None = None,
        ffprobe_path: str | None = None,
        timeout_seconds: int = 120,
    ) -> None:
        self.ffmpeg_path = ffmpeg_path or shutil.which("ffmpeg")
        self.ffprobe_path = ffprobe_path or shutil.which("ffprobe")
        self.timeout_seconds = timeout_seconds

    def check_availability(self) -> bool:
        return bool(self.ffmpeg_path and self.ffprobe_path)

    def probe(self, path: Path) -> dict[str, Any]:
        if not self.ffprobe_path:
            raise RuntimeError("ffprobe binary is required for media analysis")
        completed = subprocess.run(
            [
                self.ffprobe_path,
                "-v",
                "error",
                "-print_format",
                "json",
                "-show_format",
                "-show_streams",
                str(path),
            ],
            check=False,
            capture_output=True,
            text=True,
            timeout=self.timeout_seconds,
        )
        if completed.returncode != 0:
            raise RuntimeError(_command_error(completed.stderr))
        return json.loads(completed.stdout or "{}")

    def detect_scene_cuts(
        self,
        path: Path,
        *,
        duration_seconds: float | None = None,
        threshold: float = DEFAULT_SCENE_THRESHOLD,
        max_cuts: int = DEFAULT_MAX_SCENE_CUTS,
    ) -> SceneDetectionResult:
        """Find scene changes with FFmpeg's ``scene`` filter.

        ``metadata=print:file=-`` is what makes this parseable: ``select`` alone
        tells us *that* a frame was kept but not when, and ``showinfo`` writes to
        the log stream where it interleaves with unrelated noise. The metadata
        writer emits a deterministic ``frame:``/``lavfi.scene_score=`` pair per
        kept frame on stdout, which is a contract we can test without a binary.
        """
        result = SceneDetectionResult(threshold=threshold)
        if not self.ffmpeg_path:
            result.warnings.append("scene_detection_unavailable:ffmpeg_missing")
            return result
        if not path.is_file():
            result.warnings.append("scene_detection_unavailable:source_missing")
            return result

        command = [
            self.ffmpeg_path,
            "-hide_banner",
            "-nostats",
            "-loglevel",
            "error",
            "-i",
            str(path),
            "-vf",
            f"select='gt(scene,{threshold:.4f})',metadata=print:file=-",
            "-an",
            "-f",
            "null",
            "-",
        ]
        result.commands.append(command)
        try:
            completed = subprocess.run(
                command,
                check=False,
                capture_output=True,
                text=True,
                timeout=self.timeout_seconds,
            )
        except subprocess.TimeoutExpired:
            result.warnings.append("scene_detection_failed:timeout")
            return result
        if completed.returncode != 0:
            result.warnings.append(
                f"scene_detection_failed:{_command_error(completed.stderr)}"
            )
            return result

        cuts = parse_scene_metadata(completed.stdout or "")
        if len(cuts) > max_cuts:
            result.warnings.append(
                f"scene_cut_count_truncated:{len(cuts)}:{max_cuts}"
            )
            cuts = cuts[:max_cuts]
        result.cuts = cuts
        result.boundaries = scene_boundaries_from_cuts(
            cuts,
            duration_seconds=duration_seconds,
        )
        if not cuts:
            result.warnings.append("scene_detection_no_cut_found")
        return result

    def analyze_audio_energy(
        self,
        path: Path,
        *,
        window_seconds: float = DEFAULT_AUDIO_WINDOW_SECONDS,
        sample_rate: int = AUDIO_SAMPLE_RATE,
    ) -> AudioEnergyAnalysis:
        """Measure loudness over time by decoding PCM and reducing it in Python.

        Two designs were rejected before this one. Parsing ``astats``/``ebur128``
        stderr means depending on log formatting across FFmpeg versions and on
        ``-loglevel`` choices made elsewhere. Shelling out to ``volumedetect``
        yields one number for the whole file, which is exactly the information
        that is *not* useful for finding a pause. Decoding to raw PCM and doing
        the arithmetic here is deterministic, testable without FFmpeg, and
        streams: peak memory is one read chunk, not the whole track.
        """
        analysis = AudioEnergyAnalysis(sample_interval_seconds=window_seconds)
        if not self.ffmpeg_path:
            analysis.warnings.append("audio_energy_unavailable:ffmpeg_missing")
            return analysis
        if not path.is_file():
            analysis.warnings.append("audio_energy_unavailable:source_missing")
            return analysis
        if window_seconds <= 0:
            analysis.warnings.append("audio_energy_unavailable:invalid_window")
            return analysis

        window_samples = max(1, round(sample_rate * window_seconds))
        command = [
            self.ffmpeg_path,
            "-hide_banner",
            "-nostats",
            "-loglevel",
            "error",
            "-i",
            str(path),
            "-vn",
            "-ac",
            "1",
            "-ar",
            str(sample_rate),
            "-f",
            "s16le",
            "-",
        ]
        analysis.commands.append(command)

        carry = b""
        samples: list[AudioEnergySample] = []
        offset_seconds = 0.0
        failures: list[str] = []
        try:
            for chunk in self._stream_pcm(command):
                carry += chunk
                usable = len(carry) - (len(carry) % AUDIO_BYTES_PER_SAMPLE)
                consumed = 0
                while usable - consumed >= window_samples * AUDIO_BYTES_PER_SAMPLE:
                    span = window_samples * AUDIO_BYTES_PER_SAMPLE
                    window = carry[consumed : consumed + span]
                    consumed += span
                    samples.append(
                        _window_sample(
                            window,
                            start_seconds=offset_seconds,
                            window_seconds=window_samples / sample_rate,
                        )
                    )
                    offset_seconds += window_samples / sample_rate
                carry = carry[consumed:]
        except RuntimeError as exc:
            failures.append(str(exc))

        # A trailing partial window still describes real audio; dropping it would
        # quietly shorten every source by up to one window.
        if len(carry) >= AUDIO_BYTES_PER_SAMPLE and not failures:
            samples.append(
                _window_sample(
                    carry,
                    start_seconds=offset_seconds,
                    window_seconds=len(carry) // AUDIO_BYTES_PER_SAMPLE / sample_rate,
                )
            )

        if failures:
            analysis.warnings.append(f"audio_energy_failed:{failures[0]}")
        if not samples:
            analysis.warnings.append("audio_energy_no_samples")
            return analysis

        analysis.samples = samples
        analysis.duration_seconds = round(offset_seconds, 3)
        summary = summarise_audio_energy(
            samples,
            sample_interval_seconds=window_seconds,
        )
        analysis.segments = summary["segments"]
        analysis.segments_truncated = summary["segments_truncated"]
        analysis.mean_rms_db = summary["mean_rms_db"]
        analysis.peak_rms_db = summary["peak_rms_db"]
        analysis.dynamic_range_db = summary["dynamic_range_db"]
        analysis.silence_seconds = summary["silence_seconds"]
        analysis.silence_ratio = summary["silence_ratio"]
        return analysis

    def analyze(
        self,
        path: Path,
        *,
        output_dir: Path,
        max_frames: int = 5,
        detect_scenes: bool = True,
        analyze_audio: bool = True,
        scene_threshold: float = DEFAULT_SCENE_THRESHOLD,
        audio_window_seconds: float = DEFAULT_AUDIO_WINDOW_SECONDS,
    ) -> FfmpegMediaAnalysisResult:
        if not self.ffmpeg_path:
            raise RuntimeError("ffmpeg binary is required for frame extraction")
        if not path.is_file():
            raise ValueError("source media path does not exist")

        probe = self.probe(path)
        video_stream = _first_stream(probe, "video")
        audio_streams = _streams(probe, "audio")
        duration_seconds = _duration(probe)
        frames: list[MediaFrameSample] = []
        commands: list[list[str]] = []
        warnings: list[str] = []

        if video_stream and max_frames > 0:
            output_dir.mkdir(parents=True, exist_ok=True)
            for index, timestamp in enumerate(
                _sample_timestamps(duration_seconds, max_frames),
                start=1,
            ):
                frame_path = output_dir / f"frame_{index:04d}.jpg"
                command = [
                    self.ffmpeg_path,
                    "-y",
                    "-loglevel",
                    "error",
                    "-ss",
                    f"{timestamp:.3f}",
                    "-i",
                    str(path),
                    "-frames:v",
                    "1",
                    "-q:v",
                    "3",
                    str(frame_path),
                ]
                commands.append(command)
                completed = subprocess.run(
                    command,
                    check=False,
                    capture_output=True,
                    text=True,
                    timeout=self.timeout_seconds,
                )
                if completed.returncode != 0 or not frame_path.is_file():
                    warnings.append(
                        f"frame_extraction_failed:{index}:{_command_error(completed.stderr)}"
                    )
                    continue
                frames.append(
                    MediaFrameSample(
                        path=frame_path,
                        timestamp_seconds=round(timestamp, 3),
                        timestamp_formatted=_format_timestamp(timestamp),
                        width=_int_or_none(video_stream.get("width")),
                        height=_int_or_none(video_stream.get("height")),
                    )
                )
        elif not video_stream:
            warnings.append("no_video_stream_for_frame_extraction")

        scene_cuts: list[SceneCut] = []
        scene_boundaries: list[SceneBoundary] = []
        scene_threshold_used: float | None = None
        if video_stream and detect_scenes:
            scene_result = self.detect_scene_cuts(
                path,
                duration_seconds=duration_seconds,
                threshold=scene_threshold,
            )
            scene_cuts = scene_result.cuts
            scene_boundaries = scene_result.boundaries
            scene_threshold_used = scene_result.threshold
            warnings.extend(scene_result.warnings)
            commands.extend(scene_result.commands)
        elif detect_scenes and not video_stream:
            # A warning belongs to a request that could not be honoured. When
            # the caller disabled detection on purpose, silence is the honest
            # answer — "no video" is only a problem if we were asked to cut it.
            warnings.append("no_video_stream_for_scene_detection")

        audio_energy: AudioEnergyAnalysis | None = None
        if audio_streams and analyze_audio:
            audio_energy = self.analyze_audio_energy(
                path,
                window_seconds=audio_window_seconds,
            )
            warnings.extend(audio_energy.warnings)
            commands.extend(audio_energy.commands)
        elif analyze_audio and not audio_streams:
            warnings.append("no_audio_stream_for_energy_analysis")

        return FfmpegMediaAnalysisResult(
            source_path=path,
            probe=probe,
            duration_seconds=duration_seconds,
            width=_int_or_none(video_stream.get("width")) if video_stream else None,
            height=_int_or_none(video_stream.get("height")) if video_stream else None,
            frame_rate=_frame_rate(video_stream) if video_stream else None,
            video_codec=str(video_stream.get("codec_name") or "") or None
            if video_stream
            else None,
            audio_codecs=[
                str(stream.get("codec_name"))
                for stream in audio_streams
                if stream.get("codec_name")
            ],
            has_video=video_stream is not None,
            has_audio=bool(audio_streams),
            frames=frames,
            scene_cuts=scene_cuts,
            scene_boundaries=scene_boundaries,
            scene_detection_threshold=scene_threshold_used,
            audio_energy=audio_energy,
            warnings=warnings,
            commands=commands,
        )

    def _stream_pcm(self, command: list[str]) -> Iterator[bytes]:
        """Yield raw PCM chunks, raising ``RuntimeError`` on a failed decode.

        ``stderr`` is a pipe rather than ``DEVNULL`` so a failure comes back with
        FFmpeg's own words. That is safe here only because the decode is run at
        ``-loglevel error``: a healthy run writes nothing, so it cannot fill the
        pipe while we are blocked reading stdout.
        """
        process = subprocess.Popen(
            command,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        finished = False
        try:
            assert process.stdout is not None
            while True:
                chunk = process.stdout.read(65536)
                if not chunk:
                    break
                yield chunk
            finished = True
        finally:
            if not finished:
                process.kill()
            process.wait()
            if process.returncode not in (0, None) and finished:
                stderr = process.stderr.read() if process.stderr else b""
                raise RuntimeError(
                    _command_error(stderr.decode("utf-8", errors="replace"))
                )
            if process.stdout is not None:
                process.stdout.close()


def parse_scene_metadata(output: str) -> list[SceneCut]:
    """Turn ``metadata=print`` output into cuts.

    The printer emits the frame line first and the score second, so the parser
    carries the timestamp forward. A score line with no preceding frame line is
    still recorded (with timestamp 0) rather than dropped — a cut we cannot place
    is more useful than a cut we pretend does not exist.
    """
    cuts: list[SceneCut] = []
    pending: float | None = None
    for raw_line in output.splitlines():
        line = raw_line.strip()
        if line.startswith("frame:"):
            match = _SCENE_FRAME_RE.search(line)
            pending = float(match.group(1)) if match else None
            continue
        if line.startswith("lavfi.scene_score="):
            match = _SCENE_SCORE_RE.search(line)
            score = float(match.group(1)) if match else None
            timestamp = pending if pending is not None else 0.0
            if timestamp < 0:
                timestamp = 0.0
            cuts.append(
                SceneCut(
                    index=len(cuts) + 1,
                    timestamp_seconds=round(timestamp, 3),
                    timestamp_formatted=_format_timestamp(timestamp),
                    score=score,
                )
            )
            pending = None
    return cuts


def scene_boundaries_from_cuts(
    cuts: list[SceneCut],
    *,
    duration_seconds: float | None,
) -> list[SceneBoundary]:
    """Tile the source into spans delimited by the cuts."""
    times: list[float] = []
    scores: dict[int, float | None] = {}
    for cut in cuts:
        timestamp = round(float(cut.timestamp_seconds), 3)
        if timestamp <= 0:
            # A cut at 0 would produce a zero-length first span.
            continue
        if duration_seconds is not None and timestamp >= duration_seconds:
            # A cut at or past the end cannot open a span that exists.
            continue
        if times and math.isclose(times[-1], timestamp, abs_tol=1e-3):
            continue
        times.append(timestamp)
        scores[len(times)] = cut.score

    boundaries: list[SceneBoundary] = []
    edges = [0.0, *times]
    if duration_seconds is not None and duration_seconds > edges[-1]:
        edges.append(round(float(duration_seconds), 3))
    for index in range(len(edges) - 1):
        start = edges[index]
        end = edges[index + 1]
        if end <= start:
            continue
        boundaries.append(
            SceneBoundary(
                index=len(boundaries) + 1,
                start_seconds=round(start, 3),
                end_seconds=round(end, 3),
                duration_seconds=round(end - start, 3),
                confidence=scores.get(index + 1) if index > 0 else None,
            )
        )
    return boundaries


def summarise_audio_energy(
    samples: list[AudioEnergySample],
    *,
    sample_interval_seconds: float,
    max_segments: int = 1000,
) -> dict[str, Any]:
    """Classify windows into silence / loud / normal and summarise the track.

    ``-inf`` decodes to ``None`` in the sample model (see ``_window_sample``), so
    every statistic here has to survive a missing value: a fully silent track is
    a legitimate input, not a degenerate one.

    ``max_segments`` bounds the report, not the analysis: conversational audio
    alternates between kinds every couple of seconds, and a one-hour source
    would otherwise carry thousands of rows into every artifact it lands in.
    Earlier segments win because clip planning looks forward from the start.
    """
    rms_values = [s.rms_db for s in samples if s.rms_db is not None]
    if not rms_values:
        return {
            "segments": [],
            "segments_truncated": False,
            "mean_rms_db": None,
            "peak_rms_db": None,
            "dynamic_range_db": None,
            "silence_seconds": 0.0,
            "silence_ratio": 1.0 if samples else None,
        }

    peak_rms_db = round(max(rms_values), 3)
    mean_rms_db = round(sum(rms_values) / len(rms_values), 3)
    silence_ceiling = max(SILENCE_ABSOLUTE_DB, peak_rms_db - SILENCE_RELATIVE_MARGIN_DB)
    loud_floor = peak_rms_db - LOUD_RELATIVE_MARGIN_DB

    kinds: list[str] = []
    for sample in samples:
        value = sample.rms_db
        if value is None or value <= silence_ceiling:
            kinds.append("silence")
        elif value >= loud_floor:
            kinds.append("loud")
        else:
            kinds.append("normal")

    segments: list[AudioEnergySegment] = []
    for index, kind in enumerate(kinds):
        sample = samples[index]
        if segments and segments[-1].kind == kind and math.isclose(
            segments[-1].end_seconds, sample.start_seconds, abs_tol=1e-3
        ):
            previous = segments[-1]
            previous.end_seconds = round(sample.end_seconds, 3)
            previous.duration_seconds = round(
                previous.end_seconds - previous.start_seconds, 3
            )
            if sample.rms_db is not None:
                previous.peak_rms_db = round(
                    max(previous.peak_rms_db or sample.rms_db, sample.rms_db), 3
                )
            continue
        segments.append(
            AudioEnergySegment(
                kind=kind,
                start_seconds=round(sample.start_seconds, 3),
                end_seconds=round(sample.end_seconds, 3),
                duration_seconds=round(sample.end_seconds - sample.start_seconds, 3),
                peak_rms_db=sample.rms_db,
            )
        )

    silence_seconds = round(
        sum(
            sample.end_seconds - sample.start_seconds
            for sample, kind in zip(samples, kinds, strict=True)
            if kind == "silence"
        ),
        3,
    )
    total_seconds = round(
        sum(sample.end_seconds - sample.start_seconds for sample in samples), 3
    )
    truncated = max_segments > 0 and len(segments) > max_segments
    return {
        "segments": segments[:max_segments] if max_segments > 0 else segments,
        "segments_truncated": truncated,
        "mean_rms_db": mean_rms_db,
        "peak_rms_db": peak_rms_db,
        "dynamic_range_db": round(peak_rms_db - min(rms_values), 3),
        "silence_seconds": silence_seconds,
        "silence_ratio": round(silence_seconds / total_seconds, 4)
        if total_seconds > 0
        else None,
    }


def _window_sample(
    window: bytes,
    *,
    start_seconds: float,
    window_seconds: float,
) -> AudioEnergySample:
    values = array.array("h")
    values.frombytes(window[: len(window) - (len(window) % AUDIO_BYTES_PER_SAMPLE)])
    if sys.byteorder == "big":
        values.byteswap()
    if not values:
        return AudioEnergySample(
            start_seconds=round(start_seconds, 3),
            end_seconds=round(start_seconds + window_seconds, 3),
            rms_db=None,
            peak_db=None,
        )
    total = 0
    peak = 0
    for value in values:
        total += value * value
        peak = max(peak, value if value >= 0 else -value)
    rms = math.sqrt(total / len(values)) / 32768.0
    peak_ratio = peak / 32768.0
    return AudioEnergySample(
        start_seconds=round(start_seconds, 3),
        end_seconds=round(start_seconds + window_seconds, 3),
        rms_db=_amplitude_to_db(rms),
        peak_db=_amplitude_to_db(peak_ratio),
    )


def _amplitude_to_db(ratio: float) -> float | None:
    """Digital full scale is 0 dB; digital silence is ``None``, not ``-inf``.

    ``None`` is the honest representation: it says "no measurable energy", keeps
    the value JSON-serialisable, and stops ``-inf`` from poisoning the averages.
    """
    if ratio <= 0:
        return None
    return round(20.0 * math.log10(ratio), 3)


def _streams(probe: dict[str, Any], codec_type: str) -> list[dict[str, Any]]:
    return [
        stream
        for stream in probe.get("streams", [])
        if stream.get("codec_type") == codec_type
    ]


def _first_stream(probe: dict[str, Any], codec_type: str) -> dict[str, Any] | None:
    return next(iter(_streams(probe, codec_type)), None)


def _duration(probe: dict[str, Any]) -> float | None:
    raw_duration = (probe.get("format") or {}).get("duration")
    if raw_duration is None:
        raw_duration = next(
            (
                stream.get("duration")
                for stream in probe.get("streams", [])
                if stream.get("duration") is not None
            ),
            None,
        )
    try:
        return round(float(raw_duration), 3) if raw_duration is not None else None
    except (TypeError, ValueError):
        return None


def _frame_rate(video_stream: dict[str, Any] | None) -> float | None:
    if not video_stream:
        return None
    raw_rate = video_stream.get("avg_frame_rate") or video_stream.get("r_frame_rate")
    if not raw_rate or raw_rate == "0/0":
        return None
    if "/" in str(raw_rate):
        numerator, denominator = str(raw_rate).split("/", 1)
        try:
            denominator_value = float(denominator)
            if denominator_value == 0:
                return None
            return round(float(numerator) / denominator_value, 3)
        except (TypeError, ValueError):
            return None
    try:
        return round(float(raw_rate), 3)
    except (TypeError, ValueError):
        return None


def _sample_timestamps(duration_seconds: float | None, max_frames: int) -> list[float]:
    frame_count = max(1, min(max_frames, 20))
    if duration_seconds is None or duration_seconds <= 0:
        return [0.0]
    if frame_count == 1:
        return [0.0]
    end = max(duration_seconds - 0.05, 0.0)
    step = end / (frame_count - 1)
    return [round(min(index * step, end), 3) for index in range(frame_count)]


def _format_timestamp(seconds: float) -> str:
    total_seconds = max(0, round(seconds))
    hours, remainder = divmod(total_seconds, 3600)
    minutes, second = divmod(remainder, 60)
    if hours:
        return f"{hours:02d}:{minutes:02d}:{second:02d}"
    return f"{minutes:02d}:{second:02d}"


def _int_or_none(value: Any) -> int | None:
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _command_error(stderr: str) -> str:
    return (stderr or "ffmpeg command failed").strip()[-1000:]
