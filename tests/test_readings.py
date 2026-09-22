from app.readings import furigana
from app.subtitles import render_srt, render_ass


def test_furigana_preserves_text_and_only_annotates_kanji():
    text = "  私は日本語を食べる。\nカタカナ <b>& ありがとう！  "
    parts = furigana(text)
    assert "".join(p["text"] for p in parts) == text
    assert {"text": "日本語", "reading": "にほんご"} in parts
    assert {"text": "食", "reading": "た"} in parts
    assert all(not p["reading"] for p in parts if p["text"] in ("カタカナ", "ありがとう"))
    cue = {"id": "one", "start": 0, "end": 3, "ja": "日本語", "zh": "日语"}
    before = (render_srt([cue], 0, 3), render_ass([cue], 0, 3))
    furigana(cue["ja"])
    assert before == (render_srt([cue], 0, 3), render_ass([cue], 0, 3))
