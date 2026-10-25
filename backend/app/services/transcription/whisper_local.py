"""Local transcription with faster-whisper (CTranslate2, int8 on the M1 CPU).

Model weights are only downloaded when the user has explicitly allowed it in Settings
(``transcription_download_allowed``). Otherwise only an already-cached model is used.
"""

from __future__ import annotations

import logging
import os
import threading
from collections.abc import Callable
from pathlib import Path
from typing import Any

from app.core.errors import ErrorCode, JobError, blocked
from app.services.transcription.base import TranscriptResult

log = logging.getLogger("clipforge.transcription")

_lock = threading.Lock()
_loaded: dict[str, Any] = {}


class _Cancelled(Exception):
    pass


def _version() -> str:
    try:
        from importlib.metadata import version

        return f"faster-whisper {version('faster-whisper')} / ctranslate2 {version('ctranslate2')}"
    except Exception:  # noqa: BLE001
        return "faster-whisper (unknown version)"


class FasterWhisperEngine:
    name = "faster-whisper"

    def __init__(self, models_dir: Path, model: str, *, download_allowed: bool, low_resource: bool = False):
        self.cache_dir = models_dir / "faster-whisper"
        self.model = model
        self.download_allowed = download_allowed
        self.low_resource = low_resource

    def _local_path(self) -> str | None:
        from faster_whisper.utils import download_model

        try:
            return str(download_model(self.model, cache_dir=str(self.cache_dir), local_files_only=True))
        except Exception:  # noqa: BLE001 - huggingface raises several types when absent
            return None

    def status(self) -> dict[str, object]:
        try:
            import faster_whisper  # noqa: F401
        except ImportError:
            return {
                "available": False,
                "downloaded": False,
                "model": self.model,
                "reason": "faster-whisper är inte installerat.",
            }
        downloaded = self._local_path() is not None
        if downloaded:
            return {"available": True, "downloaded": True, "model": self.model, "reason": None}
        if self.download_allowed:
            return {
                "available": True,
                "downloaded": False,
                "model": self.model,
                "reason": "Modellen laddas ned vid första transkriberingen.",
            }
        return {
            "available": False,
            "downloaded": False,
            "model": self.model,
            "reason": f"Whisper-modellen '{self.model}' är inte nedladdad. Tillåt nedladdning under "
            "Inställningar → Transkribering.",
        }

    def _load(self) -> Any:
        key = f"{self.model}:{self.low_resource}"
        with _lock:
            if key in _loaded:
                return _loaded[key]
            from faster_whisper import WhisperModel

            path = self._local_path()
            if path is None:
                if not self.download_allowed:
                    raise blocked(ErrorCode.TRANSCRIPTION_UNAVAILABLE, str(self.status()["reason"]))
                from faster_whisper.utils import download_model

                log.info("downloading whisper model %s (user-approved)", self.model)
                self.cache_dir.mkdir(parents=True, exist_ok=True)
                path = str(download_model(self.model, cache_dir=str(self.cache_dir)))
            threads = 2 if self.low_resource else max(2, min(6, (os.cpu_count() or 4) - 2))
            # Keep only one model resident to bound RAM.
            _loaded.clear()
            _loaded[key] = WhisperModel(path, device="cpu", compute_type="int8", cpu_threads=threads)
            return _loaded[key]

    def transcribe(
        self,
        path: Path,
        *,
        language: str | None,
        duration: float,
        on_progress: Callable[[float], None] | None = None,
        should_cancel: Callable[[], bool] | None = None,
    ) -> TranscriptResult:
        model = self._load()
        try:
            seg_iter, info = model.transcribe(
                str(path),
                language=language,
                word_timestamps=True,
                vad_filter=True,
                beam_size=1 if self.low_resource else 5,
                condition_on_previous_text=False,
            )
            segments: list[dict] = []
            for s in seg_iter:  # lazy generator: decoding happens here
                if should_cancel and should_cancel():
                    raise _Cancelled()
                segments.append(
                    {
                        "start": round(float(s.start), 3),
                        "end": round(float(s.end), 3),
                        "text": s.text.strip(),
                        "avg_logprob": round(float(s.avg_logprob), 4),
                        "no_speech_prob": round(float(s.no_speech_prob), 4),
                        "words": [
                            {
                                "start": round(float(w.start), 3),
                                "end": round(float(w.end), 3),
                                "word": w.word.strip(),
                                "probability": round(float(w.probability), 4),
                            }
                            for w in (s.words or [])
                        ],
                    }
                )
                if on_progress and duration:
                    on_progress(min(99.0, 100.0 * float(s.end) / duration))
        except _Cancelled:
            raise
        except JobError:
            raise
        except Exception as e:
            log.exception("transcription failed")
            raise JobError(
                ErrorCode.TRANSCRIPTION_FAILED, "Transkriberingen misslyckades.", transient=True
            ) from e
        warnings = []
        low = [s for s in segments if s["avg_logprob"] < -1.0]
        if segments and len(low) / len(segments) > 0.3:
            warnings.append("Stora delar av transkriptionen har låg säkerhet.")
        return TranscriptResult(
            language=info.language,
            segments=segments,
            duration=float(info.duration or duration),
            engine_name=self.name,
            engine_version=_version(),
            model_name=self.model,
            warnings=warnings,
        )


TranscriptionCancelled = _Cancelled
