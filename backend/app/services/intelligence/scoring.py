"""Transparent heuristic clip scoring (0–100). A ranking aid, not a prediction of views."""

from __future__ import annotations

import re
from dataclasses import dataclass, field

HOOK_PHRASES = [
    # Swedish
    "visste du",
    "det här",
    "varför",
    "hur ",
    "aldrig",
    "ingen",
    "hemligheten",
    "sanningen",
    "tänk dig",
    "det bästa",
    "det värsta",
    "jag trodde",
    "ärligt talat",
    "lyssna",
    "vänta",
    "så här",
    # English
    "did you know",
    "here's why",
    "the secret",
    "nobody",
    "never",
    "what if",
    "the truth",
    "imagine",
    "the best",
    "the worst",
    "i thought",
    "honestly",
    "listen",
    "wait",
    "this is how",
    "why ",
    "how ",
]
CONTRAST = ["men ", "faktiskt", "egentligen", "plötsligt", "but ", "actually", "suddenly", "turns out"]
DANGLING_START = {
    "och",
    "men",
    "så",
    "eftersom",
    "att",
    "som",
    "and",
    "but",
    "so",
    "because",
    "which",
    "that",
    "then",
    "sen",
    "då",
}
PRONOUN_START = {"han", "hon", "den", "det", "de", "dem", "he", "she", "it", "they", "them", "this", "these"}
SUPERLATIVE = re.compile(r"\b\w+(ast|aste|est)\b|\b(mest|most|bäst|best|värst|worst)\b", re.I)
NUMBER = re.compile(r"\b\d+([.,]\d+)?\b")
_WORD = re.compile(r"[\wåäöÅÄÖ']+", re.UNICODE)


@dataclass
class Sentence:
    start: float
    end: float
    text: str
    word_probs: list[float] = field(default_factory=list)
    avg_logprob: float = 0.0


@dataclass
class ScoreBreakdown:
    hook: float
    coherence: float
    interest: float
    energy: float
    clarity: float
    channel_fit: float
    penalties: dict[str, float]
    final: float
    explanation: str
    warnings: list[str]


def _words(text: str) -> list[str]:
    return _WORD.findall(text.lower())


def score_window(
    sentences: list[Sentence], *, keywords: list[str] | None = None, performance_adjustment: float = 0.0
) -> ScoreBreakdown:
    text = " ".join(s.text for s in sentences).strip()
    low = text.lower()
    words = _words(text)
    first = sentences[0].text.strip()
    first_low = first.lower()
    first_words = _words(first)
    duration = max(0.1, sentences[-1].end - sentences[0].start)
    reasons: list[str] = []
    warnings: list[str] = []

    # Hook strength 0–25
    hook = 4.0
    if "?" in first:
        hook += 8
        reasons.append("öppnar med en fråga")
    if any(first_low.startswith(p) or f" {p}" in f" {first_low[:60]}" for p in HOOK_PHRASES):
        hook += 7
        reasons.append("tydlig krok i början")
    if 0 < len(first_words) <= 12:
        hook += 4
    if "!" in first:
        hook += 3
    if {"du", "dig", "you", "your"} & set(first_words):
        hook += 3
    hook = min(25.0, hook)

    # Coherence 0–25
    coherence = 8.0  # windows always start on a sentence boundary
    if re.search(r"[.!?…]$", text):
        coherence += 8
    else:
        warnings.append("Slutar mitt i en mening.")
    if 2 <= len(sentences) <= 12:
        coherence += 5
    first_tok = first_words[0] if first_words else ""
    if first_tok not in DANGLING_START and first_tok not in PRONOUN_START:
        coherence += 4
    coherence = min(25.0, coherence)

    # Interest / novelty 0–20
    interest = 0.0
    diversity = len(set(words)) / len(words) if words else 0
    interest += min(8.0, diversity * 10)
    if NUMBER.search(text):
        interest += 3
    if SUPERLATIVE.search(text):
        interest += 3
    if any(c in low for c in CONTRAST):
        interest += 4
        reasons.append("innehåller en vändning")
    if "?" in text[len(first) :]:
        interest += 2
    interest = min(20.0, interest)

    # Energy 0–10
    wps = len(words) / duration
    energy = min(5.0, max(0.0, (wps - 1.5) * 2.5))
    energy += min(3.0, text.count("!") * 1.5)
    if re.search(r"\b(haha|hahaha|skratt|laughter|wow|oj)\b", low):
        energy += 2
    energy = min(10.0, energy)

    # Clarity 0–10 (from ASR confidence where available)
    probs = [p for s in sentences for p in s.word_probs]
    if probs:
        mean_p = sum(probs) / len(probs)
        clarity = round(10 * max(0.0, min(1.0, (mean_p - 0.4) / 0.55)), 2)
    else:
        clarity = 5.0
        warnings.append("Ingen ordsäkerhet tillgänglig; tydlighet uppskattad.")

    # Channel fit 0–10
    kws = [k.lower().strip() for k in (keywords or []) if k.strip()]
    if kws:
        hits = sum(1 for k in kws if k in low)
        channel_fit = min(10.0, 3.0 + hits * 3.5)
        if hits:
            reasons.append(f"matchar {hits} kanalnyckelord")
    else:
        channel_fit = 5.0

    penalties: dict[str, float] = {}
    if first_tok in DANGLING_START or first_tok in PRONOUN_START:
        penalties["missing_context"] = 6.0
        warnings.append("Börjar med ett ord som kan kräva tidigare sammanhang.")
    if wps < 1.0:
        penalties["little_speech"] = 6.0
        warnings.append("Lite tal i klippet.")
    if probs and clarity < 3:
        penalties["low_audio_quality"] = 5.0
        warnings.append("Låg transkriptionssäkerhet – ljudet kan vara otydligt.")

    adj = max(-10.0, min(10.0, performance_adjustment))
    total = hook + coherence + interest + energy + clarity + channel_fit - sum(penalties.values()) + adj
    final = round(max(0.0, min(100.0, total)), 1)
    expl = ("Heuristisk bedömning: " + ", ".join(reasons) + ".") if reasons else "Heuristisk bedömning."
    return ScoreBreakdown(
        round(hook, 1),
        round(coherence, 1),
        round(interest, 1),
        round(energy, 1),
        clarity,
        round(channel_fit, 1),
        penalties,
        final,
        expl,
        warnings,
    )
