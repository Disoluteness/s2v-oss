from __future__ import annotations

import os
import shutil
import subprocess
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any


@dataclass(frozen=True)
class PreviewMediaResult:
    content: bytes
    mime_type: str
    duration_ms: int
    width: int | None
    height: int | None
    command: list[str]
    metadata: dict[str, Any] = field(default_factory=dict)


class LocalPreviewMediaFactory:
    """Creates deterministic local media for MVP end-to-end validation."""

    def __init__(self, *, ffmpeg_path: str | None = None, timeout_seconds: int = 180) -> None:
        self.ffmpeg_path = ffmpeg_path or shutil.which("ffmpeg")
        self.timeout_seconds = timeout_seconds
        self._filter_names: set[str] | None = None

    def check_availability(self) -> bool:
        return bool(self.ffmpeg_path)

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

    def render_avatar_preview(
        self,
        *,
        output_path: Path,
        duration_seconds: int,
        width: int,
        height: int,
        fps: int,
        title: str | None = None,
        brand_name: str | None = None,
        product_name: str | None = None,
        segments: list[dict[str, Any]] | None = None,
        shots: list[dict[str, Any]] | None = None,
        reference_assets: list[dict[str, Any]] | None = None,
    ) -> PreviewMediaResult:
        if not self.ffmpeg_path:
            raise RuntimeError("ffmpeg binary is required for local preview media")
        output_path.parent.mkdir(parents=True, exist_ok=True)
        duration_seconds = max(1, duration_seconds)
        width = _even(width)
        height = _even(height)
        font_path = _resolve_cjk_font()
        text_overlay_enabled = bool(font_path and self.supports_filter("drawtext"))
        video_filter = (
            _storyboard_filter(
                width,
                height,
                duration_seconds=duration_seconds,
                title=title,
                brand_name=brand_name,
                product_name=product_name,
                segments=segments or [],
                shots=shots or [],
                font_path=font_path,
            )
            if text_overlay_enabled and font_path is not None
            else _avatar_filter(width, height)
        )
        command = [
            self.ffmpeg_path,
            "-y",
            "-f",
            "lavfi",
            "-i",
            f"color=c=0xf6f7f9:s={width}x{height}:d={duration_seconds}:r={fps}",
            "-f",
            "lavfi",
            "-i",
            "anullsrc=channel_layout=mono:sample_rate=44100",
            "-vf",
            video_filter,
            "-t",
            str(duration_seconds),
            "-shortest",
            "-c:v",
            "libx264",
            "-preset",
            "veryfast",
            "-crf",
            "28",
            "-pix_fmt",
            "yuv420p",
            "-c:a",
            "aac",
            "-b:a",
            "128k",
            "-movflags",
            "+faststart",
            str(output_path),
        ]
        _run(command, timeout_seconds=self.timeout_seconds)
        return PreviewMediaResult(
            content=output_path.read_bytes(),
            mime_type="video/mp4",
            duration_ms=duration_seconds * 1000,
            width=width,
            height=height,
            command=command,
            metadata={
                "quality_profile": "storyboard_preview_v2"
                if text_overlay_enabled
                else "shape_preview_v2",
                "text_overlay_enabled": text_overlay_enabled,
                "font_path": str(font_path) if font_path else None,
                "reference_assets": reference_assets or [],
            },
        )

    def render_music_bed(
        self,
        *,
        output_path: Path,
        duration_seconds: int,
    ) -> PreviewMediaResult:
        if not self.ffmpeg_path:
            raise RuntimeError("ffmpeg binary is required for local preview media")
        output_path.parent.mkdir(parents=True, exist_ok=True)
        duration_seconds = max(1, duration_seconds)
        fade_out_start = max(0, duration_seconds - 0.7)
        command = [
            self.ffmpeg_path,
            "-y",
            "-f",
            "lavfi",
            "-i",
            f"sine=frequency=196:duration={duration_seconds}:sample_rate=44100",
            "-f",
            "lavfi",
            "-i",
            f"sine=frequency=247:duration={duration_seconds}:sample_rate=44100",
            "-f",
            "lavfi",
            "-i",
            f"sine=frequency=330:duration={duration_seconds}:sample_rate=44100",
            "-f",
            "lavfi",
            "-i",
            f"sine=frequency=392:duration={duration_seconds}:sample_rate=44100",
            "-filter_complex",
            (
                "[0:a]volume=0.022,afade=t=in:st=0:d=0.25,"
                f"afade=t=out:st={fade_out_start}:d=0.7[a0];"
                "[1:a]volume=0.016,afade=t=in:st=0:d=0.25,"
                f"afade=t=out:st={fade_out_start}:d=0.7[a1];"
                "[2:a]volume=0.012,afade=t=in:st=0:d=0.25,"
                f"afade=t=out:st={fade_out_start}:d=0.7[a2];"
                "[3:a]volume=0.008,afade=t=in:st=0:d=0.25,"
                f"afade=t=out:st={fade_out_start}:d=0.7[a3];"
                "[a0][a1][a2][a3]amix=inputs=4:duration=shortest,"
                "alimiter=limit=0.12[a]"
            ),
            "-map",
            "[a]",
            "-c:a",
            "pcm_s16le",
            str(output_path),
        ]
        _run(command, timeout_seconds=self.timeout_seconds)
        return PreviewMediaResult(
            content=output_path.read_bytes(),
            mime_type="audio/wav",
            duration_ms=duration_seconds * 1000,
            width=None,
            height=None,
            command=command,
            metadata={"quality_profile": "soft_chord_bed_v2"},
        )


def _avatar_filter(width: int, height: int) -> str:
    head_w = round(width * 0.32)
    head_h = round(height * 0.17)
    head_x = (width - head_w) // 2
    head_y = round(height * 0.18)
    body_w = round(width * 0.48)
    body_h = round(height * 0.36)
    body_x = (width - body_w) // 2
    body_y = round(height * 0.42)
    mouth_w = round(head_w * 0.36)
    mouth_h = max(4, round(head_h * 0.08))
    mouth_x = head_x + (head_w - mouth_w) // 2
    mouth_y = head_y + round(head_h * 0.66)
    cup_w = round(width * 0.24)
    cup_h = round(height * 0.13)
    cup_x = width - cup_w - round(width * 0.11)
    cup_y = round(height * 0.66)
    return ",".join(_base_visual_steps(width, height) + [
        f"drawbox=x={body_x}:y={body_y}:w={body_w}:h={body_h}:color=0x1662d4@0.92:t=fill",
        f"drawbox=x={head_x}:y={head_y}:w={head_w}:h={head_h}:color=0xf2c6a0:t=fill",
        (
            f"drawbox=x={mouth_x}:y={mouth_y}:w={mouth_w}:h={mouth_h}:"
            "color=0xbd2a2a:t=fill:enable='lt(mod(t,0.42),0.21)'"
        ),
        f"drawbox=x={cup_x}:y={cup_y}:w={cup_w}:h={cup_h}:color=0xf0b35a:t=fill",
        (
            f"drawbox=x={cup_x}:y={cup_y}:w={cup_w}:"
            f"h={max(4, round(cup_h * 0.12))}:color=0x8f5a2a:t=fill"
        ),
    ])


def _storyboard_filter(
    width: int,
    height: int,
    *,
    duration_seconds: int,
    title: str | None,
    brand_name: str | None,
    product_name: str | None,
    segments: list[dict[str, Any]],
    shots: list[dict[str, Any]],
    font_path: Path,
) -> str:
    brand = _clean_text(brand_name) or "Script2Video"
    product = _clean_text(product_name) or "MVP preview"
    safe_title = _clean_text(title) or f"{brand} {product}"
    steps = _base_visual_steps(width, height)

    steps.extend(
        [
            _drawbox(width * 0.08, height * 0.07, width * 0.84, height * 0.09, "0xffffff@0.88"),
            _drawtext(
                brand,
                x=width * 0.11,
                y=height * 0.085,
                fontsize=round(height * 0.026),
                font_path=font_path,
                color="0x1a1f2b",
            ),
            _drawtext(
                product,
                x=width * 0.11,
                y=height * 0.123,
                fontsize=round(height * 0.016),
                font_path=font_path,
                color="0x506070",
            ),
            _drawbox(width * 0.63, height * 0.1, width * 0.24, height * 0.032, "0xe65f42@0.92"),
            _drawtext(
                "本地预览",
                x=width * 0.675,
                y=height * 0.106,
                fontsize=round(height * 0.014),
                font_path=font_path,
                color="0xffffff",
            ),
        ]
    )

    preview_segments = _normalise_segments(segments, duration_seconds)
    for index, segment in enumerate(preview_segments, start=1):
        shot = shots[index - 1] if index - 1 < len(shots) else {}
        start = segment["start_seconds"]
        end = segment["end_seconds"]
        enable = f"between(t,{start:.3f},{end:.3f})"
        theme = _scene_theme(index)
        label = f"镜头 {index:02d} / {shot.get('framing') or '中景'}"
        visual = _clean_text(shot.get("visual_direction")) or _clean_text(segment.get("caption"))
        spoken = _clean_text(segment.get("caption") or segment.get("text"))

        steps.extend(
            [
                _drawbox(0, height * 0.18, width, height * 0.42, theme["wash"], enable=enable),
                _drawbox(width * 0.1, height * 0.22, width * 0.8, height * 0.29, "0xffffff@0.84", enable=enable),
                _drawbox(width * 0.1, height * 0.22, width * 0.8, height * 0.012, theme["accent"], enable=enable),
                _drawtext(
                    label,
                    x=width * 0.14,
                    y=height * 0.245,
                    fontsize=round(height * 0.016),
                    font_path=font_path,
                    color=theme["text"],
                    enable=enable,
                ),
                _drawtext(
                    safe_title if index == 1 else _first_line(spoken, fallback=safe_title),
                    x=width * 0.14,
                    y=height * 0.29,
                    fontsize=round(height * 0.027),
                    font_path=font_path,
                    color="0x111827",
                    enable=enable,
                ),
                *_multiline_drawtext(
                    visual,
                    x=width * 0.14,
                    y=height * 0.355,
                    fontsize=round(height * 0.019),
                    font_path=font_path,
                    color="0x334155",
                    enable=enable,
                    max_chars=18,
                    max_lines=2,
                ),
                _drawbox(width * 0.18, height * 0.64, width * 0.34, height * 0.13, theme["accent"], enable=enable),
                _drawbox(width * 0.52, height * 0.65, width * 0.24, height * 0.1, "0xf4bc63@0.96", enable=enable),
                _drawbox(width * 0.52, height * 0.65, width * 0.24, height * 0.014, "0x8a5630@0.96", enable=enable),
                _drawbox(width * 0.6, height * 0.61, width * 0.02, height * 0.032, "0xe8edf5@0.8", enable=enable),
                _drawbox(width * 0.65, height * 0.6, width * 0.02, height * 0.04, "0xe8edf5@0.8", enable=enable),
                _drawtext(
                    _first_line(spoken, fallback=product),
                    x=width * 0.12,
                    y=height * 0.8,
                    fontsize=round(height * 0.021),
                    font_path=font_path,
                    color="0xffffff",
                    enable=enable,
                    box=True,
                    boxcolor="0x121826@0.82",
                    boxborderw=max(8, round(width * 0.018)),
                ),
            ]
        )

    return ",".join(steps)


def _base_visual_steps(width: int, height: int) -> list[str]:
    return [
        f"drawbox=x=0:y=0:w={width}:h={height}:color=0xf2f4f7:t=fill",
        _drawbox(width * 0.04, height * 0.035, width * 0.92, height * 0.93, "0xffffff@0.98"),
        _drawbox(width * 0.08, height * 0.59, width * 0.84, height * 0.03, "0xdfe7f3@0.92"),
        _drawbox(width * 0.08, height * 0.62, width * 0.84, height * 0.2, "0xf7ead9@0.94"),
    ]


def _drawbox(
    x: float,
    y: float,
    w: float,
    h: float,
    color: str,
    *,
    enable: str | None = None,
) -> str:
    value = (
        f"drawbox=x={round(x)}:y={round(y)}:w={round(w)}:h={round(h)}:"
        f"color={color}:t=fill"
    )
    if enable:
        value += f":enable='{enable}'"
    return value


def _drawtext(
    text: str,
    *,
    x: float,
    y: float,
    fontsize: int,
    font_path: Path,
    color: str,
    enable: str | None = None,
    box: bool = False,
    boxcolor: str = "0xffffff@0.0",
    boxborderw: int = 0,
) -> str:
    options = [
        f"fontfile='{_escape_filter_value(str(font_path))}'",
        f"text='{_escape_filter_value(text)}'",
        "expansion=none",
        f"fontcolor={color}",
        f"fontsize={max(10, fontsize)}",
        f"x={round(x)}",
        f"y={round(y)}",
    ]
    if box:
        options.extend(["box=1", f"boxcolor={boxcolor}", f"boxborderw={boxborderw}"])
    if enable:
        options.append(f"enable='{enable}'")
    return "drawtext=" + ":".join(options)


def _multiline_drawtext(
    text: str,
    *,
    x: float,
    y: float,
    fontsize: int,
    font_path: Path,
    color: str,
    enable: str,
    max_chars: int,
    max_lines: int,
) -> list[str]:
    lines = _wrap_text(text, max_chars=max_chars, max_lines=max_lines)
    line_gap = round(fontsize * 1.25)
    return [
        _drawtext(
            line,
            x=x,
            y=y + index * line_gap,
            fontsize=fontsize,
            font_path=font_path,
            color=color,
            enable=enable,
        )
        for index, line in enumerate(lines)
    ]


def _normalise_segments(segments: list[dict[str, Any]], duration_seconds: int) -> list[dict[str, Any]]:
    if not segments:
        return [
            {
                "start_seconds": 0.0,
                "end_seconds": float(duration_seconds),
                "caption": "脚本预览",
                "text": "脚本预览",
            }
        ]
    normalised = []
    for index, segment in enumerate(segments, start=1):
        start = float(segment.get("start_seconds") or 0)
        end = float(segment.get("end_seconds") or start + float(segment.get("duration_seconds") or 3))
        if end <= start:
            end = start + 1
        start = max(0.0, min(start, duration_seconds))
        end = max(0.1, min(end, duration_seconds))
        if end <= start:
            start = max(0.0, end - 0.1)
        normalised.append(
            {
                **segment,
                "start_seconds": start,
                "end_seconds": end,
                "caption": segment.get("caption") or segment.get("text") or f"镜头 {index}",
            }
        )
    return normalised


def _scene_theme(index: int) -> dict[str, str]:
    themes = [
        {"wash": "0xfff4e3@0.9", "accent": "0xe65f42@0.94", "text": "0x9a3412"},
        {"wash": "0xeaf7f4@0.9", "accent": "0x149b84@0.94", "text": "0x0f766e"},
        {"wash": "0xeff4ff@0.9", "accent": "0x3164d8@0.94", "text": "0x1d4ed8"},
        {"wash": "0xfff0f5@0.9", "accent": "0xd9467a@0.94", "text": "0xbe185d"},
    ]
    return themes[(index - 1) % len(themes)]


def _wrap_text(text: str, *, max_chars: int, max_lines: int) -> list[str]:
    clean = _clean_text(text)
    if not clean:
        return []
    lines = []
    remaining = clean
    while remaining and len(lines) < max_lines:
        lines.append(remaining[:max_chars])
        remaining = remaining[max_chars:]
    if remaining and lines:
        lines[-1] = lines[-1].rstrip("，。,. ") + "..."
    return lines


def _first_line(text: str, *, fallback: str) -> str:
    return (_wrap_text(text, max_chars=18, max_lines=1) or [_clean_text(fallback)])[0]


def _clean_text(value: Any) -> str:
    return " ".join(str(value or "").replace("\n", " ").split())


def _escape_filter_value(value: str) -> str:
    return (
        value.replace("\\", "\\\\")
        .replace(":", "\\:")
        .replace("'", "\\'")
        .replace("%", "\\%")
    )


def _resolve_cjk_font() -> Path | None:
    candidates = [
        os.environ.get("SCRIPT2VIDEO_PREVIEW_FONT"),
        "/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc",
        "/usr/share/fonts/opentype/noto/NotoSansCJK-Bold.ttc",
        "/usr/share/fonts/truetype/noto/NotoSansCJK-Regular.ttc",
        "/System/Library/Fonts/PingFang.ttc",
        "/System/Library/Fonts/STHeiti Light.ttc",
    ]
    for candidate in candidates:
        if candidate and Path(candidate).is_file():
            return Path(candidate)
    return None


def _parse_filter_names(output: str) -> set[str]:
    names = set()
    for line in output.splitlines():
        parts = line.split()
        if len(parts) >= 2 and len(parts[0]) == 3:
            names.add(parts[1])
    return names


def _even(value: int) -> int:
    if value % 2 == 0:
        return value
    return value - 1


def _run(command: list[str], *, timeout_seconds: int) -> None:
    completed = subprocess.run(
        command,
        check=False,
        capture_output=True,
        text=True,
        timeout=timeout_seconds,
    )
    if completed.returncode != 0:
        tail = completed.stderr.strip()[-2000:]
        raise RuntimeError(tail or "ffmpeg command failed")
