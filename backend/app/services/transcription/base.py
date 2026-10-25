from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Protocol


@dataclass
class TranscriptResult:
    language: str | None
    segments: list[
        dict
    ]  # {start, end, text, avg_logprob, no_speech_prob, words: [{start,end,word,probability}]}
    duration: float
    engine_name: str
    engine_version: str
    model_name: str
    warnings: list[str] = field(default_factory=list)

    @property
    def text(self) -> str:
        return " ".join(s["text"].strip() for s in self.segments).strip()


class TranscriptionEngine(Protocol):
    name: str

    def status(self) -> dict[str, object]:
        """Return {available: bool, reason: str|None, model: str, downloaded: bool}."""
        ...

    def transcribe(
        self,
        path: Path,
        *,
        language: str | None,
        duration: float,
        on_progress: Callable[[float], None] | None = None,
        should_cancel: Callable[[], bool] | None = None,
    ) -> TranscriptResult: ...
