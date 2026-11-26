from datetime import UTC, datetime, timedelta
from decimal import ROUND_CEILING, Decimal
from pathlib import Path
from tempfile import TemporaryDirectory

from PIL import Image, ImageOps
from sqlalchemy import func, select, text

from app.core.config import settings
from app.core.exceptions import MediaError
from app.models.video import Mode, Status, VideoAIGeneration
from app.providers.base import GenerationSpec, ProviderError, TaskState, VideoProvider
from app.providers.wan import WanVideoProvider
from app.providers.wavespeed import WaveSpeedVideoProvider
from app.renderers.ffmpeg import input_duration, probe
from app.schemas.storyboard import asset_options
from app.services.ai_director import get_storyboard
from app.services.assets import IMAGE_TYPES, JobAssets
from app.services.storyboard import GeneratedClip
from app.storage.local import atomic_copy, generation_path

ACTIVE = {"SUBMITTING", "PENDING", "RUNNING", "SUCCEEDED"}
TERMINAL = {"READY", "FAILED", "SKIPPED"}
ADMISSION_LOCK = 709801942859745413
PROMPT = (
    "Slow smooth cinematic camera movement through the existing photograph. "
    "Preserve the location, scenery and architecture in the reference frame. "
    "Do not add structures, people, signage, lettering or cuts."
)
TEMPLATE_MOTION = (
    "Wide establishing shot with a slow, stable forward camera movement.",
    "Gentle lateral camera glide with subtle natural motion and stable framing.",
)
PROMPT_RULES = (
    "Preserve the source location and composition. Use slow, stable motion. "
    "Do not add, replace, or distort landmarks, people or structures. "
    "Do not generate lettering, logos or audio."
)


def provider_for(config, row=None) -> VideoProvider:
    name = config.video_provider if row is None else row.provider
    if name == "wavespeed":
        if row is not None:
            config = config.model_copy(update={
                "wavespeed_base_url": row.endpoint,
                "wavespeed_model": row.model,
                "wavespeed_resolution": row.resolution,
                "wavespeed_duration_seconds": row.duration_seconds,
            })
        return WaveSpeedVideoProvider(config)
    if name == "wan":
        if row is not None:
            config = config.model_copy(update={
                "wan_base_url": row.endpoint,
                "wan_model": row.model,
                "wan_resolution": row.resolution,
                "wan_default_duration": row.duration_seconds,
            })
        return WanVideoProvider(config)
    raise ProviderError("AI_UNKNOWN_PROVIDER")


def provider_code(name, suffix):
    return f"{name.upper()}_{suffix}"


def stage_path(job, slot, *, reference=False):
    return generation_path(job.tenant_id, job.id, slot, reference=reference)


def fallback(job, request, reason):
    if not request.allow_local_fallback:
        raise MediaError(reason, "AI generation cannot continue")
    job.fallback_reason = reason
    return ()


def preflight_references(job, request, count, candidates=None):
    if candidates is None:
        candidates = (
            request.images if request.ai_reference_images is None else request.ai_reference_images
        )
    prepared = []
    with TemporaryDirectory(prefix=f"ai-ref-{job.id}-") as directory:
        root = Path(directory)
        assets = JobAssets(root, job.tenant_id)
        for candidate in dict.fromkeys(candidates):
            try:
                source, kind = assets.fetch(candidate)
            except MediaError:
                continue
            if kind not in IMAGE_TYPES:
                continue
            with Image.open(source) as original:
                image = ImageOps.exif_transpose(original).convert("RGBA")
                if min(image.size) < 240:
                    continue
                image.thumbnail((2160, 2160))
                if min(image.size) < 240:
                    continue
                target = root / f"reference-{len(prepared) + 1}.jpg"
                rgb = Image.new("RGB", image.size, "white")
                rgb.paste(image, mask=image.getchannel("A"))
                rgb.save(target, "JPEG", quality=90)
                prepared.append((str(candidate), target))
            if len(prepared) == count:
                for slot, (_, target) in enumerate(prepared, start=1):
                    atomic_copy(target, stage_path(job, slot, reference=True))
                return [reference for reference, _ in prepared]
    return []


def cloud_allowed(config, name):
    if not getattr(config, f"{name}_enabled"):
        return provider_code(name, "DISABLED")
    if getattr(config, f"{name}_dry_run"):
        return provider_code(name, "DRY_RUN")
    if not getattr(config, f"{name}_live_test_enabled"):
        return provider_code(name, "SUBMISSION_DISABLED")
    key = getattr(config, f"{name}_api_key")
    if key is None or not key.get_secret_value().strip():
        return provider_code(name, "INVALID_API_KEY")
    return None


def sync_cost(job, rows):
    job.actual_cost_usd = sum((row.actual_cost_usd or Decimal(0) for row in rows), Decimal(0))
    job.ai_cost_uncertain = any(row.billing_uncertain for row in rows)


def initial_plan(db, job, request, provider, config, count, *, directed_refs=None):
    spec = provider.spec
    estimate = provider.estimate_cost(spec)
    job.estimated_cost_usd = estimate.total_usd * count
    if request.max_ai_cost_usd is None:
        return fallback(job, request, "AI_BUDGET_REQUIRED")
    if job.estimated_cost_usd > request.max_ai_cost_usd:
        raise MediaError("AI_BUDGET_EXCEEDED", "Estimated AI cost exceeds maxAiCostUsd")
    if (job.duration_seconds - count * spec.duration_seconds) < 5:
        return fallback(job, request, provider_code(provider.name, "DURATION_UNSUPPORTED"))
    reason = cloud_allowed(config, provider.name)
    if reason:
        return fallback(job, request, reason)
    refs = preflight_references(job, request, count, directed_refs)
    if not refs:
        return fallback(job, request, provider_code(provider.name, "REFERENCE_UNAVAILABLE"))

    timeout = getattr(config, f"{provider.name}_task_timeout_seconds")
    deadline = datetime.now(UTC) + timedelta(seconds=timeout)
    rows = [
        VideoAIGeneration(
            job_id=job.id, slot=slot, reference=ref, provider=provider.name,
            endpoint=provider.endpoint, model=spec.model, resolution=spec.resolution,
            duration_seconds=spec.duration_seconds, unit_price_usd=estimate.unit_price_usd,
            estimated_cost_usd=estimate.total_usd, state="NEW", deadline=deadline,
        )
        for slot, ref in enumerate(refs, start=1)
    ]
    job.status = Status.PROCESSING
    job.progress = max(12, job.progress)
    db.add_all(rows)
    db.commit()
    return rows


def mark_failed(row, code, *, unbilled=False):
    row.state = "FAILED"
    row.error_code = code
    row.not_before = None
    if unbilled:
        row.billing_uncertain = False
        row.lease_until = None


def postpone(row, config, minimum=0):
    interval = getattr(config, f"{row.provider}_poll_interval_seconds")
    delay = max(interval * 2 ** min(row.transient_errors, 4), minimum)
    row.not_before = min(datetime.now(UTC) + timedelta(seconds=delay), row.deadline)


def reserve(db, job, row, rows, config, *, direction=None, prompt=PROMPT):
    db.execute(text("SELECT pg_advisory_xact_lock(:key)"), {"key": ADMISSION_LOCK})
    now = datetime.now(UTC)
    active = db.scalar(select(func.count()).select_from(VideoAIGeneration).where(
        VideoAIGeneration.provider == row.provider, VideoAIGeneration.lease_until > now
    ))
    limit = (
        config.wavespeed_max_concurrent if row.provider == "wavespeed"
        else config.wan_max_concurrent_tasks
    )
    if active >= limit:
        postpone(row, config)
        db.commit()
        return False
    row.state = "SUBMITTING"
    # An unconfirmed remote operation can occupy capacity for the provider task-ID lifetime.
    row.lease_until = now + timedelta(hours=24)
    row.billing_uncertain = True
    if direction is not None:
        metadata = direction.asset_metadata
        direction.asset_metadata = {
            **metadata, "submittedPrompts": {
                **metadata.get("submittedPrompts", {}), str(row.slot): prompt,
            },
        }
    sync_cost(job, rows)
    db.commit()
    return True


def submit(db, job, row, rows, provider, config, *, direction=None, prompt=PROMPT):
    if row.endpoint != provider.endpoint:
        mark_failed(row, provider_code(row.provider, "CONFIG_CHANGED"), unbilled=True)
        return
    reason = cloud_allowed(config, provider.name)
    if reason:
        mark_failed(row, reason, unbilled=True)
        return
    spec = GenerationSpec(row.model, row.resolution, row.duration_seconds)
    try:
        current_estimate = provider.estimate_cost(spec)
    except ProviderError as exc:
        mark_failed(row, exc.code, unbilled=True)
        return
    if current_estimate.total_usd > row.estimated_cost_usd or (
        row.provider == "wavespeed" and current_estimate.unit_price_usd != row.unit_price_usd
    ):
        mark_failed(row, provider_code(row.provider, "PRICE_CHANGED"), unbilled=True)
        return
    reference = stage_path(job, row.slot, reference=True)
    if not reference.is_file():
        mark_failed(row, provider_code(row.provider, "REFERENCE_UNAVAILABLE"), unbilled=True)
        return
    if not reserve(db, job, row, rows, config, direction=direction, prompt=prompt):
        return

    try:
        row.task_id = provider.submit_generation(reference, prompt, spec)
    except ProviderError as exc:
        mark_failed(row, exc.code, unbilled=not exc.ambiguous)
    else:
        row.state = "PENDING"
        postpone(row, config)
    sync_cost(job, rows)
    db.commit()


def save_result(job, row, provider, result, config):
    with TemporaryDirectory(prefix=f"ai-result-{job.id}-") as directory:
        path = Path(directory) / "clip.mp4"
        provider.download_result(result, path, config.max_asset_bytes)
        _, length = input_duration(probe(path), "video")
        if length < row.duration_seconds - 0.2:
            raise MediaError(
                provider_code(row.provider, "INVALID_RESULT"),
                "Generated clip is shorter than requested",
            )
        atomic_copy(path, stage_path(job, row.slot))


def poll(db, job, row, rows, provider, config):
    if row.endpoint != provider.endpoint:
        mark_failed(row, provider_code(row.provider, "CONFIG_CHANGED"))
        return
    if not row.task_id:
        mark_failed(row, provider_code(row.provider, "SUBMIT_UNCERTAIN"))
        return

    try:
        result = provider.get_task_status(row.task_id)
        if result.state in (TaskState.PENDING, TaskState.RUNNING):
            if row.state != "SUCCEEDED":
                row.state = result.state
            row.transient_errors = 0
            postpone(row, config)
            return
        if result.state != TaskState.SUCCEEDED:
            known_unbilled = (
                row.provider == "wan" and row.state != "SUCCEEDED"
                and result.state in (TaskState.FAILED, TaskState.CANCELED)
            )
            suffix = "TASK_EXPIRED" if result.state == TaskState.UNKNOWN else "TASK_FAILED"
            if result.state in (TaskState.FAILED, TaskState.CANCELED):
                row.lease_until = None
            mark_failed(row, provider_code(row.provider, suffix), unbilled=known_unbilled)
            return

        row.state = "SUCCEEDED"
        row.lease_until = None
        if result.billed_seconds is not None:
            cost = result.billed_seconds * row.unit_price_usd
            row.actual_cost_usd = cost.quantize(Decimal("0.0001"), rounding=ROUND_CEILING)
            row.billing_uncertain = False
        elif row.actual_cost_usd is None:
            row.actual_cost_usd = row.estimated_cost_usd
            row.billing_uncertain = True
        if row.actual_cost_usd > row.estimated_cost_usd:
            mark_failed(row, provider_code(row.provider, "USAGE_MISMATCH"))
            return
        sync_cost(job, rows)
        db.commit()
        save_result(job, row, provider, result, config)
    except ProviderError as exc:
        if exc.retryable:
            row.transient_errors += 1
            postpone(row, config, exc.retry_after)
        else:
            mark_failed(row, exc.code)
    except MediaError:
        mark_failed(row, provider_code(row.provider, "INVALID_RESULT"))
    else:
        row.state = "READY"
        row.not_before = None


def advance_hybrid(db, job, request, *, direction=None):
    if job.fallback_reason is not None:
        return ()
    count = 2 if request.ai_scenes is None else request.ai_scenes
    if not count:
        job.estimated_cost_usd = Decimal(0)
        return ()

    config = settings()
    plan = get_storyboard(direction)
    ai_scenes = [scene for scene in plan.scenes if scene.type == "AI_VIDEO"] if plan else []
    directed_refs = None
    if plan is not None:
        count = len(ai_scenes)
        if not count:
            job.estimated_cost_usd = Decimal(0)
            return ()
        options = asset_options(request)
        directed_refs = [options[scene.asset_index][0] for scene in ai_scenes]
    if direction is not None:
        config = config.model_copy(update={"wavespeed_generate_audio": False})
    rows = list(db.scalars(
        select(VideoAIGeneration).where(VideoAIGeneration.job_id == job.id)
        .order_by(VideoAIGeneration.slot)
    ))
    if not rows:
        try:
            provider = provider_for(config)
            if ai_scenes and (
                provider.name != "wavespeed" or any(
                    scene.duration_seconds != provider.spec.duration_seconds for scene in ai_scenes
                )
            ):
                return fallback(job, request, provider_code(provider.name, "CONFIG_CHANGED"))
            rows = initial_plan(
                db, job, request, provider, config, count, directed_refs=directed_refs
            )
        except ProviderError as exc:
            return fallback(job, request, exc.code)
        if not rows:
            return ()

    now = datetime.now(UTC)
    for row in rows:
        if row.state in TERMINAL:
            continue
        if row.state == "SUBMITTING" and not row.task_id:
            mark_failed(row, provider_code(row.provider, "SUBMIT_UNCERTAIN"))
        elif row.deadline <= now:
            mark_failed(
                row, provider_code(row.provider, "TASK_TIMEOUT"), unbilled=row.state == "NEW"
            )
        elif row.state in ACTIVE and (row.not_before is None or row.not_before <= now):
            try:
                provider = provider_for(config, row)
            except ProviderError as exc:
                mark_failed(row, exc.code)
            else:
                poll(db, job, row, rows, provider, config)

    for row in rows:
        if row.state != "NEW":
            continue
        if any(other.state == "FAILED" for other in rows):
            row.state = "SKIPPED"
        elif row.not_before is None or row.not_before <= now:
            try:
                provider = provider_for(config, row)
            except ProviderError as exc:
                mark_failed(row, exc.code, unbilled=True)
            else:
                prompt = PROMPT
                if direction is not None:
                    motion = (
                        ai_scenes[row.slot - 1].video_prompt.strip()
                        if ai_scenes else TEMPLATE_MOTION[row.slot - 1]
                    )
                    prompt = f"{motion} {PROMPT_RULES}"
                submit(db, job, row, rows, provider, config, direction=direction, prompt=prompt)

    sync_cost(job, rows)
    pending = [row for row in rows if row.state not in TERMINAL]
    if pending:
        job.progress = max(job.progress, 15 + 6 * sum(row.state == "READY" for row in rows))
        job.status = Status.PROCESSING
        return min(min(row.deadline, row.not_before or now) for row in pending)

    failed = next((row for row in rows if row.state == "FAILED"), None)
    if failed:
        return fallback(job, request, failed.error_code)
    prompts = direction.asset_metadata.get("submittedPrompts", {}) if direction else {}
    return tuple(
        GeneratedClip(stage_path(job, row.slot), row.reference, row.duration_seconds,
                      row.provider, row.actual_cost_usd or Decimal(0),
                      prompts.get(str(row.slot), PROMPT))
        for row in rows
    )


def clear_hybrid_staging(job):
    if job.mode == Mode.HYBRID:
        for slot in (1, 2):
            for reference in (False, True):
                stage_path(job, slot, reference=reference).unlink(missing_ok=True)

