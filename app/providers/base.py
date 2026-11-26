from dataclasses import dataclass
from decimal import Decimal
from enum import StrEnum
from pathlib import Path
from typing import Protocol


class TaskState(StrEnum):
    PENDING = "PENDING"
    RUNNING = "RUNNING"
    SUCCEEDED = "SUCCEEDED"
    FAILED = "FAILED"
    CANCELED = "CANCELED"
    UNKNOWN = "UNKNOWN"


@dataclass(frozen=True)
class GenerationSpec:
    model: str
    resolution: str
    duration_seconds: int


@dataclass(frozen=True)
class CostEstimate:
    unit_price_usd: Decimal
    total_usd: Decimal


@dataclass(frozen=True)
class TaskResult:
    state: TaskState
    download_url: str | None = None
    billed_seconds: Decimal | None = None


class ProviderError(Exception):
    def __init__(self, code: str, *, retryable=False, ambiguous=False, retry_after=0):
        super().__init__(code)
        self.code = code
        self.retryable = retryable
        self.ambiguous = ambiguous
        self.retry_after = retry_after


class VideoProvider(Protocol):
    name: str
    endpoint: str
    spec: GenerationSpec

    def submit_generation(self, image: Path, prompt: str, spec: GenerationSpec) -> str: ...

    def get_task_status(self, task_id: str) -> TaskResult: ...

    def download_result(self, result: TaskResult, target: Path, max_bytes: int) -> None: ...

    def estimate_cost(self, spec: GenerationSpec) -> CostEstimate: ...

