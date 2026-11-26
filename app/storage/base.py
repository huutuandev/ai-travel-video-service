from pathlib import Path
from typing import Protocol


class VideoStorage(Protocol):
    def store(self, source: Path, *, storage_key: str, content_type: str) -> str: ...

