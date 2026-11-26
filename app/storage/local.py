import os
from pathlib import Path
from shutil import copyfileobj
from tempfile import NamedTemporaryFile
from uuid import UUID

from app.core.config import settings


def media_path(tenant_id: UUID, directory: str, filename: str) -> Path:
    return settings().media_root.resolve() / str(tenant_id) / directory / filename


def upload_path(tenant_id: UUID, asset_id: UUID) -> Path:
    return media_path(tenant_id, "uploads", str(asset_id))


def output_path(tenant_id: UUID, job_id: UUID) -> Path:
    return media_path(tenant_id, "videos", f"{job_id}.mp4")


def generation_path(tenant_id: UUID, job_id: UUID, slot: int, *, reference=False) -> Path:
    extension = "jpg" if reference else "mp4"
    return media_path(tenant_id, "generations", f"{job_id}-{slot}.{extension}")


def atomic_copy(source: Path, target: Path):
    target.parent.mkdir(parents=True, exist_ok=True)
    with NamedTemporaryFile(dir=target.parent, delete=False) as temporary:
        staged = Path(temporary.name)
        try:
            with source.open("rb") as original:
                copyfileobj(original, temporary)
            temporary.close()
            os.replace(staged, target)
        finally:
            staged.unlink(missing_ok=True)

