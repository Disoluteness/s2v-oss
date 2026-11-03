from __future__ import annotations

import json
import os
import re
import urllib.error
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from dotenv import dotenv_values
from pydantic import BaseModel, Field

DEFAULT_DASHSCOPE_BASE_URL = "https://dashscope.aliyuncs.com/compatible-mode/v1"
DEFAULT_DASHSCOPE_MODEL = "qwen-plus"


class PlannerResult(BaseModel):
    artifact_type: str
    payload: dict[str, Any]
    summary: str
    risk_flags: list[str] = Field(default_factory=list)


@dataclass(frozen=True)
class SceneSeed:
    scene_id: str
    duration_seconds: int
    framing: str
    pace: str
    visual: str
    spoken_text: str
    caption: str
    audio_note: str


class DashScopePlannerClient:
    """Small OpenAI-compatible DashScope client used only when explicitly enabled."""

    def __init__(
        self,
        *,
        api_key: str,
        base_url: str = DEFAULT_DASHSCOPE_BASE_URL,
        model: str = DEFAULT_DASHSCOPE_MODEL,
        timeout_seconds: int = 45,
    ) -> None:
        self.api_key = api_key
        self.base_url = base_url.rstrip("/")
        self.model = model
        self.timeout_seconds = timeout_seconds

    def generate_json(self, *, stage_key: str, context: dict[str, Any]) -> dict[str, Any]:
        prompt = {
            "role": "system",
            "content": (
                "You are a production planner for short Chinese spokesperson ads. "
                "Return one strict JSON object only. Do not wrap it in markdown. "
                "Preserve brand claims exactly as provided and avoid inventing regulated claims."
            ),
        }
        user = {
            "role": "user",
            "content": json.dumps(
                {
                    "stage_key": stage_key,
                    "required_output": self._required_output(stage_key),
                    "context": context,
                },
                ensure_ascii=False,
            ),
        }
        body = {
            "model": self.model,
            "messages": [prompt, user],
            "temperature": 0.2,
            "stream": False,
        }
        request = urllib.request.Request(
            f"{self.base_url}/chat/completions",
            data=json.dumps(body, ensure_ascii=False).encode("utf-8"),
            headers={
                "Authorization": f"Bearer {self.api_key}",
                "Content-Type": "application/json",
            },
            method="POST",
        )
        try:
            with urllib.request.urlopen(request, timeout=self.timeout_seconds) as response:
                raw = response.read().decode("utf-8")
        except urllib.error.URLError as exc:
            raise RuntimeError(f"DashScope planner request failed: {exc}") from exc

        payload = json.loads(raw)
        content = payload["choices"][0]["message"]["content"]
        return _extract_json_object(content)

    def _required_output(self, stage_key: str) -> dict[str, Any]:
        if stage_key == "plan":
            return {
                "artifact_type": "production_plan",
                "required_keys": [
                    "delivery",
                    "creative_strategy",
                    "tool_plan",
                    "stage_plan",
                    "review_checklist",
                ],
            }
        if stage_key == "script":
            return {
                "artifact_type": "spoken_script",
                "required_keys": ["segments", "full_text", "caption_style"],
            }
        return {
            "artifact_type": "shot_list",
            "required_keys": ["shots", "asset_requirements", "subtitle_plan", "music_plan"],
        }


class SpokespersonAdPlanner:
    def __init__(self, *, llm_client: DashScopePlannerClient | None = None) -> None:
        self.llm_client = llm_client

    @classmethod
    def from_environment(cls, *, env_file: Path | None = None) -> SpokespersonAdPlanner:
        env = _load_environment(env_file=env_file)
        provider = env.get("SCRIPT2VIDEO_PLANNER_PROVIDER", "rule_based").strip().lower()
        if provider != "dashscope":
            return cls()

        api_key = env.get("DASHSCOPE_API_KEY")
        if not api_key:
            return cls()
        return cls(
            llm_client=DashScopePlannerClient(
                api_key=api_key,
                base_url=env.get("DASHSCOPE_BASE_URL", DEFAULT_DASHSCOPE_BASE_URL),
                model=env.get("DASHSCOPE_LLM_MODEL", DEFAULT_DASHSCOPE_MODEL),
            )
        )

    def create_production_plan(self, context: dict[str, Any]) -> PlannerResult:
        fallback = self._rule_based_production_plan(context)
        return self._maybe_enhance_with_llm(
            stage_key="plan",
            artifact_type="production_plan",
            context=context,
            fallback=fallback,
            required_keys=[
                "delivery",
                "creative_strategy",
                "tool_plan",
                "stage_plan",
                "review_checklist",
            ],
        )

    def create_spoken_script(self, context: dict[str, Any]) -> PlannerResult:
        fallback = self._rule_based_spoken_script(context)
        return self._maybe_enhance_with_llm(
            stage_key="script",
            artifact_type="spoken_script",
            context=context,
            fallback=fallback,
            required_keys=["segments", "full_text", "caption_style"],
        )

    def create_shot_list(self, context: dict[str, Any]) -> PlannerResult:
        fallback = self._rule_based_shot_list(context)
        return self._maybe_enhance_with_llm(
            stage_key="storyboard",
            artifact_type="shot_list",
            context=context,
            fallback=fallback,
            required_keys=["shots", "asset_requirements", "subtitle_plan", "music_plan"],
        )

    def _rule_based_production_plan(self, context: dict[str, Any]) -> PlannerResult:
        intake = _intake(context)
        style_snapshot = _style_snapshot(intake)
        scenes = _scene_seeds(intake)
        available_tools = context.get("available_tools", [])
        duration_seconds = _target_duration(intake, scenes)
        tool_plan = [
            _select_tool(available_tools, capability)
            for capability in [
                "avatar_video",
                "tts",
                "subtitle_generation",
                "music_generation",
                "video_composition",
                "storage",
            ]
        ]
        hook = scenes[0].spoken_text if scenes else _first_sentence(intake.get("raw_script", ""))
        cta = scenes[-1].spoken_text if scenes else "现在就试试。"
        payload = {
            "version": "1.0",
            "planner": _planner_metadata("rule_based_planner"),
            "title": _title(intake),
            "delivery": {
                "platform": intake.get("target_platform", "douyin"),
                "aspect_ratio": intake.get("aspect_ratio", "9:16"),
                "duration_seconds": duration_seconds,
                "language": intake.get("language", "zh-CN"),
            },
            "creative_strategy": {
                "format": "digital_spokesperson_ad",
                "hook": hook,
                "core_message": _core_message(intake),
                "cta": cta,
                "tone_keywords": _tone_keywords(
                    intake.get("raw_script", ""),
                    style_snapshot=style_snapshot,
                ),
                "audience": "短视频平台上的即时消费人群",
                "style_preset": _style_preset_summary(style_snapshot),
                "brand_constraints": _brand_constraints(style_snapshot),
            },
            "tool_plan": tool_plan,
            "stage_plan": [
                {
                    "stage_key": "script",
                    "goal": "整理可口播台词，并保留原始卖点顺序。",
                    "output_artifact_type": "spoken_script",
                },
                {
                    "stage_key": "storyboard",
                    "goal": "把每段口播映射到镜头、字幕和素材需求。",
                    "output_artifact_type": "shot_list",
                },
                {
                    "stage_key": "asset_generation",
                    "goal": "生成数字人口播视频、字幕、音乐和必要补充素材。",
                    "output_artifact_type": "asset_manifest",
                },
                {
                    "stage_key": "compose",
                    "goal": "用 FFmpeg 合成 9:16 成片并输出可下载 MP4。",
                    "output_artifact_type": "render_summary",
                },
            ],
            "review_checklist": [
                "品牌名、产品名和口播卖点是否准确",
                "时长是否适合目标平台",
                "数字人、字幕、音乐和合成路径是否可接受",
                "是否存在需要法务或平台合规确认的功效表达",
            ],
            "source_scene_count": len(scenes),
        }
        return PlannerResult(
            artifact_type="production_plan",
            payload=payload,
            summary="Generated production plan from intake brief and runtime tool registry.",
        )

    def _rule_based_spoken_script(self, context: dict[str, Any]) -> PlannerResult:
        intake = _intake(context)
        style_snapshot = _style_snapshot(intake)
        scenes = _scene_seeds(intake)
        total = _target_duration(intake, scenes)
        segments = []
        cursor = 0
        for index, scene in enumerate(_fit_scene_durations(scenes, total), start=1):
            end = cursor + scene.duration_seconds
            segments.append(
                {
                    "segment_id": f"line_{index:02d}",
                    "source_scene_id": scene.scene_id,
                    "speaker": "digital_spokesperson",
                    "start_seconds": cursor,
                    "end_seconds": end,
                    "duration_seconds": scene.duration_seconds,
                    "text": scene.spoken_text,
                    "caption": scene.caption,
                    "delivery_note": _delivery_note(scene),
                }
            )
            cursor = end
        payload = {
            "version": "1.0",
            "planner": _planner_metadata("rule_based_planner"),
            "language": intake.get("language", "zh-CN"),
            "total_duration_seconds": cursor,
            "segments": segments,
            "full_text": "\n".join(segment["text"] for segment in segments),
            "caption_style": _caption_style(intake, style_snapshot=style_snapshot),
            "compliance_notes": _compliance_notes(intake.get("raw_script", "")),
        }
        return PlannerResult(
            artifact_type="spoken_script",
            payload=payload,
            summary="Generated spoken script with timed digital-spokesperson segments.",
        )

    def _rule_based_shot_list(self, context: dict[str, Any]) -> PlannerResult:
        intake = _intake(context)
        style_snapshot = _style_snapshot(intake)
        spoken_script = context.get("current_artifacts", {}).get("spoken_script", {})
        scenes = _scene_seeds(intake)
        segments = spoken_script.get("segments") or []
        if not segments:
            segments = self._rule_based_spoken_script(context).payload["segments"]

        fitted_scenes = _fit_scene_durations(scenes, sum(segment["duration_seconds"] for segment in segments))
        shots = []
        asset_requirements = []
        for index, segment in enumerate(segments, start=1):
            scene = fitted_scenes[index - 1] if index - 1 < len(fitted_scenes) else scenes[-1]
            shot_id = f"shot_{index:02d}"
            shots.append(
                {
                    "shot_id": shot_id,
                    "source_scene_id": scene.scene_id,
                    "spoken_segment_ids": [segment["segment_id"]],
                    "duration_seconds": segment["duration_seconds"],
                    "framing": scene.framing,
                    "pace": scene.pace,
                    "visual_direction": scene.visual,
                    "visual_style": _visual_style(style_snapshot),
                    "caption": segment["caption"],
                    "avatar_direction": _avatar_direction(scene),
                    "transition_in": "cut" if index == 1 else "quick_cut",
                    "transition_out": "end_card" if index == len(segments) else "cut",
                    "required_assets": [
                        {
                            "asset_type": "avatar_video_clip",
                            "description": f"数字人口播片段：{segment['text']}",
                        }
                    ],
                }
            )
            asset_requirements.append(
                {
                    "asset_key": f"avatar_clip_{index:02d}",
                    "asset_type": "avatar_video_clip",
                    "stage_key": "asset_generation",
                    "shot_ids": [shot_id],
                    "duration_seconds": segment["duration_seconds"],
                    "provider_capability": "avatar_video",
                    "prompt": scene.visual,
                    "spoken_text": segment["text"],
                }
            )

        payload = {
            "version": "1.0",
            "planner": _planner_metadata("rule_based_planner"),
            "aspect_ratio": intake.get("aspect_ratio", "9:16"),
            "shots": shots,
            "asset_requirements": asset_requirements,
            "subtitle_plan": {
                "source": "spoken_script.segments.caption",
                "format": "burned_in_srt",
                "safe_area": _safe_area(style_snapshot) or "vertical_short_video_bottom",
                "style": _caption_style(intake, style_snapshot=style_snapshot),
            },
            "music_plan": {
                "need_bgm": True,
                "mood": _music_mood(intake.get("raw_script", "")),
                "duck_under_speech": True,
                "fallback": "skip_bgm_if_no_provider_or_licensed_track",
            },
        }
        return PlannerResult(
            artifact_type="shot_list",
            payload=payload,
            summary="Generated shot list with timed shots and asset requirements.",
        )

    def _maybe_enhance_with_llm(
        self,
        *,
        stage_key: str,
        artifact_type: str,
        context: dict[str, Any],
        fallback: PlannerResult,
        required_keys: list[str],
    ) -> PlannerResult:
        if self.llm_client is None:
            return fallback
        try:
            payload = self.llm_client.generate_json(stage_key=stage_key, context=context)
        except (RuntimeError, KeyError, json.JSONDecodeError, TypeError, ValueError) as exc:
            return fallback.model_copy(
                update={
                    "risk_flags": [
                        *fallback.risk_flags,
                        f"llm_planner_fallback:{type(exc).__name__}",
                    ]
                }
            )
        missing = [key for key in required_keys if key not in payload]
        if missing:
            return fallback.model_copy(
                update={
                    "risk_flags": [
                        *fallback.risk_flags,
                        "llm_planner_fallback:missing_keys:" + ",".join(missing),
                    ]
                }
            )
        payload = {
            **payload,
            "version": payload.get("version", "1.0"),
            "planner": _planner_metadata(
                "dashscope_planner",
                provider="dashscope",
                model=self.llm_client.model,
            ),
        }
        return PlannerResult(
            artifact_type=artifact_type,
            payload=payload,
            summary=f"Generated {artifact_type} with DashScope planner.",
        )


def _load_environment(*, env_file: Path | None = None) -> dict[str, str]:
    project_root = Path(__file__).resolve().parents[2]
    values = dotenv_values(env_file or project_root / ".env")
    merged = {**values, **os.environ}
    return {key: value for key, value in merged.items() if value is not None}


def _extract_json_object(content: str) -> dict[str, Any]:
    stripped = content.strip()
    if stripped.startswith("```"):
        stripped = re.sub(r"^```(?:json)?\s*", "", stripped)
        stripped = re.sub(r"\s*```$", "", stripped)
    try:
        parsed = json.loads(stripped)
    except json.JSONDecodeError:
        start = stripped.find("{")
        end = stripped.rfind("}")
        if start == -1 or end == -1 or end <= start:
            raise
        parsed = json.loads(stripped[start : end + 1])
    if not isinstance(parsed, dict):
        raise TypeError("LLM response must be a JSON object")
    return parsed


def _intake(context: dict[str, Any]) -> dict[str, Any]:
    current = context.get("current_artifacts", {})
    user_preferences = context.get("user_preferences", {})
    intake_brief = current.get("intake_brief") or {}
    return {**user_preferences, **intake_brief}


def _style_snapshot(intake: dict[str, Any]) -> dict[str, Any]:
    snapshot = intake.get("style_preset_snapshot") or {}
    return snapshot if isinstance(snapshot, dict) else {}


def _style_preset(style_snapshot: dict[str, Any]) -> dict[str, Any]:
    preset = style_snapshot.get("style_preset") or {}
    return preset if isinstance(preset, dict) else {}


def _brand_kit(style_snapshot: dict[str, Any]) -> dict[str, Any]:
    brand = style_snapshot.get("brand_kit") or {}
    return brand if isinstance(brand, dict) else {}


def _style_preset_summary(style_snapshot: dict[str, Any]) -> dict[str, Any] | None:
    preset = _style_preset(style_snapshot)
    if not preset:
        return None
    return {
        "style_preset_id": preset.get("style_preset_id"),
        "name": preset.get("name"),
        "style_key": preset.get("style_key"),
        "aspect_ratio": preset.get("aspect_ratio"),
        "tone": preset.get("tone") or {},
        "visual_style": preset.get("visual_style") or {},
    }


def _brand_constraints(style_snapshot: dict[str, Any]) -> dict[str, Any]:
    brand = _brand_kit(style_snapshot)
    preset = _style_preset(style_snapshot)
    guardrails = preset.get("guardrails") or {}
    return {
        "color_palette": brand.get("color_palette") or {},
        "typography": brand.get("typography") or {},
        "brand_voice": brand.get("brand_voice") or {},
        "safe_area": brand.get("safe_area") or {},
        "banned_terms": sorted(
            {
                *[str(term) for term in brand.get("banned_terms") or []],
                *[str(term) for term in guardrails.get("banned_terms") or []],
            }
        ),
    }


def _caption_style(
    intake: dict[str, Any],
    *,
    style_snapshot: dict[str, Any],
) -> dict[str, Any]:
    preset = _style_preset(style_snapshot)
    brand = _brand_kit(style_snapshot)
    caption_style = {
        "placement": "bottom_safe_area",
        "max_lines": 2,
        "emphasis": "highlight_hook_and_cta",
        "language": intake.get("language", "zh-CN"),
    }
    configured = preset.get("subtitle_style")
    if isinstance(configured, dict):
        caption_style.update(configured)
    typography = brand.get("typography")
    if isinstance(typography, dict) and typography:
        caption_style.setdefault("typography", typography)
    safe_area = brand.get("safe_area")
    if isinstance(safe_area, dict) and safe_area:
        caption_style.setdefault("safe_area", safe_area)
    return caption_style


def _visual_style(style_snapshot: dict[str, Any]) -> dict[str, Any]:
    preset = _style_preset(style_snapshot)
    brand = _brand_kit(style_snapshot)
    visual_style = dict(preset.get("visual_style") or {})
    color_palette = brand.get("color_palette")
    if isinstance(color_palette, dict) and color_palette:
        visual_style.setdefault("color_palette", color_palette)
    return visual_style


def _safe_area(style_snapshot: dict[str, Any]) -> dict[str, Any] | None:
    brand = _brand_kit(style_snapshot)
    safe_area = brand.get("safe_area")
    return safe_area if isinstance(safe_area, dict) and safe_area else None


def _planner_metadata(
    tool_key: str,
    *,
    provider: str = "internal",
    model: str | None = None,
) -> dict[str, Any]:
    return {
        "tool_key": tool_key,
        "provider": provider,
        "model": model,
        "mode": "llm" if provider != "internal" else "deterministic",
    }


def _parse_scene_blocks(raw_script: str) -> list[SceneSeed]:
    lines = [line.strip() for line in raw_script.splitlines() if line.strip()]
    scene_indices = [
        index
        for index, line in enumerate(lines)
        if re.match(r"^镜头\s*\d+", line)
    ]
    if not scene_indices:
        return []

    scenes = []
    for position, start in enumerate(scene_indices):
        end = scene_indices[position + 1] if position + 1 < len(scene_indices) else len(lines)
        block = lines[start:end]
        header = block[0]
        parts = re.split(r"[|｜]", header)
        scene_number = re.search(r"\d+", parts[0])
        duration = _parse_duration(header) or 4
        fields = _extract_labeled_fields(block[1:])
        scenes.append(
            SceneSeed(
                scene_id=f"scene_{int(scene_number.group()):02d}" if scene_number else f"scene_{position + 1:02d}",
                duration_seconds=duration,
                framing=parts[2].strip() if len(parts) > 2 else "中景",
                pace=parts[3].strip() if len(parts) > 3 else "节奏中",
                visual=fields.get("画面", ""),
                spoken_text=fields.get("台词", "") or fields.get("字幕", ""),
                caption=fields.get("字幕", "") or fields.get("台词", ""),
                audio_note=fields.get("配乐", ""),
            )
        )
    return [scene for scene in scenes if scene.spoken_text or scene.caption or scene.visual]


def _scene_seeds(intake: dict[str, Any]) -> list[SceneSeed]:
    raw_script = intake.get("raw_script", "")
    parsed = _parse_scene_blocks(raw_script)
    if parsed:
        return parsed

    text_parts = _sentences(raw_script) or [
        _core_message(intake),
        "热水一冲就能享受香浓口感。",
        "追剧、自习、办公室都适合来一杯。",
        "现在就囤起来。",
    ]
    target_duration = int(intake.get("target_duration_seconds") or 15)
    per_scene = max(3, round(target_duration / max(len(text_parts), 1)))
    scenes = []
    for index, text in enumerate(text_parts, start=1):
        scenes.append(
            SceneSeed(
                scene_id=f"scene_{index:02d}",
                duration_seconds=per_scene,
                framing="近景" if index in {1, len(text_parts)} else "中景",
                pace="节奏快" if index in {1, len(text_parts)} else "节奏中",
                visual=_fallback_visual(index, text, intake),
                spoken_text=text,
                caption=text,
                audio_note="轻快背景音乐",
            )
        )
    return scenes


def _extract_labeled_fields(lines: list[str]) -> dict[str, str]:
    fields: dict[str, str] = {}
    current_label: str | None = None
    for line in lines:
        match = re.match(r"^(画面|台词|字幕|配乐)\s*[:：]\s*(.*)$", line)
        if match:
            current_label = match.group(1)
            fields[current_label] = match.group(2).strip()
        elif current_label:
            fields[current_label] = f"{fields[current_label]} {line}".strip()
    return fields


def _parse_duration(text: str) -> int | None:
    match = re.search(r"(\d+)\s*s", text, flags=re.IGNORECASE)
    return int(match.group(1)) if match else None


def _sentences(text: str) -> list[str]:
    compact = re.sub(r"\s+", " ", text).strip()
    if not compact:
        return []
    candidates = re.split(r"(?<=[。！？!?])", compact)
    return [candidate.strip() for candidate in candidates if candidate.strip()]


def _first_sentence(text: str) -> str:
    sentences = _sentences(text)
    return sentences[0] if sentences else "开场用一句直接问题抓住注意力。"


def _target_duration(intake: dict[str, Any], scenes: list[SceneSeed]) -> int:
    requested = int(intake.get("target_duration_seconds") or 0)
    if requested > 0:
        return requested
    scene_total = sum(scene.duration_seconds for scene in scenes)
    return scene_total or 15


def _fit_scene_durations(scenes: list[SceneSeed], target_duration: int) -> list[SceneSeed]:
    if not scenes:
        return []
    current_total = sum(scene.duration_seconds for scene in scenes)
    if current_total == target_duration:
        return scenes
    if current_total <= 0:
        current_total = len(scenes)

    fitted = []
    remaining = target_duration
    for index, scene in enumerate(scenes, start=1):
        if index == len(scenes):
            duration = max(1, remaining)
        else:
            duration = max(1, round(scene.duration_seconds * target_duration / current_total))
            remaining -= duration
        fitted.append(
            SceneSeed(
                scene_id=scene.scene_id,
                duration_seconds=duration,
                framing=scene.framing,
                pace=scene.pace,
                visual=scene.visual,
                spoken_text=scene.spoken_text,
                caption=scene.caption,
                audio_note=scene.audio_note,
            )
        )
    return fitted


def _title(intake: dict[str, Any]) -> str:
    brand = intake.get("brand_name") or "品牌"
    product = intake.get("product_name") or "产品"
    return f"{brand}{product}短视频口播广告"


def _core_message(intake: dict[str, Any]) -> str:
    brand = intake.get("brand_name") or "这个品牌"
    product = intake.get("product_name") or "这款产品"
    selling_points = intake.get("selling_points") or []
    if selling_points:
        return f"{brand}{product}的核心卖点是：" + "、".join(selling_points)
    raw_script = intake.get("raw_script", "")
    if "奶茶" in raw_script:
        return f"{brand}{product}主打热水即冲、香浓方便的秋日奶茶体验。"
    return f"{brand}{product}用短口播突出核心利益点和行动召唤。"


def _tone_keywords(raw_script: str, *, style_snapshot: dict[str, Any] | None = None) -> list[str]:
    keywords = ["口语化", "清晰", "短视频节奏"]
    preset = _style_preset(style_snapshot or {})
    tone = preset.get("tone") or {}
    configured = tone.get("keywords") or tone.get("tone_keywords") or []
    for keyword in configured:
        if str(keyword) not in keywords:
            keywords.append(str(keyword))
    if any(token in raw_script for token in ["俏皮", "挑眉", "快去", "别等"]):
        keywords.append("俏皮")
    if any(token in raw_script for token in ["暖", "秋天", "热水"]):
        keywords.append("温暖")
    return list(dict.fromkeys(keywords))


def _music_mood(raw_script: str) -> str:
    if any(token in raw_script for token in ["秋天", "暖", "温馨"]):
        return "warm_light_guitar"
    if any(token in raw_script for token in ["快", "囤", "冲"]):
        return "bright_pop"
    return "light_commercial"


def _fallback_visual(index: int, text: str, intake: dict[str, Any]) -> str:
    product = intake.get("product_name") or "产品"
    if index == 1:
        return "数字人面向镜头开场，用一个问题抓住注意力。"
    if any(token in text for token in ["倒", "冲", "热水"]):
        return f"展示{product}冲泡过程，保留热气和产品包装。"
    if any(token in text for token in ["囤", "买", "快去"]):
        return "数字人把成品递向镜头，形成行动召唤。"
    return f"数字人手持{product}，配合字幕强调卖点。"


def _delivery_note(scene: SceneSeed) -> str:
    if "快" in scene.pace:
        return "语速略快，结尾轻微上扬。"
    if "慢" in scene.pace:
        return "语速放缓，强调情绪和质感。"
    return "自然口播，保留停顿和亲近感。"


def _avatar_direction(scene: SceneSeed) -> str:
    if "递" in scene.visual:
        return "面带笑容，手部动作朝镜头方向。"
    if "喝" in scene.visual or "捧" in scene.visual:
        return "双手捧杯，表情满足。"
    if "撕开" in scene.visual or "倒入" in scene.visual:
        return "镜头聚焦产品动作，数字人表情自然。"
    return "看向镜头，表情明亮，动作简洁。"


def _compliance_notes(raw_script: str) -> list[str]:
    notes = []
    if re.search(r"0\s*反式脂肪酸", raw_script):
        notes.append("包含营养/成分声明，投放前应确认包装与平台要求一致。")
    return notes


def _select_tool(available_tools: list[dict[str, Any]], capability: str) -> dict[str, Any]:
    candidates = [tool for tool in available_tools if tool.get("capability") == capability]
    available = [tool for tool in candidates if tool.get("status") == "available"]
    selected = (available or candidates or [{}])[0]
    return {
        "capability": capability,
        "preferred_tool_key": selected.get("tool_key"),
        "provider": selected.get("provider"),
        "status": selected.get("status", "unavailable"),
        "fallback_tool_keys": selected.get("fallback_tool_keys", []),
        "missing_dependencies": selected.get("missing_dependencies", []),
    }
