from datetime import datetime
from decimal import Decimal
from typing import Annotated, Literal
from uuid import UUID

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    HttpUrl,
    StringConstraints,
    model_validator,
)
from pydantic.alias_generators import to_camel

from app.core.media_urls import validate_image_url
from app.models.video import Mode, Status

Identifier = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=128)]


class Schema(BaseModel):
    model_config = ConfigDict(
        alias_generator=to_camel, extra="forbid", from_attributes=True, populate_by_name=True
    )


AssetRef = HttpUrl | UUID
ShortText = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=160)]


class Tour(Schema):
    title: Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=200)]
    description: str | None = Field(default=None, max_length=4000)
    destination: str | None = Field(default=None, max_length=200)
    highlights: list[ShortText] = Field(default_factory=list, max_length=12)
    experience: ShortText | None = None
    benefits: ShortText | None = None
    price_vnd: int | None = Field(default=None, ge=0, le=999_999_999_999, strict=True)
    promotion: ShortText | None = None
    call_to_action: ShortText | None = None
    target_audience: ShortText | None = None


class VideoConfig(Schema):
    mode: Mode
    duration_seconds: int = Field(ge=5, le=180, strict=True)
    aspect_ratio: Literal["16:9", "9:16", "1:1"]


class NarrationConfig(Schema):
    enabled: bool = True
    language: Literal["vi-VN"] = "vi-VN"
    voice: str | None = Field(default=None, min_length=1, max_length=64)
    speed: float | None = Field(default=None, ge=0.9, le=1.15)
    text: str | None = Field(default=None, min_length=1, max_length=1200)
    scene_texts: list[Annotated[str, StringConstraints(max_length=240)]] | None = Field(
        default=None, min_length=1, max_length=30
    )
    subtitles: bool = True
    allow_fallback: bool = False

    @model_validator(mode="after")
    def validate_text_source(self):
        if self.voice is not None:
            self.voice = self.voice.strip()
            if not self.voice:
                raise ValueError("Voice cannot be blank")
        if self.text is not None:
            self.text = self.text.strip()
            if not self.text:
                raise ValueError("Narration text cannot be blank")
        if self.text is not None and self.scene_texts is not None:
            raise ValueError("Provide text or sceneTexts")
        if self.scene_texts is not None and (
            not any(text.strip() for text in self.scene_texts)
            or sum(map(len, self.scene_texts)) > 1200
        ):
            raise ValueError("Provide 1–1200 characters of scene narration")
        return self


class GenerateVideo(Schema):
    model_config = ConfigDict(json_schema_extra={
        "examples": [{
            "requestId": "da-nang-mua-he-1",
            "productId": "tour-da-nang-3n2d",
            "tour": {
                "title": "Đà Nẵng 3 ngày 2 đêm", "destination": "Đà Nẵng",
                "highlights": ["Bán đảo Sơn Trà", "Ngũ Hành Sơn"],
                "experience": "Dạo biển Mỹ Khê", "priceVnd": 2990000,
                "promotion": "Ưu đãi cho nhóm từ 4 khách",
                "callToAction": "Liên hệ để đặt tour",
            },
            "images": ["https://cdn.example.com/da-nang.jpg"],
            "config": {"mode": "LOCAL", "durationSeconds": 40, "aspectRatio": "9:16"},
        }, {
            "requestId": "da-nang-hybrid-1",
            "productId": "tour-da-nang-3n2d",
            "tour": {"title": "Đà Nẵng 3 ngày 2 đêm", "destination": "Đà Nẵng"},
            "images": ["https://cdn.example.com/son-tra.jpg", "https://cdn.example.com/my-khe.jpg"],
            "config": {"mode": "HYBRID", "durationSeconds": 40, "aspectRatio": "9:16"},
            "aiScenes": 2, "maxAiCostUsd": "0.2500", "allowLocalFallback": True,
        }]
    })

    request_id: Identifier
    product_id: Identifier
    tour: Tour
    images: list[AssetRef] = Field(default_factory=list, max_length=30)
    videos: list[AssetRef] = Field(default_factory=list, max_length=30)
    logo: AssetRef | None = None
    background_music: AssetRef | None = None
    voiceover: AssetRef | None = None
    narration: NarrationConfig | None = None
    config: VideoConfig
    ai_scenes: int | None = Field(default=None, ge=0, le=2, strict=True)
    ai_reference_images: list[AssetRef] | None = Field(default=None, max_length=2)
    max_ai_cost_usd: Decimal | None = Field(default=None, ge=0, max_digits=12, decimal_places=4)
    allow_local_fallback: bool = True

    @model_validator(mode="after")
    def validate_assets(self):
        if not 1 <= len(self.images) + len(self.videos) <= 30:
            raise ValueError("Provide 1–30 image or video assets")
        if (
            self.config.duration_seconds not in (30, 35, 40)
            or self.config.aspect_ratio not in ("16:9", "9:16")
        ):
            raise ValueError("Video requires duration 30, 35 or 40 and aspect ratio 16:9 or 9:16")
        if self.config.mode == Mode.LOCAL and self.ai_scenes:
            raise ValueError("AI scenes require HYBRID mode")
        optional = [self.logo, self.background_music, self.voiceover]
        for ref in [*self.images, *self.videos, *optional, *(self.ai_reference_images or [])]:
            if isinstance(ref, HttpUrl):
                validate_image_url(ref)
        return self


class JobAccepted(Schema):
    job_id: UUID


class JobDetails(Schema):
    model_config = ConfigDict(populate_by_name=True)

    id: UUID = Field(serialization_alias="jobId")
    request_id: str
    product_id: str
    mode: Mode
    status: Status
    progress: int
    duration_seconds: int
    aspect_ratio: str
    estimated_cost_usd: Decimal | None
    actual_cost_usd: Decimal
    execution_mode: Mode | None
    fallback_reason: str | None
    ai_cost_uncertain: bool
    output_url: str | None
    error_code: str | None
    error_message: str | None
    created_at: datetime
    updated_at: datetime
    completed_at: datetime | None


class JobPage(Schema):
    items: list[JobDetails]
    page: int
    page_size: int = Field(serialization_alias="pageSize", validation_alias="page_size")
    total: int

