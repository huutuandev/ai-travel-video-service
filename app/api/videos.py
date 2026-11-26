from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from fastapi.responses import FileResponse
from sqlalchemy.orm import Session
from starlette.concurrency import run_in_threadpool

from app.core.config import settings
from app.core.db import session
from app.core.exceptions import MediaError, RequestConflict
from app.core.security import authenticated_tenant
from app.models.video import Status
from app.repositories.videos import get_job, list_jobs
from app.schemas.video import GenerateVideo, JobAccepted, JobDetails, JobPage
from app.services.assets import save_upload
from app.services.videos import submit_video
from app.storage.local import output_path

router = APIRouter(prefix="/api/v1/videos", tags=["videos"])
Tenant = Annotated[UUID, Depends(authenticated_tenant)]
Database = Annotated[Session, Depends(session)]


@router.post("/generate", status_code=202, response_model=JobAccepted)
def generate(request: GenerateVideo, tenant_id: Tenant, db: Database):
    try:
        job_id = submit_video(db, tenant_id, request)
    except RequestConflict as exc:
        raise HTTPException(409, "requestId already used with a different payload") from exc
    return {"jobId": job_id}


@router.post(
    "/assets", status_code=201,
    openapi_extra={"requestBody": {"required": True, "content": {
        "application/octet-stream": {"schema": {"type": "string", "format": "binary"}}
    }}},
)
async def upload(request: Request, tenant_id: Tenant):
    limit = settings().max_asset_bytes
    with TemporaryDirectory(prefix="video-upload-") as directory:
        path = Path(directory) / "source"
        total = 0
        with path.open("wb") as stream:
            async for chunk in request.stream():
                total += len(chunk)
                if total > limit:
                    raise HTTPException(413, "Asset exceeds the configured size limit")
                await run_in_threadpool(stream.write, chunk)
        try:
            asset_id = await run_in_threadpool(
                save_upload, tenant_id, path, request.headers.get("content-type")
            )
        except MediaError as exc:
            raise HTTPException(415, str(exc)) from exc
    return {"assetId": asset_id}


@router.get("/{jobId}/download", response_class=FileResponse)
def download(jobId: UUID, tenant_id: Tenant, db: Database):
    job = get_job(db, tenant_id, jobId)
    if job is None or job.status != Status.COMPLETED:
        raise HTTPException(404, "Video output not found")
    path = output_path(tenant_id, jobId)
    if not path.is_file():
        raise HTTPException(404, "Video output not found")
    return FileResponse(
        path, media_type="video/mp4", filename=f"{jobId}.mp4",
        headers={"Cache-Control": "private, no-store"},
    )


@router.get("/{jobId}", response_model=JobDetails)
def detail(jobId: UUID, tenant_id: Tenant, db: Database):
    job = get_job(db, tenant_id, jobId)
    if job is None:
        raise HTTPException(404, "Video job not found")
    return job


@router.get("", response_model=JobPage)
def listing(
    tenant_id: Tenant,
    db: Database,
    page: Annotated[int, Query(ge=1, le=1_000_000)] = 1,
    page_size: Annotated[int, Query(alias="pageSize", ge=1, le=100)] = 20,
):
    items, total = list_jobs(db, tenant_id, page, page_size)
    return {"items": items, "page": page, "page_size": page_size, "total": total}

