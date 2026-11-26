import json
from decimal import Decimal
from io import BytesIO

import pytest
from pydantic import SecretStr

from app.core.config import settings
from app.providers import wan
from app.providers.base import ProviderError, TaskResult, TaskState


class Response(BytesIO):
    def __init__(self, status, data, retry_after=""):
        super().__init__(json.dumps(data).encode())
        self.status = status
        self.retry_after = retry_after

    def getheader(self, name, default=None):
        assert name == "Retry-After"
        return self.retry_after or default


@pytest.fixture
def provider(configuration):
    config = settings().model_copy(update={
        "wan_enabled": True, "wan_dry_run": False, "wan_live_test_enabled": True,
        "wan_api_key": SecretStr("test-secret"),
        "wan_base_url": "https://llm-test.ap-southeast-1.maas.aliyuncs.com/api/v1",
        "wan_model": "wan2.6-i2v-flash", "wan_resolution": "720P", "wan_default_duration": 5,
        "wan_price_per_second_usd": None,
    })
    return wan.WanVideoProvider(config)


def transport(monkeypatch, reply):
    calls = []

    class Connection:
        def __init__(self, host, timeout):
            self.host = host
            assert timeout > 0

        def request(self, method, path, payload, headers):
            calls.append((self.host, method, path, payload, headers))
            if isinstance(reply, Exception):
                raise reply

        def getresponse(self):
            return reply

        def close(self):
            pass

    monkeypatch.setattr(wan.http.client, "HTTPSConnection", Connection)
    return calls


def test_submit_contract(provider, monkeypatch, tmp_path):
    fake = {"output": {"task_id": "remote-42", "task_status": "PENDING"}}
    calls = transport(monkeypatch, Response(200, fake))
    image = tmp_path / "reference.jpg"
    image.write_bytes(b"jpeg fixture bytes")
    assert provider.submit_generation(image, "Camera movement", provider.spec) == "remote-42"
    host, method, path, encoded, headers = calls[0]
    assert host == "llm-test.ap-southeast-1.maas.aliyuncs.com"
    assert method == "POST"
    assert path == "/api/v1/services/aigc/video-generation/video-synthesis"
    assert headers["X-DashScope-Async"] == "enable"
    assert headers["Authorization"] == "Bearer test-secret"
    payload = json.loads(encoded)
    assert payload["input"]["img_url"].startswith("data:image/jpeg;base64,")
    assert payload["model"] == "wan2.6-i2v-flash"
    assert payload["parameters"] == {
        "resolution": "720P", "duration": 5, "audio": False, "watermark": False,
        "shot_type": "single", "prompt_extend": False,
    }


@pytest.mark.parametrize("state", list(TaskState))
def test_poll_task_states(provider, monkeypatch, state):
    data = {"output": {"task_id": "remote-42", "task_status": state}}
    if state == TaskState.SUCCEEDED:
        data["usage"] = {"output_video_duration": 5.0}
        data["output"]["video_url"] = "https://dashscope-result-bj.oss-cn-beijing.aliyuncs.com/v.mp4"
    calls = transport(monkeypatch, Response(200, data))
    result = provider.get_task_status("remote-42")
    assert result.state == state
    if state == TaskState.SUCCEEDED:
        assert result.billed_seconds == Decimal(5)
        assert result.download_url == data["output"]["video_url"]
    assert calls[0][1:3] == ("GET", "/api/v1/tasks/remote-42")


def test_submit_transport_timeout_is_ambiguous_and_single_attempt(provider, monkeypatch, tmp_path):
    file = tmp_path / "image.jpg"
    file.write_bytes(b"file")
    calls = transport(monkeypatch, TimeoutError())
    with pytest.raises(ProviderError, match="WAN_SUBMIT_UNCERTAIN") as err:
        provider.submit_generation(file, "prompt", provider.spec)
    assert err.value.ambiguous and not err.value.retryable
    assert len(calls) == 1


def test_rate_limit_and_invalid_key(provider, monkeypatch):
    transport(monkeypatch, Response(429, {"code": "Throttling"}, retry_after="45"))
    with pytest.raises(ProviderError, match="WAN_RATE_LIMIT") as err:
        provider.get_task_status("remote-42")
    assert err.value.retryable and err.value.retry_after == 45
    transport(monkeypatch, Response(401, {"code": "InvalidApiKey", "message": "sensitive"}))
    with pytest.raises(ProviderError, match="^WAN_INVALID_API_KEY$"):
        provider.get_task_status("remote-42")


@pytest.mark.parametrize("changes", [
    {"wan_enabled": False}, {"wan_dry_run": True}, {"wan_live_test_enabled": False},
])
def test_paid_submission_requires_all_flags(provider, monkeypatch, tmp_path, changes):
    calls = transport(monkeypatch, AssertionError("network must not be called"))
    disabled = wan.WanVideoProvider(provider.config.model_copy(update=changes))
    with pytest.raises(ProviderError, match="WAN_SUBMISSION_DISABLED"):
        disabled.submit_generation(tmp_path / "unused.jpg", "prompt", disabled.spec)
    assert calls == []


def test_regional_price_and_untrusted_endpoint(provider):
    sg = provider.estimate_cost(provider.spec)
    assert sg.total_usd == Decimal("0.125")
    bj = wan.WanVideoProvider(provider.config.model_copy(update={
        "wan_base_url": "https://dashscope.aliyuncs.com", "wan_resolution": "1080P",
    }))
    assert bj.estimate_cost(bj.spec).total_usd == Decimal("0.1792")
    with pytest.raises(ProviderError, match="WAN_INVALID_ENDPOINT"):
        wan.WanVideoProvider(provider.config.model_copy(update={
            "wan_base_url": "https://dashscope.aliyuncs.com.example.org",
        }))


@pytest.mark.parametrize("url", [
    "https://localhost/test.mp4",
    "https://dashscope-result-bj.oss-cn-beijing.aliyuncs.com.evil.example/test",
    "https://user@dashscope-result-bj.oss-cn-beijing.aliyuncs.com/test",
    "http://dashscope-result-bj.oss-cn-beijing.aliyuncs.com/test",
])
def test_unsafe_result_links_never_download(provider, tmp_path, monkeypatch, url):
    def unexpected(*args):
        pytest.fail("Untrusted URL would have been contacted")

    monkeypatch.setattr(wan, "download", unexpected)
    with pytest.raises(ProviderError, match="WAN_INVALID_RESULT_URL"):
        provider.download_result(TaskResult(TaskState.SUCCEEDED, url), tmp_path / "x", 20)


def test_result_download_uses_size_limit(provider, tmp_path, monkeypatch):
    targets = []

    def transfer(url, target, limit):
        targets.append((url.host, target, limit))
        return "mov"

    monkeypatch.setattr(wan, "download", transfer)
    url = "https://dashscope-result-sg.oss-ap-southeast-1.aliyuncs.com/result.mp4?Expires=1"
    target = tmp_path / "out"
    provider.download_result(TaskResult(TaskState.SUCCEEDED, url), target, 200)
    assert targets == [("dashscope-result-sg.oss-ap-southeast-1.aliyuncs.com", target, 200)]

