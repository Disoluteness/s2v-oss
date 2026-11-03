from packages.script_screening_kit import ScriptScreeningRoomParser

SAMPLE_STORYBOARD = """
【茶小朵】抖音 · 15s · 口播风格分镜脚本
主题：秋天第一杯奶茶
钩子：秋天第一杯奶茶，还没人给你点？
概述：女生宿舍口播安利茶小朵冲泡奶茶，热水一冲即享秋日香浓。

镜头 01｜3s｜近景｜节奏快
画面：女生穿着针织开衫坐在宿舍桌前，对着镜头挑眉，手里举着空手做要奶茶状
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


def test_script_screening_room_parser_builds_structured_plan() -> None:
    result = ScriptScreeningRoomParser().parse(raw_script=SAMPLE_STORYBOARD)

    scene_plan = result["scene_plan"]
    spoken_script = result["spoken_script"]
    shot_list = result["shot_list"]

    assert scene_plan["title"] == "茶小朵"
    assert scene_plan["theme"] == "秋天第一杯奶茶"
    assert scene_plan["target_platform"] == "douyin"
    assert scene_plan["target_duration_seconds"] == 15
    assert len(scene_plan["shots"]) == 4
    assert scene_plan["shots"][0]["shot_id"] == "shot_01"
    assert scene_plan["shots"][1]["start_seconds"] == 3
    assert scene_plan["shots"][3]["end_seconds"] == 15
    assert "自己冲就好" in scene_plan["shots"][1]["dialogue"]
    assert len(spoken_script["segments"]) == 4
    assert spoken_script["segments"][0]["source_shot_id"] == "shot_01"
    assert len(shot_list["shots"]) == 4
    assert shot_list["shots"][2]["spoken_segment_ids"] == ["seg_03"]
    assert shot_list["music_plan"]["style_notes"][0] == "轻快俏皮卡点音效起"
