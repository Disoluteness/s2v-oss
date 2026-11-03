from __future__ import annotations

from copy import deepcopy
from typing import Any

from pydantic import BaseModel, Field

DEFAULT_RENDER_PROFILE_KEY = "douyin_vertical"


class RenderProfile(BaseModel):
    schema_version: str = "1.0"
    profile_key: str
    display_name: str
    platform_key: str
    platform_label: str
    aspect_ratio: str
    width: int = Field(ge=2)
    height: int = Field(ge=2)
    fps: int = Field(ge=1, le=120)
    video_bitrate: str
    audio_bitrate: str = "128k"
    crf: int = Field(default=22, ge=0, le=51)
    preset: str = "medium"
    safe_area: dict[str, int]
    caption: dict[str, Any]
    cover: dict[str, Any]
    export: dict[str, Any] = Field(default_factory=dict)
    aliases: list[str] = Field(default_factory=list)
    notes: list[str] = Field(default_factory=list)


def _normalize(value: str | None) -> str:
    return (value or "").strip().lower().replace("-", "_").replace(" ", "_")


_BUILTIN_PROFILES: tuple[RenderProfile, ...] = (
    RenderProfile(
        profile_key="douyin_vertical",
        display_name="抖音竖屏",
        platform_key="douyin",
        platform_label="抖音",
        aspect_ratio="9:16",
        width=1080,
        height=1920,
        fps=30,
        video_bitrate="6000k",
        audio_bitrate="128k",
        crf=22,
        preset="medium",
        safe_area={"top": 180, "right": 72, "bottom": 260, "left": 72},
        caption={
            "placement": "bottom",
            "anchor": "center",
            "max_width_ratio": 0.86,
            "font_family": "Noto Sans CJK SC",
            "font_size": 56,
            "line_height": 1.16,
            "margin_bottom": 220,
            "ffmpeg_font_size": 38,
            "ffmpeg_margin_v": 96,
            "safe_area": "douyin_vertical_bottom",
        },
        cover={
            "width": 1080,
            "height": 1920,
            "safe_area": {"top": 220, "right": 90, "bottom": 300, "left": 90},
            "title_zone": "center",
        },
        export={"container": "mp4", "video_codec": "h264", "audio_codec": "aac"},
        aliases=["douyin", "抖音", "tiktok_cn", "china_tiktok"],
        notes=["默认短视频竖屏规格，适合口播、带字幕广告和知识短视频。"],
    ),
    RenderProfile(
        profile_key="xiaohongshu_portrait",
        display_name="小红书竖版封面友好",
        platform_key="xiaohongshu",
        platform_label="小红书",
        aspect_ratio="3:4",
        width=1080,
        height=1440,
        fps=30,
        video_bitrate="6000k",
        audio_bitrate="128k",
        crf=22,
        preset="medium",
        safe_area={"top": 144, "right": 72, "bottom": 220, "left": 72},
        caption={
            "placement": "bottom",
            "anchor": "center",
            "max_width_ratio": 0.84,
            "font_family": "Noto Sans CJK SC",
            "font_size": 50,
            "line_height": 1.18,
            "margin_bottom": 180,
            "ffmpeg_font_size": 34,
            "ffmpeg_margin_v": 84,
            "safe_area": "xiaohongshu_portrait_bottom",
        },
        cover={
            "width": 1080,
            "height": 1440,
            "safe_area": {"top": 160, "right": 90, "bottom": 260, "left": 90},
            "title_zone": "upper_middle",
        },
        export={"container": "mp4", "video_codec": "h264", "audio_codec": "aac"},
        aliases=["xiaohongshu", "小红书", "rednote", "little_red_book", "xhs"],
        notes=["偏封面和图文笔记友好，适合种草口播和产品安利。"],
    ),
    RenderProfile(
        profile_key="bilibili_landscape",
        display_name="B站横屏",
        platform_key="bilibili",
        platform_label="B站",
        aspect_ratio="16:9",
        width=1920,
        height=1080,
        fps=30,
        video_bitrate="8000k",
        audio_bitrate="192k",
        crf=21,
        preset="medium",
        safe_area={"top": 72, "right": 96, "bottom": 120, "left": 96},
        caption={
            "placement": "bottom",
            "anchor": "center",
            "max_width_ratio": 0.76,
            "font_family": "Noto Sans CJK SC",
            "font_size": 46,
            "line_height": 1.18,
            "margin_bottom": 84,
            "ffmpeg_font_size": 34,
            "ffmpeg_margin_v": 52,
            "safe_area": "bilibili_landscape_bottom",
        },
        cover={
            "width": 1920,
            "height": 1080,
            "safe_area": {"top": 90, "right": 120, "bottom": 140, "left": 120},
            "title_zone": "left_center",
        },
        export={"container": "mp4", "video_codec": "h264", "audio_codec": "aac"},
        aliases=["bilibili", "b站", "哔哩哔哩", "bili"],
        notes=["横屏内容规格，适合教程、解释视频和较长口播。"],
    ),
    RenderProfile(
        profile_key="wechat_channels_vertical",
        display_name="视频号竖屏",
        platform_key="wechat_channels",
        platform_label="视频号",
        aspect_ratio="9:16",
        width=1080,
        height=1920,
        fps=30,
        video_bitrate="6000k",
        audio_bitrate="128k",
        crf=22,
        preset="medium",
        safe_area={"top": 170, "right": 72, "bottom": 280, "left": 72},
        caption={
            "placement": "bottom",
            "anchor": "center",
            "max_width_ratio": 0.86,
            "font_family": "Noto Sans CJK SC",
            "font_size": 54,
            "line_height": 1.16,
            "margin_bottom": 240,
            "ffmpeg_font_size": 38,
            "ffmpeg_margin_v": 104,
            "safe_area": "wechat_channels_bottom",
        },
        cover={
            "width": 1080,
            "height": 1920,
            "safe_area": {"top": 220, "right": 90, "bottom": 320, "left": 90},
            "title_zone": "center",
        },
        export={"container": "mp4", "video_codec": "h264", "audio_codec": "aac"},
        aliases=["wechat_channels", "视频号", "微信视频号", "video_account"],
        notes=["竖屏社交流分发规格，底部保留更高操作区。"],
    ),
    RenderProfile(
        profile_key="youtube_shorts_vertical",
        display_name="YouTube Shorts",
        platform_key="youtube_shorts",
        platform_label="YouTube Shorts",
        aspect_ratio="9:16",
        width=1080,
        height=1920,
        fps=30,
        video_bitrate="8000k",
        audio_bitrate="192k",
        crf=21,
        preset="medium",
        safe_area={"top": 160, "right": 72, "bottom": 300, "left": 72},
        caption={
            "placement": "bottom",
            "anchor": "center",
            "max_width_ratio": 0.84,
            "font_family": "Noto Sans CJK SC",
            "font_size": 54,
            "line_height": 1.16,
            "margin_bottom": 260,
            "ffmpeg_font_size": 38,
            "ffmpeg_margin_v": 112,
            "safe_area": "youtube_shorts_bottom",
        },
        cover={
            "width": 1080,
            "height": 1920,
            "safe_area": {"top": 200, "right": 90, "bottom": 340, "left": 90},
            "title_zone": "center",
        },
        export={"container": "mp4", "video_codec": "h264", "audio_codec": "aac"},
        aliases=["youtube_shorts", "shorts", "youtube", "yt_shorts"],
        notes=["国际竖屏分发规格，保留底部交互安全区。"],
    ),
    RenderProfile(
        profile_key="default_vertical",
        display_name="默认竖屏",
        platform_key="generic_vertical",
        platform_label="通用竖屏",
        aspect_ratio="9:16",
        width=1080,
        height=1920,
        fps=30,
        video_bitrate="6000k",
        audio_bitrate="128k",
        crf=22,
        preset="medium",
        safe_area={"top": 160, "right": 72, "bottom": 240, "left": 72},
        caption={
            "placement": "bottom",
            "anchor": "center",
            "max_width_ratio": 0.86,
            "font_family": "Noto Sans CJK SC",
            "font_size": 54,
            "line_height": 1.16,
            "margin_bottom": 210,
            "ffmpeg_font_size": 38,
            "ffmpeg_margin_v": 96,
            "safe_area": "generic_vertical_bottom",
        },
        cover={
            "width": 1080,
            "height": 1920,
            "safe_area": {"top": 200, "right": 90, "bottom": 300, "left": 90},
            "title_zone": "center",
        },
        export={"container": "mp4", "video_codec": "h264", "audio_codec": "aac"},
        aliases=["vertical", "竖屏", "9:16"],
        notes=["当目标平台未知但需要竖屏时使用。"],
    ),
    RenderProfile(
        profile_key="default_landscape",
        display_name="默认横屏",
        platform_key="generic_landscape",
        platform_label="通用横屏",
        aspect_ratio="16:9",
        width=1920,
        height=1080,
        fps=30,
        video_bitrate="8000k",
        audio_bitrate="192k",
        crf=21,
        preset="medium",
        safe_area={"top": 72, "right": 96, "bottom": 120, "left": 96},
        caption={
            "placement": "bottom",
            "anchor": "center",
            "max_width_ratio": 0.76,
            "font_family": "Noto Sans CJK SC",
            "font_size": 46,
            "line_height": 1.18,
            "margin_bottom": 84,
            "ffmpeg_font_size": 34,
            "ffmpeg_margin_v": 52,
            "safe_area": "generic_landscape_bottom",
        },
        cover={
            "width": 1920,
            "height": 1080,
            "safe_area": {"top": 90, "right": 120, "bottom": 140, "left": 120},
            "title_zone": "center",
        },
        export={"container": "mp4", "video_codec": "h264", "audio_codec": "aac"},
        aliases=["landscape", "横屏", "16:9"],
        notes=["当目标平台未知但需要横屏时使用。"],
    ),
)

_PROFILE_BY_KEY = {profile.profile_key: profile for profile in _BUILTIN_PROFILES}
_ALIAS_TO_KEY = {
    _normalize(alias): profile.profile_key
    for profile in _BUILTIN_PROFILES
    for alias in [profile.profile_key, profile.platform_key, profile.display_name, *profile.aliases]
}


def list_render_profiles() -> list[dict[str, Any]]:
    return [profile.model_dump() for profile in _BUILTIN_PROFILES]


def get_render_profile(profile_key: str) -> dict[str, Any] | None:
    key = _ALIAS_TO_KEY.get(_normalize(profile_key), profile_key)
    profile = _PROFILE_BY_KEY.get(key)
    return profile.model_dump() if profile is not None else None


def resolve_render_profile(
    *,
    target_platform: str | None = None,
    aspect_ratio: str | None = None,
    style_snapshot: dict[str, Any] | None = None,
    render_profile_key: str | None = None,
    overrides: dict[str, Any] | None = None,
) -> dict[str, Any]:
    style_preset = (style_snapshot or {}).get("style_preset") or {}
    render_defaults = style_preset.get("render_defaults") or {}
    requested_key = (
        render_profile_key
        or render_defaults.get("render_profile_key")
        or target_platform
        or _fallback_profile_key(aspect_ratio or style_preset.get("aspect_ratio"))
    )
    profile = get_render_profile(str(requested_key)) or get_render_profile(DEFAULT_RENDER_PROFILE_KEY)
    assert profile is not None
    merged_overrides = _deep_merge(
        render_defaults.get("render_profile_overrides") or {},
        overrides or {},
    )
    if merged_overrides:
        profile = _deep_merge(profile, merged_overrides)
    return profile


def render_settings_from_profile(
    profile: dict[str, Any],
    explicit_settings: dict[str, Any] | None = None,
) -> dict[str, Any]:
    settings = {
        "runtime": "ffmpeg",
        "width": profile["width"],
        "height": profile["height"],
        "fps": profile["fps"],
        "crf": profile["crf"],
        "preset": profile["preset"],
        "bgm_volume": 0.18,
        "video_bitrate": profile.get("video_bitrate"),
        "audio_bitrate": profile.get("audio_bitrate") or "128k",
        "aspect_ratio": profile.get("aspect_ratio"),
        "safe_area": profile.get("safe_area") or {},
        "caption": profile.get("caption") or {},
        "cover": profile.get("cover") or {},
        "render_profile_key": profile.get("profile_key"),
        "render_profile": profile,
    }
    for key, value in (explicit_settings or {}).items():
        if key == "render_profile_overrides":
            continue
        if value is not None:
            settings[key] = value
    return settings


def _fallback_profile_key(aspect_ratio: str | None) -> str:
    normalized = _normalize(aspect_ratio or "")
    if normalized in {"16:9", "16_9", "landscape", "横屏"}:
        return "default_landscape"
    return DEFAULT_RENDER_PROFILE_KEY


def _deep_merge(base: dict[str, Any], overlay: dict[str, Any]) -> dict[str, Any]:
    result = deepcopy(base)
    for key, value in overlay.items():
        if isinstance(value, dict) and isinstance(result.get(key), dict):
            result[key] = _deep_merge(result[key], value)
        else:
            result[key] = deepcopy(value)
    return result
