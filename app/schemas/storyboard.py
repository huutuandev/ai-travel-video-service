from typing import Literal

from pydantic import ConfigDict, Field

from app.schemas.video import Schema


class DirectorScene(Schema):
    model_config = ConfigDict(strict=True)

    scene_id: int = Field(ge=1)
    start_seconds: int = Field(ge=0)
    duration_seconds: int = Field(gt=0)
    type: Literal["AI_VIDEO", "LOCAL_IMAGE", "LOCAL_VIDEO"]
    asset_index: int = Field(ge=0)
    video_prompt: str | None = Field(max_length=640)
    narration: str = Field(min_length=1, max_length=240)
    caption: str = Field(min_length=1, max_length=100)
    transition: Literal["cut", "crossfade"]


class DirectorStoryboard(Schema):
    model_config = ConfigDict(strict=True)

    campaign_title: str = Field(min_length=1, max_length=120)
    creative_concept: str = Field(min_length=1, max_length=240)
    duration_seconds: Literal[30, 35, 40]
    aspect_ratio: Literal["16:9", "9:16"]
    language: Literal["vi-VN"]
    scenes: list[DirectorScene] = Field(min_length=1, max_length=30)


def asset_options(request):
    options = [(ref, "LOCAL_IMAGE") for ref in request.images]
    options.extend((ref, "LOCAL_VIDEO") for ref in request.videos)
    for ref in request.ai_reference_images or ():
        if (ref, "LOCAL_IMAGE") not in options:
            options.append((ref, "AI_REFERENCE"))
    return options

