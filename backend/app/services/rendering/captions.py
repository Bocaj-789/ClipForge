"""Caption cue building, SRT export and PNG caption rendering (Pillow).

Homebrew's ffmpeg build ships without libass/freetype, so captions are rasterised with
Pillow and composited with ffmpeg's ``overlay`` filter instead of ``subtitles``/``drawtext``.
This also removes any subtitle-markup injection risk: transcript text is only ever drawn
as pixels, never interpreted.
"""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

from app.core.security import clean_text

FONT_CANDIDATES = [
    "/System/Library/Fonts/Supplemental/Arial Bold.ttf",
    "/Library/Fonts/Arial Unicode.ttf",
    "/System/Library/Fonts/Helvetica.ttc",
    "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
]


@dataclass
class Word:
    start: float
    end: float
    text: str


@dataclass
class Cue:
    start: float
    end: float
    words: list[Word]

    @property
    def text(self) -> str:
        return " ".join(w.text for w in self.words)


_TERMINAL = re.compile(r"[.!?…:;,]$")


def sanitize_caption_text(text: str) -> str:
    text = unicodedata.normalize("NFC", text)
    text = clean_text(text.replace("\n", " ").replace("\r", " "), 200)
    return re.sub(r"\s+", " ", text)


def words_for_range(segments: list[dict], start: float, end: float) -> list[Word]:
    """Collect words in [start, end] (clip-relative timings). Falls back to segment text."""
    out: list[Word] = []
    for seg in segments:
        if seg["end"] < start or seg["start"] > end:
            continue
        words = seg.get("words") or []
        if words:
            for w in words:
                mid = (w["start"] + w["end"]) / 2
                if start <= mid <= end:
                    t = sanitize_caption_text(w["word"])
                    if t:
                        out.append(Word(max(0.0, w["start"] - start), min(end, w["end"]) - start, t))
        else:
            # No word timings: distribute segment text evenly.
            toks = sanitize_caption_text(seg.get("text", "")).split()
            if not toks:
                continue
            s0, s1 = max(seg["start"], start), min(seg["end"], end)
            step = (s1 - s0) / len(toks) if s1 > s0 else 0
            for i, t in enumerate(toks):
                out.append(Word(s0 - start + i * step, s0 - start + (i + 1) * step, t))
    return out


def build_cues(
    words: list[Word], *, max_words: int = 4, max_chars: int = 22, max_dur: float = 2.6
) -> list[Cue]:
    cues: list[Cue] = []
    cur: list[Word] = []
    for w in words:
        if cur:
            chars = len(" ".join(x.text for x in cur)) + 1 + len(w.text)
            too_long = len(cur) >= max_words or chars > max_chars or (w.end - cur[0].start) > max_dur
            gap = w.start - cur[-1].end > 0.6
            if too_long or gap:
                cues.append(Cue(cur[0].start, cur[-1].end, cur))
                cur = []
        cur.append(w)
        if _TERMINAL.search(w.text):
            cues.append(Cue(cur[0].start, cur[-1].end, cur))
            cur = []
    if cur:
        cues.append(Cue(cur[0].start, cur[-1].end, cur))
    # Ensure monotonic, non-overlapping and minimally readable timings.
    for i, c in enumerate(cues):
        nxt = cues[i + 1].start if i + 1 < len(cues) else None
        c.end = max(c.end, c.start + 0.35)
        if nxt is not None and c.end > nxt:
            c.end = nxt
    return [c for c in cues if c.end > c.start]


def _srt_time(t: float) -> str:
    ms = int(round(max(0.0, t) * 1000))
    h, ms = divmod(ms, 3_600_000)
    m, ms = divmod(ms, 60_000)
    s, ms = divmod(ms, 1000)
    return f"{h:02}:{m:02}:{s:02},{ms:03}"


def to_srt(cues: list[Cue]) -> str:
    lines = []
    for i, c in enumerate(cues, 1):
        # "-->" and blank lines would corrupt the SRT structure.
        text = c.text.replace("-->", "→").strip() or "…"
        lines += [str(i), f"{_srt_time(c.start)} --> {_srt_time(c.end)}", text, ""]
    return "\n".join(lines)


def resolve_font(path: str | None) -> str:
    for p in [path, *FONT_CANDIDATES]:
        if p and Path(p).is_file():
            return p
    raise FileNotFoundError(
        "Inget typsnitt för undertexter hittades. Ange caption_font_path i inställningarna."
    )


@dataclass(frozen=True)
class CaptionStyle:
    name: str
    size_ratio: float  # font size relative to canvas width
    uppercase: bool
    fill: tuple[int, int, int, int]
    highlight: tuple[int, int, int, int] | None
    stroke_ratio: float
    box: bool


STYLES = {
    "clean": CaptionStyle("clean", 0.058, False, (255, 255, 255, 255), None, 0.0, True),
    "bold": CaptionStyle("bold", 0.072, True, (255, 255, 255, 255), None, 0.11, False),
    "karaoke": CaptionStyle("karaoke", 0.072, True, (255, 255, 255, 255), (255, 214, 10, 255), 0.11, False),
}


def _wrap(
    draw: ImageDraw.ImageDraw, words: list[str], font: ImageFont.FreeTypeFont, max_w: int
) -> list[list[int]]:
    lines: list[list[int]] = [[]]
    for i, _w in enumerate(words):
        trial = " ".join(words[j] for j in [*lines[-1], i])
        if lines[-1] and draw.textlength(trial, font=font) > max_w:
            lines.append([i])
        else:
            lines[-1].append(i)
    return lines


def render_caption_png(
    text_words: list[str],
    out: Path,
    *,
    canvas_w: int,
    band_h: int,
    style: CaptionStyle,
    font_path: str,
    highlight_index: int | None = None,
) -> None:
    img = Image.new("RGBA", (canvas_w, band_h), (0, 0, 0, 0))
    if text_words:
        draw = ImageDraw.Draw(img)
        size = int(canvas_w * style.size_ratio)
        words = [w.upper() if style.uppercase else w for w in text_words]
        max_w = int(canvas_w * 0.84)
        while True:
            font = ImageFont.truetype(font_path, size)
            lines = _wrap(draw, words, font, max_w)
            if len(lines) <= 2 or size <= 28:
                break
            size = int(size * 0.88)
        stroke = int(size * style.stroke_ratio)
        line_h = int(size * 1.18)
        total_h = line_h * len(lines)
        y = (band_h - total_h) // 2
        space = draw.textlength(" ", font=font)
        for line in lines:
            widths = [draw.textlength(words[i], font=font) for i in line]
            line_w = sum(widths) + space * (len(line) - 1)
            x = (canvas_w - line_w) / 2
            if style.box:
                pad = size * 0.25
                draw.rounded_rectangle(
                    (x - pad, y - pad * 0.4, x + line_w + pad, y + line_h - pad * 0.2),
                    radius=int(size * 0.25),
                    fill=(0, 0, 0, 165),
                )
            for i, wdt in zip(line, widths, strict=True):
                fill = style.highlight if (style.highlight and i == highlight_index) else style.fill
                draw.text(
                    (x, y), words[i], font=font, fill=fill, stroke_width=stroke, stroke_fill=(0, 0, 0, 255)
                )
                x += wdt + space
            y += line_h
    img.save(out, "PNG")


def build_caption_track(
    cues: list[Cue],
    *,
    clip_duration: float,
    work_dir: Path,
    canvas_w: int,
    band_h: int,
    style_name: str,
    font_path: str,
) -> Path:
    """Render PNG frames and an ffconcat list that forms one timed caption stream."""
    style = STYLES.get(style_name, STYLES["bold"])
    work_dir.mkdir(parents=True, exist_ok=True)
    blank = work_dir / "blank.png"
    render_caption_png([], blank, canvas_w=canvas_w, band_h=band_h, style=style, font_path=font_path)
    entries: list[tuple[str, float]] = []
    t = 0.0
    n = 0

    def add(name: str, until: float) -> None:
        nonlocal t
        dur = round(until - t, 3)
        if dur > 0.001:
            entries.append((name, dur))
            t = until

    for cue in cues:
        start, end = max(cue.start, t), min(cue.end, clip_duration)
        if end <= start:
            continue
        add("blank.png", start)
        words = [w.text for w in cue.words]
        if style.highlight:
            for k, _w in enumerate(cue.words):
                n += 1
                name = f"c{n:05}.png"
                render_caption_png(
                    words,
                    work_dir / name,
                    canvas_w=canvas_w,
                    band_h=band_h,
                    style=style,
                    font_path=font_path,
                    highlight_index=k,
                )
                w_end = cue.words[k + 1].start if k + 1 < len(cue.words) else end
                add(name, min(max(w_end, t + 0.05), end))
            add(entries[-1][0] if entries else "blank.png", end)
        else:
            n += 1
            name = f"c{n:05}.png"
            render_caption_png(
                words, work_dir / name, canvas_w=canvas_w, band_h=band_h, style=style, font_path=font_path
            )
            add(name, end)
    add("blank.png", clip_duration)
    if not entries:
        entries.append(("blank.png", clip_duration))
    lines = ["ffconcat version 1.0"]
    for name, dur in entries:
        lines += [f"file {name}", f"duration {dur:.3f}"]
    lines.append(f"file {entries[-1][0]}")  # concat demuxer needs the last file repeated
    listing = work_dir / "captions.ffconcat"
    listing.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return listing
