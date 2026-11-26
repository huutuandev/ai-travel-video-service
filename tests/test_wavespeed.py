import json
from decimal import Decimal
from io import BytesIO
from types import SimpleNamespace
from uuid import UUID

import pytest
from test_api import create
from test_hybrid import ready, state
from test_local_video import artwork, upload

from app.core.config import settings
from app.core.exceptions import MediaError
from app.providers.base import GenerationSpec, ProviderError, TaskResult, TaskState
from app.providers.wavespeed import MODEL, WaveSpeedVideoProvider
from app.renderers.ffmpeg import ffmpeg, probe
from app.services import execution, hybrid
from app.storage.local import atomic_copy

ID = "894b84aa0fbd4a5e963194bb8d4983bc"
IMAGE = "https://media.example.com/reference.jpg"
VIDEO = "https://media.example.com/output.mp4"


class Response(BytesIO):
    def __init__(self, data, status, retry_after):
        super().__init__(json.dumps({"code": 200, "data": data}).encode())
        self.status = status
        self.retry_after = retry_after

    def getheader(self, name, default):
        return self.retry_after if name == "Retry-After" else default


@pytest.fixture
def transport(monkeypatch):
    requests = []
    replies = []

    class Connection:
        def __init__(self, host, timeout):
            assert host == "api.wavespeed.ai"
            assert timeout == 30

        def request(self, method, path, body, headers):
            requests.append((method, path, body, headers))

        def getresponse(self):
            reply = replies.pop(0)
            if isinstance(reply, BaseException):
                raise reply
            return Response(*reply)

        def close(self):
            pass

    monkeypatch.setattr("app.providers.wavespeed.http.client.HTTPSConnection", Connection)
    return requests, replies


@pytest.fixture
def provider(configuration, monkeypatch):
    for name, value in {
        "WAVESPEED_ENABLED": "true",
        "WAVESPEED_DRY_RUN": "false",
        "WAVESPEED_LIVE_TEST_ENABLED": "true",
        "WAVESPEED_API_KEY": "mock-api-key",
        "WAVESPEED_BASE_URL": "https://api.wavespeed.ai",
        "WAVESPEED_RESOLUTION": "720p",
        "WAVESPEED_DURATION_SECONDS": "5",
        "WAVESPEED_GENERATE_AUDIO": "false",
        "WAVESPEED_ENABLE_PROMPT_EXPANSION": "false",
        "WAVESPEED_HTTP_TIMEOUT_SECONDS": "30",
    }.items():
        monkeypatch.setenv(name, value)
    return WaveSpeedVideoProvider(settings())


@pytest.fixture
def jpeg(tmp_path):
    image = tmp_path / "normalized.jpg"
    image.write_bytes(b"\xff\xd8\xfftest")
    return image


def test_submit_uploads_normalized_jpeg_and_uses_returned_url(provider, transport, jpeg):
    requests, replies = transport
    replies.extend([({"download_url": IMAGE}, 200, ""), ({"id": ID, "status": "created"}, 200, "")])
    assert provider.submit_generation(jpeg, "camera movement", provider.spec) == ID

    upload, submit = requests
    assert upload == (
        "POST", "/api/v3/media/upload/binary", jpeg.read_bytes(),
        {"Authorization": "Bearer mock-api-key", "Content-Type": "image/jpeg",
         "file": "reference.jpg"},
    )
    assert submit[:2] == ("POST", f"/api/v3/{MODEL}")
    assert submit[3] == {"Authorization": "Bearer mock-api-key", "Content-Type": "application/json"}
    assert json.loads(submit[2]) == {
        "duration": 5, "enable_prompt_expansion": False, "generate_audio": False,
        "image": IMAGE, "prompt": "camera movement", "resolution": "720p", "shot_type": "single",
    }
    assert provider.estimate_cost(provider.spec).total_usd == Decimal("0.1250")


@pytest.mark.parametrize(("status", "expected"), [
    ("created", TaskState.PENDING), ("processing", TaskState.RUNNING),
    ("completed", TaskState.SUCCEEDED), ("failed", TaskState.FAILED),
    ("cancelled", TaskState.CANCELED), ("timeout", TaskState.FAILED),
    ("deleted", TaskState.UNKNOWN),
])
def test_poll(provider, transport, status, expected):
    requests, replies = transport
    outputs = [{"unexpected": "object"}, VIDEO] if status == "completed" else []
    replies.append(({"id": ID, "status": status, "outputs": outputs,
                     "timings": {"inference": 5000}}, 200, ""))
    result = provider.get_task_status(ID)
    assert result == TaskResult(expected, VIDEO if status == "completed" else None)
    assert requests == [(
        "GET", f"/api/v3/predictions/{ID}/result", None, {"Authorization": "Bearer mock-api-key"}
    )]


@pytest.mark.parametrize("data", [
    {"id": "other-id", "status": "created"}, {"id": ID, "status": []},
    {"id": ID, "status": "completed", "outputs": ["https://127.0.0.1/video.mp4"]},
])
def test_bad_poll_response(provider, transport, data):
    transport[1].append((data, 200, ""))
    with pytest.raises(ProviderError) as error:
        provider.get_task_status(ID)
    assert error.value.retryable


def test_invalid_prediction_id_stops_before_network(provider, transport):
    with pytest.raises(ProviderError, match="WAVESPEED_INVALID_TASK"):
        provider.get_task_status("../invalid/path")
    assert not transport[0]


@pytest.mark.parametrize("reply", [
    TimeoutError(), ({}, 408, ""), ({}, 429, "47"), ({}, 503, ""),
    ({"id": "../invalid"}, 200, ""),
])
def test_uncertain_submit_is_a_single_post(provider, transport, jpeg, reply):
    requests, replies = transport
    replies.extend([({"download_url": IMAGE}, 200, ""), reply])
    with pytest.raises(ProviderError) as error:
        provider.submit_generation(jpeg, "camera movement", provider.spec)
    assert error.value.ambiguous
    assert not error.value.retryable
    assert [request[1] for request in requests] == [
        "/api/v3/media/upload/binary", f"/api/v3/{MODEL}",
    ]


def test_bad_upload_does_not_submit_prediction(provider, transport, jpeg):
    requests, replies = transport
    replies.append(({"download_url": "https://127.0.0.1/image.jpg"}, 200, ""))
    with pytest.raises(ProviderError, match="WAVESPEED_INVALID_UPLOAD_RESPONSE") as error:
        provider.submit_generation(jpeg, "camera movement", provider.spec)
    assert not error.value.ambiguous
    assert len(requests) == 1


def test_submit_gates_and_unpriced_profile_prevent_upload(provider, transport, jpeg):
    config = provider.config.model_copy(update={"wavespeed_live_test_enabled": False})
    disabled = WaveSpeedVideoProvider(config)
    with pytest.raises(ProviderError, match="WAVESPEED_SUBMISSION_DISABLED"):
        disabled.submit_generation(jpeg, "prompt", disabled.spec)

    config = provider.config.model_copy(update={"wavespeed_model": "unknown/model"})
    unknown = WaveSpeedVideoProvider(config)
    with pytest.raises(ProviderError, match="WAVESPEED_PRICE_UNAVAILABLE"):
        unknown.submit_generation(jpeg, "prompt", unknown.spec)
    assert not transport[0]


def test_api_key_cannot_be_sent_to_an_alternate_base_url(provider):
    config = provider.config.model_copy(update={
        "wavespeed_base_url": "https://api.wavespeed.ai.example.com",
    })
    with pytest.raises(ProviderError, match="WAVESPEED_INVALID_ENDPOINT"):
        WaveSpeedVideoProvider(config)


@pytest.mark.parametrize(("reply", "code", "retryable", "delay"), [
    (({}, 401, ""), "WAVESPEED_INVALID_API_KEY", False, 0),
    (({}, 403, ""), "WAVESPEED_ACCESS_DENIED", False, 0),
    (({}, 404, ""), "WAVESPEED_NOT_FOUND", True, 0),
    (({}, 408, ""), "WAVESPEED_REMOTE_UNAVAILABLE", True, 0),
    (({}, 429, "47"), "WAVESPEED_RATE_LIMIT", True, 47),
    (({}, 503, ""), "WAVESPEED_REMOTE_UNAVAILABLE", True, 0),
    (TimeoutError(), "WAVESPEED_NETWORK_ERROR", True, 0),
])
def test_poll_error_policy(provider, transport, reply, code, retryable, delay):
    transport[1].append(reply)
    with pytest.raises(ProviderError, match=code) as error:
        provider.get_task_status(ID)
    assert (error.value.retryable, error.value.retry_after) == (retryable, delay)


def test_download_is_bounded_and_unauthenticated(provider, tmp_path, monkeypatch):
    def download(url, target, max_bytes):
        assert str(url) == VIDEO
        assert max_bytes == 1024
        target.write_bytes(b"mp4")
        return "mov"

    monkeypatch.setattr("app.providers.wavespeed.download", download)
    target = tmp_path / "output.mp4"
    provider.download_result(TaskResult(TaskState.SUCCEEDED, VIDEO), target, 1024)
    assert target.read_bytes() == b"mp4"
    with pytest.raises(ProviderError, match="WAVESPEED_INVALID_RESULT"):
        provider.download_result(
            TaskResult(TaskState.SUCCEEDED, "https://127.0.0.1/video.mp4"), target, 1024
        )


def test_legacy_factory_uses_saved_endpoint_and_spec(provider):
    config = provider.config.model_copy(update={"video_provider": "wavespeed"})
    row = SimpleNamespace(
        provider="wan", endpoint="https://dashscope.aliyuncs.com",
        model="wan2.6-i2v-flash", resolution="1080P", duration_seconds=8,
    )
    legacy = hybrid.provider_for(config, row)
    assert legacy.name == "wan"
    assert legacy.endpoint == row.endpoint
    assert legacy.spec == GenerationSpec(row.model, row.resolution, row.duration_seconds)
    assert hybrid.provider_for(config).name == "wavespeed"


def test_budget_blocks_preflight_and_transport(provider, transport):
    job = SimpleNamespace(duration_seconds=40, estimated_cost_usd=None)
    request = SimpleNamespace(max_ai_cost_usd=Decimal("0.2499"))
    with pytest.raises(MediaError) as error:
        hybrid.initial_plan(None, job, request, provider, provider.config, 2)
    assert error.value.code == "AI_BUDGET_EXCEEDED"
    assert job.estimated_cost_usd == Decimal("0.2500")
    assert not transport[0]


@pytest.mark.parametrize("source_kind", ["url", "uuid"])
def test_wave_resume_keeps_prediction_and_billing_uncertainty(
    migrated, client, payload, headers, provider, transport, tmp_path, monkeypatch, source_kind,
):
    monkeypatch.setenv("VIDEO_PROVIDER", "wavespeed")
    monkeypatch.setenv("MEDIA_ROOT", str(tmp_path))
    settings.cache_clear()
    image = artwork("JPEG", "#367ace")
    if source_kind == "uuid":
        payload["images"] = [upload(client, headers, image, "image/jpeg")]
    else:
        def download_reference(url, target, max_bytes):
            assert str(url) == payload["images"][0]
            assert len(image) <= max_bytes
            target.write_bytes(image)
            return "jpeg"

        monkeypatch.setattr("app.services.assets.download", download_reference)

    payload["config"]["mode"] = "HYBRID"
    payload.update(aiScenes=1, maxAiCostUsd="0.1250", allowLocalFallback=False)
    requests, replies = transport
    replies.extend([({"download_url": IMAGE}, 200, ""), ({"id": ID}, 200, "")])
    job_id = UUID(create(client, payload, headers))
    execution.execute_job(job_id)
    assert state(job_id)[2][0].task_id == ID
    assert requests[0][2].startswith(b"\xff\xd8")

    monkeypatch.setenv("VIDEO_PROVIDER", "wan")
    monkeypatch.setenv("WAVESPEED_ENABLED", "false")
    settings.cache_clear()
    ready(job_id)
    replies.append(({"id": ID, "status": "failed"}, 200, ""))
    execution.execute_job(job_id)
    job, dispatch, rows = state(job_id)
    assert [call[0] for call in requests] == ["POST", "POST", "GET"]
    assert rows[0].provider == "wavespeed"
    assert job.status == "FAILED"
    assert job.error_code == "WAVESPEED_TASK_FAILED"
    assert job.ai_cost_uncertain
    assert dispatch is None


def test_completed_prediction_renders_and_publishes_mp4(
    migrated, client, payload, headers, provider, transport, tmp_path, monkeypatch,
):
    monkeypatch.setenv("VIDEO_PROVIDER", "wavespeed")
    monkeypatch.setenv("MEDIA_ROOT", str(tmp_path))
    settings.cache_clear()
    fixture = tmp_path / "generated.mp4"
    ffmpeg([
        "-f", "lavfi", "-i", "testsrc2=size=320x240:rate=30",
        "-t", "5", "-c:v", "libx264", "-threads", "2", str(fixture),
    ])

    def download_video(url, target, limit):
        assert str(url) == VIDEO
        assert fixture.stat().st_size <= limit
        atomic_copy(fixture, target)
        return "mov"

    monkeypatch.setattr("app.providers.wavespeed.download", download_video)
    payload["images"] = [upload(client, headers, artwork("JPEG", "#367ace"), "image/jpeg")]
    payload["config"]["mode"] = "HYBRID"
    payload.update(aiScenes=1, maxAiCostUsd="0.1250", allowLocalFallback=False)
    requests, replies = transport
    replies.extend([
        ({"download_url": IMAGE}, 200, ""),
        ({"id": ID, "status": "created"}, 200, ""),
        ({"id": ID, "status": "completed", "outputs": [VIDEO]}, 200, ""),
    ])

    job_id = UUID(create(client, payload, headers))
    execution.execute_job(job_id)
    assert state(job_id)[0].status == "PROCESSING"
    ready(job_id)
    execution.execute_job(job_id)
    data = client.get(f"/api/v1/videos/{job_id}", headers=headers).json()
    assert data["status"] == "COMPLETED", data
    assert data["executionMode"] == "HYBRID"
    assert data["actualCostUsd"] == "0.1250"
    assert data["aiCostUncertain"]
    response = client.get(data["outputUrl"], headers=headers)
    assert response.status_code == 200
    final = tmp_path / "final.mp4"
    final.write_bytes(response.content)
    assert float(probe(final)["format"]["duration"]) == pytest.approx(30, abs=0.2)
    assert [call[0] for call in requests] == ["POST", "POST", "GET"]


def test_restart_after_prediction_post_does_not_repost(
    migrated, client, payload, headers, provider, transport, tmp_path, monkeypatch,
):
    monkeypatch.setenv("VIDEO_PROVIDER", "wavespeed")
    monkeypatch.setenv("MEDIA_ROOT", str(tmp_path))
    settings.cache_clear()
    payload["images"] = [upload(client, headers, artwork("JPEG", "#367ace"), "image/jpeg")]
    payload["config"]["mode"] = "HYBRID"
    payload.update(aiScenes=1, maxAiCostUsd="0.1250", allowLocalFallback=False)
    requests, replies = transport
    replies.extend([({"download_url": IMAGE}, 200, ""), SystemExit("worker lost")])
    job_id = UUID(create(client, payload, headers))

    with pytest.raises(SystemExit):
        execution.execute_job(job_id)
    assert state(job_id)[2][0].state == "SUBMITTING"
    ready(job_id)
    execution.execute_job(job_id)

    job, dispatch, rows = state(job_id)
    assert [call[1] for call in requests] == ["/api/v3/media/upload/binary", f"/api/v3/{MODEL}"]
    assert job.error_code == "WAVESPEED_SUBMIT_UNCERTAIN"
    assert job.ai_cost_uncertain
    assert rows[0].task_id is None
    assert dispatch is None


@pytest.mark.parametrize("mode", ["LOCAL", "HYBRID"])
def test_local_path_never_calls_provider_http(
    migrated, client, payload, headers, provider, transport, tmp_path, monkeypatch, mode,
):
    monkeypatch.setenv("VIDEO_PROVIDER", "wavespeed")
    monkeypatch.setenv("WAVESPEED_DRY_RUN", "true")
    monkeypatch.setenv("MEDIA_ROOT", str(tmp_path))
    settings.cache_clear()
    payload["images"] = [upload(client, headers, artwork("JPEG", "#367ace"), "image/jpeg")]
    payload["config"]["mode"] = mode
    if mode == "HYBRID":
        payload.update(aiScenes=1, maxAiCostUsd="0.1250")
    job_id = UUID(create(client, payload, headers))
    execution.execute_job(job_id)
    job, _, rows = state(job_id)
    assert job.status == "COMPLETED"
    assert job.execution_mode == "LOCAL"
    assert job.fallback_reason == ("WAVESPEED_DRY_RUN" if mode == "HYBRID" else None)
    assert not rows
    assert not transport[0]

