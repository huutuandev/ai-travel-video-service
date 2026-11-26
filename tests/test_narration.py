import re
import wave
from array import array
from types import SimpleNamespace

import pytest

from app.core.config import settings
from app.renderers.ffmpeg import FFmpegRenderer, ffmpeg, media_input
from app.schemas.video import GenerateVideo
from app.services import narration
from app.services.storyboard import FPS, Shot
from app.services.tts import NarrationError


def mock_wav(path, duration):
    pattern = array("h", [600, -600]) * round(12000 * duration)
    with wave.open(str(path), "wb") as stream:
        stream.setparams((1, 2, 24000, 0, "NONE", "not compressed"))
        stream.writeframes(pattern.tobytes())


def test_director_custom_and_template_sources(payload):
    request = GenerateVideo.model_validate(payload)
    first = Shot(request.images[0], True, FPS * 5, "Giá 2.990.000đ", False,
                 narration="Lời dẫn từ storyboard")
    second = Shot(request.images[0], True, FPS * 25, "Đặt tour", True)
    scenes = narration.narration_sources([first, second], request)
    assert scenes[0]["narration"] == "Lời dẫn từ storyboard"
    assert scenes[1]["narration"] == "Đặt tour"
    payload["narration"] = {"sceneTexts": ["Giá 2.990.000đ", "Đặt ngay"]}
    request = GenerateVideo.model_validate(payload)
    assert narration.narration_sources([first, second], request)[0]["narration"] == "Giá 2.990.000đ"


def test_measured_segments_reuse_cache_on_resume(
    configuration, payload, tmp_path, monkeypatch,
):
    monkeypatch.setenv("MEDIA_ROOT", str(tmp_path))
    monkeypatch.setenv("TTS_ENABLED", "true")
    settings.cache_clear()
    config = settings()
    calls = []

    def synthesize(text, voice, path, deadline):
        calls.append(text)
        mock_wav(path, 0.6)

    monkeypatch.setattr(narration, "local_tts", lambda _: SimpleNamespace(synthesize=synthesize))
    request = GenerateVideo.model_validate(payload)
    shots = [
        Shot(request.images[0], True, 5 * FPS, "Caption", False, narration="Giảm 20%"),
        Shot(request.images[0], True, 25 * FPS, "CTA", True, narration="Hẹn gặp bạn"),
    ]
    job = SimpleNamespace(tenant_id="test-tenant", id="test-job")
    for index in range(2):
        directory = tmp_path / str(index)
        directory.mkdir()
        voice, segments = narration.synthesize_narration(job, shots, request, directory)
        assert voice[3] == 30
        assert segments[1]["startSeconds"] == pytest.approx(5.04)
        assert 0 < segments[0]["audioDuration"] < 1
        assert segments[0]["normalizedText"] == "Giảm hai mươi phần trăm"
    assert calls == ["Giảm hai mươi phần trăm", "Hẹn gặp bạn"]
    assert list(narration.staging(job).glob("*.wav"))
    assert config.tts_enabled
    narration.clear_narration(job)
    assert not narration.staging(job).exists()


def test_overlong_audio_fails_without_truncating(configuration, tmp_path, monkeypatch):
    source = tmp_path / "long.wav"
    mock_wav(source, 5)
    monkeypatch.setattr(narration, "cached_segment", lambda *_: (source, 5.0))
    scene = {"sceneId": 1, "startFrame": 0, "durationFrames": 2 * FPS, "narration": "Đặt tour"}
    with pytest.raises(NarrationError) as error:
        narration.fit_segment(None, scene, tmp_path, "default", 1, settings(), float("inf"))
    assert error.value.code == "TTS_SCENE_TOO_LONG"


def test_mixer_ducks_music_and_limits_peak(configuration, tmp_path, monkeypatch):
    video = tmp_path / "input.mp4"
    music = tmp_path / "music.wav"
    voice = tmp_path / "voice.wav"
    ffmpeg(["-f", "lavfi", "-i", "testsrc2=size=160x90:rate=30", "-t", "3", str(video)])
    ffmpeg(["-f", "lavfi", "-i", "sine=frequency=400:duration=3", str(music)])
    ffmpeg([
        "-f", "lavfi", "-i", "sine=frequency=1700:duration=1",
        "-af", "volume=8", str(voice),
    ])
    renderer = FFmpegRenderer(tmp_path, "16:9")
    output = renderer.finish(video, (music, "wav", 0, 3), (voice, "wav", 0, 1), 3)

    def peak(path, filters):
        diagnostics = ffmpeg([
            *media_input(path), "-vn", "-af", filters + "volumedetect", "-f", "null", "-",
        ], info=True)
        return float(re.search(r"max_volume:\s+(-?[\d.]+) dB", diagnostics)[1])

    band = "atrim=start=0.4:end=0.8,lowpass=f=600:p=2,lowpass=f=600:p=2,"
    ducked = peak(output, band)
    assert peak(output, "") <= 0
    monkeypatch.setenv("AUDIO_DUCKING_ENABLED", "false")
    settings.cache_clear()
    output = renderer.finish(video, (music, "wav", 0, 3), (voice, "wav", 0, 1), 3)
    assert ducked < peak(output, band)

