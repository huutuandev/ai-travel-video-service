import json
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from uuid import UUID

import pytest
from sqlalchemy import inspect, select
from sqlalchemy.orm import Session
from test_api import create
from test_local_video import artwork, upload

from app.core.config import settings
from app.core.db import engine
from app.core.exceptions import MediaError, ProcessingUnavailable, RetryableExecutionError
from app.models.video import Status, VideoAsset, VideoDispatch, VideoJob
from app.services import execution
from app.services.ai_director import CONTENT_TYPE
from app.services.tts import NarrationError


def test_dispatch_retries_failed_publication(migrated, client, payload, headers):
    job_id = create(client, payload, headers)

    def broker_down(_):
        raise ConnectionError("broker unavailable")

    with pytest.raises(ConnectionError):
        execution.dispatch_due_jobs(broker_down)
    sent = []
    execution.dispatch_due_jobs(sent.append)
    execution.dispatch_due_jobs(sent.append)
    assert sent == [job_id]


def test_worker_retry_and_idempotency(migrated, client, payload, headers, monkeypatch):
    job_id = UUID(create(client, payload, headers))

    def transient_failure(db, job):
        raise RetryableExecutionError

    monkeypatch.setattr(execution, "process_video", transient_failure)
    for attempt in (1, 2, 3):
        before = datetime.now(UTC)
        execution.execute_job(job_id)
        execution.execute_job(job_id)
        with Session(engine()) as db:
            job = db.get(VideoJob, job_id)
            dispatch = db.get(VideoDispatch, job_id)
            assert job.attempts == attempt
            if attempt < 3:
                assert job.status == Status.QUEUED
                assert dispatch.available_at >= before + timedelta(seconds=2**attempt)
                dispatch.available_at = dispatch.next_dispatch_at = datetime.now(UTC)
                db.commit()
            else:
                assert dispatch is None
                assert job.status == Status.FAILED
                assert job.error_code == "ATTEMPTS_EXHAUSTED"


def test_recover_interrupted_job(migrated, client, payload, headers, monkeypatch):
    def interrupted_stub(db, job):
        raise ProcessingUnavailable("Test renderer unavailable")

    monkeypatch.setattr(execution, "process_video", interrupted_stub)
    job_id = UUID(create(client, payload, headers))
    with Session(engine()) as db:
        job = db.get(VideoJob, job_id)
        job.status = Status.PROCESSING
        job.attempts = 1
        db.commit()

    sent = []
    execution.dispatch_due_jobs(sent.append)
    assert sent == [str(job_id)]
    execution.execute_job(job_id)
    with Session(engine()) as db:
        job = db.get(VideoJob, job_id)
        assert job.attempts == 2
        assert job.status == Status.FAILED
        assert job.error_code == "PROCESSING_UNAVAILABLE"
        assert job.output_url is None


@pytest.mark.parametrize("error_code", ["GROQ_RATE_LIMIT", "GROQ_MODEL_UNAVAILABLE"])
def test_director_fallback_persisted_across_render_retry(
    migrated, client, payload, headers, tmp_path, monkeypatch, error_code,
):
    monkeypatch.setenv("AI_DIRECTOR_ENABLED", "true")
    monkeypatch.setenv("AI_DIRECTOR_FALLBACK_ENABLED", "true")
    monkeypatch.setenv("GROQ_API_KEY", "mock-only")
    monkeypatch.setenv("MEDIA_ROOT", str(tmp_path))
    settings.cache_clear()
    payload["images"] = [upload(client, headers, artwork("JPEG", "#367ace"), "image/jpeg")]
    job_id = UUID(create(client, payload, headers))
    calls, rendered = [], []

    def complete(*args):
        calls.append(args)
        raise MediaError(error_code, "Simulated Groq error")

    def render(db, job, generated, *, direction):
        assert not generated
        assert direction.asset_metadata["source"] == "TEMPLATE_FALLBACK"
        rendered.append(job.id)
        if len(rendered) == 1:
            raise RetryableExecutionError
        raise ProcessingUnavailable("Second render attempt")

    monkeypatch.setattr("app.providers.groq.GroqClient.complete", complete)
    monkeypatch.setattr(execution, "process_video", render)
    execution.execute_job(job_id)
    with Session(engine()) as db:
        dispatch = db.get(VideoDispatch, job_id)
        dispatch.available_at = dispatch.next_dispatch_at = datetime.now(UTC)
        db.commit()
    settings.cache_clear()
    execution.execute_job(job_id)
    assert len(calls) == 1
    assert rendered == [job_id, job_id]
    assert UUID(create(client, payload, headers)) == job_id
    with Session(engine()) as db:
        direction = db.scalar(select(VideoAsset).where(
            VideoAsset.job_id == job_id, VideoAsset.content_type == CONTENT_TYPE
        ))
        assert direction.asset_metadata["errorCode"] == error_code


def test_directed_local_render_publishes_narration_timeline(
    migrated, client, payload, headers, tmp_path, monkeypatch,
):
    from test_narration import mock_wav
    from test_storyboard import plan_payload

    monkeypatch.setenv("AI_DIRECTOR_ENABLED", "true")
    monkeypatch.setenv("AI_DIRECTOR_FALLBACK_ENABLED", "false")
    monkeypatch.setenv("GROQ_API_KEY", "mock-only")
    monkeypatch.setenv("TTS_ENABLED", "true")
    monkeypatch.setenv("MEDIA_ROOT", str(tmp_path))
    settings.cache_clear()
    payload["images"] = [upload(client, headers, artwork("JPEG", "#367ace"), "image/jpeg")]
    monkeypatch.setattr(
        "app.providers.groq.GroqClient.complete",
        lambda *_: (json.dumps(plan_payload(ai=False)), {"total_tokens": 160}),
    )
    monkeypatch.setattr(
        "app.services.hybrid.provider_for", lambda *_: pytest.fail("LOCAL called video provider")
    )

    def synthesize(text, voice, path, deadline):
        mock_wav(path, 0.6)

    monkeypatch.setattr(
        "app.services.narration.local_tts", lambda *_: SimpleNamespace(synthesize=synthesize)
    )
    job_id = UUID(create(client, payload, headers))
    execution.execute_job(job_id)
    with Session(engine()) as db:
        job = db.get(VideoJob, job_id)
        assert job.status == Status.COMPLETED, job.error_code
        assert job.execution_mode == "LOCAL"
        output = db.scalar(select(VideoAsset).where(
            VideoAsset.job_id == job_id, VideoAsset.content_type == "video/mp4",
        ))
        timeline = output.asset_metadata["narrationTimeline"]
        assert output.asset_metadata["directorSource"] == "GROQ_DIRECTOR"
        assert timeline["scenes"][1]["startFrame"] == 150
        assert timeline["scenes"][1]["narration"] == "Liên hệ để đặt tour ngay."
        assert output.asset_metadata["subtitleBurnedIn"]
        assert output.asset_metadata["voiceoverSegments"][1]["originalText"] == (
            "Liên hệ để đặt tour ngay."
        )
        assert client.get(job.output_url, headers=headers).status_code == 200


def test_required_tts_error_fails_hybrid_without_video_fallback(
    migrated, client, payload, headers, monkeypatch,
):
    payload["config"]["mode"] = "HYBRID"
    payload["narration"] = {"enabled": True, "allowFallback": False}
    job_id = UUID(create(client, payload, headers))
    calls = []

    def advance(*args, **kwargs):
        calls.append("advance")
        return (SimpleNamespace(provider="wavespeed"),)

    def render(*args, **kwargs):
        raise NarrationError("TTS_MODEL_UNAVAILABLE", "Model missing")

    monkeypatch.setattr(execution, "advance_hybrid", advance)
    monkeypatch.setattr(execution, "process_video", render)
    execution.execute_job(job_id)
    execution.execute_job(job_id)
    with Session(engine()) as db:
        job = db.get(VideoJob, job_id)
        assert job.status == Status.FAILED
        assert job.error_code == "TTS_MODEL_UNAVAILABLE"
        assert job.fallback_reason is None
    assert calls == ["advance"]


def test_migrations(test_schema):
    from alembic import command
    from alembic.config import Config

    config = Config("alembic.ini")
    command.upgrade(config, "head")
    tables = {
        "video_jobs", "video_scenes", "video_assets", "video_dispatches", "video_ai_generations",
    }
    assert tables <= set(inspect(engine()).get_table_names())
    command.check(config)
    command.downgrade(config, "base")
    assert tables.isdisjoint(inspect(engine()).get_table_names())
    command.upgrade(config, "head")

