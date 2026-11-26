from io import BytesIO
from pathlib import Path
from types import SimpleNamespace
from uuid import UUID, uuid4
from wave import open as wave_open

import pytest
from PIL import Image, ImageDraw
from sqlalchemy import select
from sqlalchemy.orm import Session
from test_api import create
from test_narration import mock_wav

from app.core.config import settings
from app.core.db import engine
from app.core.exceptions import MediaError
from app.models.video import VideoAsset
from app.renderers.ffmpeg import FFmpegRenderer, ffmpeg, make_overlay, probe
from app.services import assets, execution, narration
from app.services.storyboard import FPS, Shot


@pytest.fixture
def media_root(configuration, monkeypatch, tmp_path):
    monkeypatch.setenv("MEDIA_ROOT", str(tmp_path))
    settings.cache_clear()
    return tmp_path


def upload(client, headers, content, mime):
    response = client.post(
        "/api/v1/videos/assets", content=content, headers={**headers, "Content-Type": mime},
    )
    assert response.status_code == 201, response.text
    return response.json()["assetId"]


def artwork(kind, color):
    image = Image.new("RGB", (320, 240), color)
    draw = ImageDraw.Draw(image)
    draw.ellipse((25, 25, 155, 155), fill="#fff29a")
    draw.polygon([(0, 230), (140, 70), (320, 230)], fill="#29be98")
    out = BytesIO()
    image.save(out, format=kind)
    return out.getvalue()


@pytest.fixture
def local_payload(payload):
    payload["tour"] = {
        "title": "Đà Nẵng — Ngũ Hành Sơn", "destination": "Việt Nam",
        "highlights": ["Bán đảo Sơn Trà", "Bãi biển Mỹ Khê"],
        "experience": "Thưởng thức đặc sản địa phương",
        "priceVnd": 2990000, "promotion": "Ưu đãi mùa hè",
        "callToAction": "Đặt chuyến đi của bạn",
    }
    payload["config"] = {"mode": "LOCAL", "durationSeconds": 30, "aspectRatio": "9:16"}
    return payload


def test_two_actual_renders(migrated, media_root, client, local_payload, headers, tmp_path):
    portrait = verify_render(client, local_payload, headers, tmp_path, 30, "9:16", (1080, 1920))
    landscape = verify_render(client, local_payload, headers, tmp_path, 40, "16:9", (1920, 1080))
    assert portrait != landscape
    assert portrait.parent == landscape.parent
    assert portrait.is_file() and landscape.is_file()


def verify_render(client, local_payload, headers, tmp_path, duration, ratio, size):
    local_payload["requestId"] = f"render-{duration}"
    local_payload["config"].update(durationSeconds=duration, aspectRatio=ratio)
    local_payload["images"] = [
        upload(client, headers, artwork("JPEG", "#367ace"), "image/jpeg"),
        upload(client, headers, artwork("WEBP", "#a64b80"), "image/webp"),
    ]
    local_payload["logo"] = upload(client, headers, artwork("PNG", "#e5a552"), "image/png")
    if duration == 40:
        source = tmp_path / "motion.mov"
        ffmpeg([
            "-f", "lavfi", "-i", "testsrc2=size=320x240:rate=30", "-t", "7",
            "-c:v", "libx264", "-threads", "2", str(source),
        ])
        local_payload["videos"] = [upload(client, headers, source.read_bytes(), "video/quicktime")]
        wav = tmp_path / "original-test-tone.wav"
        ffmpeg([
            "-f", "lavfi", "-i", "sine=frequency=400:sample_rate=16000",
            "-t", "44", str(wav),
        ])
        music = upload(client, headers, wav.read_bytes(), "audio/wav")
        with wave_open(str(wav), "rb") as stream:
            assert stream.getnframes() / stream.getframerate() > duration
        local_payload.update(backgroundMusic=music)
        voice = tmp_path / "voice.wav"
        ffmpeg(["-f", "lavfi", "-i", "sine=frequency=800:duration=2", str(voice)])
        local_payload["voiceover"] = upload(client, headers, voice.read_bytes(), "audio/wav")
        local_payload["narration"] = {"enabled": True, "text": "Prefer the uploaded voiceover."}

    job_id = UUID(create(client, local_payload, headers))
    execution.execute_job(job_id)
    job = client.get(f"/api/v1/videos/{job_id}", headers=headers).json()
    assert job["status"] == "COMPLETED", job
    assert job["progress"] == 100
    response = client.get(job["outputUrl"], headers=headers)
    assert response.status_code == 200
    from conftest import KEY_B

    other_headers = {"X-Service-API-Key": KEY_B}
    assert client.get(job["outputUrl"], headers=other_headers).status_code == 404
    path = tmp_path / "download.mp4"
    path.write_bytes(response.content)
    assert response.content.index(b"moov") < response.content.index(b"mdat")
    data = probe(path)
    video, audio = data["streams"]
    assert (video["width"], video["height"]) == size
    assert video["codec_name"] == "h264"
    assert audio["codec_name"] == "aac"
    assert abs(float(data["format"]["duration"]) - duration) <= 0.2
    assert abs(float(audio["duration"]) - duration) <= 0.2
    with Session(engine()) as db:
        asset = db.scalar(select(VideoAsset).where(VideoAsset.job_id == job_id))
        assert asset.asset_metadata["durationSeconds"] == duration
        return settings().media_root / asset.storage_key


def test_offline_custom_voiceover_and_subtitles_render_35_seconds(
    migrated, media_root, client, local_payload, headers, monkeypatch,
):
    monkeypatch.setenv("TTS_ENABLED", "true")
    settings.cache_clear()
    calls = []

    def synthesize(text, voice, path, deadline):
        calls.append(text)
        mock_wav(path, 0.8)

    monkeypatch.setattr(narration, "local_tts", lambda _: SimpleNamespace(synthesize=synthesize))
    monkeypatch.setattr(
        "app.providers.groq.GroqClient.complete", lambda *args: pytest.fail("Unexpected Groq call")
    )
    monkeypatch.setattr(
        "app.services.hybrid.provider_for", lambda *args: pytest.fail("Unexpected video AI call")
    )
    local_payload["config"]["durationSeconds"] = 35
    local_payload["images"] = [upload(client, headers, artwork("JPEG", "#367ace"), "image/jpeg")]
    local_payload["narration"] = {"sceneTexts": ["Giá 2.990.000 VNĐ"], "subtitles": True}
    job_id = UUID(create(client, local_payload, headers))
    execution.execute_job(job_id)
    job = client.get(f"/api/v1/videos/{job_id}", headers=headers).json()
    assert job["status"] == "COMPLETED", job
    assert calls == ["Giá hai triệu chín trăm chín mươi nghìn đồng"]
    with Session(engine()) as db:
        asset = db.scalar(select(VideoAsset).where(VideoAsset.job_id == job_id))
        assert asset.asset_metadata["subtitleBurnedIn"]
        assert asset.asset_metadata["voiceoverSegments"][0]["startSeconds"] == 0.04
        output = probe(media_root / asset.storage_key)
        video, audio = output["streams"]
        assert (video["width"], video["height"]) == (1080, 1920)
        assert video["avg_frame_rate"] == "30/1"
        assert audio["codec_name"] == "aac"
        assert float(output["format"]["duration"]) == pytest.approx(35, abs=0.2)


def test_invalid_and_missing_asset(migrated, media_root, client, local_payload, headers):
    broken = upload(client, headers, b"\x89PNG\r\n\x1a\ninvalid", "image/png")
    for ref, code in ((broken, "INVALID_ASSET"), (str(uuid4()), "ASSET_NOT_FOUND")):
        local_payload["requestId"] = str(uuid4())
        local_payload["images"] = [ref]
        job_id = UUID(create(client, local_payload, headers))
        execution.execute_job(job_id)
        job = client.get(f"/api/v1/videos/{job_id}", headers=headers).json()
        assert job["status"] == "FAILED"
        assert job["errorCode"] == code
        assert job["outputUrl"] is None


def test_ffmpeg_error(migrated, media_root, client, local_payload, headers, monkeypatch):
    def bad_render(*args, **kwargs):
        ffmpeg(["-invalid-option"])

    monkeypatch.setattr(execution, "render_local", bad_render)
    job_id = UUID(create(client, local_payload, headers))
    execution.execute_job(job_id)
    job = client.get(f"/api/v1/videos/{job_id}", headers=headers).json()
    assert job["status"] == "FAILED"
    assert job["errorCode"] == "FFMPEG_ERROR"


def test_ssrf_rejects_mixed_dns_records(monkeypatch):
    monkeypatch.setattr(assets.socket, "getaddrinfo", lambda *args, **kwargs: [
        (2, 1, 6, "", ("1.1.1.1", 443)), (2, 1, 6, "", ("169.254.169.254", 443)),
    ])
    with pytest.raises(MediaError, match="prohibited address"):
        assets.public_addresses("cdn.example.com")


@pytest.mark.parametrize(("color", "violation"), [("black", "black"), ("blue", "freeze")])
def test_quality_rejects_black_or_frozen_video(media_root, color, violation):
    path = media_root / "quality.mp4"
    ffmpeg([
        "-f", "lavfi", "-i", f"color={color}:size=1920x1080:rate=30",
        "-f", "lavfi", "-i", "anullsrc=r=48000:cl=stereo", "-t", "2",
        "-c:v", "libx264", "-threads", "2", "-pix_fmt", "yuv420p",
        "-c:a", "aac", str(path),
    ])
    renderer = FFmpegRenderer(media_root, "16:9")
    with pytest.raises(MediaError, match=violation):
        renderer.validate(path, [Shot(uuid4(), False, 2 * FPS, "", False)], 2)
    if color == "blue":
        renderer.validate(path, [Shot(uuid4(), True, 2 * FPS, "", False)], 2)


def test_vietnamese_overlay(media_root):
    def render(label, path: Path):
        make_overlay(path, (1080, 1920), label)
        with Image.open(path) as image:
            return image.copy()

    with_diacritics = render("Đà Nẵng • Ưu đãi 2.990.000 ₫", media_root / "overlay.png")
    plain = render("Da Nang • Uu dai 2.990.000 ₫", media_root / "plain.png")
    assert with_diacritics.tobytes() != plain.tobytes()
    left, top, right, bottom = with_diacritics.getbbox()
    assert 80 < left < right < 900
    assert 1000 < top < bottom < 1500

