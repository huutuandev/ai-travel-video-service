from app.services.subtitles import write_subtitles


def test_ass_uses_measured_timestamps_and_escapes_tags(tmp_path):
    path = write_subtitles(tmp_path, [{
        "spokenText": "Khám phá Đà Nẵng {\\pos(0,0)}",
        "startSeconds": 2.04, "endSeconds": 4.32,
    }], (1080, 1920))
    ass = path.read_text(encoding="utf-8")
    assert "PlayResY: 1920" in ass
    assert "0:00:02.04,0:00:04.32" in ass
    assert "Khám phá Đà Nẵng" in ass
    assert "{\\pos" not in ass

