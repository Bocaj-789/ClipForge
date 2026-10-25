from __future__ import annotations

import shutil
import subprocess
from collections.abc import Iterator
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from app.config import Config
from app.db.migrate import upgrade_to_head
from app.db.session import init_engine, new_session

HAS_FFMPEG = shutil.which("ffmpeg") is not None and shutil.which("ffprobe") is not None
requires_ffmpeg = pytest.mark.skipif(not HAS_FFMPEG, reason="ffmpeg not installed")

PASSWORD = "korrekt-häst-batteri-42"


@pytest.fixture
def cfg(tmp_path: Path) -> Config:
    c = Config(data_dir=tmp_path / "data")
    c.ensure_dirs()
    return c


@pytest.fixture
def db(cfg: Config) -> Iterator[Session]:
    upgrade_to_head(cfg.database_url)
    init_engine(cfg.database_url)
    s = new_session()
    try:
        yield s
    finally:
        s.close()


@pytest.fixture
def client(cfg: Config) -> Iterator[TestClient]:
    from app.main import create_app

    app = create_app(cfg, configure_logging=False)
    with TestClient(app, base_url="http://127.0.0.1:8000") as c:
        yield c


def login(client: TestClient, cfg: Config) -> dict[str, str]:
    token = (cfg.secrets_dir / "setup-token.txt").read_text().strip()
    r = client.post(
        "/api/v1/auth/setup", json={"setup_token": token, "username": "agaren", "password": PASSWORD}
    )
    assert r.status_code == 200, r.text
    return {"x-csrf-token": r.json()["csrf_token"]}


@pytest.fixture
def authed(client: TestClient, cfg: Config) -> tuple[TestClient, dict[str, str]]:
    return client, login(client, cfg)


def make_video(path: Path, seconds: float = 40, size: str = "1280x720", audio: bool = True) -> Path:
    args = [
        "ffmpeg",
        "-y",
        "-loglevel",
        "error",
        "-f",
        "lavfi",
        "-i",
        f"testsrc2=size={size}:rate=30:duration={seconds}",
    ]
    if audio:
        args += ["-f", "lavfi", "-i", f"sine=frequency=330:duration={seconds}", "-c:a", "aac", "-shortest"]
    args += ["-c:v", "libx264", "-preset", "ultrafast", "-pix_fmt", "yuv420p", str(path)]
    subprocess.run(args, check=True)
    return path


SWEDISH_SCRIPT = (
    "Visste du att räksmörgåsar är Sveriges mest älskade mat? Det låter konstigt, men det är faktiskt sant. "
    "Jag testade tio olika varianter på en enda dag. Den bästa kom från en liten kiosk på Öland! "
    "Hemligheten var inte räkorna utan majonnäsen. Ingen hade berättat det för mig tidigare. "
    "Så här gör du själv hemma, steg för steg. Först behöver du färskt bröd och kalla ägg. "
    "Sedan blandar du allt försiktigt i en skål. Det tar mindre än fem minuter totalt. "
    "Varför gör inte fler så här? Det är billigt, snabbt och gott."
)


def fake_segments(text: str = SWEDISH_SCRIPT, wps: float = 2.6, start: float = 0.5) -> list[dict]:
    """Deterministic word-timed transcript for pipeline tests (no model download needed)."""
    segs, t = [], start
    for sent in [
        s.strip() for s in text.replace("? ", "?|").replace(". ", ".|").replace("! ", "!|").split("|")
    ]:
        words = []
        for w in sent.split():
            words.append(
                {"start": round(t, 3), "end": round(t + 0.8 / wps * 2, 3), "word": w, "probability": 0.92}
            )
            t += 1 / wps
        segs.append(
            {
                "start": words[0]["start"],
                "end": words[-1]["end"],
                "text": sent,
                "avg_logprob": -0.2,
                "no_speech_prob": 0.01,
                "words": words,
            }
        )
        t += 0.4
    return segs
