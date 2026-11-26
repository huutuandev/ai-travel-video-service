from pathlib import Path
from typing import Protocol

from app.schemas.video import VideoConfig


class VideoRenderer(Protocol):
    def render(self, scenes: list[Path], config: VideoConfig, *, destination: Path) -> Path: ...

