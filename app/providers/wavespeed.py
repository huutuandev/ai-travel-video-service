import http.client
import json
import re
from decimal import ROUND_CEILING, Decimal
from pathlib import Path
from urllib.parse import urlsplit

from pydantic import HttpUrl

from app.core.config import Settings
from app.core.exceptions import MediaError, RetryableExecutionError
from app.core.media_urls import validate_image_url
from app.providers.base import CostEstimate, GenerationSpec, ProviderError, TaskResult, TaskState
from app.services.assets import download

HOST = "api.wavespeed.ai"
MODEL = "alibaba/wan-2.6/image-to-video-flash"
PREDICTION_ID = re.compile(r"[A-Za-z0-9_-]{1,128}")
# https://wavespeed.ai/docs/docs-api/alibaba/wan-2.6/image-to-video-flash
RATES = {
    ("720p", False): Decimal("0.025"),
    ("720p", True): Decimal("0.05"),
    ("1080p", False): Decimal("0.0375"),
    ("1080p", True): Decimal("0.075"),
}
STATES = {
    "created": TaskState.PENDING,
    "processing": TaskState.RUNNING,
    "completed": TaskState.SUCCEEDED,
    "failed": TaskState.FAILED,
    "cancelled": TaskState.CANCELED,
    "timeout": TaskState.FAILED,
    "deleted": TaskState.UNKNOWN,
}


def checked_url(value: object) -> HttpUrl:
    if not isinstance(value, str):
        raise ValueError("Expected a URL string")
    url = validate_image_url(HttpUrl(value))
    if url.fragment is not None:
        raise ValueError("Unexpected URL fragment")
    return url


class WaveSpeedVideoProvider:
    name = "wavespeed"

    def __init__(self, config: Settings):
        self.config = config
        try:
            parsed = urlsplit(config.wavespeed_base_url)
            if (
                parsed.scheme != "https" or parsed.hostname != HOST
                or parsed.port not in (None, 443) or parsed.username is not None
                or parsed.password is not None or parsed.path not in ("", "/")
                or parsed.query or parsed.fragment
            ):
                raise ValueError
        except ValueError:
            raise ProviderError("WAVESPEED_INVALID_ENDPOINT") from None
        self.endpoint = f"https://{HOST}"
        self.spec = GenerationSpec(
            config.wavespeed_model, config.wavespeed_resolution, config.wavespeed_duration_seconds
        )

    def estimate_cost(self, spec: GenerationSpec) -> CostEstimate:
        rate = RATES.get((spec.resolution, self.config.wavespeed_generate_audio))
        if spec.model != MODEL or rate is None or not 2 <= spec.duration_seconds <= 15:
            raise ProviderError("WAVESPEED_PRICE_UNAVAILABLE")
        total = (rate * spec.duration_seconds).quantize(Decimal("0.0001"), rounding=ROUND_CEILING)
        return CostEstimate(rate, total)

    def submit_generation(self, image: Path, prompt: str, spec: GenerationSpec) -> str:
        config = self.config
        if (
            not config.wavespeed_enabled or config.wavespeed_dry_run
            or not config.wavespeed_live_test_enabled
        ):
            raise ProviderError("WAVESPEED_SUBMISSION_DISABLED")
        self.estimate_cost(spec)
        image_url = self._upload_reference(image)
        payload = {
            "duration": spec.duration_seconds,
            "enable_prompt_expansion": config.wavespeed_enable_prompt_expansion,
            "generate_audio": config.wavespeed_generate_audio,
            "image": image_url,
            "prompt": prompt,
            "resolution": spec.resolution,
            "shot_type": "single",
        }
        data = self._request(
            "POST", f"/api/v3/{spec.model}", json.dumps(payload).encode(),
            content_type="application/json",
            prediction=True,
        )
        identifier = data.get("id")
        if not isinstance(identifier, str) or not PREDICTION_ID.fullmatch(identifier):
            raise ProviderError("WAVESPEED_SUBMIT_UNCERTAIN", ambiguous=True)
        return identifier

    def _upload_reference(self, image: Path) -> str:
        # The documented raw binary endpoint accepts the JPEG without a multipart dependency.
        limit = min(self.config.max_asset_bytes, 100 * 1024 * 1024)
        try:
            with image.open("rb") as source:
                jpeg = source.read(limit + 1)
        except OSError:
            raise ProviderError("WAVESPEED_REFERENCE_UNAVAILABLE") from None
        if not jpeg.startswith(b"\xff\xd8\xff") or len(jpeg) > limit:
            raise ProviderError("WAVESPEED_INVALID_REFERENCE")
        data = self._request(
            "POST", "/api/v3/media/upload/binary", jpeg,
            content_type="image/jpeg", filename="reference.jpg",
        )
        try:
            return str(checked_url(data.get("download_url")))
        except ValueError:
            raise ProviderError("WAVESPEED_INVALID_UPLOAD_RESPONSE") from None

    def get_task_status(self, task_id: str) -> TaskResult:
        if not isinstance(task_id, str) or not PREDICTION_ID.fullmatch(task_id):
            raise ProviderError("WAVESPEED_INVALID_TASK")
        data = self._request("GET", f"/api/v3/predictions/{task_id}/result")
        status = data.get("status")
        state = STATES.get(status) if isinstance(status, str) else None
        if data.get("id") != task_id or state is None:
            raise ProviderError("WAVESPEED_INVALID_RESPONSE", retryable=True)
        if state != TaskState.SUCCEEDED:
            return TaskResult(state)
        outputs = data.get("outputs")
        if isinstance(outputs, list):
            for output in outputs:
                try:
                    return TaskResult(state, str(checked_url(output)))
                except ValueError:
                    continue
        raise ProviderError("WAVESPEED_INVALID_RESULT", retryable=True)

    def download_result(self, result: TaskResult, target: Path, max_bytes: int) -> None:
        try:
            url = checked_url(result.download_url)
            if download(url, target, max_bytes) != "mov":
                raise ProviderError("WAVESPEED_INVALID_RESULT")
        except RetryableExecutionError:
            raise ProviderError("WAVESPEED_DOWNLOAD_TEMPORARY", retryable=True) from None
        except (ValueError, MediaError):
            raise ProviderError("WAVESPEED_INVALID_RESULT") from None

    def _request(
        self, method, path, body=None, *, content_type=None, filename=None, prediction=False
    ):
        key = self.config.wavespeed_api_key
        if key is None or not key.get_secret_value().strip():
            raise ProviderError("WAVESPEED_INVALID_API_KEY")
        headers = {"Authorization": f"Bearer {key.get_secret_value()}"}
        if content_type:
            headers["Content-Type"] = content_type
        if filename:
            headers["file"] = filename

        connection = http.client.HTTPSConnection(
            HOST, timeout=self.config.wavespeed_http_timeout_seconds
        )
        polling = method == "GET"
        try:
            connection.request(method, path, body, headers)
            with connection.getresponse() as response:
                status = response.status
                retry_after = response.getheader("Retry-After", "")
                raw = response.read(262145)
        except (OSError, ValueError, http.client.HTTPException):
            code = "WAVESPEED_SUBMIT_UNCERTAIN" if prediction else "WAVESPEED_NETWORK_ERROR"
            raise ProviderError(code, ambiguous=prediction, retryable=polling) from None
        finally:
            connection.close()

        if status == 401:
            raise ProviderError("WAVESPEED_INVALID_API_KEY")
        if status == 403:
            raise ProviderError("WAVESPEED_ACCESS_DENIED")
        if status == 429:
            wait = min(int(retry_after), 3600) if re.fullmatch(r"[0-9]{1,5}", retry_after) else 0
            raise ProviderError(
                "WAVESPEED_RATE_LIMIT", retryable=polling, ambiguous=prediction, retry_after=wait
            )
        if status == 404:
            raise ProviderError("WAVESPEED_NOT_FOUND", retryable=polling)
        if status == 408 or status >= 500:
            code = "WAVESPEED_SUBMIT_UNCERTAIN" if prediction else "WAVESPEED_REMOTE_UNAVAILABLE"
            raise ProviderError(code, ambiguous=prediction, retryable=polling)
        if status in (400, 402, 413, 422):
            raise ProviderError("WAVESPEED_REQUEST_REJECTED")

        if 200 <= status < 300 and len(raw) <= 262144:
            try:
                document = json.loads(raw)
                data = document.get("data") if isinstance(document, dict) else None
                if isinstance(data, dict) and document.get("code") in (200, 201):
                    return data
            except (ValueError, RecursionError):
                pass
        code = "WAVESPEED_SUBMIT_UNCERTAIN" if prediction else "WAVESPEED_INVALID_RESPONSE"
        raise ProviderError(code, ambiguous=prediction, retryable=polling)

