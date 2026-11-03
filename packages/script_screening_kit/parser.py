from __future__ import annotations

import re
from typing import Any

SHOT_HEADER_RE = re.compile(
    r"(?m)^\s*镜头\s*(?P<number>\d+)\s*[｜|]\s*"
    r"(?P<duration>\d+(?:\.\d+)?)\s*(?:s|秒)?\s*"
    r"(?:[｜|]\s*(?P<camera>[^｜|\n]+?)\s*)?"
    r"(?:[｜|]\s*(?P<pace>[^\n]+?)\s*)?$"
)
FIELD_RE = re.compile(r"^\s*(?P<label>画面|台词|字幕|配乐|音效|备注|BGM)\s*[:：]\s*(?P<value>.*)\s*$")
HEADER_FIELD_TEMPLATE = r"(?m)^\s*{label}\s*[:：]\s*(?P<value>.+?)\s*$"
DURATION_RE = re.compile(r"(?P<duration>\d+(?:\.\d+)?)\s*(?:s|S|秒)")
SEPARATOR_RE = re.compile(r"^[\-—─_=\s]+$")

FIELD_MAP = {
    "画面": "visual",
    "台词": "dialogue",
    "字幕": "caption",
    "配乐": "music",
    "BGM": "music",
    "音效": "sfx",
    "备注": "notes",
}

PLATFORM_HINTS = {
    "抖音": "douyin",
    "快手": "kuaishou",
    "小红书": "xiaohongshu",
    "视频号": "wechat_channels",
    "B站": "bilibili",
    "哔哩": "bilibili",
}


class ScriptScreeningRoomParser:
    """Rule-based parser for Chinese storyboard scripts from an internal screening room."""

    def parse(
        self,
        *,
        raw_script: str,
        title: str | None = None,
        brand_name: str | None = None,
        product_name: str | None = None,
        target_platform: str | None = None,
        target_duration_seconds: int | None = None,
        aspect_ratio: str = "9:16",
        default_language: str = "zh-CN",
        style_key: str | None = None,
        parser_options: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        normalized = _normalize_script(raw_script)
        options = parser_options or {}
        shot_matches = list(SHOT_HEADER_RE.finditer(normalized))
        header_text = normalized[: shot_matches[0].start()] if shot_matches else normalized
        warnings: list[dict[str, Any]] = []

        resolved_title = title or _title_from_script(header_text, normalized)
        theme = _header_value(header_text, "主题")
        hook = _header_value(header_text, "钩子")
        overview = _header_value(header_text, "概述")
        platform = target_platform or _infer_platform(header_text) or "douyin"
        header_duration = _duration_from_text(header_text)
        header_style = _style_from_header(header_text)
        style = style_key or header_style or options.get("style") or "口播风格分镜"

        shots = _parse_shots(
            normalized=normalized,
            matches=shot_matches,
            fallback_text=overview or hook or normalized,
            fallback_duration=target_duration_seconds or header_duration or 15,
            warnings=warnings,
        )
        if not shot_matches:
            warnings.append(
                {
                    "code": "no_shot_header_detected",
                    "message": "未识别到“镜头 01｜3s｜...”格式，已按单镜头脚本处理。",
                }
            )

        planned_duration = sum(float(shot["duration_seconds"]) for shot in shots)
        resolved_duration = target_duration_seconds or round(planned_duration or header_duration or 15)
        full_dialogue = "\n".join(
            shot["dialogue"] for shot in shots if str(shot.get("dialogue") or "").strip()
        ).strip()
        caption_text = "\n".join(
            shot["caption"] for shot in shots if str(shot.get("caption") or "").strip()
        ).strip()
        inferred_brand = brand_name or _brand_from_title(resolved_title)
        inferred_product = product_name or theme or resolved_title

        scene_plan = {
            "version": "1.0",
            "source": {
                "format": "screening_room_storyboard",
                "parser": "rule_based_script_screening_room_parser",
            },
            "title": resolved_title,
            "theme": theme,
            "hook": hook,
            "overview": overview,
            "language": default_language,
            "target_platform": platform,
            "aspect_ratio": aspect_ratio,
            "target_duration_seconds": resolved_duration,
            "style": style,
            "brand_name": inferred_brand,
            "product_name": inferred_product,
            "shots": shots,
            "full_dialogue": full_dialogue or None,
            "caption_text": caption_text or None,
            "parse_warnings": warnings,
        }
        spoken_script = _spoken_script_from_scene_plan(scene_plan)
        shot_list = _shot_list_from_scene_plan(scene_plan, spoken_script=spoken_script)
        run_input = {
            "pipeline_key": options.get("pipeline_key") or "avatar_spokesperson",
            "raw_script": full_dialogue or normalized,
            "brand_name": inferred_brand,
            "product_name": inferred_product,
            "target_platform": platform,
            "target_duration_seconds": max(5, min(300, round(resolved_duration))),
            "aspect_ratio": aspect_ratio,
            "style_key": style_key,
        }
        return {
            "scene_plan": scene_plan,
            "spoken_script": spoken_script,
            "shot_list": shot_list,
            "run_input": run_input,
        }


def _parse_shots(
    *,
    normalized: str,
    matches: list[re.Match[str]],
    fallback_text: str,
    fallback_duration: float,
    warnings: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    if not matches:
        text = _single_line(fallback_text)
        return [
            {
                "shot_id": "shot_01",
                "shot_number": 1,
                "start_seconds": 0,
                "end_seconds": float(fallback_duration),
                "duration_seconds": float(fallback_duration),
                "camera": None,
                "pace": None,
                "visual": text,
                "dialogue": text,
                "caption": text,
                "music": None,
                "sfx": None,
                "notes": [],
            }
        ]

    shots: list[dict[str, Any]] = []
    cursor = 0.0
    for index, match in enumerate(matches):
        next_start = matches[index + 1].start() if index + 1 < len(matches) else len(normalized)
        body = normalized[match.end() : next_start]
        fields = _extract_shot_fields(body)
        duration = _safe_float(match.group("duration"), default=3.0)
        number = int(match.group("number"))
        visual = fields.get("visual") or fields.get("dialogue") or fields.get("caption") or ""
        dialogue = fields.get("dialogue") or fields.get("caption") or visual
        caption = fields.get("caption") or dialogue
        if not visual:
            warnings.append(
                {
                    "code": "shot_visual_missing",
                    "shot_number": number,
                    "message": "镜头缺少画面字段，已用台词/字幕兜底。",
                }
            )
        shot = {
            "shot_id": f"shot_{number:02d}",
            "shot_number": number,
            "start_seconds": round(cursor, 2),
            "end_seconds": round(cursor + duration, 2),
            "duration_seconds": duration,
            "camera": _clean_optional(match.group("camera")),
            "pace": _clean_optional(match.group("pace")),
            "visual": _single_line(visual),
            "dialogue": _single_line(dialogue),
            "caption": _single_line(caption),
            "music": _clean_optional(fields.get("music")),
            "sfx": _clean_optional(fields.get("sfx")),
            "notes": fields.get("notes", []),
        }
        shots.append(shot)
        cursor += duration
    return shots


def _extract_shot_fields(body: str) -> dict[str, Any]:
    fields: dict[str, Any] = {"notes": []}
    current_key: str | None = None
    for raw_line in body.splitlines():
        line = raw_line.strip()
        if not line or SEPARATOR_RE.match(line):
            continue
        match = FIELD_RE.match(line)
        if match:
            label = match.group("label")
            current_key = FIELD_MAP[label]
            value = match.group("value").strip()
            if current_key == "notes":
                if value:
                    fields["notes"].append(value)
            elif value:
                fields[current_key] = value
            else:
                fields.setdefault(current_key, "")
            continue
        if current_key == "notes":
            fields["notes"].append(line)
        elif current_key:
            fields[current_key] = f"{fields.get(current_key, '')} {line}".strip()
    return fields


def _spoken_script_from_scene_plan(scene_plan: dict[str, Any]) -> dict[str, Any]:
    segments: list[dict[str, Any]] = []
    for index, shot in enumerate(scene_plan["shots"], start=1):
        text = shot.get("dialogue") or shot.get("caption") or shot.get("visual") or ""
        segments.append(
            {
                "segment_id": f"seg_{index:02d}",
                "start_seconds": shot["start_seconds"],
                "end_seconds": shot["end_seconds"],
                "text": text,
                "caption": shot.get("caption") or text,
                "source_shot_id": shot["shot_id"],
            }
        )
    return {
        "version": "1.0",
        "planner": {
            "provider": "rule_based",
            "tool_key": "script_screening_room_parser",
            "source_artifact_type": "screening_room_scene_plan",
        },
        "language": scene_plan["language"],
        "total_duration_seconds": scene_plan["target_duration_seconds"],
        "segments": segments,
        "full_text": "\n".join(segment["text"] for segment in segments).strip(),
        "caption_style": {
            "format": "short_video_burned_in",
            "language": scene_plan["language"],
            "source": "screening_room_caption_fields",
        },
    }


def _shot_list_from_scene_plan(
    scene_plan: dict[str, Any],
    *,
    spoken_script: dict[str, Any],
) -> dict[str, Any]:
    segment_by_shot = {
        segment["source_shot_id"]: segment["segment_id"]
        for segment in spoken_script.get("segments", [])
    }
    shots = []
    for shot in scene_plan["shots"]:
        shots.append(
            {
                "shot_id": shot["shot_id"],
                "spoken_segment_ids": [segment_by_shot[shot["shot_id"]]],
                "duration_seconds": shot["duration_seconds"],
                "framing": shot.get("camera"),
                "pace": shot.get("pace"),
                "visual": shot.get("visual"),
                "caption": shot.get("caption"),
                "audio_note": shot.get("music"),
                "sfx": shot.get("sfx"),
                "source": {
                    "artifact_type": "screening_room_scene_plan",
                    "source_shot_id": shot["shot_id"],
                },
            }
        )
    return {
        "version": "1.0",
        "planner": {
            "provider": "rule_based",
            "tool_key": "script_screening_room_parser",
            "source_artifact_type": "screening_room_scene_plan",
        },
        "aspect_ratio": scene_plan["aspect_ratio"],
        "shots": shots,
        "asset_requirements": [
            {
                "asset_type": "person_image",
                "role": "digital_spokesperson",
                "required": True,
                "notes": "可由平台生成或用户上传，用于数字人口播。",
            },
            {
                "asset_type": "background_music",
                "role": "music_bed",
                "required": False,
                "notes": "优先使用分镜中的配乐描述生成或检索。",
            },
        ],
        "subtitle_plan": {
            "source": "screening_room_caption_fields",
            "mode": "burned_in",
            "captions": [
                {
                    "shot_id": shot["shot_id"],
                    "start_seconds": shot["start_seconds"],
                    "end_seconds": shot["end_seconds"],
                    "text": shot.get("caption") or shot.get("dialogue"),
                }
                for shot in scene_plan["shots"]
            ],
        },
        "music_plan": {
            "source": "screening_room_music_fields",
            "style_notes": [
                shot["music"] for shot in scene_plan["shots"] if str(shot.get("music") or "").strip()
            ],
            "recommended_volume": 0.18,
        },
    }


def _normalize_script(raw_script: str) -> str:
    lines = [line.rstrip() for line in raw_script.replace("\r\n", "\n").replace("\r", "\n").split("\n")]
    return "\n".join(line for line in lines).strip()


def _header_value(text: str, label: str) -> str | None:
    pattern = re.compile(HEADER_FIELD_TEMPLATE.format(label=re.escape(label)))
    match = pattern.search(text)
    return _clean_optional(match.group("value")) if match else None


def _title_from_script(header_text: str, normalized: str) -> str:
    bracketed = re.search(r"【(?P<title>[^】]+)】", normalized)
    if bracketed:
        return bracketed.group("title").strip()
    for line in header_text.splitlines():
        cleaned = line.strip()
        if cleaned and not SEPARATOR_RE.match(cleaned):
            return cleaned
    return "脚本放映室分镜"


def _brand_from_title(title: str) -> str | None:
    cleaned = title.strip()
    return cleaned or None


def _infer_platform(text: str) -> str | None:
    for hint, platform in PLATFORM_HINTS.items():
        if hint in text:
            return platform
    return None


def _duration_from_text(text: str) -> int | None:
    match = DURATION_RE.search(text)
    if not match:
        return None
    return round(_safe_float(match.group("duration"), default=15))


def _style_from_header(text: str) -> str | None:
    for line in text.splitlines():
        if "·" not in line and "|" not in line and "｜" not in line:
            continue
        tokens = [
            token.strip()
            for token in re.split(r"[·|｜]", line)
            if token.strip() and not DURATION_RE.search(token)
        ]
        style_tokens = [token for token in tokens if not _infer_platform(token)]
        if style_tokens:
            return style_tokens[-1]
    return None


def _single_line(value: Any) -> str:
    return re.sub(r"\s+", " ", str(value or "")).strip()


def _clean_optional(value: Any) -> str | None:
    cleaned = _single_line(value)
    return cleaned or None


def _safe_float(value: str | None, *, default: float) -> float:
    if value is None:
        return default
    try:
        return float(value)
    except ValueError:
        return default
