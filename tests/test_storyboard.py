from decimal import Decimal
from pathlib import Path

import pytest
from pydantic import ValidationError

from app.core.exceptions import MediaError
from app.models.video import Mode
from app.schemas.storyboard import DirectorStoryboard, asset_options
from app.schemas.video import GenerateVideo
from app.services.ai_director import validate_direction
from app.services.storyboard import FPS, GeneratedClip, directed_shots, narration_timeline


def plan_payload(*, ai=True):
    return {
        "campaignTitle": "Khám phá Đà Nẵng", "creativeConcept": "Chuyến đi đáng nhớ",
        "durationSeconds": 30, "aspectRatio": "16:9", "language": "vi-VN",
        "scenes": [
            {
                "sceneId": 1, "startSeconds": 0, "durationSeconds": 5,
                "assetIndex": 0, "type": "AI_VIDEO" if ai else "LOCAL_IMAGE",
                "videoPrompt": "Slow camera glide." if ai else None,
                "narration": "Cùng khám phá Đà Nẵng nhé!", "caption": "Đà Nẵng",
                "transition": "cut",
            },
            {
                "sceneId": 2, "startSeconds": 5, "durationSeconds": 25,
                "assetIndex": 0, "type": "LOCAL_IMAGE", "videoPrompt": None,
                "narration": "Liên hệ để đặt tour ngay.", "caption": "Đặt tour ngay",
                "transition": "cut" if ai else "crossfade",
            },
        ],
    }


def test_valid_storyboard_drives_shots_and_vietnamese_narration(payload):
    payload["config"]["mode"] = "HYBRID"
    request = GenerateVideo.model_validate(payload)
    options = asset_options(request)
    plan = DirectorStoryboard.model_validate(plan_payload())
    context = {
        "durationSeconds": 30, "aspectRatio": "16:9", "mode": Mode.HYBRID,
        "maxScenes": 8, "maxAiScenes": 1, "aiClipSeconds": 5, "tour": {},
        "assets": [{"assetIndex": 0, "type": "LOCAL_IMAGE", "aiEligible": True}],
    }
    validate_direction(plan, context, options)
    clip = GeneratedClip(Path("clip.mp4"), str(options[0][0]), 5, "wavespeed", Decimal(0), "Prompt")
    shots = directed_shots(request, plan, (clip,))
    timeline = narration_timeline(shots, request)
    assert shots[0].generated == clip
    assert shots[-1].closing
    assert timeline["scenes"][0]["narration"] == "Cùng khám phá Đà Nẵng nhé!"
    assert timeline["scenes"][1]["startFrame"] == 5 * FPS
    assert timeline["scenes"][0]["videoPrompt"] == "Prompt"
    assert directed_shots(request, plan, ())[0].image


@pytest.mark.parametrize(("index", "changes"), [
    (0, {"assetIndex": 999}), (1, {"durationSeconds": 24}),
    (1, {"startSeconds": 4}), (1, {"transition": "crossfade"}),
    (1, {"caption": "Chỉ 9.999.000 ₫"}),
    (0, {"videoPrompt": "See https://example.com/source"}),
])
def test_validate_rejects_invalid_business_constraints(payload, index, changes):
    raw = plan_payload()
    raw["scenes"][index].update(changes)
    plan = DirectorStoryboard.model_validate(raw)
    context = {
        "durationSeconds": 30, "aspectRatio": "16:9", "mode": Mode.HYBRID,
        "maxScenes": 8, "maxAiScenes": 1, "aiClipSeconds": 5, "tour": {},
        "assets": [{"assetIndex": 0, "type": "LOCAL_IMAGE", "aiEligible": True}],
    }
    with pytest.raises(MediaError):
        validate_direction(plan, context, asset_options(GenerateVideo.model_validate(payload)))


def test_invalid_json_and_executable_fields_rejected():
    with pytest.raises(ValidationError):
        DirectorStoryboard.model_validate_json("not JSON")
    raw = plan_payload()
    raw["scenes"][0]["shellCommand"] = "ignored"
    with pytest.raises(ValidationError):
        DirectorStoryboard.model_validate(raw)


def test_ai_scene_above_budget_limit_is_rejected(payload):
    plan = DirectorStoryboard.model_validate(plan_payload())
    context = {
        "durationSeconds": 30, "aspectRatio": "16:9", "mode": Mode.HYBRID,
        "maxScenes": 8, "maxAiScenes": 0, "aiClipSeconds": 5, "tour": {},
        "assets": [{"assetIndex": 0, "type": "LOCAL_IMAGE", "aiEligible": True}],
    }
    with pytest.raises(MediaError, match="violated video constraints"):
        validate_direction(plan, context, asset_options(GenerateVideo.model_validate(payload)))

