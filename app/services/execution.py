from datetime import UTC, datetime, timedelta
from uuid import UUID

import structlog
from billiard.exceptions import SoftTimeLimitExceeded
from pydantic import ValidationError
from sqlalchemy import delete, select, text, update
from sqlalchemy.orm import Session

from app.core.config import settings
from app.core.db import engine
from app.core.exceptions import MediaError, ProcessingUnavailable, RetryableExecutionError
from app.models.video import TERMINAL, Mode, Status, VideoDispatch, VideoJob, VideoScene
from app.schemas.video import GenerateVideo
from app.services.ai_director import ensure_director
from app.services.hybrid import advance_hybrid, clear_hybrid_staging, provider_code
from app.services.local_video import render_local
from app.services.narration import clear_narration
from app.services.tts import NarrationError

log = structlog.get_logger()


def load_request(job):
    try:
        return GenerateVideo.model_validate(job.request_payload)
    except ValidationError as exc:
        raise MediaError("INVALID_JOB_CONFIG", "Stored job settings cannot be rendered") from exc


def process_video(db, job, generated=(), *, direction=None):
    request = load_request(job)
    job.execution_mode = Mode.HYBRID if generated else Mode.LOCAL

    def progress(value, *, rendering=False):
        job.progress = max(job.progress, value)
        if rendering:
            job.status = Status.RENDERING
        db.commit()

    if direction is not None:
        return render_local(job, request, progress, generated=generated, direction=direction)
    if generated:
        return render_local(job, request, progress, generated=generated)
    return render_local(job, request, progress)


def schedule_again(db, job_id, due):
    db.execute(
        update(VideoDispatch)
        .where(VideoDispatch.job_id == job_id)
        .values(available_at=due, next_dispatch_at=due)
    )
    db.commit()


def cleanup(job):
    try:
        clear_hybrid_staging(job)
        clear_narration(job)
    except OSError:
        log.warning("video_staging_cleanup_failed", job_id=str(job.id))


def retry_job(db, job, dispatch, max_attempts):
    if job.attempts >= max_attempts:
        fail_job(db, job, dispatch, "ATTEMPTS_EXHAUSTED", "Worker attempts exhausted")
        return
    due = datetime.now(UTC) + timedelta(seconds=2 ** min(job.attempts, 8))
    job.status = Status.QUEUED
    job.progress = 0
    schedule_again(db, job.id, due)
    log.info("video_job_retry_scheduled", job_id=str(job.id), attempt=job.attempts)


def fail_job(db, job, dispatch, code, message):
    job.status = Status.FAILED
    job.error_code = code
    job.error_message = message
    job.completed_at = datetime.now(UTC)
    db.execute(
        update(VideoScene).where(VideoScene.job_id == job.id).values(status=Status.FAILED)
    )
    db.delete(dispatch)
    db.commit()
    cleanup(job)
    log.info("video_job_failed", job_id=str(job.id), error_code=code)


def run_job(db, job_id):
    dispatch = db.get(VideoDispatch, job_id)
    if dispatch is None or dispatch.available_at > datetime.now(UTC):
        return
    job = db.get(VideoJob, job_id)
    if job.status in TERMINAL:
        db.delete(dispatch)
        db.commit()
        return

    max_attempts = settings().max_job_attempts
    if job.attempts >= max_attempts:
        fail_job(db, job, dispatch, "ATTEMPTS_EXHAUSTED", "Worker attempts exhausted")
        return

    generated = ()
    try:
        request = load_request(job)
        direction = ensure_director(db, job, request)
        if job.mode == Mode.HYBRID:
            step = advance_hybrid(db, job, request, direction=direction)
            if isinstance(step, datetime):
                schedule_again(db, job.id, step)
                return
            generated = step
    except MediaError as exc:
        fail_job(db, job, dispatch, exc.code, str(exc))
        return
    except ProcessingUnavailable as exc:
        fail_job(db, job, dispatch, "PROCESSING_UNAVAILABLE", str(exc))
        return
    except (RetryableExecutionError, SoftTimeLimitExceeded):
        job.attempts += 1
        retry_job(db, job, dispatch, max_attempts)
        return

    job.attempts += 1
    job.status = Status.PROCESSING
    job.progress = max(job.progress, 5)
    db.commit()

    try:
        if direction is not None:
            asset, scenes = process_video(db, job, generated, direction=direction)
        elif generated:
            asset, scenes = process_video(db, job, generated)
        else:
            asset, scenes = process_video(db, job)
    except MediaError as exc:
        if (
            not isinstance(exc, NarrationError) and generated
            and load_request(job).allow_local_fallback and job.attempts < max_attempts
        ):
            job.fallback_reason = provider_code(generated[0].provider, "RENDER_FAILED")
            job.execution_mode = None
            retry_job(db, job, dispatch, max_attempts)
        else:
            fail_job(db, job, dispatch, exc.code, str(exc))
    except ProcessingUnavailable as exc:
        fail_job(db, job, dispatch, "PROCESSING_UNAVAILABLE", str(exc))
    except (RetryableExecutionError, SoftTimeLimitExceeded):
        retry_job(db, job, dispatch, max_attempts)
    else:
        db.execute(delete(VideoScene).where(VideoScene.job_id == job.id))
        for scene in scenes:
            scene.status = Status.COMPLETED
        db.add_all([asset, *scenes])
        job.status = Status.COMPLETED
        job.progress = 100
        job.output_url = f"/api/v1/videos/{job.id}/download"
        if job.fallback_reason is None:
            job.fallback_reason = asset.asset_metadata.get("audioFallbackReason")
        job.completed_at = datetime.now(UTC)
        db.delete(dispatch)
        db.commit()
        cleanup(job)
        log.info("video_job_completed", job_id=str(job.id))


def execute_job(job_id: UUID):
    # Hold a session-level lock on a dedicated connection across status commits.
    lock_key = int.from_bytes(job_id.bytes[:8], "big", signed=True)
    with engine().connect() as connection:
        acquired = connection.scalar(
            text("SELECT pg_try_advisory_lock(:key)"), {"key": lock_key}
        )
        if not acquired:
            return
        try:
            connection.commit()
            with Session(connection, expire_on_commit=False) as db:
                run_job(db, job_id)
        finally:
            try:
                connection.rollback()
                connection.execute(text("SELECT pg_advisory_unlock(:key)"), {"key": lock_key})
                connection.commit()
            except Exception:
                connection.invalidate()
                raise


def dispatch_due_jobs(publish):
    now = datetime.now(UTC)
    with Session(engine()) as db, db.begin():
        pending = db.scalars(
            select(VideoDispatch)
            .where(VideoDispatch.available_at <= now, VideoDispatch.next_dispatch_at <= now)
            .order_by(VideoDispatch.next_dispatch_at, VideoDispatch.job_id)
            .limit(50)
            .with_for_update(skip_locked=True)
        ).all()
        for dispatch in pending:
            publish(str(dispatch.job_id))
            dispatch.next_dispatch_at = now + timedelta(seconds=settings().redispatch_after)

