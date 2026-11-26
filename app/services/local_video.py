from decimal import Decimal
from pathlib import Path
from tempfile import TemporaryDirectory
from unicodedata import normalize

from app.core.config import settings
from app.core.exceptions import MediaError
from app.models.video import VideoAsset, VideoScene
from app.renderers.ffmpeg import FFmpegRenderer, input_duration, make_overlay, probe
from app.services.ai_director import get_storyboard
from app.services.assets import IMAGE_TYPES, JobAssets
from app.services.narration import synthesize_narration
from app.services.storyboard import FPS, OVERLAP, directed_shots, narration_timeline, storyboard
from app.services.subtitles import write_subtitles
from app.services.tts import NarrationError
from app.storage.local import atomic_copy, output_path


def render_local(job, request, progress, *, generated=(), direction=None):
    plan = get_storyboard(direction)
    shots = directed_shots(request, plan, generated) if plan else storyboard(request, generated)
    progress(10)
    with TemporaryDirectory(prefix=f"video-{job.id}-") as directory:
        workdir = Path(directory)
        assets = JobAssets(workdir, job.tenant_id)
        sources = {}
        for shot in shots:
            if shot.generated is not None:
                continue
            key = (shot.reference, shot.image)
            if key not in sources:
                media = assets.fetch(shot.reference)
                if shot.image != (media[1] in IMAGE_TYPES) or (
                    not shot.image and media[1] != "mov"
                ):
                    raise MediaError("INVALID_ASSET", "Asset does not match the image/video list")
                index = 0 if shot.image else input_duration(probe(*media), "video")[0]
                sources[key] = (media, index)

        logo = None
        if request.logo is not None:
            logo, kind = assets.fetch(request.logo)
            if kind not in IMAGE_TYPES:
                raise MediaError("INVALID_ASSET", "Logo must be an image")

        audio = []
        for reference in (request.background_music, request.voiceover):
            if reference is None:
                audio.append(None)
                continue
            source, kind = assets.fetch(reference)
            if kind in IMAGE_TYPES:
                raise MediaError("INVALID_ASSET", "Audio asset must contain an audio stream")
            stream, length = input_duration(probe(source, kind), "audio")
            audio.append((source, kind, stream, length))

        progress(30, rendering=True)
        renderer = FFmpegRenderer(workdir, request.config.aspect_ratio)
        config = settings()
        options = request.narration
        wants_voice = options.enabled if options is not None else config.tts_enabled
        scenes_audio = []
        audio_fallback = None
        subtitles = False
        if audio[1] is None and wants_voice:
            try:
                audio[1], scenes_audio = synthesize_narration(job, shots, request, workdir)
                if (
                    scenes_audio and config.subtitle_enabled
                    and (options is None or options.subtitles)
                ):
                    write_subtitles(workdir, scenes_audio, renderer.size)
                    subtitles = True
            except OSError as exc:
                if options is None or not options.allow_fallback:
                    raise NarrationError("TTS_STORAGE_FAILED", "Narration staging failed") from exc
                audio_fallback = "TTS_STORAGE_FAILED"
                audio[1], scenes_audio = None, []
            except NarrationError as exc:
                if options is None or not options.allow_fallback:
                    raise
                audio_fallback = exc.code
        clips = []
        for index, shot in enumerate(shots, start=1):
            overlay = None
            if not generated:
                overlay = workdir / "overlay.png"
                make_overlay(
                    overlay, renderer.size, normalize("NFC", shot.caption),
                    logo if shot.closing else None,
                )
            if shot.generated is None:
                media, stream = sources[(shot.reference, shot.image)]
            else:
                media = (shot.generated.path, "mov")
                stream = input_duration(probe(*media), "video")[0]
            overlap = (
                index < len(shots) and shot.generated is None and shots[index].generated is None
            )
            frames = shot.frames + (OVERLAP if overlap else 0)
            clips.append(renderer.scene(index, shot, media, stream, overlay, frames))
            progress(30 + 35 * index // len(shots), rendering=True)

        assembled = renderer.join_hybrid(clips, shots) if generated else renderer.join(clips)
        progress(75, rendering=True)
        duration = request.config.duration_seconds
        caption_timeline = renderer.caption_timeline(shots, logo) if generated else None
        try:
            final = renderer.finish(
                assembled, *audio, duration, captions=caption_timeline,
                subtitles=subtitles, synthesized=bool(scenes_audio),
            )
        except MediaError as exc:
            if not subtitles or exc.code != "SUBTITLE_RENDER_FAILED":
                raise
            if options is None or not options.allow_fallback:
                raise NarrationError(exc.code, str(exc)) from exc
            audio_fallback = exc.code
            subtitles = False
            final = renderer.finish(
                assembled, *audio, duration, captions=caption_timeline, synthesized=True,
            )
        progress(88, rendering=True)
        renderer.validate(final, shots, duration)
        progress(95, rendering=True)

        target = output_path(job.tenant_id, job.id)
        atomic_copy(final, target)
        width, height = renderer.size
        asset = VideoAsset(
            job_id=job.id, storage_backend="local", content_type="video/mp4",
            storage_key=f"{job.tenant_id}/videos/{job.id}.mp4",
            asset_metadata={
                "width": width, "height": height, "durationSeconds": duration,
                "fps": FPS, "sizeBytes": target.stat().st_size,
                "generatedScenes": len(generated),
            },
        )
        if direction is not None or wants_voice:
            asset.asset_metadata["narrationTimeline"] = narration_timeline(shots, request)
        if direction is not None:
            asset.asset_metadata["directorSource"] = direction.asset_metadata["source"]
        if scenes_audio:
            asset.asset_metadata["voiceoverSegments"] = [
                {key: value for key, value in segment.items() if key != "path"}
                for segment in scenes_audio
            ]
            asset.asset_metadata["subtitleBurnedIn"] = subtitles
        if audio_fallback:
            asset.asset_metadata["audioFallbackReason"] = audio_fallback
        scenes = [
            VideoScene(
                job_id=job.id, position=index,
                source_url=shot.generated.reference if shot.generated else str(shot.reference),
                duration_seconds=Decimal(shot.frames) / FPS,
                provider=shot.generated.provider if shot.generated else "local",
                cost_usd=shot.generated.cost_usd if shot.generated else Decimal(0),
                prompt=shot.generated.prompt if shot.generated else (shot.caption or None),
            )
            for index, shot in enumerate(shots, start=1)
        ]
        return asset, scenes

