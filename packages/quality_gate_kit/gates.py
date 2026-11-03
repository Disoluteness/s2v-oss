from __future__ import annotations

import json
import re
import shutil
import subprocess
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from pydantic import BaseModel, Field

DEFAULT_CHECKS = [
    "ffprobe_valid",
    "resolution",
    "frame_rate",
    "audio_presence",
    "audio_loudness",
    "black_frames",
    "frozen_frames",
    "subtitle_bounds",
    "cjk_font",
    "provider_output",
]


class QualityGateEvaluationRequest(BaseModel):
    media_path: Path
    render_asset: dict[str, Any] = Field(default_factory=dict)
    render_summary: dict[str, Any] = Field(default_factory=dict)
    composition_graph: dict[str, Any] = Field(default_factory=dict)
    checks: list[str] | None = None
    min_score: int = Field(default=80, ge=0, le=100)
    expected_audio: bool | None = None


class QualityGateRunner:
    runner_key = "quality_gate_runner"
    provider_key = "local"

    def __init__(
        self,
        *,
        ffmpeg_path: str | None = None,
        ffprobe_path: str | None = None,
        timeout_seconds: int = 180,
    ) -> None:
        self.ffmpeg_path = ffmpeg_path or shutil.which("ffmpeg")
        self.ffprobe_path = ffprobe_path or shutil.which("ffprobe")
        self.timeout_seconds = timeout_seconds

    def check_availability(self) -> bool:
        return bool(self.ffprobe_path)

    def evaluate(self, request: QualityGateEvaluationRequest) -> dict[str, Any]:
        commands: list[list[str]] = []
        checks = []
        active_checks = request.checks or DEFAULT_CHECKS
        probe: dict[str, Any] = {}
        media_summary: dict[str, Any] = {}

        if not request.media_path.is_file():
            checks.append(
                _check(
                    "media_file_exists",
                    "failed",
                    "error",
                    "Rendered media file does not exist.",
                    measured={"path": str(request.media_path)},
                )
            )
            return _report(request=request, checks=checks, media=media_summary, commands=commands)

        try:
            probe, probe_command = self._probe(request.media_path)
            commands.append(probe_command)
            media_summary = _media_summary(probe)
            checks.append(
                _check(
                    "ffprobe_valid",
                    "passed",
                    "info",
                    "ffprobe could read the rendered media.",
                    measured={"format": (probe.get("format") or {}).get("format_name")},
                )
            )
        except RuntimeError as exc:
            checks.append(
                _check(
                    "ffprobe_valid",
                    "failed",
                    "error",
                    "ffprobe could not read the rendered media.",
                    measured={"error": str(exc)},
                )
            )
            return _report(request=request, checks=checks, media=media_summary, commands=commands)

        expected = _expected_render_settings(request)
        if "resolution" in active_checks:
            checks.append(_resolution_check(media_summary, expected))
        if "frame_rate" in active_checks:
            checks.append(_frame_rate_check(media_summary, expected))
        if "audio_presence" in active_checks:
            checks.append(_audio_presence_check(media_summary, request))
        if "audio_loudness" in active_checks:
            loudness_check, loudness_commands = self._audio_loudness_check(request, media_summary)
            checks.append(loudness_check)
            commands.extend(loudness_commands)
        if "black_frames" in active_checks:
            black_check, black_commands = self._black_frame_check(request, media_summary)
            checks.append(black_check)
            commands.extend(black_commands)
        if "frozen_frames" in active_checks:
            freeze_check, freeze_commands = self._frozen_frame_check(request, media_summary)
            checks.append(freeze_check)
            commands.extend(freeze_commands)
        if "subtitle_bounds" in active_checks:
            checks.append(_subtitle_bounds_check(request))
        if "cjk_font" in active_checks:
            checks.append(_cjk_font_check(request))
        if "provider_output" in active_checks:
            checks.append(_provider_output_check(request))

        return _report(request=request, checks=checks, media=media_summary, commands=commands)

    def _probe(self, path: Path) -> tuple[dict[str, Any], list[str]]:
        if not self.ffprobe_path:
            raise RuntimeError("ffprobe binary is required for quality gates")
        command = [
            self.ffprobe_path,
            "-v",
            "error",
            "-print_format",
            "json",
            "-show_format",
            "-show_streams",
            str(path),
        ]
        completed = subprocess.run(
            command,
            check=False,
            capture_output=True,
            text=True,
            timeout=self.timeout_seconds,
        )
        if completed.returncode != 0:
            raise RuntimeError(_command_error(completed.stderr))
        return json.loads(completed.stdout or "{}"), command

    def _audio_loudness_check(
        self,
        request: QualityGateEvaluationRequest,
        media_summary: dict[str, Any],
    ) -> tuple[dict[str, Any], list[list[str]]]:
        if not media_summary.get("has_audio"):
            return (
                _check(
                    "audio_loudness",
                    "skipped",
                    "info",
                    "Audio loudness check skipped because the media has no audio stream.",
                ),
                [],
            )
        if not self.ffmpeg_path:
            return (
                _check(
                    "audio_loudness",
                    "warning",
                    "warning",
                    "ffmpeg is unavailable, so audio loudness could not be measured.",
                ),
                [],
            )
        command = [
            self.ffmpeg_path,
            "-hide_banner",
            "-nostats",
            "-i",
            str(request.media_path),
            "-af",
            "volumedetect",
            "-f",
            "null",
            "-",
        ]
        completed = subprocess.run(
            command,
            check=False,
            capture_output=True,
            text=True,
            timeout=self.timeout_seconds,
        )
        if completed.returncode != 0:
            return (
                _check(
                    "audio_loudness",
                    "warning",
                    "warning",
                    "Audio loudness measurement failed.",
                    measured={"error": _command_error(completed.stderr)},
                ),
                [command],
            )
        mean_volume = _parse_db_value(completed.stderr, "mean_volume")
        max_volume = _parse_db_value(completed.stderr, "max_volume")
        if mean_volume is None:
            status = "warning"
            message = "Audio loudness output did not include mean volume."
        elif mean_volume < -35:
            status = "warning"
            message = "Audio appears very quiet and may need normalization."
        elif mean_volume > -8:
            status = "warning"
            message = "Audio appears hot and may clip on some platforms."
        else:
            status = "passed"
            message = "Audio loudness is within the preview delivery range."
        return (
            _check(
                "audio_loudness",
                status,
                "warning" if status == "warning" else "info",
                message,
                expected={"mean_volume_db_range": [-35, -8]},
                measured={"mean_volume_db": mean_volume, "max_volume_db": max_volume},
            ),
            [command],
        )

    def _black_frame_check(
        self,
        request: QualityGateEvaluationRequest,
        media_summary: dict[str, Any],
    ) -> tuple[dict[str, Any], list[list[str]]]:
        if not media_summary.get("has_video"):
            return (
                _check("black_frames", "skipped", "info", "No video stream to inspect."),
                [],
            )
        if not self.ffmpeg_path:
            return (
                _check(
                    "black_frames",
                    "warning",
                    "warning",
                    "ffmpeg is unavailable, so black frames could not be detected.",
                ),
                [],
            )
        command = [
            self.ffmpeg_path,
            "-hide_banner",
            "-nostats",
            "-i",
            str(request.media_path),
            "-vf",
            "blackdetect=d=0.5:pix_th=0.10",
            "-an",
            "-f",
            "null",
            "-",
        ]
        completed = subprocess.run(
            command,
            check=False,
            capture_output=True,
            text=True,
            timeout=self.timeout_seconds,
        )
        if completed.returncode != 0:
            return (
                _check(
                    "black_frames",
                    "warning",
                    "warning",
                    "Black frame detection failed.",
                    measured={"error": _command_error(completed.stderr)},
                ),
                [command],
            )
        durations = [
            float(match)
            for match in re.findall(r"black_duration:([0-9.]+)", completed.stderr)
        ]
        longest = max(durations) if durations else 0.0
        status = "warning" if longest >= 1.5 else "passed"
        return (
            _check(
                "black_frames",
                status,
                "warning" if status == "warning" else "info",
                "Long black frames detected." if status == "warning" else "No long black frames detected.",
                expected={"max_black_duration_seconds": 1.5},
                measured={"black_segment_count": len(durations), "longest_seconds": round(longest, 3)},
            ),
            [command],
        )

    def _frozen_frame_check(
        self,
        request: QualityGateEvaluationRequest,
        media_summary: dict[str, Any],
    ) -> tuple[dict[str, Any], list[list[str]]]:
        if not media_summary.get("has_video"):
            return (
                _check("frozen_frames", "skipped", "info", "No video stream to inspect."),
                [],
            )
        if not self.ffmpeg_path:
            return (
                _check(
                    "frozen_frames",
                    "warning",
                    "warning",
                    "ffmpeg is unavailable, so frozen frames could not be detected.",
                ),
                [],
            )
        command = [
            self.ffmpeg_path,
            "-hide_banner",
            "-nostats",
            "-i",
            str(request.media_path),
            "-vf",
            "freezedetect=n=0.003:d=1.0",
            "-an",
            "-f",
            "null",
            "-",
        ]
        completed = subprocess.run(
            command,
            check=False,
            capture_output=True,
            text=True,
            timeout=self.timeout_seconds,
        )
        if completed.returncode != 0:
            return (
                _check(
                    "frozen_frames",
                    "warning",
                    "warning",
                    "Frozen frame detection failed.",
                    measured={"error": _command_error(completed.stderr)},
                ),
                [command],
            )
        durations = [
            float(match)
            for match in re.findall(r"lavfi\.freezedetect\.freeze_duration:\s*([0-9.]+)", completed.stderr)
        ]
        longest = max(durations) if durations else 0.0
        status = "warning" if longest >= 2.5 else "passed"
        return (
            _check(
                "frozen_frames",
                status,
                "warning" if status == "warning" else "info",
                "Long static segments detected." if status == "warning" else "No long static segments detected.",
                expected={"max_static_duration_seconds": 2.5},
                measured={"freeze_segment_count": len(durations), "longest_seconds": round(longest, 3)},
            ),
            [command],
        )


def _media_summary(probe: dict[str, Any]) -> dict[str, Any]:
    video_stream = _first_stream(probe, "video")
    audio_streams = _streams(probe, "audio")
    return {
        "duration_seconds": _duration(probe),
        "has_video": video_stream is not None,
        "has_audio": bool(audio_streams),
        "width": _int_or_none(video_stream.get("width")) if video_stream else None,
        "height": _int_or_none(video_stream.get("height")) if video_stream else None,
        "frame_rate": _frame_rate(video_stream) if video_stream else None,
        "video_codec": str(video_stream.get("codec_name") or "") or None
        if video_stream
        else None,
        "audio_codecs": [
            str(stream.get("codec_name"))
            for stream in audio_streams
            if stream.get("codec_name")
        ],
    }


def _expected_render_settings(request: QualityGateEvaluationRequest) -> dict[str, Any]:
    graph = request.composition_graph or {}
    canvas = graph.get("canvas") or {}
    render_target = next(
        (
            target.get("settings") or {}
            for target in graph.get("render_targets") or []
            if target.get("runtime") == "ffmpeg"
        ),
        {},
    )
    summary = request.render_summary or {}
    settings = summary.get("settings") or {}
    output = summary.get("output") or {}
    return {
        "width": render_target.get("width") or settings.get("width") or output.get("width") or canvas.get("width"),
        "height": render_target.get("height")
        or settings.get("height")
        or output.get("height")
        or canvas.get("height"),
        "fps": render_target.get("fps") or settings.get("fps") or canvas.get("fps"),
        "render_profile_key": render_target.get("render_profile_key") or settings.get("render_profile_key"),
    }


def _resolution_check(media: dict[str, Any], expected: dict[str, Any]) -> dict[str, Any]:
    expected_width = _int_or_none(expected.get("width"))
    expected_height = _int_or_none(expected.get("height"))
    measured = {"width": media.get("width"), "height": media.get("height")}
    if expected_width is None or expected_height is None:
        return _check(
            "resolution",
            "skipped",
            "info",
            "No expected resolution was available.",
            measured=measured,
        )
    passed = media.get("width") == expected_width and media.get("height") == expected_height
    return _check(
        "resolution",
        "passed" if passed else "failed",
        "info" if passed else "error",
        "Rendered resolution matches the target profile."
        if passed
        else "Rendered resolution does not match the target profile.",
        expected={"width": expected_width, "height": expected_height},
        measured=measured,
    )


def _frame_rate_check(media: dict[str, Any], expected: dict[str, Any]) -> dict[str, Any]:
    expected_fps = _float_or_none(expected.get("fps"))
    measured_fps = _float_or_none(media.get("frame_rate"))
    if expected_fps is None or measured_fps is None:
        return _check(
            "frame_rate",
            "skipped",
            "info",
            "Frame-rate comparison skipped because expected or measured FPS is unavailable.",
            expected={"fps": expected_fps},
            measured={"fps": measured_fps},
        )
    passed = abs(measured_fps - expected_fps) <= 0.75
    return _check(
        "frame_rate",
        "passed" if passed else "warning",
        "info" if passed else "warning",
        "Rendered frame rate matches the target profile."
        if passed
        else "Rendered frame rate differs from the target profile.",
        expected={"fps": expected_fps},
        measured={"fps": measured_fps},
    )


def _audio_presence_check(
    media: dict[str, Any],
    request: QualityGateEvaluationRequest,
) -> dict[str, Any]:
    expected_audio = request.expected_audio
    if expected_audio is None:
        expected_audio = bool((request.render_summary.get("output") or {}).get("has_audio"))
        expected_audio = expected_audio or bool(
            (request.composition_graph.get("audio_mix") or {}).get("tracks")
        )
    has_audio = bool(media.get("has_audio"))
    if expected_audio and not has_audio:
        return _check(
            "audio_presence",
            "failed",
            "error",
            "The render was expected to include audio, but no audio stream was found.",
            expected={"has_audio": True},
            measured={"has_audio": has_audio},
        )
    return _check(
        "audio_presence",
        "passed",
        "info",
        "Audio stream presence matches expectations.",
        expected={"has_audio": bool(expected_audio)},
        measured={"has_audio": has_audio},
    )


def _subtitle_bounds_check(request: QualityGateEvaluationRequest) -> dict[str, Any]:
    graph = request.composition_graph or {}
    canvas = graph.get("canvas") or {}
    captions = (graph.get("captions") or {}).get("segments") or []
    if not captions:
        return _check("subtitle_bounds", "skipped", "info", "No caption segments to inspect.")
    width = _int_or_none(canvas.get("width")) or 1080
    style = (graph.get("captions") or {}).get("style") or (canvas.get("style") or {}).get(
        "caption_style"
    ) or {}
    font_size = _int_or_none(style.get("font_size")) or 56
    max_width_ratio = _float_or_none(style.get("max_width_ratio")) or 0.86
    max_width = width * max_width_ratio
    overflow = []
    for segment in captions:
        text = str(segment.get("text") or "")
        line_width = max((_estimated_text_width(line, font_size) for line in text.splitlines()), default=0)
        if line_width > max_width:
            overflow.append(
                {
                    "caption_id": segment.get("caption_id"),
                    "estimated_width": round(line_width, 1),
                    "max_width": round(max_width, 1),
                    "text_preview": text[:40],
                }
            )
    return _check(
        "subtitle_bounds",
        "warning" if overflow else "passed",
        "warning" if overflow else "info",
        "Some captions may exceed the profile subtitle width."
        if overflow
        else "Caption text fits within the estimated subtitle safe width.",
        expected={"max_width_ratio": max_width_ratio, "max_width": round(max_width, 1)},
        measured={"overflow_count": len(overflow), "overflow": overflow[:5]},
    )


def _cjk_font_check(request: QualityGateEvaluationRequest) -> dict[str, Any]:
    graph = request.composition_graph or {}
    captions = (graph.get("captions") or {}).get("segments") or []
    text = "".join(str(segment.get("text") or "") for segment in captions)
    if not _contains_cjk(text):
        return _check("cjk_font", "skipped", "info", "No Chinese subtitle text detected.")
    canvas = graph.get("canvas") or {}
    style = (graph.get("captions") or {}).get("style") or (canvas.get("style") or {}).get(
        "caption_style"
    ) or {}
    font_family = str(style.get("font_family") or "")
    cjk_ready = _is_cjk_font_name(font_family)
    return _check(
        "cjk_font",
        "passed" if cjk_ready else "warning",
        "info" if cjk_ready else "warning",
        "Subtitle font is CJK-capable."
        if cjk_ready
        else "Chinese subtitles may render as boxes because the declared font is not CJK-capable.",
        expected={"cjk_capable_font": True},
        measured={"font_family": font_family or None},
    )


def _provider_output_check(request: QualityGateEvaluationRequest) -> dict[str, Any]:
    asset = request.render_asset or {}
    provider = asset.get("provider")
    tool_key = asset.get("tool_key")
    metadata = asset.get("asset_metadata") or {}
    quality_profile = str(metadata.get("quality_profile") or metadata.get("mode") or "")
    is_preview = any(token in quality_profile for token in ["preview", "local_preview", "shape"])
    if not provider or not tool_key:
        return _check(
            "provider_output",
            "warning",
            "warning",
            "Rendered asset is missing provider or tool provenance.",
            measured={"provider": provider, "tool_key": tool_key},
        )
    if is_preview:
        return _check(
            "provider_output",
            "warning",
            "warning",
            "Rendered asset appears to be preview-tier output and should be reviewed before delivery.",
            measured={"provider": provider, "tool_key": tool_key, "quality_profile": quality_profile},
        )
    return _check(
        "provider_output",
        "passed",
        "info",
        "Rendered asset has provider and tool provenance.",
        measured={"provider": provider, "tool_key": tool_key, "quality_profile": quality_profile or None},
    )


def _report(
    *,
    request: QualityGateEvaluationRequest,
    checks: list[dict[str, Any]],
    media: dict[str, Any],
    commands: list[list[str]] | None = None,
) -> dict[str, Any]:
    score = _quality_score(checks)
    failed = [check for check in checks if check.get("status") == "failed"]
    warnings = [check for check in checks if check.get("status") == "warning"]
    status = "failed" if failed else "warning" if warnings or score < request.min_score else "passed"
    review_required = status != "passed"
    return {
        "schema_version": "1.0",
        "generated_at": datetime.now(UTC).isoformat(),
        "runner": "quality_gate_runner",
        "status": status,
        "quality_score": score,
        "min_score": request.min_score,
        "media": media,
        "checks": checks,
        "summary": {
            "failed": len(failed),
            "warnings": len(warnings),
            "passed": len([check for check in checks if check.get("status") == "passed"]),
            "skipped": len([check for check in checks if check.get("status") == "skipped"]),
        },
        "human_review": {
            "required": review_required,
            "recommended_action": "review_and_fix" if failed else "review_warnings" if warnings else "approve",
            "reasons": [
                check["check_key"]
                for check in checks
                if check.get("status") in {"failed", "warning"}
            ],
        },
        "commands": commands or [],
    }


def _check(
    check_key: str,
    status: str,
    severity: str,
    message: str,
    *,
    expected: dict[str, Any] | None = None,
    measured: dict[str, Any] | None = None,
) -> dict[str, Any]:
    return {
        "check_key": check_key,
        "status": status,
        "severity": severity,
        "message": message,
        "expected": expected or {},
        "measured": measured or {},
    }


def _quality_score(checks: list[dict[str, Any]]) -> int:
    score = 100
    for check in checks:
        if check.get("status") == "failed":
            score -= 25
        elif check.get("status") == "warning":
            score -= 8
    return max(0, min(100, score))


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
    return _float_or_none(raw_rate)


def _estimated_text_width(text: str, font_size: int) -> float:
    width = 0.0
    for char in text:
        if char.isspace():
            width += font_size * 0.32
        elif _contains_cjk(char):
            width += font_size
        else:
            width += font_size * 0.56
    return width


def _contains_cjk(value: str) -> bool:
    return any("\u4e00" <= char <= "\u9fff" for char in value)


def _is_cjk_font_name(font_family: str) -> bool:
    normalized = font_family.lower()
    return any(
        name in normalized
        for name in [
            "noto sans cjk",
            "source han",
            "pingfang",
            "microsoft yahei",
            "wenquanyi",
            "hiragino sans gb",
            "simhei",
            "simsun",
        ]
    )


def _parse_db_value(stderr: str, key: str) -> float | None:
    match = re.search(rf"{re.escape(key)}:\s*([-0-9.]+)\s*dB", stderr)
    if not match:
        return None
    return _float_or_none(match.group(1))


def _int_or_none(value: Any) -> int | None:
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _float_or_none(value: Any) -> float | None:
    try:
        return round(float(value), 3)
    except (TypeError, ValueError):
        return None


def _command_error(stderr: str) -> str:
    return (stderr or "ffmpeg command failed").strip()[-1000:]
