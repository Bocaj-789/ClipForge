"""Clip-proposal providers. Heuristic is always available; Ollama is optional and local-only.

All LLM output is validated with Pydantic and mapped back onto real transcript sentence
boundaries, so a model can never produce timestamps outside the media.
"""

from __future__ import annotations

import json
import logging
from typing import Protocol

import httpx
from pydantic import BaseModel, Field, ValidationError

from app.services.intelligence.candidate_extractor import (
    Window,
    enumerate_windows,
    make_window,
    select_non_overlapping,
    validate_range,
)
from app.services.intelligence.metadata import sanitize_title
from app.services.intelligence.prompts import SYSTEM, build_prompt
from app.services.intelligence.scoring import Sentence

log = logging.getLogger("clipforge.llm")


class ClipProvider(Protocol):
    name: str

    def propose(
        self,
        sentences: list[Sentence],
        *,
        min_s: int,
        max_s: int,
        media_duration: float,
        keywords: list[str],
        limit: int,
    ) -> list[Window]: ...


class HeuristicProvider:
    name = "heuristic"

    def propose(
        self,
        sentences: list[Sentence],
        *,
        min_s: int,
        max_s: int,
        media_duration: float,
        keywords: list[str],
        limit: int,
    ) -> list[Window]:
        wins = enumerate_windows(
            sentences, min_s=min_s, max_s=max_s, media_duration=media_duration, keywords=keywords
        )
        return select_non_overlapping(wins, limit=limit)


class _LlmSegment(BaseModel):
    start_sentence: int = Field(ge=0)
    end_sentence: int = Field(ge=0)
    title: str = Field(default="", max_length=200)
    rationale: str = Field(default="", max_length=400)


class _LlmResponse(BaseModel):
    segments: list[_LlmSegment] = Field(default_factory=list, max_length=30)


class OllamaProvider:
    name = "ollama"
    CHUNK = 120  # sentences per prompt to bound context size

    def __init__(self, base_url: str, model: str, timeout: float = 120.0) -> None:
        self.base_url = base_url
        self.model = model
        self.timeout = timeout

    def health(self) -> tuple[bool, str | None]:
        try:
            r = httpx.get(f"{self.base_url}/api/tags", timeout=3)
            r.raise_for_status()
            names = {m.get("name") for m in r.json().get("models", [])}
        except (httpx.HTTPError, ValueError):
            return False, "Ollama svarar inte på 127.0.0.1. Starta Ollama eller välj heuristik."
        if self.model not in names and f"{self.model}:latest" not in names:
            return False, f"Ollama-modellen '{self.model}' är inte installerad (ollama pull {self.model})."
        return True, None

    def _ask(self, prompt: str) -> _LlmResponse:
        r = httpx.post(
            f"{self.base_url}/api/chat",
            json={
                "model": self.model,
                "stream": False,
                "format": "json",
                "options": {"temperature": 0.2, "num_predict": 1200},
                "messages": [{"role": "system", "content": SYSTEM}, {"role": "user", "content": prompt}],
            },
            timeout=self.timeout,
        )
        r.raise_for_status()
        content = (r.json().get("message") or {}).get("content", "")[:20000]
        return _LlmResponse.model_validate(json.loads(content))

    def propose(
        self,
        sentences: list[Sentence],
        *,
        min_s: int,
        max_s: int,
        media_duration: float,
        keywords: list[str],
        limit: int,
    ) -> list[Window]:
        wins: list[Window] = []
        for off in range(0, len(sentences), self.CHUNK):
            chunk = sentences[off : off + self.CHUNK]
            numbered = [(off + k, s.start, s.end, s.text) for k, s in enumerate(chunk)]
            try:
                resp = self._ask(build_prompt(numbered, min_s=min_s, max_s=max_s, limit=limit))
            except (httpx.HTTPError, json.JSONDecodeError, ValidationError, ValueError) as e:
                log.warning("ollama chunk rejected: %s", type(e).__name__)
                continue
            for seg in resp.segments:
                i, j = seg.start_sentence, seg.end_sentence
                if not (off <= i <= j < off + len(chunk)):
                    continue
                w = make_window(sentences, i, j, media_duration, keywords, provider=f"ollama:{self.model}")
                if not validate_range(
                    w.start, w.end, media_duration=media_duration, min_s=min_s, max_s=max_s
                ):
                    continue
                w.title = sanitize_title(seg.title) or None
                w.rationale = sanitize_title(seg.rationale) or None
                wins.append(w)
        return select_non_overlapping(wins, limit=limit)
