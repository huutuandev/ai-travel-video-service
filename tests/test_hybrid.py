import json
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from uuid import UUID, uuid4

import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session
from test_api import create
from test_local_video import artwork, upload

from app.core.config import settings
from app.core.db import engine
from app.core.exceptions import MediaError
from app.models.video import VideoAIGeneration, VideoAsset, VideoDispatch, VideoJob, VideoScene
from app.providers.base import CostEstimate, GenerationSpec, ProviderError, TaskResult, TaskState
from app.renderers.ffmpeg import ffmpeg, media_input, probe
from app.services import execution, hybrid
from app.services.ai_director import CONTENT_TYPE
from app.storage.local import atomic_copy


class MockProvider:
    name = "wan"
    endpoint = "https://dashscope-intl.aliyuncs.com"
    spec = GenerationSpec("wan2.6-i2v-flash", "720P", 5)

    def __init__(self):
        self.submits = []
        self.polls = []
        self.downloads = []
        self.responses = {}
        self.fixture = None

    def estimate_cost(self, spec):
        assert spec == self.spec
        return CostEstimate(Decimal("0.025"), Decimal("0.1250"))

    def submit_generation(self, image, prompt, spec):
        assert spec == self.spec
        assert image.read_bytes().startswith(b"\xff\xd8")
        assert "Preserve" in prompt
        identifier = f"mock-{len(self.submits) + 1}"
        self.submits.append(identifier)
        return identifier

    def get_task_status(self, task_id):
        self.polls.append(task_id)
        value = self.responses.get(task_id, TaskResult(TaskState.PENDING))
        if isinstance(value, Exception):
            raise value
        return value

    def download_result(self, result, target, max_bytes):
        self.downloads.append(target.name)
        assert self.fixture.stat().st_size <= max_bytes
        atomic_copy(self.fixture, target)


def test_director_drives_two_distinct_prompts_and_persists_them(
    migrated, client, payload, headers, tmp_path, monkeypatch,
):
    from types import SimpleNamespace

    from test_narration import mock_wav
    from test_storyboard import plan_payload

    for key, value in {
        "MEDIA_ROOT": str(tmp_path), "AI_DIRECTOR_ENABLED": "true",
        "AI_DIRECTOR_FALLBACK_ENABLED": "false",
        "GROQ_API_KEY": "mock-only",
        "VIDEO_PROVIDER": "wavespeed", "WAVESPEED_ENABLED": "true",
        "WAVESPEED_DRY_RUN": "false", "WAVESPEED_LIVE_TEST_ENABLED": "true",
        "WAVESPEED_API_KEY": "mock-only", "WAVESPEED_MAX_CONCURRENT": "2",
    }.items():
        monkeypatch.setenv(key, value)
    settings.cache_clear()
    provider = MockProvider()
    provider.name = "wavespeed"
    sent_prompts = []

    def submit(_, prompt, spec):
        assert spec == provider.spec
        sent_prompts.append(prompt)
        return f"directed-{len(sent_prompts)}"

    monkeypatch.setattr(provider, "submit_generation", submit)
    monkeypatch.setattr(hybrid, "provider_for", lambda _, row=None: provider)
    payload["config"]["mode"] = "HYBRID"
    payload["images"] = [
        upload(client, headers, artwork("JPEG", color), "image/jpeg")
        for color in ("#367ace", "#a64b80")
    ]
    payload.update(aiScenes=2, maxAiCostUsd="0.2500", allowLocalFallback=False)
    raw = plan_payload()
    first, last = raw["scenes"]
    raw["scenes"] = [
        first,
        {**first, "sceneId": 2, "startSeconds": 5, "assetIndex": 1,
         "videoPrompt": "Gentle lateral camera motion.", "narration": "Mời bạn cùng trải nghiệm."},
        {**last, "sceneId": 3, "startSeconds": 10, "durationSeconds": 20},
    ]
    calls = []

    def complete(_, messages, schema):
        calls.append(messages)
        return json.dumps(raw), {"total_tokens": 320}

    monkeypatch.setattr("app.providers.groq.GroqClient.complete", complete)
    monkeypatch.setenv("TTS_ENABLED", "true")
    settings.cache_clear()
    monkeypatch.setattr(
        "app.services.narration.local_tts", lambda *_: pytest.fail("TTS ran while polling video")
    )
    job_id = UUID(create(client, payload, headers))
    execution.execute_job(job_id)
    assert len(sent_prompts) == 2
    assert sent_prompts[0].startswith("Slow camera glide.")
    assert sent_prompts[1].startswith("Gentle lateral camera motion.")
    ready(job_id)
    execution.execute_job(job_id)
    assert len(calls) == 1
    assert provider.polls == ["directed-1", "directed-2"]
    assert UUID(create(client, payload, headers)) == job_id
    with Session(engine()) as db:
        direction = db.scalar(select(VideoAsset).where(
            VideoAsset.job_id == job_id, VideoAsset.content_type == CONTENT_TYPE
        ))
        assert direction.asset_metadata["submittedPrompts"] == {
            "1": sent_prompts[0], "2": sent_prompts[1],
        }
        assert direction.asset_metadata["tokenUsage"] == {"total_tokens": 320}

    provider.fixture = tmp_path / "directed-fixture.mp4"
    ffmpeg([
        "-f", "lavfi", "-i", "testsrc2=size=320x240:rate=30",
        "-t", "5", "-c:v", "libx264", "-threads", "2", str(provider.fixture),
    ])
    for task_id in ("directed-1", "directed-2"):
        provider.responses[task_id] = TaskResult(
            TaskState.SUCCEEDED, "https://mock.example/clip.mp4"
        )
    spoken = []

    def synthesize(text, voice, path, deadline):
        spoken.append(text)
        mock_wav(path, 0.7)

    monkeypatch.setattr(
        "app.services.narration.local_tts", lambda *_: SimpleNamespace(synthesize=synthesize)
    )
    ready(job_id)
    execution.execute_job(job_id)
    data = client.get(f"/api/v1/videos/{job_id}", headers=headers).json()
    assert data["status"] == "COMPLETED", data
    assert data["executionMode"] == "HYBRID"
    assert data["aiCostUncertain"]
    assert len(sent_prompts) == 2 and len(calls) == 1
    assert len(provider.downloads) == 2
    assert spoken == [scene["narration"] for scene in raw["scenes"]]
    assert client.get(data["outputUrl"], headers=headers).status_code == 200
    with Session(engine()) as db:
        output = db.scalar(select(VideoAsset).where(
            VideoAsset.job_id == job_id, VideoAsset.content_type == "video/mp4"
        ))
        assert output.asset_metadata["generatedScenes"] == 2
        assert output.asset_metadata["subtitleBurnedIn"]
        assert len(output.asset_metadata["voiceoverSegments"]) == 3


@pytest.fixture
def cloud(migrated, monkeypatch, tmp_path):
    overrides = {
        "VIDEO_PROVIDER": "wan",
        "MEDIA_ROOT": str(tmp_path),
        "WAN_ENABLED": "true",
        "WAN_DRY_RUN": "false",
        "WAN_LIVE_TEST_ENABLED": "true",
        "WAN_API_KEY": "test-key",
        "WAN_MAX_CONCURRENT_TASKS": "1",
        "WAN_POLL_INTERVAL_SECONDS": "15",
        "WAN_TASK_TIMEOUT_SECONDS": "900",
    }
    for key, value in overrides.items():
        monkeypatch.setenv(key, value)
    settings.cache_clear()
    provider = MockProvider()
    monkeypatch.setattr(hybrid, "provider_for", lambda _, row=None: provider)
    return provider


@pytest.fixture
def hybrid_payload(cloud, payload, client, headers):
    payload["config"] = {"mode": "HYBRID", "durationSeconds": 40, "aspectRatio": "16:9"}
    payload["tour"] = {
        "title": "Đà Nẵng", "highlights": ["Sơn Trà", "Mỹ Khê"], "priceVnd": 2900000,
    }
    payload["images"] = [
        upload(client, headers, artwork("JPEG", color), "image/jpeg")
        for color in ("#367ace", "#a64b80")
    ]
    payload.update(aiScenes=2, maxAiCostUsd="0.2500", allowLocalFallback=False)
    return payload


def state(job_id):
    with Session(engine()) as db:
        job = db.get(VideoJob, job_id)
        dispatch = db.get(VideoDispatch, job_id)
        rows = list(db.scalars(
            select(VideoAIGeneration).where(VideoAIGeneration.job_id == job_id)
            .order_by(VideoAIGeneration.slot)
        ))
    return job, dispatch, rows


def ready(job_id, *, expire=False):
    now = datetime.now(UTC) - timedelta(seconds=1)
    with Session(engine()) as db, db.begin():
        dispatch = db.get(VideoDispatch, job_id)
        dispatch.available_at = dispatch.next_dispatch_at = now
        for row in db.scalars(
            select(VideoAIGeneration).where(VideoAIGeneration.job_id == job_id)
        ):
            row.not_before = now
            if expire:
                row.deadline = now


def test_hybrid_renders_40_seconds_after_worker_restart(
    cloud, client, hybrid_payload, headers, tmp_path,
):
    cloud.fixture = tmp_path / "mock-wan.mp4"
    ffmpeg([
        "-f", "lavfi", "-i", "testsrc2=size=320x240:rate=30",
        "-f", "lavfi", "-i", "sine=frequency=900",
        "-t", "5", "-c:v", "libx264", "-threads", "2", "-c:a", "aac", str(cloud.fixture),
    ])
    job_id = UUID(create(client, hybrid_payload, headers))
    execution.execute_job(job_id)
    job, dispatch, rows = state(job_id)
    assert job.attempts == 0
    assert job.ai_cost_uncertain
    assert cloud.submits == ["mock-1"]
    assert [row.task_id for row in rows] == ["mock-1", None]
    assert [row.state for row in rows] == ["PENDING", "NEW"]
    assert dispatch.available_at > datetime.now(UTC)

    execution.execute_job(job_id)
    assert not cloud.polls
    ready(job_id)
    execution.execute_job(job_id)
    assert cloud.polls == ["mock-1"]
    assert cloud.submits == ["mock-1"]

    result = TaskResult(TaskState.SUCCEEDED, "https://mock.example/clip.mp4", Decimal(5))
    cloud.responses["mock-1"] = result
    ready(job_id)
    execution.execute_job(job_id)
    assert cloud.submits == ["mock-1", "mock-2"]
    assert state(job_id)[2][0].state == "READY"

    ready(job_id)
    cloud.responses["mock-2"] = result
    execution.execute_job(job_id)
    execution.execute_job(job_id)
    assert len(cloud.submits) == len(cloud.downloads) == 2
    job, dispatch, rows = state(job_id)
    assert [row.task_id for row in rows] == ["mock-1", "mock-2"]
    assert all(row.state == "READY" for row in rows)
    assert dispatch is None

    data = client.get(f"/api/v1/videos/{job_id}", headers=headers).json()
    assert data["status"] == "COMPLETED", data
    assert data["executionMode"] == "HYBRID"
    assert data["actualCostUsd"] == data["estimatedCostUsd"] == "0.2500"
    assert not data["aiCostUncertain"]
    assert "mock-1" not in str(data)
    clip = client.get(data["outputUrl"], headers=headers)
    assert clip.status_code == 200
    output = tmp_path / "download.mp4"
    output.write_bytes(clip.content)
    streams = probe(output)["streams"]
    assert streams[0]["codec_name"] == "h264"
    assert streams[0]["avg_frame_rate"] == "30/1"
    assert streams[1]["codec_name"] == "aac"
    assert float(streams[0]["duration"]) == 40.0
    diagnostics = ffmpeg([
        *media_input(output), "-vn", "-af", "volumedetect", "-f", "null", "-",
    ], info=True)
    assert "max_volume: -91.0 dB" in diagnostics

    with Session(engine()) as db:
        scenes = db.scalars(select(VideoScene).where(VideoScene.job_id == job_id)).all()
        assert [s.duration_seconds for s in scenes if s.provider == "wan"] == [5, 5]
        assert sum(s.duration_seconds for s in scenes if s.provider == "local") == 30


@pytest.mark.parametrize(("budget", "reason"), [
    ("0.2499", "AI_BUDGET_EXCEEDED"), (None, "AI_BUDGET_REQUIRED"),
])
def test_budget_prevents_submission(cloud, client, hybrid_payload, headers, budget, reason):
    hybrid_payload["maxAiCostUsd"] = budget
    job_id = UUID(create(client, hybrid_payload, headers))
    execution.execute_job(job_id)
    job, _, rows = state(job_id)
    assert job.status == "FAILED"
    assert job.error_code == reason
    assert job.estimated_cost_usd == Decimal("0.25")
    assert job.actual_cost_usd == 0
    assert cloud.submits == rows == []


def test_worker_lost_during_submit_never_reposts(
    cloud, client, hybrid_payload, headers, monkeypatch,
):
    original = cloud.submit_generation

    def lose_worker(*args):
        original(*args)
        raise SystemExit("worker lost after remote accepted")

    monkeypatch.setattr(cloud, "submit_generation", lose_worker)
    job_id = UUID(create(client, hybrid_payload, headers))
    with pytest.raises(SystemExit):
        execution.execute_job(job_id)
    assert state(job_id)[2][0].state == "SUBMITTING"

    ready(job_id)
    execution.execute_job(job_id)
    job, _, rows = state(job_id)
    assert cloud.submits == ["mock-1"]
    assert rows[0].task_id is None
    assert rows[0].lease_until is not None
    assert job.ai_cost_uncertain
    assert job.error_code == "WAN_SUBMIT_UNCERTAIN"


@pytest.mark.parametrize(("reply", "expire", "code", "uncertain"), [
    (TaskResult(TaskState.FAILED), False, "WAN_TASK_FAILED", False),
    (TaskResult(TaskState.UNKNOWN), False, "WAN_TASK_EXPIRED", True),
    (ProviderError("WAN_INVALID_API_KEY"), False, "WAN_INVALID_API_KEY", True),
    (TaskResult(TaskState.PENDING), True, "WAN_TASK_TIMEOUT", True),
])
def test_terminal_cloud_states(
    cloud, client, hybrid_payload, headers, reply, expire, code, uncertain,
):
    hybrid_payload["aiScenes"] = 1
    job_id = UUID(create(client, hybrid_payload, headers))
    execution.execute_job(job_id)
    ready(job_id, expire=expire)
    cloud.responses["mock-1"] = reply
    execution.execute_job(job_id)
    job, dispatch, rows = state(job_id)
    assert job.status == "FAILED"
    assert job.error_code == rows[0].error_code == code
    assert job.ai_cost_uncertain == uncertain
    assert cloud.submits == ["mock-1"]
    assert dispatch is None


def test_poll_rate_limit_honors_backoff(cloud, client, hybrid_payload, headers):
    hybrid_payload["aiScenes"] = 1
    job_id = UUID(create(client, hybrid_payload, headers))
    execution.execute_job(job_id)
    ready(job_id)
    cloud.responses["mock-1"] = ProviderError("WAN_RATE_LIMIT", retryable=True, retry_after=45)
    before = datetime.now(UTC)
    execution.execute_job(job_id)
    job, dispatch, rows = state(job_id)
    assert cloud.submits == cloud.polls == ["mock-1"]
    assert dispatch.available_at >= before + timedelta(seconds=45)
    assert rows[0].transient_errors == 1
    assert job.attempts == 0


@pytest.mark.parametrize(("count", "dry_run", "refs", "reason"), [
    (0, False, None, None),
    (1, True, None, "WAN_DRY_RUN"),
    (1, False, [], "WAN_REFERENCE_UNAVAILABLE"),
])
def test_no_cloud_routes_to_real_local_renderer_entrypoint(
    cloud, client, hybrid_payload, headers, monkeypatch, count, dry_run, refs, reason,
):
    hybrid_payload.update(aiScenes=count, aiReferenceImages=refs, allowLocalFallback=True)
    monkeypatch.setenv("WAN_DRY_RUN", str(dry_run).lower())
    settings.cache_clear()
    renders = []

    def local_entrypoint(job, request, progress, **kwargs):
        renders.append((job.execution_mode, request.config.duration_seconds, kwargs))
        raise MediaError("STOP_BEFORE_RENDER", "Test exit before invoking FFmpeg")

    monkeypatch.setattr(execution, "render_local", local_entrypoint)
    job_id = UUID(create(client, hybrid_payload, headers))
    execution.execute_job(job_id)
    job, _, rows = state(job_id)
    assert renders == [("LOCAL", 40, {})]
    assert job.fallback_reason == reason
    assert cloud.submits == rows == []


def test_price_increase_blocks_a_scene_still_waiting_for_a_slot(
    cloud, client, hybrid_payload, headers, monkeypatch,
):
    job_id = UUID(create(client, hybrid_payload, headers))
    execution.execute_job(job_id)
    with Session(engine()) as db, db.begin():
        first = db.get(VideoAIGeneration, (job_id, 1))
        first.state = "READY"
        first.lease_until = None
        first.billing_uncertain = False
        first.actual_cost_usd = first.estimated_cost_usd
    monkeypatch.setattr(
        cloud, "estimate_cost", lambda _: CostEstimate(Decimal("0.05"), Decimal("0.25"))
    )
    ready(job_id)
    execution.execute_job(job_id)
    job, _, rows = state(job_id)
    assert job.error_code == rows[1].error_code == "WAN_PRICE_CHANGED"
    assert job.actual_cost_usd == Decimal("0.1250")
    assert cloud.submits == ["mock-1"]


def test_cloud_concurrency_is_shared_between_jobs(cloud, client, hybrid_payload, headers):
    hybrid_payload["aiScenes"] = 1
    first = UUID(create(client, hybrid_payload, headers))
    hybrid_payload["requestId"] = str(uuid4())
    second = UUID(create(client, hybrid_payload, headers))
    execution.execute_job(first)
    execution.execute_job(second)
    assert state(second)[2][0].state == "NEW"
    assert cloud.submits == ["mock-1"]

