from __future__ import annotations

from pathlib import Path

from PIL import Image

from app.services.intelligence.candidate_extractor import (
    enumerate_windows,
    overlap_ratio,
    select_non_overlapping,
    sentences_from_segments,
    validate_range,
)
from app.services.intelligence.llm_provider import HeuristicProvider
from app.services.intelligence.metadata import hashtag, sanitize_description, sanitize_title, suggest_hashtags
from app.services.intelligence.scoring import Sentence, score_window
from app.services.rendering import captions as cap
from tests.conftest import fake_segments


def test_sentences_split_on_punctuation_and_pauses() -> None:
    sents = sentences_from_segments(fake_segments())
    assert len(sents) >= 10
    assert all(s.end > s.start for s in sents)
    assert sents[0].text.startswith("Visste du")


def test_windows_respect_duration_bounds_and_do_not_overlap() -> None:
    sents = sentences_from_segments(fake_segments())
    duration = sents[-1].end + 1
    wins = HeuristicProvider().propose(
        sents, min_s=10, max_s=25, media_duration=duration, keywords=[], limit=5
    )
    assert wins
    for w in wins:
        assert validate_range(w.start, w.end, media_duration=duration, min_s=10, max_s=25)
        assert 0 <= w.score.final <= 100
    for i, a in enumerate(wins):
        for b in wins[i + 1 :]:
            assert overlap_ratio((a.start, a.end), (b.start, b.end)) <= 0.25


def test_select_non_overlapping_prefers_higher_scores() -> None:
    sents = sentences_from_segments(fake_segments())
    wins = enumerate_windows(sents, min_s=8, max_s=20, media_duration=sents[-1].end + 1)
    chosen = select_non_overlapping(wins, limit=2)
    assert chosen[0].score.final == max(w.score.final for w in wins)


def test_validate_range_rejects_out_of_media() -> None:
    assert not validate_range(-1, 20, media_duration=100, min_s=10, max_s=60)
    assert not validate_range(90, 120, media_duration=100, min_s=10, max_s=60)
    assert not validate_range(10, 12, media_duration=100, min_s=10, max_s=60)


def test_hook_and_context_heuristics() -> None:
    good = score_window(
        [
            Sentence(0, 3, "Visste du att det här är hemligheten?", [0.9] * 7),
            Sentence(3, 8, "Men det är faktiskt sant.", [0.9] * 5),
        ]
    )
    bad = score_window(
        [Sentence(0, 3, "och sen gick han hem", [0.9] * 5), Sentence(3, 8, "typ så där", [0.9] * 3)]
    )
    assert good.final > bad.final
    assert "missing_context" in bad.penalties
    assert "fråga" in good.explanation


def test_performance_adjustment_is_capped() -> None:
    s = [Sentence(0, 5, "Hej där, hur mår du idag?", [0.9] * 6)]
    base = score_window(s).final
    assert score_window(s, performance_adjustment=50).final <= base + 10


def test_title_and_description_sanitisation() -> None:
    assert sanitize_title("<b>Hej</b>\x00 världen " * 20).startswith("bHej/b")
    assert len(sanitize_title("ord " * 100)) <= 95
    assert "<" not in sanitize_description("a <script> b")
    assert hashtag("räksmörgås!") == "#räksmörgås"
    assert hashtag("x") is None
    assert suggest_hashtags(["mat", "mat", "#Öland"], "matlagning")[:3] == ["#shorts", "#matlagning", "#mat"]


def test_caption_cues_and_srt() -> None:
    words = cap.words_for_range(fake_segments(), 0, 12)
    cues = cap.build_cues(words, max_words=3)
    assert cues and all(len(c.words) <= 3 for c in cues)
    assert all(cues[i].end <= cues[i + 1].start + 1e-6 for i in range(len(cues) - 1))
    srt = cap.to_srt([cap.Cue(0, 1, [cap.Word(0, 1, "a-->b")])])
    assert "-->" in srt.splitlines()[1] and "a→b" in srt


def test_caption_text_is_sanitised() -> None:
    assert cap.sanitize_caption_text("{\\an8}hej\x00\nvärld") == "{\\an8}hej värld"


def test_caption_png_renders_swedish_characters(tmp_path: Path) -> None:
    font = cap.resolve_font(None)
    out = tmp_path / "c.png"
    cap.render_caption_png(
        ["ÅÄÖ", "räksmörgås"], out, canvas_w=1080, band_h=300, style=cap.STYLES["bold"], font_path=font
    )
    img = Image.open(out)
    assert img.size == (1080, 300) and img.mode == "RGBA"
    assert img.getbbox() is not None  # something was drawn
    blank = tmp_path / "b.png"
    cap.render_caption_png([], blank, canvas_w=100, band_h=50, style=cap.STYLES["bold"], font_path=font)
    assert Image.open(blank).getbbox() is None


def test_caption_track_listing(tmp_path: Path) -> None:
    font = cap.resolve_font(None)
    cues = cap.build_cues(cap.words_for_range(fake_segments(), 0, 10))
    listing = cap.build_caption_track(
        cues,
        clip_duration=10,
        work_dir=tmp_path,
        canvas_w=540,
        band_h=200,
        style_name="karaoke",
        font_path=font,
    )
    lines = listing.read_text().splitlines()
    assert lines[0] == "ffconcat version 1.0"
    total = sum(float(x.split()[1]) for x in lines if x.startswith("duration"))
    assert abs(total - 10) < 0.01
    assert all("/" not in x and ".." not in x for x in lines if x.startswith("file"))
