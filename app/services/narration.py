import hashlib
import json
import math
import re
import wave
from array import array
from pathlib import Path
from time import monotonic

from app.core.config import settings
from app.core.exceptions import MediaError
from app.renderers.ffmpeg import ffmpeg, input_duration, media_input, probe
from app.services.storyboard import FPS, narration_timeline
from app.services.text_normalization import normalize_vietnamese
from app.services.tts import MODEL_REVISION, NarrationError, local_tts, synthesis_slot, tts_error
from app.storage.local import atomic_copy, media_path

RATE = 24000
MAX_TEMPO = 1.18
FILLER = re.compile(r"^(?:quý khách thân mến|xin mời quý khách|hãy cùng chúng tôi)[, ]+", re.I)


def staging(job):
    return media_path(job.tenant_id, "narration", str(job.id))


def clear_narration(job):
    directory = staging(job)
    if directory.is_dir():
        for path in directory.glob("*.wav"):
            path.unlink(missing_ok=True)
        directory.rmdir()


def narration_sources(shots, request):
    scenes = narration_timeline(shots, request)["scenes"]
    options = request.narration
    if options and options.scene_texts is not None:
        if len(options.scene_texts) > len(scenes):
            raise NarrationError("TTS_SCENE_COUNT", "sceneTexts exceeds the rendered scene count")
        texts = options.scene_texts
    elif options and options.text is not None:
        sentences = re.split(r"(?<!\bTP\.)(?<=[.!?;])\s+|\n+", options.text, flags=re.I)
        texts = [""] * len(scenes)
        for index, sentence in enumerate(sentences):
            slot = index * len(scenes) // len(sentences)
            texts[slot] = f"{texts[slot]} {sentence}".strip()
    else:
        return scenes
    return [
        {**scene, "narration": texts[index].strip() if index < len(texts) else ""}
        for index, scene in enumerate(scenes)
    ]


def inspect_wav(path):
    try:
        with wave.open(str(path), "rb") as stream:
            frames = stream.getnframes()
            if (
                stream.getsampwidth() != 2 or stream.getnchannels() not in (1, 2)
                or not RATE <= stream.getframerate() <= 48000
                or not 0 < frames <= stream.getframerate() * 120
                or len(stream.readframes(frames)) != frames * 2 * stream.getnchannels()
            ):
                raise ValueError
        _, duration = input_duration(probe(path, "wav", error_code="TTS_INVALID_AUDIO"), "audio")
        if not math.isfinite(duration) or duration <= 0:
            raise ValueError
        return duration
    except (OSError, EOFError, wave.Error, MediaError, ValueError):
        raise NarrationError("TTS_INVALID_AUDIO", "Invalid local narration WAV") from None


def cached_segment(job, path, voice, spoken, stem, config, deadline):
    identity = json.dumps([MODEL_REVISION, str(config.tts_model_dir), voice, spoken])
    fingerprint = hashlib.sha256(identity.encode()).hexdigest()[:20]
    cached = staging(job) / f"{stem}-{fingerprint}.wav"
    if cached.is_file():
        try:
            return cached, inspect_wav(cached)
        except NarrationError:
            cached.unlink(missing_ok=True)
    with synthesis_slot(config, deadline):
        local_tts(config.tts_model_dir).synthesize(spoken, voice, path, deadline)
    duration = inspect_wav(path)
    atomic_copy(path, cached)
    return cached, duration


def fit_segment(job, scene, workdir, voice, speed, config, deadline):
    if monotonic() >= deadline:
        raise tts_error("TTS_TIMEOUT")
    original = scene["narration"].strip()
    if not original:
        return None
    room = scene["durationFrames"] / FPS - 0.10
    if room <= 0:
        raise NarrationError("TTS_SCENE_TOO_LONG", "Scene too short for narration")
    label = f"scene-{scene['sceneId']:03}"
    raw = workdir / f"{label}-raw.wav"
    spoken = original
    normalized = normalize_vietnamese(spoken)
    source, measured = cached_segment(job, raw, voice, normalized, label, config, deadline)
    if measured / MAX_TEMPO > room:
        shorter = FILLER.sub("", original).strip()
        if shorter and shorter != original:
            spoken = shorter
            normalized = normalize_vietnamese(spoken)
            source, measured = cached_segment(job, raw, voice, normalized, label, config, deadline)
    tempo = max(speed, measured / room)
    if tempo > MAX_TEMPO:
        raise NarrationError("TTS_SCENE_TOO_LONG", "Narration does not fit its scene")
    target = workdir / f"{label}.wav"
    try:
        ffmpeg([
            *media_input(source, "wav"), "-af", f"atempo={tempo:.6f}", "-ac", "1",
            "-ar", str(RATE), "-c:a", "pcm_s16le", str(target),
        ], error_code="TTS_AUDIO_PROCESSING_FAILED")
        actual = inspect_wav(target)
    except MediaError as exc:
        raise NarrationError(exc.code, str(exc)) from exc
    if actual > room + 0.03:
        raise NarrationError("TTS_SCENE_TOO_LONG", "Adjusted audio still exceeds its scene")
    start = scene["startFrame"] / FPS + 0.04
    return {
        "sceneId": scene["sceneId"], "originalText": original, "spokenText": spoken,
        "normalizedText": normalized, "audioDuration": actual,
        "startSeconds": start, "endSeconds": start + actual,
        "path": target,
    }


def synthesize_narration(job, shots, request, workdir):
    config = settings()
    if not config.tts_enabled:
        raise tts_error("TTS_DISABLED")
    options = request.narration
    voice = (options.voice if options else None) or config.tts_voice
    speed = (options.speed if options else None) or config.tts_speed
    deadline = monotonic() + config.tts_timeout_seconds
    segments = []
    total = request.config.duration_seconds
    samples = array("h", [0]) * (RATE * total)
    fitted = narration_sources(shots, request)
    for scene in fitted:
        segment = fit_segment(job, scene, workdir, voice, speed, config, deadline)
        if segment is None:
            continue
        with wave.open(str(segment["path"]), "rb") as stream:
            pcm = array("h")
            pcm.frombytes(stream.readframes(stream.getnframes()))
        peak = max(map(abs, pcm), default=0)
        if peak < 10:
            raise NarrationError("TTS_INVALID_AUDIO", "Local narration contains no audible signal")
        gain = min(4.0, 23170 / peak)
        pcm = array("h", (round(value * gain) for value in pcm))
        offset = round(segment["startSeconds"] * RATE)
        if offset + len(pcm) > len(samples):
            raise NarrationError("TTS_SCENE_TOO_LONG", "Narration exceeds the video timeline")
        samples[offset:offset + len(pcm)] = pcm
        segments.append(segment)
    if not segments:
        return None, []
    output = Path(workdir) / "voiceover.wav"
    with wave.open(str(output), "wb") as stream:
        stream.setparams((1, 2, RATE, 0, "NONE", "not compressed"))
        stream.writeframes(samples.tobytes())
    try:
        index, length = input_duration(probe(output, "wav"), "audio")
    except MediaError as exc:
        raise NarrationError("TTS_INVALID_AUDIO", "Assembled narration WAV is invalid") from exc
    return (output, "wav", index, length), segments

