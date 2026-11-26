import json
from io import BytesIO

import pytest
from pydantic import SecretStr

from app.core.config import settings
from app.core.exceptions import MediaError
from app.providers.groq import GroqClient
from app.schemas.storyboard import DirectorStoryboard

MODEL = "openai/gpt-oss-120b"
MODEL_LIST = {"data": [{"id": MODEL, "active": True, "max_completion_tokens": 2048}]}
COMPLETION = {"choices": [{"finish_reason": "stop", "message": {"content": "{}"}}],
              "usage": {"total_tokens": 250}}


@pytest.fixture
def config(configuration):
    return settings().model_copy(update={
        "groq_model": MODEL, "groq_api_key": SecretStr("mock-groq-key"),
        "groq_base_url": "https://api.groq.com/openai/v1", "groq_max_retries": 1,
        "groq_max_completion_tokens": 4096,
    })


@pytest.fixture
def transport(monkeypatch):
    calls, replies, sleeps = [], [], []

    class Response(BytesIO):
        def getheader(self, name, default):
            return self.retry_after if name == "Retry-After" else default

    class Connection:
        def __init__(self, host, timeout):
            assert host == "api.groq.com"
            assert 0 < timeout <= 120

        def request(self, method, path, body, headers):
            calls.append((method, path, body, headers))

        def getresponse(self):
            status, body, wait = replies.pop(0)
            response = Response(json.dumps(body).encode() if isinstance(body, dict) else body)
            response.status, response.retry_after = status, wait
            return response

        def close(self):
            pass

    monkeypatch.setattr("app.providers.groq.http.client.HTTPSConnection", Connection)
    monkeypatch.setattr("app.providers.groq.sleep", sleeps.append)
    return calls, replies, sleeps


def test_model_catalog_auth_and_strict_completion(config, transport):
    calls, replies, _ = transport
    replies.extend([(200, MODEL_LIST, ""), (200, COMPLETION, "")])
    schema = DirectorStoryboard.model_json_schema()
    content, usage = GroqClient(config).complete([], schema)
    assert content == "{}"
    assert usage == {"total_tokens": 250}
    assert [call[:2] for call in calls] == [
        ("GET", "/openai/v1/models"), ("POST", "/openai/v1/chat/completions"),
    ]
    assert all(call[3]["Authorization"] == "Bearer mock-groq-key" for call in calls)
    payload = json.loads(calls[-1][2])
    assert payload["max_completion_tokens"] == 2048
    assert payload["response_format"]["json_schema"] == {
        "name": "travel_storyboard", "strict": True, "schema": schema,
    }
    assert set(schema["required"]) == set(schema["properties"])
    scene = schema["$defs"]["DirectorScene"]
    assert scene["additionalProperties"] is False
    assert set(scene["required"]) == set(scene["properties"])
    assert "tools" not in payload


def test_non_strict_model_uses_json_mode(config, transport):
    calls, replies, _ = transport
    model = "approved-model"
    replies.extend([(200, {"data": [{"id": model}]}, ""), (200, COMPLETION, "")])
    GroqClient(config.model_copy(update={"groq_model": model})).complete([], {})
    assert json.loads(calls[-1][2])["response_format"] == {"type": "json_object"}


@pytest.mark.parametrize(("reply", "code"), [
    ((401, {}, ""), "GROQ_AUTH_FAILED"),
    ((403, {}, ""), "GROQ_ACCESS_DENIED"),
    ((404, {}, ""), "GROQ_MODEL_UNAVAILABLE"),
    ((200, {"data": [{"id": MODEL, "active": False}]}, ""), "GROQ_MODEL_UNAVAILABLE"),
    ((200, b"not JSON", ""), "GROQ_INVALID_RESPONSE"),
])
def test_catalog_failures_do_not_send_completion(config, transport, reply, code, capsys):
    calls, replies, _ = transport
    replies.append(reply)
    with pytest.raises(MediaError) as error:
        GroqClient(config).complete([], {})
    assert error.value.code == code
    assert len(calls) == 1
    assert "mock-groq-key" not in str(error.value)
    assert "mock-groq-key" not in capsys.readouterr().out


def test_rate_limit_and_token_limit(config, transport):
    calls, replies, sleeps = transport
    replies.extend([
        (200, MODEL_LIST, ""), (429, {}, "2"),
        (200, {"choices": [{"finish_reason": "length"}]}, ""),
    ])
    with pytest.raises(MediaError) as error:
        GroqClient(config).complete([], {})
    assert error.value.code == "GROQ_TOKEN_LIMIT"
    assert sleeps == [2]
    assert [call[0] for call in calls] == ["GET", "POST", "POST"]


def test_config_rejects_missing_key_and_untrusted_host(config):
    for update, code in [
        ({"groq_api_key": None}, "GROQ_MISSING_KEY"),
        (
            {"groq_base_url": "https://api.groq.com@other.example/openai/v1"},
            "GROQ_INVALID_ENDPOINT",
        ),
    ]:
        with pytest.raises(MediaError) as error:
            GroqClient(config.model_copy(update=update))
        assert error.value.code == code

