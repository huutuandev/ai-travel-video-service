from dataclasses import dataclass
from decimal import Decimal
from pathlib import Path
from textwrap import shorten

from app.schemas.storyboard import asset_options
from app.schemas.video import AssetRef, GenerateVideo

FPS = 30
OVERLAP = 12
CHAPTER_SECONDS = (5, 7, 7, 8, 8, 5)


@dataclass(frozen=True)
class GeneratedClip:
    path: Path
    reference: str
    duration_seconds: int
    provider: str
    cost_usd: Decimal
    prompt: str | None = None


@dataclass(frozen=True)
class Shot:
    reference: AssetRef
    image: bool
    frames: int
    caption: str
    closing: bool
    generated: GeneratedClip | None = None
    narration: str = ""


def directed_shots(request, plan, generated):
    options = asset_options(request)
    clips = iter(generated)
    shots = []
    for scene in plan.scenes:
        reference, _ = options[scene.asset_index]
        clip = next(clips, None) if scene.type == "AI_VIDEO" else None
        shots.append(Shot(
            reference, clip is None and scene.type != "LOCAL_VIDEO",
            scene.duration_seconds * FPS, scene.caption, scene is plan.scenes[-1],
            clip, scene.narration,
        ))
    return shots


def narration_timeline(shots, request):
    timeline = []
    start = 0
    seen = set()
    for index, shot in enumerate(shots, start=1):
        narration = shot.narration or " ".join(shot.caption.split())
        if not shot.narration:
            budget = shot.frames * 3 // FPS
            words = narration.split()
            # Captions containing exact prices or offers should not be truncated mid-fact.
            if len(words) > budget:
                narration = ""
            if not narration and index == 1 and budget >= 3:
                narration = "Cùng khám phá nhé!" if budget >= 4 else "Cùng khám phá!"
            if shot is shots[-1] and not narration and budget >= 2:
                narration = "Đặt tour"
            if narration.casefold() in seen:
                narration = ""
        seen.add(narration.casefold())
        timeline.append({
            "sceneId": index, "startFrame": start, "durationFrames": shot.frames,
            "narration": narration, "caption": shot.caption,
            "type": (
                "AI_VIDEO" if shot.generated else "LOCAL_IMAGE" if shot.image else "LOCAL_VIDEO"
            ),
            "videoPrompt": shot.generated.prompt if shot.generated else None,
            "transition": (
                "cut" if index == 1 or shot.generated or shots[index - 2].generated
                else "crossfade"
            ),
        })
        start += shot.frames
    return {"fps": FPS, "language": "vi-VN", "durationSeconds": request.config.duration_seconds,
            "scenes": timeline}


def storyboard(request: GenerateVideo, generated: tuple[GeneratedClip, ...] = ()) -> list[Shot]:
    tour = request.tour
    intro = "\n".join(dict.fromkeys(text for text in (tour.title, tour.destination) if text))
    offer = [tour.benefits, tour.promotion]
    if tour.price_vnd is not None:
        offer.insert(0, f"{tour.price_vnd:,}".replace(",", ".") + " ₫")
    # Keep all supplied highlights in their provided order across the sightseeing chapters.
    midpoint = (len(tour.highlights) + 1) // 2
    captions = [
        [intro],
        tour.highlights[:midpoint],
        tour.highlights[midpoint:],
        [tour.experience or shorten(tour.description or "", width=150, placeholder="…")],
        ["\n".join(text for text in offer if text)],
        [tour.call_to_action or "Đặt tour"],
    ]
    sources = [(ref, True) for ref in request.images] + [
        (ref, False) for ref in request.videos
    ]
    counts = [max(1, len(lines)) for lines in captions]
    while sum(counts) < len(sources):
        index = max(range(len(counts)), key=lambda i: CHAPTER_SECONDS[i] / (counts[i] + 1))
        counts[index] += 1

    ai_seconds = sum(clip.duration_seconds for clip in generated)
    total_frames = (request.config.duration_seconds - ai_seconds) * FPS
    shots = []
    preceding = 0
    for i, (seconds, lines, count) in enumerate(zip(CHAPTER_SECONDS, captions, counts)):
        endpoint = round((sum(CHAPTER_SECONDS[:i]) + seconds) * total_frames / 40)
        frames, remainder = divmod(endpoint - preceding, count)
        for slot in range(count):
            ref, image = sources[len(shots) % len(sources)]
            shots.append(Shot(
                ref, image, frames + (slot < remainder),
                lines[slot % len(lines)] if lines else "", i == len(counts) - 1,
            ))
        preceding = endpoint
    if generated:
        closing = next(index for index, shot in enumerate(shots) if shot.closing)
        positions = [closing] if len(generated) == 1 else [max(1, closing // 2), closing]
        for index, clip in reversed(list(zip(positions, generated))):
            shots.insert(index, Shot(
                shots[index - 1].reference, False, clip.duration_seconds * FPS,
                shots[index - 1].caption, False, clip,
            ))
    return shots

