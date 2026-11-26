"""Exercise an already running, isolated LOCAL stack using generated media."""
import argparse
import json
import os
import subprocess
from io import BytesIO
from pathlib import Path
from time import monotonic, sleep
from urllib.request import Request, urlopen
from uuid import uuid4

from PIL import Image, ImageDraw


def request(base, route, *, payload=None, mime=None, authenticated=True):
    headers = {}
    if authenticated:
        headers["X-Service-API-Key"] = os.environ["PHASE3_SMOKE_API_KEY"]
    if mime:
        headers["Content-Type"] = mime
    with urlopen(Request(base + route, data=payload, headers=headers), timeout=25) as response:
        return response.status, response.read()


def ffmpeg(*args):
    return subprocess.run(
        ["ffmpeg", "-hide_banner", "-nostdin", "-y", *args], check=True,
        capture_output=True, text=True,
    ).stderr


def upload_fixture(base, index):
    colors = [("#96cade", "#407878", "#edd0a1"), ("#e6b28a", "#686177", "#f8dac1")]
    sky, hill, coast = colors[index]
    image = Image.new("RGB", (1280, 720), sky)
    draw = ImageDraw.Draw(image)
    draw.ellipse((865, 80, 990, 205), fill="#ffe6af")
    draw.polygon([(0, 470), (340, 230), (790, 460), (1280, 305), (1280, 720), (0, 720)],
                 fill=hill)
    draw.polygon([(0, 540), (1280, 480), (1280, 720), (0, 720)], fill=coast)
    draw.line([(0, 550), (450, 580), (840, 517), (1280, 520)], width=12, fill="#fff8dc")
    with BytesIO() as file:
        image.save(file, format="JPEG")
        status, body = request(base, "/api/v1/videos/assets", payload=file.getvalue(),
                               mime="image/jpeg")
    assert status == 201
    return json.loads(body)["assetId"]


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base-url", default="http://127.0.0.1:8000")
    parser.add_argument("--output", type=Path, default=Path("artifacts/phase3-local-offline.mp4"))
    args = parser.parse_args()
    base = args.base_url.rstrip("/")
    for route in ("/health/live", "/health/ready", "/docs", "/openapi.json"):
        assert request(base, route, authenticated=False)[0] == 200
        print(f"{route} 200", flush=True)
    payload = {
        "requestId": f"offline-phase3-{uuid4()}", "productId": "phase3-tour",
        "tour": {
            "title": "Hành trình bên biển", "destination": "Đà Nẵng",
            "highlights": ["Ghé Sơn Trà", "Dạo biển Mỹ Khê"],
            "experience": "Tận hưởng buổi sáng bên biển",
            "priceVnd": 2990000, "callToAction": "Liên hệ để đặt tour ngay",
        },
        "config": {"mode": "LOCAL", "durationSeconds": 40, "aspectRatio": "16:9"},
        "images": [upload_fixture(base, index) for index in range(2)],
        "narration": {"enabled": True, "subtitles": True, "allowFallback": False},
    }
    data = json.dumps(payload, ensure_ascii=False).encode()
    route = "/api/v1/videos/generate"
    started = monotonic()
    status, body = request(base, route, payload=data, mime="application/json")
    assert status == 202
    identifier = json.loads(body)["jobId"]
    again, duplicate = request(base, route, payload=data, mime="application/json")
    assert again == 202 and json.loads(duplicate)["jobId"] == identifier
    print(f"jobId={identifier} accepted=202 idempotent=true", flush=True)
    previous = None
    while monotonic() - started < 360:
        job = json.loads(request(base, f"/api/v1/videos/{identifier}")[1])
        if job["status"] != previous:
            previous = job["status"]
            print(f"{previous} progress={job['progress']}", flush=True)
        if previous == "COMPLETED":
            break
        if previous in {"FAILED", "CANCELLED"}:
            raise SystemExit(f"{previous} {job.get('errorCode')}")
        sleep(3)
    else:
        raise SystemExit("Job polling deadline exceeded")

    assert job["executionMode"] == "LOCAL" and not job["fallbackReason"]
    assert float(job["actualCostUsd"]) == 0
    http_status, video = request(base, job["outputUrl"])
    assert http_status == 200
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_bytes(video)
    output = str(args.output)
    record = subprocess.check_output(
        ["ffprobe", "-v", "error", "-show_streams", "-show_format", "-of", "json", output],
        text=True,
    )
    data = json.loads(record)
    v, a = data["streams"]
    assert (v["width"], v["height"], v["codec_name"], v["pix_fmt"], v["avg_frame_rate"]) == (
        1920, 1080, "h264", "yuv420p", "30/1",
    )
    assert a["codec_name"] == "aac"
    assert all(abs(float(part["duration"]) - 40) <= 0.2 for part in (v, a, data["format"]))
    diagnostics = ffmpeg(
        "-i", output, "-vn", "-af", "silencedetect=noise=-40dB:d=0.15,volumedetect",
        "-f", "null", "-",
    )
    assert "max_volume: -91.0 dB" not in diagnostics
    frame_hashes = subprocess.check_output([
        "ffmpeg", "-v", "error", "-ss", "38", "-i", output, "-t", "1.5", "-an",
        "-f", "framemd5", "-",
    ], text=True)
    unique_tail_frames = len({
        line.rsplit(",", 1)[-1].strip()
        for line in frame_hashes.splitlines() if line and not line.startswith("#")
    })
    assert unique_tail_frames >= 30
    for name, second in (("hook", "0.8"), ("cta", "36.0")):
        ffmpeg("-ss", second, "-i", output, "-vf", "scale=960:-1", "-frames:v", "1",
               str(args.output.with_name(f"{args.output.stem}-{name}.png")))
    print(json.dumps({
        "jobId": identifier, "duration": data["format"]["duration"], "video": "1920x1080 H.264",
        "audio": f"{a['codec_name']} {a['sample_rate']} Hz, {a['channels']} channels",
        "sizeBytes": len(video), "jobSeconds": round(monotonic() - started, 2),
        "uniqueTailFrames": unique_tail_frames,
        "audioPeak": next(line.split("max_volume:")[1].strip()
                          for line in diagnostics.splitlines() if "max_volume:" in line),
    }), flush=True)


if __name__ == "__main__":
    main()

