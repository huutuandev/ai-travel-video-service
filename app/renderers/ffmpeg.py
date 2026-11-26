import json
import re
import subprocess
from pathlib import Path
from unicodedata import normalize

import structlog
from PIL import Image, ImageDraw, ImageFont, ImageOps

from app.core.config import settings
from app.core.exceptions import MediaError
from app.services.assets import FORMATS, IMAGE_TYPES
from app.services.storyboard import FPS, OVERLAP

log = structlog.get_logger()

SAFE_PROTOCOLS = "file,pipe"
CODEC = ["-c:v", "libx264", "-preset", "ultrafast", "-crf", "19", "-pix_fmt", "yuv420p"]
WEB_CODEC = [
    "-c:v", "libx264", "-preset", "veryfast", "-crf", "21", "-maxrate", "7M", "-bufsize",
    "14M", "-pix_fmt", "yuv420p",
]


def command(args, timeout=180, error_code="FFMPEG_ERROR", cwd=None):
    try:
        with subprocess.Popen(
            args, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE, cwd=cwd
        ) as process:
            try:
                stdout, stderr = process.communicate(timeout=timeout)
            except BaseException:
                process.kill()
                process.communicate()
                raise
            if process.returncode:
                log.warning(
                    "media_command_failed", executable=args[0], return_code=process.returncode,
                    diagnostic=stderr[-1600:].decode(errors="replace"),
                )
                raise MediaError(error_code, "Media processing command failed")
            return stdout, stderr
    except subprocess.TimeoutExpired as exc:
        raise MediaError(error_code, "Media processing command timed out") from exc
    except FileNotFoundError as exc:
        raise MediaError(error_code, "Media processing executable unavailable") from exc


def ffmpeg(args, *, info=False, error_code="FFMPEG_ERROR", cwd=None):
    threads = str(settings().ffmpeg_threads)
    return command([
        "ffmpeg", "-nostdin", "-hide_banner", "-loglevel", "info" if info else "error",
        "-y", "-filter_threads", threads, "-filter_complex_threads", "1", *args,
    ], error_code=error_code, cwd=cwd)[1].decode(errors="replace")


def probe(path: Path, kind: str = "mov", *, error_code="INVALID_ASSET"):
    stdout, _ = command([
        "ffprobe", "-v", "error", "-protocol_whitelist", SAFE_PROTOCOLS,
        *demuxer(kind), "-show_streams", "-show_format", "-of", "json", str(path),
    ], timeout=15, error_code=error_code)
    return json.loads(stdout)


def demuxer(kind):
    options = ["-enable_drefs", "0"] if kind == "mov" else []
    return ["-f", FORMATS[kind][1], *options]


def media_input(path: Path, kind: str = "mov", *, loop=False):
    loop_options = ["-stream_loop", "-1"] if loop else []
    return [
        "-protocol_whitelist", SAFE_PROTOCOLS, "-threads", str(settings().ffmpeg_threads),
        *loop_options, *demuxer(kind), "-i", str(path),
    ]


def seconds(frames):
    return f"{frames / FPS:.6f}"


def wrap_caption(draw, text, font, width):
    lines = []
    for paragraph in text.splitlines():
        line = ""
        for word in paragraph.split():
            candidate = f"{line} {word}".strip()
            if draw.textlength(candidate, font=font) <= width:
                line = candidate
                continue
            if line:
                lines.append(line)
            line = ""
            for character in word:
                if line and draw.textlength(line + character, font=font) > width:
                    lines.append(line)
                    line = ""
                line += character
        if line:
            lines.append(line)
    return lines


def make_overlay(path, size, text, logo=None):
    width, height = size
    portrait = height > width
    image = Image.new("RGBA", size)
    draw = ImageDraw.Draw(image)
    pad = width // 35
    text_width = int(width * (0.68 if portrait else 0.80))
    if text:
        font_size = int(width * (0.058 if portrait else 0.034))
        while True:
            font = ImageFont.truetype(str(settings().font_path), font_size)
            lines = wrap_caption(draw, text, font, text_width - 2 * pad)
            spacing = font_size // 4
            caption = "\n".join(lines)
            left, top, right, bottom = draw.multiline_textbbox(
                (0, 0), caption, font=font, spacing=spacing, align="center"
            )
            if bottom - top <= height * 0.33 or font_size <= 22:
                break
            font_size -= 2
        x = int(width * (0.44 if portrait else 0.50))
        y = int(height * (0.76 if portrait else 0.88))
        text_height = bottom - top
        draw.rounded_rectangle(
            (x - text_width // 2, y - text_height - 2 * pad,
             x + text_width // 2, y),
            radius=pad, fill=(10, 17, 24, 218),
        )
        draw.multiline_text(
            (x - (right + left) / 2, y - text_height - pad - top), caption,
            font=font, spacing=spacing, align="center", fill="white",
        )
    if logo:
        with Image.open(logo) as original:
            mark = ImageOps.exif_transpose(original).convert("RGBA")
            mark.thumbnail((width // 4, height // 7))
            image.alpha_composite(mark, (int(width * 0.10), int(height * 0.14)))
    image.save(path)


def input_duration(data, stream_type):
    streams = [stream for stream in data["streams"] if stream["codec_type"] == stream_type]
    if stream_type == "video":
        streams = [
            stream for stream in streams if not stream.get("disposition", {}).get("attached_pic")
        ]
    if not streams:
        raise MediaError("INVALID_ASSET", f"Media has no usable {stream_type} stream")
    stream = streams[0]
    try:
        overall = float(data["format"]["duration"])
        duration = float(stream.get("duration", overall))
    except (KeyError, TypeError, ValueError) as exc:
        raise MediaError("INVALID_ASSET", "Media duration cannot be determined") from exc
    maximum = settings().max_asset_duration
    if not (0 < overall <= maximum and 0 < duration <= maximum):
        raise MediaError("INVALID_ASSET", "Media duration is outside the configured limit")
    if stream_type == "video" and not (
        0 < stream.get("width", 0) * stream.get("height", 0) <= 9_000_000
    ):
        raise MediaError("INVALID_ASSET", "Video dimensions exceed the supported limit")
    return stream["index"], duration


class FFmpegRenderer:
    def __init__(self, workdir: Path, aspect_ratio: str):
        self.workdir = workdir
        self.size = (1080, 1920) if aspect_ratio == "9:16" else (1920, 1080)
        self.threads = str(settings().ffmpeg_threads)

    def scene(self, index, shot, media, stream_index, overlay, frames):
        source, kind = media
        width, height = self.size
        output = self.workdir / f"scene-{index}.mp4"
        if kind in IMAGE_TYPES:
            normalized = self.workdir / "still.png"
            with Image.open(source) as original:
                image = ImageOps.exif_transpose(original).convert("RGBA")
                image = ImageOps.fit(image, (width, height), Image.Resampling.LANCZOS)
                background = Image.new("RGB", image.size, "#18232e")
                background.paste(image, mask=image.getchannel("A"))
                background.save(normalized)
            # Supersample the pan to reduce visible pixel jumps at 1080p.
            motion = (
                f"scale={width * 2}:{height * 2},"
                f"zoompan=z='1.02+0.13*on/{frames}':"
                f"x='(iw-iw/zoom)*(0.35+0.30*on/{frames})':y='(ih-ih/zoom)/2':"
                f"d={frames}:s={width}x{height}:fps={FPS},setsar=1"
            )
            inputs = media_input(normalized, "png")
        else:
            motion = (
                f"fps={FPS},scale={width}:{height}:force_original_aspect_ratio=increase,"
                f"crop={width}:{height},setsar=1"
            )
            if shot.generated is not None:
                motion += ",tpad=stop_mode=clone:stop_duration=0.2"
            inputs = media_input(source, kind, loop=shot.generated is None)
        if overlay is None:
            graph = f"[0:{stream_index}]{motion},format=yuv420p[out]"
        else:
            inputs.extend(media_input(overlay, "png"))
            graph = f"[0:{stream_index}]{motion}[base];[base][1:v]overlay,format=yuv420p[out]"
        ffmpeg([
            "-xerror", *inputs, "-filter_complex", graph,
            "-map", "[out]", "-frames:v", str(frames), "-an", *CODEC,
            "-threads", self.threads, str(output),
        ])
        return output, frames

    def join(self, clips):
        level = 0
        while len(clips) > 1:
            joined = []
            for start in range(0, len(clips), 4):
                group = clips[start:start + 4]
                if len(group) == 1:
                    joined.extend(group)
                    continue
                inputs, graph = [], []
                for index, (path, _) in enumerate(group):
                    inputs.extend(media_input(path))
                    graph.append(
                        f"[{index}:v]fps={FPS}:start_time=0,settb=1/{FPS},format=yuv444p[v{index}]"
                    )
                previous = "[v0]"
                frames = group[0][1]
                for index, (_, length) in enumerate(group[1:], start=1):
                    output = f"[x{index}]"
                    graph.append(
                        f"{previous}[v{index}]xfade=transition=fade:"
                        f"duration={seconds(OVERLAP)}:offset={seconds(frames - OVERLAP)}{output}"
                    )
                    previous = output
                    frames += length - OVERLAP
                target = self.workdir / f"join-{level}-{start}.mp4"
                ffmpeg([
                    *inputs, "-filter_complex", ";".join(graph), "-map", previous,
                    "-frames:v", str(frames), "-an", *CODEC, "-threads", self.threads, str(target),
                ])
                for path, _ in group:
                    path.unlink()
                joined.append((target, frames))
            clips = joined
            level += 1
        return clips[0][0]

    def join_hybrid(self, clips, shots):
        joined, originals = [], []
        for clip, shot in zip(clips, shots):
            if shot.generated is None:
                originals.append(clip)
                continue
            if originals:
                joined.append(self.join(originals))
                originals = []
            joined.append(clip[0])
        if originals:
            joined.append(self.join(originals))

        inputs, filters = [], []
        for index, path in enumerate(joined):
            inputs.extend(media_input(path))
            filters.append(f"[{index}:v]setpts=PTS-STARTPTS[v{index}]")
        labels = "".join(f"[v{index}]" for index in range(len(joined)))
        filters.append(f"{labels}concat=n={len(joined)}:v=1:a=0,fps={FPS}[out]")
        target = self.workdir / "hybrid.mp4"
        ffmpeg([
            *inputs, "-filter_complex", ";".join(filters), "-map", "[out]", "-an",
            "-frames:v", str(sum(shot.frames for shot in shots)), *CODEC,
            "-threads", self.threads, str(target),
        ])
        return target

    def caption_timeline(self, shots, logo):
        lines = ["ffconcat version 1.0"]
        for index, shot in enumerate(shots):
            name = f"caption-{index}.png"
            make_overlay(
                self.workdir / name, self.size, normalize("NFC", shot.caption),
                logo if shot.closing else None,
            )
            lines.extend((f"file {name}", f"duration {seconds(shot.frames)}"))
        lines.append(f"file caption-{len(shots) - 1}.png")
        timeline = self.workdir / "captions.ffconcat"
        timeline.write_text("\n".join(lines) + "\n", encoding="ascii")
        return timeline

    def finish(self, video, music, voice, duration, *, captions=None, subtitles=False,
               synthesized=False):
        inputs = media_input(video)
        graph = []
        tracks = {}
        for name, media in (("music", music), ("voice", voice)):
            if media:
                path, kind, stream, length = media
                index = len(tracks) + 1
                inputs.extend(media_input(path, kind))
                tracks[name] = f"[{name}]"
                volume = 0.25 if name == "music" else 0.90
                fade = ""
                if name == "music":
                    audible = min(length, duration)
                    fade_length = min(0.6, audible / 2)
                    fade = (
                        "afade=t=in:d=0.25,"
                        f"afade=t=out:st={audible - fade_length:.6f}:d={fade_length:.6f},"
                    )
                graph.append(
                    f"[{index}:{stream}]aresample=48000,"
                    "aformat=sample_fmts=fltp:channel_layouts=stereo,"
                    f"asetpts=PTS-STARTPTS,volume={volume},{fade}"
                    f"apad,atrim=duration={duration}[{name}]"
                )
        if len(tracks) == 2:
            inputs_for_mix = "[music][voice]"
            if settings().audio_ducking_enabled:
                graph.extend([
                    "[voice]asplit=2[vo][sc]",
                    "[music][sc]sidechaincompress=threshold=0.02:ratio=8:attack=25:release=350[duck]",
                ])
                inputs_for_mix = "[duck][vo]"
            graph.append(f"{inputs_for_mix}amix=inputs=2:normalize=0[mixed]")
            audio = "[mixed]"
        elif tracks:
            audio = next(iter(tracks.values()))
        else:
            graph.append("anullsrc=r=48000:cl=stereo[silence]")
            audio = "[silence]"
        fade_out = 0.05 if synthesized else 0.6
        graph.append(
            f"{audio}alimiter=level=0:limit=0.95:latency=1,"
            f"afade=t=out:st={duration - fade_out}:d={fade_out}[audio]"
        )
        video_filter = f"fade=t=in:d=0.3,fade=t=out:st={duration - 0.4}:d=0.4"
        if subtitles:
            video_filter += ",subtitles=filename=narration.ass"
        if captions is None:
            video_mapping = ["-map", "0:v:0", "-vf", video_filter]
        else:
            inputs.extend([
                "-protocol_whitelist", SAFE_PROTOCOLS, "-f", "concat", "-safe", "1",
                "-i", str(captions),
            ])
            index = 1 + len(tracks)
            graph.extend([
                f"[{index}:v]fps={FPS}:start_time=0[labels]",
                f"[0:v][labels]overlay,{video_filter}[out]",
            ])
            video_mapping = ["-map", "[out]"]
        final = self.workdir / "output.mp4"
        ffmpeg([
            *inputs, "-filter_complex", ";".join(graph), *video_mapping, "-map", "[audio]",
            "-t", str(duration), "-r", str(FPS), *WEB_CODEC, "-threads", self.threads,
            "-c:a", "aac", "-b:a", "160k", "-movflags", "+faststart", str(final),
        ], cwd=self.workdir if subtitles else None,
            error_code="SUBTITLE_RENDER_FAILED" if subtitles else "FFMPEG_ERROR")
        return final

    def validate(self, path, shots, duration):
        metadata = probe(path, error_code="QUALITY_CHECK_FAILED")
        video = [s for s in metadata["streams"] if s["codec_type"] == "video"]
        audio = [s for s in metadata["streams"] if s["codec_type"] == "audio"]
        if len(video) != 1 or len(audio) != 1:
            raise MediaError("QUALITY_CHECK_FAILED", "Output must contain video and audio")
        stream = video[0]
        if (
            stream["codec_name"] != "h264" or stream["pix_fmt"] != "yuv420p"
            or (stream["width"], stream["height"]) != self.size
            or stream["avg_frame_rate"] != "30/1" or audio[0]["codec_name"] != "aac"
            or any(abs(float(s["duration"]) - duration) > 0.2 for s in
                   (stream, audio[0], metadata["format"]))
        ):
            raise MediaError("QUALITY_CHECK_FAILED", "Output format or duration is incorrect")
        diagnostics = ffmpeg([
            "-xerror", *media_input(path), "-map", "0:v:0", "-map", "0:a:0",
            "-vf", "scale=320:-2,blackdetect=d=0.7:pix_th=0.10,freezedetect=n=-48dB:d=1.2",
            "-f", "null", "-",
        ], info=True, error_code="QUALITY_CHECK_FAILED")
        if re.search(r"black_start:", diagnostics):
            raise MediaError("QUALITY_CHECK_FAILED", "Output contains a prolonged black scene")

        moving = []
        cursor = 0
        for shot in shots:
            if not shot.image:
                moving.append((cursor / FPS, (cursor + shot.frames) / FPS))
            cursor += shot.frames
        start = None
        for key, timestamp in re.findall(r"freeze_(start|end): ([\d.]+)", diagnostics):
            if key == "start":
                start = float(timestamp)
            elif start is not None:
                self.check_freeze(start, float(timestamp), moving)
                start = None
        if start is not None:
            self.check_freeze(start, duration, moving)
        return metadata

    @staticmethod
    def check_freeze(start, end, moving):
        if any(min(end, right) - max(start, left) >= 1.2 for left, right in moving):
            raise MediaError("QUALITY_CHECK_FAILED", "Output contains a freeze in a video scene")

