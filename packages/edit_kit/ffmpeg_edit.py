from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path
from typing import Any

from pydantic import BaseModel, Field


class FfmpegEditRequest(BaseModel):
    operation: str
    source_paths: list[Path]
    output_path: Path
    subtitle_path: Path | None = None
    start_seconds: float = Field(default=0.0, ge=0.0)
    duration_seconds: float | None = Field(default=None, gt=0.0)
    cover_time_seconds: float = Field(default=0.0, ge=0.0)
    width: int | None = Field(default=None, ge=2)
    height: int | None = Field(default=None, ge=2)
    fps: int | None = Field(default=None, ge=1, le=120)
    fit: str = "cover"
    watermark_text: str | None = None
    watermark_position: str = "bottom_right"
    watermark_opacity: float = Field(default=0.72, ge=0.0, le=1.0)
    preset: str = "veryfast"
    crf: int = Field(default=23, ge=0, le=51)
    audio_bitrate: str = "128k"
    # P5 enhancement operations. ``grade_preset``/``grade_strength`` drive the
    # color-grading chain; ``target_loudness_lufs`` drives loudnorm; the pip_*
    # fields place the second source (source_paths[1]) over the first.
    grade_preset: str = "neutral"
    grade_strength: float = Field(default=1.0, ge=0.0, le=1.0)
    target_loudness_lufs: float = Field(default=-16.0, ge=-40.0, le=-5.0)
    pip_position: str = "bottom_right"
    pip_scale: float = Field(default=0.3, ge=0.05, le=0.9)
    pip_margin: int = Field(default=48, ge=0)


class FfmpegEditResult(BaseModel):
    operation: str
    output_path: Path
    mime_type: str
    asset_type: str
    commands: list[list[str]]
    duration_seconds: float | None = None
    width: int | None = None
    height: int | None = None
    has_audio: bool | None = None
    warnings: list[str] = Field(default_factory=list)
    metadata: dict[str, Any] = Field(default_factory=dict)


class FfmpegEditWorker:
    def __init__(
        self,
        *,
        ffmpeg_path: str | None = None,
        ffprobe_path: str | None = None,
        timeout_seconds: int = 300,
    ) -> None:
        self.ffmpeg_path = ffmpeg_path or shutil.which("ffmpeg")
        self.ffprobe_path = ffprobe_path or shutil.which("ffprobe")
        self.timeout_seconds = timeout_seconds
        self._filter_names: set[str] | None = None

    def check_availability(self) -> bool:
        return bool(self.ffmpeg_path and self.ffprobe_path)

    def supports_filter(self, filter_name: str) -> bool:
        if not self.ffmpeg_path:
            return False
        if self._filter_names is None:
            completed = subprocess.run(
                [self.ffmpeg_path, "-hide_banner", "-filters"],
                check=False,
                capture_output=True,
                text=True,
                timeout=60,
            )
            self._filter_names = _parse_filter_names(completed.stdout + completed.stderr)
        return filter_name in self._filter_names

    def run(self, request: FfmpegEditRequest) -> FfmpegEditResult:
        if not self.ffmpeg_path:
            raise RuntimeError("ffmpeg binary is required for edit tools")
        if not self.ffprobe_path:
            raise RuntimeError("ffprobe binary is required for edit tools")
        _assert_sources_exist(request)
        request.output_path.parent.mkdir(parents=True, exist_ok=True)
        command = self._build_command(request)
        completed = subprocess.run(
            command,
            check=False,
            capture_output=True,
            text=True,
            timeout=self.timeout_seconds,
        )
        if completed.returncode != 0:
            raise RuntimeError(_ffmpeg_error(completed.stderr))
        if not request.output_path.is_file():
            raise RuntimeError("ffmpeg edit command completed without producing output")
        return self._result(request, command=command)

    def probe(self, path: Path) -> dict[str, Any]:
        if not self.ffprobe_path:
            return {}
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
            timeout=60,
        )
        if completed.returncode != 0:
            raise RuntimeError(_ffmpeg_error(completed.stderr))
        return json.loads(completed.stdout or "{}")

    def _build_command(self, request: FfmpegEditRequest) -> list[str]:
        operation = request.operation
        if operation == "cover_generation":
            return self._cover_command(request)
        if operation == "concat":
            return self._concat_command(request)
        if operation in {"trim", "subtitle_burn_in", "auto_reframe", "watermark"}:
            return self._single_input_video_command(request)
        if operation == "color_grade":
            return self._color_grade_command(request)
        if operation == "loudness_normalize":
            return self._loudness_normalize_command(request)
        if operation == "picture_in_picture":
            return self._picture_in_picture_command(request)
        raise ValueError(f"unsupported edit operation: {operation}")

    def _single_input_video_command(self, request: FfmpegEditRequest) -> list[str]:
        source_path = request.source_paths[0]
        command = [self.ffmpeg_path or "ffmpeg", "-y"]
        if request.operation == "trim" and request.start_seconds > 0:
            command.extend(["-ss", f"{request.start_seconds:.3f}"])
        command.extend(["-i", str(source_path)])
        if request.operation == "subtitle_burn_in":
            if request.subtitle_path is None:
                raise ValueError("subtitle_path is required for subtitle_burn_in")
            if not self.supports_filter("subtitles"):
                raise RuntimeError("ffmpeg subtitles filter is required for subtitle_burn_in")
        filter_chain = _video_filter_chain(
            request,
            drawtext_supported=self.supports_filter("drawtext"),
        )
        if filter_chain:
            command.extend(["-vf", filter_chain])
        if request.operation == "trim" and request.duration_seconds is not None:
            command.extend(["-t", f"{request.duration_seconds:.3f}"])
        command.extend(
            [
                "-map",
                "0:v:0",
                "-map",
                "0:a?",
                *(_video_encode_args(request)),
                "-c:a",
                "aac",
                "-b:a",
                request.audio_bitrate,
                "-movflags",
                "+faststart",
                str(request.output_path),
            ]
        )
        return command

    def _concat_command(self, request: FfmpegEditRequest) -> list[str]:
        list_path = request.output_path.with_suffix(".concat.txt")
        list_path.write_text(
            "\n".join(f"file '{_concat_path(path)}'" for path in request.source_paths) + "\n",
            encoding="utf-8",
        )
        filter_chain = _video_filter_chain(
            request,
            drawtext_supported=self.supports_filter("drawtext"),
        )
        command = [
            self.ffmpeg_path or "ffmpeg",
            "-y",
            "-f",
            "concat",
            "-safe",
            "0",
            "-i",
            str(list_path),
        ]
        if filter_chain:
            command.extend(["-vf", filter_chain])
        command.extend(
            [
                "-map",
                "0:v:0",
                "-map",
                "0:a?",
                *(_video_encode_args(request)),
                "-c:a",
                "aac",
                "-b:a",
                request.audio_bitrate,
                "-movflags",
                "+faststart",
                str(request.output_path),
            ]
        )
        return command

    def _cover_command(self, request: FfmpegEditRequest) -> list[str]:
        source_path = request.source_paths[0]
        filter_chain = _video_filter_chain(
            request,
            include_fps=False,
            drawtext_supported=self.supports_filter("drawtext"),
        )
        command = [
            self.ffmpeg_path or "ffmpeg",
            "-y",
            "-ss",
            f"{request.cover_time_seconds:.3f}",
            "-i",
            str(source_path),
        ]
        if filter_chain:
            command.extend(["-vf", filter_chain])
        command.extend(["-frames:v", "1", "-q:v", "2", str(request.output_path)])
        return command

    def _color_grade_command(self, request: FfmpegEditRequest) -> list[str]:
        grade_chain = _grade_filter_chain(request, eq_supported=self.supports_filter("eq"))
        if not grade_chain:
            raise ValueError(
                f"unknown grade preset: {request.grade_preset}; "
                f"valid presets: {', '.join(sorted(GRADE_PRESETS))}"
            )
        command = [
            self.ffmpeg_path or "ffmpeg",
            "-y",
            "-i",
            str(request.source_paths[0]),
            "-vf",
            grade_chain,
            "-map",
            "0:v:0",
            "-map",
            "0:a?",
            *(_video_encode_args(request)),
            "-c:a",
            "aac",
            "-b:a",
            request.audio_bitrate,
            "-movflags",
            "+faststart",
            str(request.output_path),
        ]
        return command

    def _loudness_normalize_command(self, request: FfmpegEditRequest) -> list[str]:
        if not self.supports_filter("loudnorm"):
            raise RuntimeError("ffmpeg loudnorm filter is required for loudness_normalize")
        command = [
            self.ffmpeg_path or "ffmpeg",
            "-y",
            "-i",
            str(request.source_paths[0]),
            # Video is copied, not re-encoded: loudness is an audio property and
            # a normalize pass must not cost a video generation.
            "-map",
            "0:v:0",
            "-map",
            "0:a:0",
            "-c:v",
            "copy",
            "-af",
            f"loudnorm=I={request.target_loudness_lufs}:TP=-1.5:LRA=11",
            "-c:a",
            "aac",
            "-b:a",
            request.audio_bitrate,
            "-movflags",
            "+faststart",
            str(request.output_path),
        ]
        return command

    def _picture_in_picture_command(self, request: FfmpegEditRequest) -> list[str]:
        if len(request.source_paths) < 2:
            raise ValueError(
                "picture_in_picture requires two source paths: base video first, overlay second"
            )
        if not self.supports_filter("overlay") or not self.supports_filter("scale"):
            raise RuntimeError("ffmpeg overlay and scale filters are required for picture_in_picture")
        base_path = request.source_paths[0]
        overlay_path = request.source_paths[1]
        position = _pip_position(request.pip_position, request.pip_margin)
        if position is None:
            raise ValueError(f"unknown pip position: {request.pip_position}")
        x_expr, y_expr = position
        filter_complex = (
            f"[1:v]scale=iw*{request.pip_scale:.4f}:ih*{request.pip_scale:.4f}[pip];"
            f"[0:v][pip]overlay=x={x_expr}:y={y_expr}[vout]"
        )
        command = [
            self.ffmpeg_path or "ffmpeg",
            "-y",
            "-i",
            str(base_path),
            "-i",
            str(overlay_path),
            "-filter_complex",
            filter_complex,
            "-map",
            "[vout]",
            "-map",
            "0:a?",
            *(_video_encode_args(request)),
            "-c:a",
            "aac",
            "-b:a",
            request.audio_bitrate,
            "-movflags",
            "+faststart",
            str(request.output_path),
        ]
        return command

    def _result(self, request: FfmpegEditRequest, *, command: list[str]) -> FfmpegEditResult:
        if request.operation == "cover_generation":
            width, height = _image_dimensions(self.probe(request.output_path))
            return FfmpegEditResult(
                operation=request.operation,
                output_path=request.output_path,
                mime_type="image/jpeg",
                asset_type="cover_image",
                commands=[command],
                width=width,
                height=height,
                has_audio=False,
                metadata={"cover_time_seconds": request.cover_time_seconds},
            )
        probe = self.probe(request.output_path)
        video_stream = _first_stream(probe, "video")
        warnings = []
        if request.operation == "watermark" and not self.supports_filter("drawtext"):
            warnings.append("drawtext_filter_unavailable_watermark_box_used")
        return FfmpegEditResult(
            operation=request.operation,
            output_path=request.output_path,
            mime_type="video/mp4",
            asset_type="edited_video",
            commands=[command],
            duration_seconds=_duration(probe),
            width=_int_or_none(video_stream.get("width")) if video_stream else None,
            height=_int_or_none(video_stream.get("height")) if video_stream else None,
            has_audio=_first_stream(probe, "audio") is not None,
            warnings=warnings,
            metadata={
                "fit": request.fit,
                "fps": request.fps,
                "watermark_text": request.watermark_text,
                "subtitle_burned": request.operation == "subtitle_burn_in",
                "grade_preset": request.grade_preset
                if request.operation == "color_grade"
                else None,
                "target_loudness_lufs": request.target_loudness_lufs
                if request.operation == "loudness_normalize"
                else None,
                "pip_position": request.pip_position
                if request.operation == "picture_in_picture"
                else None,
                "pip_scale": request.pip_scale
                if request.operation == "picture_in_picture"
                else None,
            },
        )


def _assert_sources_exist(request: FfmpegEditRequest) -> None:
    if not request.source_paths:
        raise ValueError("at least one source path is required")
    for source_path in request.source_paths:
        if not source_path.is_file():
            raise ValueError(f"source path does not exist: {source_path}")
    if request.subtitle_path is not None and not request.subtitle_path.is_file():
        raise ValueError("subtitle_path does not exist")


def _video_filter_chain(
    request: FfmpegEditRequest,
    *,
    include_fps: bool = True,
    drawtext_supported: bool = True,
) -> str:
    filters = []
    if request.width and request.height:
        if request.fit == "contain":
            filters.append(
                f"scale={request.width}:{request.height}:"
                "force_original_aspect_ratio=decrease"
            )
            filters.append(f"pad={request.width}:{request.height}:(ow-iw)/2:(oh-ih)/2")
        else:
            filters.append(
                f"scale={request.width}:{request.height}:"
                "force_original_aspect_ratio=increase"
            )
            filters.append(f"crop={request.width}:{request.height}")
        filters.append("setsar=1")
    if request.operation == "subtitle_burn_in" and request.subtitle_path is not None:
        filters.append(f"subtitles=filename='{_escape_filter_path(request.subtitle_path)}'")
    if request.operation == "watermark" and request.watermark_text:
        filters.append(_watermark_filter(request, drawtext_supported=drawtext_supported))
    if include_fps and request.fps:
        filters.append(f"fps={request.fps}")
    return ",".join(filters)


def _watermark_filter(
    request: FfmpegEditRequest,
    *,
    drawtext_supported: bool,
) -> str:
    if not drawtext_supported:
        x_expr, y_expr = _watermark_box_position(request.watermark_position)
        return f"drawbox=x={x_expr}:y={y_expr}:w=120:h=44:color=black@0.35:t=fill"
    x_expr, y_expr = _watermark_position(request.watermark_position)
    text = _escape_drawtext(request.watermark_text or "")
    font_path = _resolve_cjk_font()
    font_arg = f":fontfile='{_escape_filter_path(font_path)}'" if font_path else ""
    return (
        "drawtext="
        f"text='{text}'"
        f"{font_arg}"
        ":fontsize=36"
        f":fontcolor=white@{request.watermark_opacity}"
        ":box=1:boxcolor=black@0.35:boxborderw=12"
        f":x={x_expr}:y={y_expr}"
    )


def _watermark_position(position: str) -> tuple[str, str]:
    positions = {
        "top_left": ("24", "24"),
        "top_right": ("w-tw-24", "24"),
        "bottom_left": ("24", "h-th-24"),
        "bottom_right": ("w-tw-24", "h-th-24"),
        "center": ("(w-tw)/2", "(h-th)/2"),
    }
    return positions.get(position, positions["bottom_right"])


def _watermark_box_position(position: str) -> tuple[str, str]:
    positions = {
        "top_left": ("24", "24"),
        "top_right": ("w-144", "24"),
        "bottom_left": ("24", "h-68"),
        "bottom_right": ("w-144", "h-68"),
        "center": ("(w-120)/2", "(h-44)/2"),
    }
    return positions.get(position, positions["bottom_right"])


# P5 color-grade presets. Values are deltas from the neutral baseline (1.0);
# ``grade_strength`` lerps between neutral and the preset so a subtle grade and
# a full grade produce different — both deterministic — outputs.
GRADE_PRESETS: dict[str, dict[str, float]] = {
    "warm": {"rm": 0.12, "bm": -0.12},
    "cool": {"rm": -0.12, "bm": 0.12},
    "high_contrast": {"contrast": 1.25, "saturation": 1.05},
    "filmic": {"contrast": 1.12, "saturation": 0.9, "gamma": 0.95},
    "vivid": {"contrast": 1.1, "saturation": 1.3},
}


def _grade_filter_chain(request: FfmpegEditRequest, *, eq_supported: bool) -> str | None:
    preset = GRADE_PRESETS.get(request.grade_preset)
    if preset is None:
        return None
    if not eq_supported:
        raise RuntimeError("ffmpeg eq filter is required for color_grade")
    strength = request.grade_strength
    filters = []
    eq_parts = []
    for key in ("contrast", "saturation", "gamma"):
        if key in preset:
            value = 1.0 + (preset[key] - 1.0) * strength
            eq_parts.append(f"{key}={value:.4f}")
    if eq_parts:
        filters.append("eq=" + ":".join(eq_parts))
    balance_parts = []
    for key in ("rm", "gm", "bm"):
        if key in preset:
            balance_parts.append(f"{key}={preset[key] * strength:.4f}")
    if balance_parts:
        filters.append("colorbalance=" + ":".join(balance_parts))
    return ",".join(filters)


def _pip_position(position: str, margin: int) -> tuple[str, str] | None:
    positions = {
        "top_left": ("{m}", "{m}"),
        "top_right": ("main_w-w-{m}", "{m}"),
        "bottom_left": ("{m}", "main_h-h-{m}"),
        "bottom_right": ("main_w-w-{m}", "main_h-h-{m}"),
        "center": ("(main_w-w)/2", "(main_h-h)/2"),
    }
    template = positions.get(position)
    if template is None:
        return None
    return (
        template[0].replace("{m}", str(max(margin, 0))),
        template[1].replace("{m}", str(max(margin, 0))),
    )


def _video_encode_args(request: FfmpegEditRequest) -> list[str]:
    return [
        "-c:v",
        "libx264",
        "-preset",
        request.preset,
        "-crf",
        str(request.crf),
        "-pix_fmt",
        "yuv420p",
    ]


def _parse_filter_names(output: str) -> set[str]:
    names = set()
    for line in output.splitlines():
        parts = line.split()
        if len(parts) < 3:
            continue
        # Filter lines are "<flags> <name> <in-><out> ...". ffmpeg 9 changed the
        # flag column to two characters and added a legend block ("T.. = Timeline
        # support") whose third token carries no arrow — the old "flags are three
        # characters" rule then matched every legend line and no real filter.
        if "->" not in parts[2]:
            continue
        names.add(parts[1])
    return names


def _concat_path(path: Path) -> str:
    return str(path).replace("'", "'\\''")


def _escape_filter_path(path: Path) -> str:
    value = str(path)
    return value.replace("\\", "\\\\").replace(":", "\\:").replace("'", "\\'")


def _escape_drawtext(value: str) -> str:
    return (
        value.replace("\\", "\\\\")
        .replace(":", "\\:")
        .replace("'", "\\'")
        .replace("%", "\\%")
    )


def _resolve_cjk_font() -> Path | None:
    candidates = [
        Path("/System/Library/Fonts/PingFang.ttc"),
        Path("/System/Library/Fonts/STHeiti Light.ttc"),
        Path("/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc"),
        Path("/usr/share/fonts/truetype/noto/NotoSansCJK-Regular.ttc"),
        Path("/usr/share/fonts/truetype/wqy/wqy-microhei.ttc"),
    ]
    return next((path for path in candidates if path.is_file()), None)


def _first_stream(probe: dict[str, Any], codec_type: str) -> dict[str, Any] | None:
    for stream in probe.get("streams", []):
        if stream.get("codec_type") == codec_type:
            return stream
    return None


def _duration(probe: dict[str, Any]) -> float | None:
    duration = probe.get("format", {}).get("duration")
    try:
        return round(float(duration), 3) if duration is not None else None
    except (TypeError, ValueError):
        return None


def _image_dimensions(probe: dict[str, Any]) -> tuple[int | None, int | None]:
    stream = _first_stream(probe, "video")
    if stream is None:
        return None, None
    return _int_or_none(stream.get("width")), _int_or_none(stream.get("height"))


def _int_or_none(value: Any) -> int | None:
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _ffmpeg_error(stderr: str) -> str:
    tail = stderr.strip()[-2000:]
    return tail or "ffmpeg edit command failed"
