"""Turn timestamped transcript segments into sentence units and candidate clip windows."""

from __future__ import annotations

import re
from dataclasses import dataclass

from app.services.intelligence.scoring import ScoreBreakdown, Sentence, score_window

_END = re.compile(r"[.!?…]$")
PAUSE_SPLIT = 0.8


def sentences_from_segments(segments: list[dict]) -> list[Sentence]:
    out: list[Sentence] = []
    cur_words: list[dict] = []

    def flush() -> None:
        if not cur_words:
            return
        text = " ".join(w["word"].strip() for w in cur_words).strip()
        text = re.sub(r"\s+([,.!?…])", r"\1", text)
        out.append(
            Sentence(
                cur_words[0]["start"],
                cur_words[-1]["end"],
                text,
                [float(w.get("probability", 0.0)) for w in cur_words],
            )
        )
        cur_words.clear()

    for seg in segments:
        words = seg.get("words") or []
        if not words:
            flush()
            if seg.get("text", "").strip():
                out.append(
                    Sentence(
                        seg["start"], seg["end"], seg["text"].strip(), [], float(seg.get("avg_logprob", 0.0))
                    )
                )
            continue
        for w in words:
            if cur_words and w["start"] - cur_words[-1]["end"] > PAUSE_SPLIT:
                flush()
            cur_words.append(w)
            if _END.search(w["word"].strip()):
                flush()
    flush()
    return [s for s in out if s.end > s.start]


@dataclass
class Window:
    start_idx: int
    end_idx: int
    start: float
    end: float
    score: ScoreBreakdown
    excerpt: str
    provider: str = "heuristic"
    title: str | None = None
    rationale: str | None = None


def pad_bounds(start: float, end: float, media_duration: float) -> tuple[float, float]:
    return max(0.0, start - 0.25), min(media_duration, end + 0.45)


def overlap_ratio(a: tuple[float, float], b: tuple[float, float]) -> float:
    inter = max(0.0, min(a[1], b[1]) - max(a[0], b[0]))
    shorter = min(a[1] - a[0], b[1] - b[0])
    return inter / shorter if shorter > 0 else 0.0


def make_window(
    sentences: list[Sentence],
    i: int,
    j: int,
    media_duration: float,
    keywords: list[str] | None,
    provider: str = "heuristic",
) -> Window:
    span = sentences[i : j + 1]
    s, e = pad_bounds(span[0].start, span[-1].end, media_duration)
    sc = score_window(span, keywords=keywords)
    excerpt = " ".join(x.text for x in span)
    return Window(i, j, round(s, 3), round(e, 3), sc, excerpt[:2000], provider)


def enumerate_windows(
    sentences: list[Sentence],
    *,
    min_s: float,
    max_s: float,
    media_duration: float,
    keywords: list[str] | None = None,
) -> list[Window]:
    """For every starting sentence keep the best-scoring valid end. Linear in video length."""
    wins: list[Window] = []
    n = len(sentences)
    for i in range(n):
        best: Window | None = None
        for j in range(i, n):
            dur = sentences[j].end - sentences[i].start
            if dur > max_s:
                break
            if dur >= min_s:
                w = make_window(sentences, i, j, media_duration, keywords)
                if best is None or w.score.final > best.score.final:
                    best = w
        if best is not None:
            wins.append(best)
    return wins


def select_non_overlapping(wins: list[Window], *, limit: int, max_overlap: float = 0.25) -> list[Window]:
    """Greedy pick by score, rejecting near-duplicates of already chosen windows."""
    chosen: list[Window] = []
    for w in sorted(wins, key=lambda x: x.score.final, reverse=True):
        if all(overlap_ratio((w.start, w.end), (c.start, c.end)) <= max_overlap for c in chosen):
            chosen.append(w)
        if len(chosen) >= limit:
            break
    return chosen


def validate_range(start: float, end: float, *, media_duration: float, min_s: float, max_s: float) -> bool:
    return 0 <= start < end <= media_duration + 0.01 and (min_s - 1.0) <= end - start <= max_s + 1.5
