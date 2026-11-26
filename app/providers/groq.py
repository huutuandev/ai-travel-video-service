import http.client
import json
from time import monotonic, sleep
from urllib.parse import urlsplit

from app.core.exceptions import MediaError

HOST = "api.groq.com"
PREFIX = "/openai/v1"
STRICT_MODELS = {
    "openai/gpt-oss-20b", "openai/gpt-oss-120b", "qwen/qwen3.8-27b",
    "meta-models/muse-spark-open",
}
MAX_RESPONSE_BYTES = 262144


class GroqClient:
    def __init__(self, config):
        self.config = config
        try:
            url = urlsplit(config.groq_base_url)
            if (
                url.scheme != "https" or url.netloc not in (HOST, f"{HOST}:443")
                or url.path.rstrip("/") != PREFIX or url.query or url.fragment
            ):
                raise ValueError
        except ValueError:
            raise MediaError(
                "GROQ_INVALID_ENDPOINT", "Groq endpoint must use api.groq.com"
            ) from None
        key = config.groq_api_key
        if key is None or not key.get_secret_value().strip():
            raise MediaError("GROQ_MISSING_KEY", "Configure GROQ_API_KEY in the worker")
        token = key.get_secret_value().strip()
        if not token.isascii() or any(not 33 <= ord(char) <= 126 for char in token):
            raise MediaError("GROQ_AUTH_FAILED", "Check GROQ_API_KEY")
        self.headers = {"Authorization": f"Bearer {token}"}

    def complete(self, messages, schema):
        deadline = monotonic() + min(
            self.config.groq_timeout_seconds, max(1, self.config.task_soft_timeout - 5)
        )
        model = self.config.groq_model
        catalog = self._request("GET", "/models", deadline)
        models = catalog.get("data")
        if not isinstance(models, list):
            raise MediaError("GROQ_INVALID_RESPONSE", "Invalid Groq model list")
        available = next((
            item for item in models
            if isinstance(item, dict) and item.get("id") == model
            and item.get("active") is not False
        ), None)
        if available is None:
            raise MediaError(
                "GROQ_MODEL_UNAVAILABLE", "Choose GROQ_MODEL from the account's /models listing"
            )
        limit = self.config.groq_max_completion_tokens
        model_limit = available.get("max_completion_tokens")
        if type(model_limit) is int and model_limit > 0:
            limit = min(limit, model_limit)
        format_spec = {"type": "json_object"}
        if model in STRICT_MODELS:
            format_spec = {
                "type": "json_schema",
                "json_schema": {"name": "travel_storyboard", "strict": True, "schema": schema},
            }
        response = self._request("POST", "/chat/completions", deadline, {
            "model": model,
            "messages": messages,
            "response_format": format_spec,
            "max_completion_tokens": limit,
        })
        try:
            choice = response["choices"][0]
            if choice["finish_reason"] == "length":
                raise MediaError("GROQ_TOKEN_LIMIT", "Groq completion reached its token limit")
            if choice["finish_reason"] != "stop" or choice["message"].get("tool_calls"):
                raise ValueError
            content = choice["message"]["content"]
            if not isinstance(content, str):
                raise ValueError
        except (AttributeError, IndexError, KeyError, TypeError, ValueError):
            raise MediaError("GROQ_INVALID_RESPONSE", "Invalid Groq completion") from None
        usage = response.get("usage")
        tokens = {
            name: usage[name] for name in ("prompt_tokens", "completion_tokens", "total_tokens")
            if isinstance(usage, dict) and type(usage.get(name)) is int and usage[name] >= 0
        }
        return content, tokens

    def _request(self, method, path, deadline, payload=None):
        headers = self.headers.copy()
        body = None
        if payload is not None:
            headers["Content-Type"] = "application/json"
            body = json.dumps(payload, ensure_ascii=False).encode()
        for attempt in range(self.config.groq_max_retries + 1):
            remaining = deadline - monotonic()
            if remaining <= 0:
                raise MediaError("GROQ_TIMEOUT", "Groq deadline exceeded")
            connection = http.client.HTTPSConnection(HOST, timeout=remaining)
            delay = 0
            retryable = False
            try:
                connection.request(method, PREFIX + path, body, headers)
                with connection.getresponse() as response:
                    status = response.status
                    if 200 <= status < 300:
                        data = bytearray()
                        while len(data) <= MAX_RESPONSE_BYTES:
                            if monotonic() >= deadline:
                                raise TimeoutError
                            chunk = response.read1(min(8192, MAX_RESPONSE_BYTES + 1 - len(data)))
                            if not chunk:
                                break
                            data.extend(chunk)
                        return self._decode(data)
                    if status == 429:
                        retry_after = response.getheader("Retry-After", "")
                        if (
                            retry_after.isascii() and retry_after.isdecimal()
                            and len(retry_after) < 6
                        ):
                            delay = int(retry_after)
                    retryable = status in (408, 429) or status >= 500
                    code, message = {
                        401: ("GROQ_AUTH_FAILED", "Check GROQ_API_KEY"),
                        403: ("GROQ_ACCESS_DENIED", "Check account and model permissions"),
                        404: ("GROQ_MODEL_UNAVAILABLE", "Choose GROQ_MODEL from /models"),
                        408: ("GROQ_TIMEOUT", "Groq request timed out"),
                        429: ("GROQ_RATE_LIMIT", "Groq quota or rate limit reached"),
                    }.get(status, ("GROQ_HTTP_ERROR", "Groq rejected the request"))
            except (OSError, http.client.HTTPException):
                retryable = True
                code, message = "GROQ_TIMEOUT", "Groq transport failed or timed out"
            finally:
                connection.close()

            delay = max(delay, 2 ** attempt)
            if not retryable or attempt == self.config.groq_max_retries or (
                monotonic() + delay >= deadline
            ):
                raise MediaError(code, message)
            sleep(delay)

    @staticmethod
    def _decode(raw):
        if len(raw) <= MAX_RESPONSE_BYTES:
            try:
                result = json.loads(raw)
                if isinstance(result, dict):
                    return result
            except (ValueError, RecursionError):
                pass
        raise MediaError("GROQ_INVALID_RESPONSE", "Invalid Groq JSON response")

