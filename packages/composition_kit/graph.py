from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from packages.render_profile_kit import render_settings_from_profile, resolve_render_profile

DEFAULT_RENDER_SETTINGS = {
    "runtime": "ffmpeg",
    "width": 1080,
    "height": 1920,
    "fps": 30,
    "crf": 22,
    "preset": "medium",
    "bgm_volume": 0.18,
}


def build_composition_graph(
    *,
    run: dict[str, Any],
    current_artifacts: dict[str, Any] | None = None,
    assets: list[dict[str, Any]] | None = None,
    render_settings: dict[str, Any] | None = None,
    selected_asset_ids: dict[str, str | None] | None = None,
    source_artifacts: dict[str, str | None] | None = None,
) -> dict[str, Any]:
    """Normalize production artifacts and assets into a renderer-neutral graph."""

    artifacts = current_artifacts or {}
    asset_list = assets or []
    run_input = run.get("input_payload") or {}
    style_snapshot = run_input.get("style_preset_snapshot") or {}
    profile = resolve_render_profile(
        target_platform=run_input.get("target_platform"),
        aspect_ratio=run_input.get("aspect_ratio"),
        style_snapshot=style_snapshot,
        render_profile_key=(render_settings or {}).get("render_profile_key"),
        overrides=(render_settings or {}).get("render_profile_overrides"),
    )
    settings = render_settings_from_profile(profile, render_settings or {})
    selected_assets = _select_assets(asset_list, selected_asset_ids or {})
    duration_seconds = _duration_seconds(
        run_input=run_input,
        artifacts=artifacts,
        selected_assets=selected_assets,
    )
    canvas = _canvas(run_input=run_input, artifacts=artifacts, settings=settings)
    captions = _captions(
        artifacts=artifacts,
        duration_seconds=duration_seconds,
        profile_caption=settings.get("caption") or {},
    )
    tracks = _tracks(
        selected_assets=selected_assets,
        captions=captions,
        duration_seconds=duration_seconds,
        settings=settings,
    )
    layers = _layers(tracks)
    missing_inputs = _missing_inputs(selected_assets)
    return {
        "schema_version": "1.0",
        "generated_at": datetime.now(UTC).isoformat(),
        "source": {
            "run_id": run.get("run_id"),
            "pipeline_key": run.get("pipeline_key"),
            "artifact_ids": source_artifacts or {},
            "asset_ids": {
                role: asset.get("asset_id")
                for role, asset in selected_assets.items()
                if asset is not None
            },
        },
        "render_profile": profile,
        "canvas": canvas,
        "duration_seconds": duration_seconds,
        "timeline": {
            "timebase": "seconds",
            "duration_seconds": duration_seconds,
            "track_order": [track["track_id"] for track in tracks],
            "markers": _markers(artifacts=artifacts),
        },
        "tracks": tracks,
        "layers": layers,
        "captions": captions,
        "transitions": _transitions(duration_seconds=duration_seconds),
        "effects": _effects(canvas=canvas, selected_assets=selected_assets),
        "audio_mix": _audio_mix(selected_assets=selected_assets, settings=settings),
        "render_targets": _render_targets(settings=settings),
        "validation": {
            "ready_for_render": not missing_inputs,
            "missing_inputs": missing_inputs,
            "warnings": _warnings(selected_assets=selected_assets, captions=captions),
        },
    }


def _select_assets(
    assets: list[dict[str, Any]],
    selected_asset_ids: dict[str, str | None],
) -> dict[str, dict[str, Any] | None]:
    return {
        "primary_video": _select_asset(
            assets,
            explicit_asset_id=selected_asset_ids.get("avatar_video_asset_id")
            or selected_asset_ids.get("primary_video_asset_id"),
            asset_types=["avatar_video_clip", "source_video", "rendered_video"],
        ),
        "narration_audio": _select_asset(
            assets,
            explicit_asset_id=selected_asset_ids.get("narration_audio_asset_id"),
            asset_types=["narration_audio", "tts_audio"],
        ),
        "subtitles": _select_asset(
            assets,
            explicit_asset_id=selected_asset_ids.get("subtitle_asset_id"),
            asset_types=["subtitle_file"],
        ),
        "background_music": _select_asset(
            assets,
            explicit_asset_id=selected_asset_ids.get("music_asset_id"),
            asset_types=["background_music"],
        ),
    }


def _select_asset(
    assets: list[dict[str, Any]],
    *,
    explicit_asset_id: str | None,
    asset_types: list[str],
) -> dict[str, Any] | None:
    if explicit_asset_id:
        return next((asset for asset in assets if asset.get("asset_id") == explicit_asset_id), None)
    matches = [asset for asset in assets if asset.get("asset_type") in asset_types]
    return matches[-1] if matches else None


def _duration_seconds(
    *,
    run_input: dict[str, Any],
    artifacts: dict[str, Any],
    selected_assets: dict[str, dict[str, Any] | None],
) -> float:
    spoken_script = artifacts.get("spoken_script") or {}
    if spoken_script.get("total_duration_seconds"):
        return float(spoken_script["total_duration_seconds"])
    shot_list = artifacts.get("shot_list") or {}
    shot_duration = sum(
        float(shot.get("duration_seconds") or 0)
        for shot in shot_list.get("shots", [])
        if isinstance(shot, dict)
    )
    if shot_duration > 0:
        return round(shot_duration, 3)
    scene_plan = artifacts.get("screening_room_scene_plan") or {}
    scene_duration = scene_plan.get("target_duration_seconds")
    if scene_duration:
        return float(scene_duration)
    primary_video = selected_assets.get("primary_video")
    if primary_video and primary_video.get("duration_ms"):
        return round(float(primary_video["duration_ms"]) / 1000, 3)
    return float(run_input.get("target_duration_seconds") or 15)


def _canvas(
    *,
    run_input: dict[str, Any],
    artifacts: dict[str, Any],
    settings: dict[str, Any],
) -> dict[str, Any]:
    style_snapshot = run_input.get("style_preset_snapshot") or {}
    style_preset = style_snapshot.get("style_preset") or {}
    brand_kit = style_snapshot.get("brand_kit") or {}
    safe_area = brand_kit.get("safe_area") or settings.get("safe_area") or {}
    if not safe_area:
        safe_area = {"top": 96, "right": 64, "bottom": 160, "left": 64}
    return {
        "width": int(settings["width"]),
        "height": int(settings["height"]),
        "fps": int(settings["fps"]),
        "aspect_ratio": settings.get("aspect_ratio")
        or style_preset.get("aspect_ratio")
        or run_input.get("aspect_ratio")
        or _aspect_ratio(int(settings["width"]), int(settings["height"])),
        "background": {
            "type": "solid",
            "color": _background_color(brand_kit),
        },
        "safe_area": safe_area,
        "cover": settings.get("cover") or {},
        "style": {
            "style_key": run_input.get("style_key") or style_preset.get("style_key"),
            "visual_style": style_preset.get("visual_style") or {},
            "caption_style": _caption_style(
                artifacts=artifacts,
                style_preset=style_preset,
                profile_caption=settings.get("caption") or {},
            ),
            "brand_colors": brand_kit.get("color_palette") or {},
            "typography": brand_kit.get("typography") or {},
        },
    }


def _captions(
    *,
    artifacts: dict[str, Any],
    duration_seconds: float,
    profile_caption: dict[str, Any],
) -> dict[str, Any]:
    spoken_script = artifacts.get("spoken_script") or {}
    scene_plan = artifacts.get("screening_room_scene_plan") or {}
    segments = []
    if spoken_script.get("segments"):
        segments = [
            {
                "caption_id": f"caption_{index:03d}",
                "source_segment_id": segment.get("segment_id"),
                "start_seconds": float(segment.get("start_seconds") or 0),
                "end_seconds": float(segment.get("end_seconds") or duration_seconds),
                "text": segment.get("caption") or segment.get("text") or "",
            }
            for index, segment in enumerate(spoken_script["segments"], start=1)
        ]
    elif scene_plan.get("shots"):
        segments = [
            {
                "caption_id": f"caption_{index:03d}",
                "source_segment_id": shot.get("shot_id"),
                "start_seconds": float(shot.get("start_seconds") or 0),
                "end_seconds": float(shot.get("end_seconds") or duration_seconds),
                "text": shot.get("caption") or shot.get("dialogue") or "",
            }
            for index, shot in enumerate(scene_plan["shots"], start=1)
            if shot.get("caption") or shot.get("dialogue")
        ]
    return {
        "source": "spoken_script"
        if spoken_script.get("segments")
        else "screening_room_scene_plan"
        if scene_plan.get("shots")
        else "none",
        "style": _caption_style(
            artifacts=artifacts,
            style_preset={},
            profile_caption=profile_caption,
        ),
        "segments": segments,
    }


def _tracks(
    *,
    selected_assets: dict[str, dict[str, Any] | None],
    captions: dict[str, Any],
    duration_seconds: float,
    settings: dict[str, Any],
) -> list[dict[str, Any]]:
    tracks = []
    primary_video = selected_assets.get("primary_video")
    if primary_video:
        tracks.append(
            {
                "track_id": "track_video_primary",
                "type": "video",
                "role": "primary_picture",
                "layer_id": "layer_video_primary",
                "clips": [
                    _asset_clip(
                        clip_id="clip_video_primary_001",
                        asset=primary_video,
                        start_seconds=0,
                        duration_seconds=duration_seconds,
                        role="primary_picture",
                        transform={
                            "fit": "contain",
                            "align": "center",
                            "pad_to_canvas": True,
                        },
                    )
                ],
            }
        )
    if captions.get("segments"):
        tracks.append(
            {
                "track_id": "track_caption_main",
                "type": "caption",
                "role": "burn_or_soft_subtitles",
                "layer_id": "layer_caption_main",
                "clips": [
                    {
                        "clip_id": segment["caption_id"],
                        "type": "caption",
                        "start_seconds": segment["start_seconds"],
                        "duration_seconds": max(
                            0.001,
                            segment["end_seconds"] - segment["start_seconds"],
                        ),
                        "text": segment["text"],
                        "source_segment_id": segment.get("source_segment_id"),
                    }
                    for segment in captions["segments"]
                ],
            }
        )
    narration = selected_assets.get("narration_audio")
    if narration:
        tracks.append(
            {
                "track_id": "track_audio_narration",
                "type": "audio",
                "role": "dialogue",
                "clips": [
                    _asset_clip(
                        clip_id="clip_audio_narration_001",
                        asset=narration,
                        start_seconds=0,
                        duration_seconds=duration_seconds,
                        role="dialogue",
                    )
                ],
            }
        )
    music = selected_assets.get("background_music")
    if music:
        tracks.append(
            {
                "track_id": "track_audio_bgm",
                "type": "audio",
                "role": "background_music",
                "clips": [
                    {
                        **_asset_clip(
                            clip_id="clip_audio_bgm_001",
                            asset=music,
                            start_seconds=0,
                            duration_seconds=duration_seconds,
                            role="background_music",
                        ),
                        "loop": True,
                        "volume": float(settings.get("bgm_volume", 0.18)),
                    }
                ],
            }
        )
    return tracks


def _asset_clip(
    *,
    clip_id: str,
    asset: dict[str, Any],
    start_seconds: float,
    duration_seconds: float,
    role: str,
    transform: dict[str, Any] | None = None,
) -> dict[str, Any]:
    clip = {
        "clip_id": clip_id,
        "type": asset.get("asset_type"),
        "role": role,
        "asset_id": asset.get("asset_id"),
        "provider": asset.get("provider"),
        "tool_key": asset.get("tool_key"),
        "start_seconds": start_seconds,
        "duration_seconds": duration_seconds,
        "source": {
            "storage_url": asset.get("storage_url"),
            "mime_type": asset.get("mime_type"),
            "duration_ms": asset.get("duration_ms"),
            "width": asset.get("width"),
            "height": asset.get("height"),
        },
    }
    if transform:
        clip["transform"] = transform
    return clip


def _layers(tracks: list[dict[str, Any]]) -> list[dict[str, Any]]:
    base_layers = [
        {
            "layer_id": "layer_background",
            "type": "background",
            "z_index": 0,
            "blend_mode": "normal",
        }
    ]
    z_index = 10
    for track in tracks:
        if track.get("type") == "audio":
            continue
        base_layers.append(
            {
                "layer_id": track.get("layer_id"),
                "type": track.get("type"),
                "track_id": track.get("track_id"),
                "z_index": z_index,
                "blend_mode": "normal",
            }
        )
        z_index += 10
    return base_layers


def _markers(*, artifacts: dict[str, Any]) -> list[dict[str, Any]]:
    markers = []
    for shot in (artifacts.get("shot_list") or {}).get("shots", []):
        if not isinstance(shot, dict):
            continue
        markers.append(
            {
                "marker_id": f"marker_{shot.get('shot_id')}",
                "type": "shot_boundary",
                "start_seconds": float(shot.get("start_seconds") or 0),
                "duration_seconds": float(shot.get("duration_seconds") or 0),
                "label": shot.get("shot_id"),
            }
        )
    return markers


def _transitions(*, duration_seconds: float) -> list[dict[str, Any]]:
    if duration_seconds <= 1:
        return []
    return [
        {
            "transition_id": "transition_intro_fade",
            "type": "fade",
            "target": "track_video_primary",
            "start_seconds": 0,
            "duration_seconds": min(0.35, duration_seconds / 6),
        },
        {
            "transition_id": "transition_outro_fade",
            "type": "fade",
            "target": "track_video_primary",
            "start_seconds": max(0, duration_seconds - 0.35),
            "duration_seconds": min(0.35, duration_seconds / 6),
        },
    ]


def _effects(
    *,
    canvas: dict[str, Any],
    selected_assets: dict[str, dict[str, Any] | None],
) -> list[dict[str, Any]]:
    effects = [
        {
            "effect_id": "effect_fit_primary_video",
            "type": "fit_canvas",
            "target": "track_video_primary",
            "params": {
                "width": canvas["width"],
                "height": canvas["height"],
                "fit": "contain",
                "pad_color": canvas["background"]["color"],
            },
        }
    ]
    if selected_assets.get("subtitles") is not None:
        effects.append(
            {
                "effect_id": "effect_subtitle_render",
                "type": "subtitle_render",
                "target": "track_caption_main",
                "params": canvas["style"].get("caption_style") or {},
            }
        )
    return effects


def _audio_mix(
    *,
    selected_assets: dict[str, dict[str, Any] | None],
    settings: dict[str, Any],
) -> dict[str, Any]:
    tracks = []
    if selected_assets.get("narration_audio") is not None or selected_assets.get("primary_video"):
        tracks.append(
            {
                "track_id": "track_audio_narration",
                "role": "dialogue",
                "volume": 1.0,
                "priority": 100,
            }
        )
    if selected_assets.get("background_music") is not None:
        tracks.append(
            {
                "track_id": "track_audio_bgm",
                "role": "background_music",
                "volume": float(settings.get("bgm_volume", 0.18)),
                "priority": 20,
                "ducking": {
                    "enabled": True,
                    "sidechain_track_id": "track_audio_narration",
                    "attenuation_db": -8,
                },
            }
        )
    return {
        "sample_rate": 44100,
        "strategy": "amix_duration_first",
        "tracks": tracks,
    }


def _render_targets(*, settings: dict[str, Any]) -> list[dict[str, Any]]:
    return [
        {
            "runtime": "ffmpeg",
            "renderer": "ffmpeg_compose",
            "status": "available",
            "settings": {
                "width": int(settings["width"]),
                "height": int(settings["height"]),
                "fps": int(settings["fps"]),
                "crf": int(settings["crf"]),
                "preset": settings["preset"],
                "video_bitrate": settings.get("video_bitrate"),
                "audio_bitrate": settings.get("audio_bitrate"),
                "bgm_volume": float(settings["bgm_volume"]),
                "render_profile_key": settings.get("render_profile_key"),
                "safe_area": settings.get("safe_area") or {},
                "caption": settings.get("caption") or {},
                "cover": settings.get("cover") or {},
            },
        },
        {
            "runtime": "html_canvas",
            "renderer": "script2video_canvas_renderer",
            "status": "planned",
            "settings": {
                "width": int(settings["width"]),
                "height": int(settings["height"]),
                "fps": int(settings["fps"]),
                "render_profile_key": settings.get("render_profile_key"),
                "safe_area": settings.get("safe_area") or {},
                "caption": settings.get("caption") or {},
                "cover": settings.get("cover") or {},
            },
        },
    ]


def _missing_inputs(selected_assets: dict[str, dict[str, Any] | None]) -> list[dict[str, Any]]:
    if selected_assets.get("primary_video") is not None:
        return []
    return [
        {
            "input": "primary_video",
            "accepted_asset_types": ["avatar_video_clip", "source_video"],
            "severity": "error",
            "message": "A primary video asset is required before rendering.",
        }
    ]


def _warnings(
    *,
    selected_assets: dict[str, dict[str, Any] | None],
    captions: dict[str, Any],
) -> list[dict[str, Any]]:
    warnings = []
    if selected_assets.get("subtitles") is None and not captions.get("segments"):
        warnings.append(
            {
                "code": "caption_source_missing",
                "message": "No subtitle asset or caption segments are available.",
                "severity": "warning",
            }
        )
    if selected_assets.get("background_music") is None:
        warnings.append(
            {
                "code": "background_music_missing",
                "message": "No background music asset is available.",
                "severity": "info",
            }
        )
    return warnings


def _caption_style(
    *,
    artifacts: dict[str, Any],
    style_preset: dict[str, Any],
    profile_caption: dict[str, Any] | None = None,
) -> dict[str, Any]:
    spoken_script = artifacts.get("spoken_script") or {}
    return (
        style_preset.get("subtitle_style")
        or spoken_script.get("caption_style")
        or profile_caption
        or {
            "placement": "bottom",
            "font_family": "Noto Sans CJK SC",
            "font_size": 56,
            "safe_area": "vertical_short_video_bottom",
        }
    )


def _background_color(brand_kit: dict[str, Any]) -> str:
    palette = brand_kit.get("color_palette") or {}
    return palette.get("background") or palette.get("primary") or "#000000"


def _aspect_ratio(width: int, height: int) -> str:
    if width == 0 or height == 0:
        return "unknown"
    if height >= width:
        return "9:16" if round(width / height, 2) == 0.56 else f"{width}:{height}"
    return "16:9" if round(width / height, 2) == 1.78 else f"{width}:{height}"
