from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path
from typing import Any

from pydantic import BaseModel, Field


class FfmpegComposeRequest(BaseModel):
    avatar_video_path: Path
    output_path: Path
    narration_path: Path | None = None
    subtitle_path: Path | None = None
    music_path: Path | None = None
    width: int = Field(default=1080, ge=2)
    height: int = Field(default=1920, ge=2)
    fps: int = Field(default=30, ge=1, le=120)
    crf: int = Field(default=22, ge=0, le=51)
    preset: str = "medium"
    video_bitrate: str | None = None
    audio_bitrate: str = "128k"
    bgm_volume: float = Field(default=0.18, ge=0.0, le=1.0)
    subtitle_style: dict[str, Any] = Field(default_factory=dict)


class FfmpegComposeResult(BaseModel):
    output_path: Path
    command: list[str]
    duration_seconds: float | None = None
    width: int | None = None
    height: int | None = None
    has_audio: bool
    subtitles_embedded: bool = False
    subtitle_track_added: bool = False
    warnings: list[str] = Field(default_factory=list)
    stderr_tail: str = ""


class FfmpegComposeWorker:
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
            if completed.returncode != 0:
                self._filter_names = set()
            else:
                self._filter_names = _parse_filter_names(completed.stdout)
        return filter_name in self._filter_names

    def compose(self, request: FfmpegComposeRequest) -> FfmpegComposeResult:
        if not self.ffmpeg_path:
            raise RuntimeError("ffmpeg binary is required for video composition")
        if not request.avatar_video_path.is_file():
            raise ValueError("avatar_video_path does not exist")
        if request.narration_path is not None and not request.narration_path.is_file():
            raise ValueError("narration_path does not exist")
        if request.subtitle_path is not None and not request.subtitle_path.is_file():
            raise ValueError("subtitle_path does not exist")
        if request.music_path is not None and not request.music_path.is_file():
            raise ValueError("music_path does not exist")

        request.output_path.parent.mkdir(parents=True, exist_ok=True)
        source_probe = self.probe(request.avatar_video_path)
        source_duration = _duration(source_probe)
        has_source_audio = _has_audio_stream(source_probe)
        burn_subtitles = request.subtitle_path is not None and self.supports_filter("subtitles")
        add_soft_subtitles = request.subtitle_path is not None and not burn_subtitles
        warnings = []
        if add_soft_subtitles:
            warnings.append("ffmpeg_subtitles_filter_unavailable_soft_track_used")
        command = self._build_command(
            request,
            has_source_audio=has_source_audio,
            burn_subtitles=burn_subtitles,
            add_soft_subtitles=add_soft_subtitles,
            source_duration_seconds=source_duration,
        )
        completed = subprocess.run(
            command,
            check=False,
            capture_output=True,
            text=True,
            timeout=self.timeout_seconds,
        )
        if completed.returncode != 0:
            raise RuntimeError(_ffmpeg_error(completed.stderr))

        output_probe = self.probe(request.output_path)
        video_stream = _first_stream(output_probe, "video")
        return FfmpegComposeResult(
            output_path=request.output_path,
            command=command,
            duration_seconds=_duration(output_probe),
            width=video_stream.get("width") if video_stream else None,
            height=video_stream.get("height") if video_stream else None,
            has_audio=_has_audio_stream(output_probe),
            subtitles_embedded=burn_subtitles,
            subtitle_track_added=_has_subtitle_stream(output_probe),
            warnings=warnings,
            stderr_tail=completed.stderr[-2000:],
        )

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

    def _build_command(
        self,
        request: FfmpegComposeRequest,
        *,
        has_source_audio: bool,
        burn_subtitles: bool,
        add_soft_subtitles: bool,
        source_duration_seconds: float | None,
    ) -> list[str]:
        command = [self.ffmpeg_path or "ffmpeg", "-y", "-i", str(request.avatar_video_path)]
        next_input_index = 1
        narration_input_index = None
        if request.narration_path is not None:
            narration_input_index = next_input_index
            next_input_index += 1
            command.extend(["-i", str(request.narration_path)])
        music_input_index = None
        if request.music_path is not None:
            music_input_index = next_input_index
            next_input_index += 1
            command.extend(["-stream_loop", "-1", "-i", str(request.music_path)])
        subtitle_input_index = None
        if add_soft_subtitles and request.subtitle_path is not None:
            subtitle_input_index = next_input_index
            command.extend(["-i", str(request.subtitle_path)])

        filters = [_video_filter(request, burn_subtitles=burn_subtitles)]
        map_args = ["-map", "[v]"]
        audio_labels = []
        if narration_input_index is not None:
            filters.append(f"[{narration_input_index}:a]volume=1.0,aresample=44100[a_narration]")
            audio_labels.append("[a_narration]")
        elif has_source_audio:
            filters.append("[0:a]volume=1.0,aresample=44100[a_source]")
            audio_labels.append("[a_source]")
        if music_input_index is not None:
            filters.append(
                f"[{music_input_index}:a]volume={request.bgm_volume},aresample=44100[a_music]"
            )
            audio_labels.append("[a_music]")

        if len(audio_labels) == 1:
            filters.append(f"{audio_labels[0]}anull[a]")
            map_args.extend(["-map", "[a]"])
        elif len(audio_labels) > 1:
            filters.append(
                "".join(audio_labels)
                + f"amix=inputs={len(audio_labels)}:duration=first:dropout_transition=2[a]"
            )
            map_args.extend(["-map", "[a]"])
        else:
            map_args.extend(["-map", "0:a?"])
        if subtitle_input_index is not None:
            map_args.extend(["-map", f"{subtitle_input_index}:0"])

        command.extend(["-filter_complex", ";".join(filters), *map_args])
        if source_duration_seconds is not None:
            command.extend(["-t", f"{source_duration_seconds:.3f}"])
        else:
            command.append("-shortest")
        video_quality_args = (
            ["-b:v", request.video_bitrate]
            if request.video_bitrate
            else ["-crf", str(request.crf)]
        )
        command.extend(
            [
                "-c:v",
                "libx264",
                "-preset",
                request.preset,
                *video_quality_args,
                "-pix_fmt",
                "yuv420p",
                "-c:a",
                "aac",
                "-b:a",
                request.audio_bitrate,
                *(["-c:s", "mov_text"] if subtitle_input_index is not None else []),
                "-movflags",
                "+faststart",
                str(request.output_path),
            ]
        )
        return command


def _video_filter(request: FfmpegComposeRequest, *, burn_subtitles: bool) -> str:
    steps = [
        (
            f"[0:v]scale={request.width}:{request.height}:"
            "force_original_aspect_ratio=decrease"
        ),
        f"pad={request.width}:{request.height}:(ow-iw)/2:(oh-ih)/2",
        "setsar=1",
        f"fps={request.fps}",
    ]
    if request.subtitle_path is not None and burn_subtitles:
        subtitle_path = _escape_filter_path(request.subtitle_path)
        steps.append(
            f"subtitles=filename='{subtitle_path}':"
            f"force_style='{_subtitle_force_style(request.subtitle_style)}'"
        )
    return ",".join(steps) + "[v]"


def _parse_filter_names(output: str) -> set[str]:
    names = set()
    for line in output.splitlines():
        parts = line.split()
        if len(parts) >= 2 and len(parts[0]) == 3:
            names.add(parts[1])
    return names


def _escape_filter_path(path: Path) -> str:
    value = str(path)
    return value.replace("\\", "\\\\").replace(":", "\\:").replace("'", "\\'")


def _subtitle_force_style(style: dict[str, Any]) -> str:
    placement = str(style.get("placement") or "bottom").lower()
    alignment = 8 if placement == "top" else 2
    font_name = str(style.get("font_family") or "Noto Sans CJK SC")
    font_size = int(style.get("ffmpeg_font_size") or 38)
    margin_v = int(
        style.get("ffmpeg_margin_v")
        or style.get("margin_bottom")
        or style.get("margin_top")
        or 96
    )
    safe_font = font_name.replace(",", " ").replace("'", "")
    return (
        f"FontName={safe_font},"
        f"Fontsize={font_size},"
        f"Alignment={alignment},"
        f"MarginV={margin_v}"
    )


def _first_stream(probe: dict[str, Any], codec_type: str) -> dict[str, Any] | None:
    for stream in probe.get("streams", []):
        if stream.get("codec_type") == codec_type:
            return stream
    return None


def _has_audio_stream(probe: dict[str, Any]) -> bool:
    return _first_stream(probe, "audio") is not None


def _has_subtitle_stream(probe: dict[str, Any]) -> bool:
    return _first_stream(probe, "subtitle") is not None


def _duration(probe: dict[str, Any]) -> float | None:
    duration = probe.get("format", {}).get("duration")
    return float(duration) if duration is not None else None


def _ffmpeg_error(stderr: str) -> str:
    tail = stderr.strip()[-2000:]
    return tail or "ffmpeg command failed"
