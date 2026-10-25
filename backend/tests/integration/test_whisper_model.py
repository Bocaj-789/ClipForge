"""Opt-in: real faster-whisper transcription. Requires a downloaded model and a speech sample.

Run with:  CLIPFORGE_SPEECH_SAMPLE=/path/to/speech.wav pytest -m whisper_model
The model must already be in data/models/faster-whisper (see docs/SETUP_MACOS.md).
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from app.config import get_config
from app.services.transcription.whisper_local import FasterWhisperEngine

pytestmark = pytest.mark.whisper_model


def test_real_transcription() -> None:
    sample = os.environ.get("CLIPFORGE_SPEECH_SAMPLE")
    if not sample:
        pytest.skip("CLIPFORGE_SPEECH_SAMPLE not set")
    eng = FasterWhisperEngine(
        get_config().models_dir, os.environ.get("CLIPFORGE_TEST_MODEL", "base"), download_allowed=False
    )
    if not eng.status()["available"]:
        pytest.skip("model not downloaded")
    res = eng.transcribe(Path(sample), language=None, duration=0)
    assert res.segments and res.segments[0]["words"]
