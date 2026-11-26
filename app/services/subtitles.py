from pathlib import Path
from textwrap import wrap


def timestamp(centiseconds):
    minutes, remaining = divmod(centiseconds, 6000)
    hours, minutes = divmod(minutes, 60)
    seconds, fraction = divmod(remaining, 100)
    return f"{hours}:{minutes:02d}:{seconds:02d}.{fraction:02d}"


def write_subtitles(directory: Path, segments, size):
    width, height = size
    portrait = height > width
    font = 43 if portrait else 38
    margin = 100 if portrait else 34
    path = directory / "narration.ass"
    lines = [
        "[Script Info]", "ScriptType: v4.00+", f"PlayResX: {width}", f"PlayResY: {height}",
        "WrapStyle: 2", "ScaledBorderAndShadow: yes", "[V4+ Styles]",
        "Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, "
        "BackColour, Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, "
        "BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding",
        f"Style: Default,DejaVu Sans,{font},&H00FFFFFF,&H00FFFFFF,&H00101010,&H80000000,"
        f"-1,0,0,0,100,100,0,0,1,3,1,2,55,55,{margin},1",
        "[Events]",
        "Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text",
    ]
    for segment in segments:
        text = " ".join(segment["spokenText"].split()).translate(str.maketrans({
            "\\": "＼", "{": "｛", "}": "｝",
        }))
        wrapped = wrap(text, width=34 if portrait else 48)
        chunks = ["\\N".join(wrapped[i:i + 2]) for i in range(0, len(wrapped), 2)]
        start = round(segment["startSeconds"] * 100)
        end = round(segment["endSeconds"] * 100)
        width_sum = sum(map(len, chunks))
        consumed = 0
        for chunk in chunks:
            left = start + round((end - start) * consumed / width_sum)
            consumed += len(chunk)
            right = start + round((end - start) * consumed / width_sum)
            if right > left:
                lines.append(
                    f"Dialogue: 0,{timestamp(left)},{timestamp(right)},Default,,0,0,0,,{chunk}"
                )
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return path

