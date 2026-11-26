from decimal import Decimal
from functools import lru_cache
from pathlib import Path
from typing import Literal
from uuid import UUID

from pydantic import AliasChoices, Field, PositiveInt, SecretStr, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    database_url: SecretStr
    redis_url: SecretStr = SecretStr("redis://redis:6379/0")
    service_api_keys: dict[UUID, SecretStr]
    max_request_bytes: PositiveInt = 262144
    worker_concurrency: PositiveInt = 2
    task_soft_timeout: PositiveInt = 240
    task_hard_timeout: PositiveInt = 270
    max_job_attempts: PositiveInt = 3
    dispatch_interval: PositiveInt = 5
    redispatch_after: PositiveInt = 600
    media_root: Path = Path("media")
    max_asset_bytes: PositiveInt = 32 * 1024 * 1024
    max_job_asset_bytes: PositiveInt = 160 * 1024 * 1024
    max_asset_duration: PositiveInt = 180
    ffmpeg_threads: PositiveInt = 2
    font_path: Path = Path("/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf")
    wan_enabled: bool = False
    wan_dry_run: bool = Field(default=True, validation_alias=AliasChoices("WAN_DRY_RUN", "DRY_RUN"))
    wan_live_test_enabled: bool = False
    wan_api_key: SecretStr | None = None
    wan_base_url: str = "https://dashscope-intl.aliyuncs.com"
    wan_model: Literal["wan2.6-i2v-flash"] = "wan2.6-i2v-flash"
    wan_resolution: Literal["720P", "1080P"] = "720P"
    wan_default_duration: int = Field(default=5, ge=2, le=15)
    wan_max_concurrent_tasks: PositiveInt = 2
    wan_price_per_second_usd: Decimal | None = Field(
        default=None, gt=0, le=1000, max_digits=12, decimal_places=8
    )
    wan_poll_interval_seconds: int = Field(default=15, ge=1, le=120)
    wan_task_timeout_seconds: int = Field(default=900, ge=5, le=86400)
    wan_http_timeout_seconds: int = Field(default=15, ge=1, le=60)
    video_provider: Literal["wan", "wavespeed"] = "wavespeed"
    wavespeed_api_key: SecretStr | None = None
    wavespeed_base_url: str = "https://api.wavespeed.ai"
    wavespeed_model: Literal["alibaba/wan-2.6/image-to-video-flash"] = (
        "alibaba/wan-2.6/image-to-video-flash"
    )
    wavespeed_enabled: bool = False
    wavespeed_dry_run: bool = True
    wavespeed_live_test_enabled: bool = False
    wavespeed_resolution: Literal["720p", "1080p"] = "720p"
    wavespeed_duration_seconds: int = Field(default=5, ge=2, le=15)
    wavespeed_generate_audio: bool = False
    wavespeed_enable_prompt_expansion: bool = False
    wavespeed_max_concurrent: PositiveInt = 2
    wavespeed_http_timeout_seconds: int = Field(default=30, ge=1, le=60)
    wavespeed_poll_interval_seconds: int = Field(default=15, ge=1, le=120)
    wavespeed_task_timeout_seconds: int = Field(default=900, ge=5, le=86400)
    ai_director_enabled: bool = False
    ai_director_provider: Literal["groq"] = "groq"
    groq_api_key: SecretStr | None = None
    groq_base_url: str = "https://api.groq.com/openai/v1"
    groq_model: str = Field(default="openai/gpt-oss-120b", min_length=1, max_length=128)
    groq_timeout_seconds: int = Field(default=60, ge=1, le=120)
    groq_max_completion_tokens: int = Field(default=4096, ge=128, le=8192)
    groq_max_retries: int = Field(default=1, ge=0, le=2)
    ai_director_fallback_enabled: bool = True
    ai_director_max_scenes: int = Field(default=8, ge=2, le=30)
    tts_enabled: bool = False
    tts_provider: Literal["vieneu"] = "vieneu"
    tts_device: Literal["cpu"] = "cpu"
    tts_offline_only: bool = True
    tts_model_dir: Path = Path("/data/models/tts")
    tts_voice: str = Field(default="default", min_length=1, max_length=64)
    tts_speed: float = Field(default=1.0, ge=0.9, le=1.15)
    tts_max_concurrent: int = Field(default=1, ge=1, le=4)
    tts_timeout_seconds: int = Field(default=180, ge=1, le=300)
    subtitle_enabled: bool = True
    subtitle_format: Literal["ass"] = "ass"
    audio_ducking_enabled: bool = True

    @model_validator(mode="after")
    def validate_settings(self):
        if not self.tts_offline_only:
            raise ValueError("TTS_OFFLINE_ONLY must be true")
        keys = [key.get_secret_value() for key in self.service_api_keys.values()]
        if not keys or len(set(keys)) != len(keys) or any(len(k) < 32 for k in keys):
            raise ValueError("Configure unique API keys of at least 32 characters")
        if not self.task_soft_timeout < self.task_hard_timeout < self.redispatch_after:
            raise ValueError("Expected soft timeout < hard timeout < redispatch interval")
        return self


@lru_cache
def settings() -> Settings:
    return Settings()

