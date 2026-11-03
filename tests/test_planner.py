from packages.agent_runner.planner import SpokespersonAdPlanner

SCRIPT = """【茶小朵】抖音 · 15s · 口播风格分镜脚本
镜头 01｜3s｜近景｜节奏快
画面：女生穿着针织开衫坐在宿舍桌前，对着镜头挑眉
台词：秋天第一杯奶茶，还没人给你点？
字幕：秋天第一杯奶茶 还没人给你点？
配乐：轻快俏皮卡点音效起

镜头 02｜4s｜特写｜节奏中
画面：撕开茶小朵奶茶包装，将奶茶粉倒入杯中，热水注入瞬间热气升腾
台词：别等啦！自己冲就好！热水一倒，满屋子都是香浓的味儿～
字幕：自己冲就好！热水一倒 满屋香浓
配乐：咕噜噜倒水音效+温馨吉他BGM

镜头 03｜5s｜中景｜节奏中
画面：女生双手捧着冲好的茶小朵奶茶，满足地喝一大口，嘴角带笑
台词：0反式脂肪酸，喝着没负担。追剧自习来一杯，暖到心里去！
字幕：0反式脂肪酸 追剧自习 暖到心里
配乐：温馨吉他BGM继续

镜头 04｜3s｜近景｜节奏快
画面：女生把另一杯冲好的奶茶递向镜头，像递给屏幕前的朋友
台词：快去囤！你的秋天第一杯，我请了！
字幕：快去囤！你的秋天第一杯 我请了！
配乐：叮~提示音+BGM渐弱收尾
"""


def planner_context() -> dict:
    intake = {
        "version": "1.0",
        "language": "zh-CN",
        "brand_name": "茶小朵",
        "product_name": "冲泡奶茶",
        "raw_script": SCRIPT,
        "target_platform": "douyin",
        "aspect_ratio": "9:16",
        "target_duration_seconds": 15,
    }
    return {
        "run_id": "run_test",
        "pipeline_key": "spokesperson_ad",
        "current_artifacts": {"intake_brief": intake},
        "available_tools": [
            {
                "tool_key": "dashscope_avatar_video",
                "capability": "avatar_video",
                "provider": "dashscope",
                "status": "available",
                "fallback_tool_keys": ["local_asset_upload"],
            }
        ],
        "user_preferences": intake,
        "cost_policy": {},
    }


def test_planner_generates_production_plan_from_chinese_storyboard() -> None:
    planner = SpokespersonAdPlanner()

    result = planner.create_production_plan(planner_context())

    assert result.artifact_type == "production_plan"
    assert result.payload["delivery"]["duration_seconds"] == 15
    assert result.payload["creative_strategy"]["hook"] == "秋天第一杯奶茶，还没人给你点？"
    assert result.payload["tool_plan"][0]["preferred_tool_key"] == "dashscope_avatar_video"
    assert result.payload["source_scene_count"] == 4


def test_planner_generates_spoken_script_and_shot_list() -> None:
    planner = SpokespersonAdPlanner()
    context = planner_context()
    spoken = planner.create_spoken_script(context)
    context["current_artifacts"]["spoken_script"] = spoken.payload

    shot_list = planner.create_shot_list(context)

    assert spoken.payload["total_duration_seconds"] == 15
    assert [segment["duration_seconds"] for segment in spoken.payload["segments"]] == [3, 4, 5, 3]
    assert "0反式脂肪酸" in spoken.payload["full_text"]
    assert len(shot_list.payload["shots"]) == 4
    assert shot_list.payload["shots"][1]["framing"] == "特写"
    assert shot_list.payload["asset_requirements"][0]["provider_capability"] == "avatar_video"
    assert shot_list.payload["music_plan"]["need_bgm"] is True
