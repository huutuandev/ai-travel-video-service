from datetime import UTC, datetime
from decimal import Decimal
from uuid import uuid4

from sqlalchemy import func, select
from sqlalchemy.dialects.postgresql import insert

from app.models.video import Status, VideoDispatch, VideoJob, VideoScene


def add_job(db, tenant_id, request):
    payload = request.model_dump(mode="json")
    stmt = (
        insert(VideoJob)
        .values(
            id=uuid4(),
            tenant_id=tenant_id,
            request_id=request.request_id,
            product_id=request.product_id,
            mode=request.config.mode,
            status=Status.QUEUED,
            progress=0,
            duration_seconds=request.config.duration_seconds,
            aspect_ratio=request.config.aspect_ratio,
            request_payload=payload,
        )
        .on_conflict_do_nothing(constraint="uq_video_jobs_tenant_request")
        .returning(VideoJob.id)
    )
    job_id = db.scalar(stmt)
    if job_id is None:
        return db.execute(
            select(VideoJob.id, VideoJob.request_payload).where(
                VideoJob.tenant_id == tenant_id, VideoJob.request_id == request.request_id
            )
        ).one()

    now = datetime.now(UTC)
    db.add(VideoDispatch(job_id=job_id, available_at=now, next_dispatch_at=now))
    sources = [*request.images, *request.videos]
    milliseconds, remainder = divmod(request.config.duration_seconds * 1000, len(sources))
    db.add_all(
        VideoScene(
            job_id=job_id,
            position=position,
            source_url=str(url),
            duration_seconds=Decimal(milliseconds + (position <= remainder)) / 1000,
            status=Status.QUEUED,
        )
        for position, url in enumerate(sources, start=1)
    )
    return job_id, payload


def get_job(db, tenant_id, job_id):
    return db.scalar(
        select(VideoJob).where(VideoJob.tenant_id == tenant_id, VideoJob.id == job_id)
    )


def list_jobs(db, tenant_id, page, page_size):
    query = select(VideoJob).where(VideoJob.tenant_id == tenant_id)
    total = db.scalar(select(func.count()).select_from(query.subquery()))
    items = db.scalars(
        query.order_by(VideoJob.created_at.desc(), VideoJob.id.desc())
        .offset((page - 1) * page_size)
        .limit(page_size)
    ).all()
    return items, total

