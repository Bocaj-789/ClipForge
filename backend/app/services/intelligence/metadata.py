"""Deterministic title / description / hashtag suggestions with strict sanitisation."""

from __future__ import annotations

import re

from app.core.security import clean_text

TITLE_MAX = 95  # YouTube's limit is 100 characters; keep headroom
DESC_MAX = 4500  # YouTube's limit is 5000 bytes
_TAG = re.compile(r"[^0-9A-Za-zÅÄÖåäöÉéÜü_]")


def sanitize_title(text: str) -> str:
    t = clean_text(text.replace("\n", " "), 300)
    t = t.replace("<", "").replace(">", "")  # YouTube rejects angle brackets
    t = re.sub(r"\s+", " ", t).strip(" -–,;:")
    if len(t) > TITLE_MAX:
        t = t[: TITLE_MAX - 1].rsplit(" ", 1)[0].rstrip(" ,;:") + "…"
    return t


def sanitize_description(text: str) -> str:
    t = text.replace("<", "").replace(">", "")
    t = re.sub(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]", "", t)
    return t[:DESC_MAX].strip()


def hashtag(word: str) -> str | None:
    w = _TAG.sub("", word.strip().lstrip("#"))
    return f"#{w}" if 2 <= len(w) <= 30 else None


def suggest_title(excerpt: str) -> str:
    first = re.split(r"(?<=[.!?…])\s+", excerpt.strip(), maxsplit=1)[0]
    if len(first) < 12 and len(excerpt) > len(first):
        first = excerpt[:80]
    return sanitize_title(first) or "Klipp"


def suggest_hashtags(keywords: list[str], category: str | None) -> list[str]:
    tags = ["#shorts"]
    for k in [category or "", *keywords]:
        h = hashtag(k)
        if h and h.lower() not in {t.lower() for t in tags}:
            tags.append(h)
    return tags[:8]


def suggest_description(
    excerpt: str,
    *,
    source_title: str | None,
    creator: str | None,
    source_url: str | None,
    hashtags: list[str],
) -> str:
    parts = [clean_text(excerpt, 300)]
    credit = []
    if creator:
        credit.append(f"Original: {clean_text(creator, 100)}")
    if source_title:
        credit.append(f'"{clean_text(source_title, 150)}"')
    if source_url and source_url.startswith("https://"):
        credit.append(source_url)
    if credit:
        parts.append("Källa: " + " – ".join(credit))
    parts.append(" ".join(hashtags))
    return sanitize_description("\n\n".join(p for p in parts if p))
