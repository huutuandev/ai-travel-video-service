import http.client
import json
import re
from base64 import b64encode
from decimal import ROUND_CEILING, Decimal
from urllib.parse import urlsplit

from pydantic import HttpUrl, ValidationError

from app.core.config import Settings
from app.core.exceptions import MediaError, RetryableExecutionError
from app.providers.base import CostEstimate, GenerationSpec, ProviderError, TaskResult, TaskState
from app.services.assets import download

API_PREFIX = "/api/v1"
SUBMIT_PATH = API_PREFIX + "/services/aigc/video-generation/video-synthesis"
TASK_ID = re.compile(r"[a-zA-Z0-9_-]{1,128}")
WORKSPACE = re.compile(r"[a-z0-9-]+\.(cn-beijing|ap-southeast-1)\.maas\.aliyuncs\.com")
RESULT_HOST = re.compile(
    r"dashscope-result-[a-z0-9-]+\."
    r"oss-(?:accelerate(?:-overseas)?|cn-beijing|ap-southeast-1)\.aliyuncs\.com"
)
PRICES = {
    ("ap-southeast-1", "720P"): Decimal("0.025"),
    ("ap-southeast-1", "1080P"): Decimal("0.0375"),
    ("cn-beijing", "720P"): Decimal("0.021503"),
    ("cn-beijing", "1080P"): Decimal("0.035838"),
}


def endpoint_region(host):
    legacy = {
        "dashscope-intl.aliyuncs.com": "ap-southeast-1",
        "dashscope.aliyuncs.com": "cn-beijing",
    }
    match = WORKSPACE.fullmatch(host)
    return legacy.get(host) or (match[1] if match else None)


def money(seconds: Decimal, rate: Decimal) -> Decimal:
    return (seconds * rate).quantize(Decimal("0.0001"), rounding=ROUND_CEILING)


class WanVideoProvider:
    name = "wan"

    def __init__(self, config: Settings):
        self.config = config
        try:
            parsed = urlsplit(config.wan_base_url)
            self.host = parsed.hostname or ""
            self.region = endpoint_region(self.host)
            if (
                parsed.scheme != "https" or parsed.username or parsed.password
                or parsed.port not in (None, 443) or not self.region
                or parsed.path.rstrip("/") not in ("", API_PREFIX)
                or parsed.query or parsed.fragment
            ):
                raise ValueError
        except ValueError:
            raise ProviderError("WAN_INVALID_ENDPOINT") from None
        self.endpoint = f"https://{self.host}"
        self.spec = GenerationSpec(
            config.wan_model, config.wan_resolution, config.wan_default_duration
        )

    def estimate_cost(self, spec):
        public_rate = PRICES.get((self.region, spec.resolution))
        if public_rate is None or spec.model != self.config.wan_model:
            raise ProviderError("WAN_PRICE_UNAVAILABLE")
        rate = max(public_rate, self.config.wan_price_per_second_usd or public_rate)
        return CostEstimate(rate, money(Decimal(spec.duration_seconds), rate))

    def submit_generation(self, image, prompt, spec):
        config = self.config
        if not config.wan_enabled or config.wan_dry_run or not config.wan_live_test_enabled:
            raise ProviderError("WAN_SUBMISSION_DISABLED")
        if spec != self.spec:
            raise ProviderError("WAN_CONFIG_CHANGED")
        encoded = b64encode(image.read_bytes()).decode("ascii")
        result = self._request("POST", SUBMIT_PATH, {
            "model": spec.model,
            "input": {"img_url": f"data:image/jpeg;base64,{encoded}", "prompt": prompt},
            "parameters": {
                "resolution": spec.resolution, "duration": spec.duration_seconds,
                "audio": False, "watermark": False, "prompt_extend": False, "shot_type": "single",
            },
        })
        output = result.get("output")
        identifier = output.get("task_id") if isinstance(output, dict) else None
        if not isinstance(identifier, str) or not TASK_ID.fullmatch(identifier):
            raise ProviderError("WAN_SUBMIT_UNCERTAIN", ambiguous=True)
        return identifier

    def get_task_status(self, task_id):
        if not isinstance(task_id, str) or not TASK_ID.fullmatch(task_id):
            raise ProviderError("WAN_INVALID_TASK")
        result = self._request("GET", f"{API_PREFIX}/tasks/{task_id}")
        try:
            output = result["output"]
            state = TaskState(output["task_status"])
            response_id = output.get("task_id")
            if response_id != task_id and not (state == TaskState.UNKNOWN and response_id is None):
                raise ValueError
        except (KeyError, TypeError, ValueError):
            raise ProviderError("WAN_INVALID_RESPONSE", retryable=True) from None

        usage = result.get("usage")
        value = usage.get("output_video_duration") if isinstance(usage, dict) else None
        if value is None and isinstance(usage, dict) and usage.get("input_video_duration") == 0:
            value = usage.get("duration")
        billed = Decimal(value) if type(value) in (Decimal, int) else None
        if billed is not None and (not billed.is_finite() or billed <= 0 or billed > 86400):
            billed = None
        url = output.get("video_url")
        return TaskResult(state, url if isinstance(url, str) else None, billed)

    def download_result(self, result, target, max_bytes):
        try:
            url = HttpUrl(result.download_url)
        except (ValidationError, TypeError):
            raise ProviderError("WAN_INVALID_RESULT_URL") from None
        if (
            url.scheme != "https" or url.port != 443 or url.username or url.password
            or url.fragment or not RESULT_HOST.fullmatch(url.host or "")
        ):
            raise ProviderError("WAN_INVALID_RESULT_URL")
        try:
            if download(url, target, max_bytes) != "mov":
                raise ProviderError("WAN_INVALID_RESULT")
        except RetryableExecutionError:
            raise ProviderError("WAN_DOWNLOAD_TEMPORARY", retryable=True) from None
        except MediaError:
            raise ProviderError("WAN_INVALID_RESULT") from None

    def _request(self, method, path, payload=None):
        key = self.config.wan_api_key
        if key is None or not key.get_secret_value().strip():
            raise ProviderError("WAN_INVALID_API_KEY")
        headers = {"Authorization": f"Bearer {key.get_secret_value()}"}
        if payload is not None:
            headers.update({"X-DashScope-Async": "enable", "Content-Type": "application/json"})
        connection = http.client.HTTPSConnection(
            self.host, timeout=self.config.wan_http_timeout_seconds
        )
        try:
            connection.request(method, path, json.dumps(payload) if payload is not None else None,
                               headers)
            with connection.getresponse() as response:
                status = response.status
                retry_after = response.getheader("Retry-After", "")
                raw = response.read(262145)
        except (OSError, ValueError, http.client.HTTPException):
            if method == "POST":
                raise ProviderError("WAN_SUBMIT_UNCERTAIN", ambiguous=True) from None
            raise ProviderError("WAN_NETWORK_ERROR", retryable=True) from None
        finally:
            connection.close()

        document = None
        if len(raw) <= 262144:
            try:
                document = json.loads(raw, parse_float=Decimal)
            except (ValueError, RecursionError):
                pass
        code = document.get("code") if isinstance(document, dict) else None
        output = document.get("output") if isinstance(document, dict) else None
        # A returned task ID takes precedence over an unusual submit HTTP status.
        task_id = output.get("task_id") if isinstance(output, dict) else None
        if method == "POST" and isinstance(task_id, str) and TASK_ID.fullmatch(task_id):
            return document
        if status in (401, 403) or code in ("InvalidApiKey", "InvalidApiKeyError"):
            raise ProviderError("WAN_INVALID_API_KEY")
        if status == 429:
            wait = min(int(retry_after), 3600) if re.fullmatch(r"[0-9]{1,5}", retry_after) else 0
            raise ProviderError(
                "WAN_RATE_LIMIT", retryable=method == "GET", ambiguous=method == "POST",
                retry_after=wait,
            )
        if method == "POST" and status in (400, 404, 422):
            raise ProviderError("WAN_SUBMIT_REJECTED")
        if method == "POST" and not 200 <= status < 300:
            raise ProviderError("WAN_SUBMIT_UNCERTAIN", ambiguous=True)
        if status == 404:
            return {"output": {"task_id": path.rsplit("/", 1)[1], "task_status": "UNKNOWN"}}
        if status >= 500 or status == 408:
            raise ProviderError("WAN_NETWORK_ERROR", retryable=True)
        if not isinstance(document, dict) or not 200 <= status < 300:
            raise ProviderError(
                "WAN_INVALID_RESPONSE", ambiguous=method == "POST", retryable=method == "GET"
            )
        return document

