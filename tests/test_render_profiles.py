from packages.render_profile_kit import (
    get_render_profile,
    list_render_profiles,
    render_settings_from_profile,
    resolve_render_profile,
)


def test_render_profile_aliases_resolve_to_platform_specs() -> None:
    douyin = get_render_profile("抖音")
    bilibili = get_render_profile("b站")

    assert douyin is not None
    assert douyin["profile_key"] == "douyin_vertical"
    assert douyin["width"] == 1080
    assert douyin["height"] == 1920
    assert douyin["safe_area"]["bottom"] > douyin["safe_area"]["top"]
    assert bilibili is not None
    assert bilibili["profile_key"] == "bilibili_landscape"
    assert bilibili["aspect_ratio"] == "16:9"
    assert bilibili["width"] == 1920
    assert {profile["platform_key"] for profile in list_render_profiles()} >= {
        "douyin",
        "xiaohongshu",
        "bilibili",
        "wechat_channels",
        "youtube_shorts",
    }


def test_render_profile_resolution_uses_platform_before_default_aspect_ratio() -> None:
    profile = resolve_render_profile(target_platform="xiaohongshu", aspect_ratio="9:16")
    settings = render_settings_from_profile(profile, {"width": 720, "fps": 24})

    assert profile["profile_key"] == "xiaohongshu_portrait"
    assert profile["aspect_ratio"] == "3:4"
    assert settings["width"] == 720
    assert settings["height"] == 1440
    assert settings["fps"] == 24
    assert settings["video_bitrate"] == "6000k"


def test_render_profile_overrides_are_deep_merged() -> None:
    profile = resolve_render_profile(
        target_platform="youtube_shorts",
        overrides={
            "video_bitrate": "10000k",
            "caption": {"placement": "top", "ffmpeg_margin_v": 72},
        },
    )
    settings = render_settings_from_profile(profile)

    assert settings["render_profile_key"] == "youtube_shorts_vertical"
    assert settings["video_bitrate"] == "10000k"
    assert settings["caption"]["placement"] == "top"
    assert settings["caption"]["font_family"] == "Noto Sans CJK SC"
    assert settings["caption"]["ffmpeg_margin_v"] == 72
