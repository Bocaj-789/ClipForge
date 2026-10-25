"""Local administration CLI (run on the Mac itself, never exposed over HTTP).

python -m app.cli doctor
python -m app.cli reset-password <username>
python -m app.cli status
"""

from __future__ import annotations

import getpass
import json
import platform
import shutil
import subprocess
import sys

from sqlalchemy import func, select

from app.config import get_config
from app.core import auth
from app.db.migrate import migration_status, upgrade_to_head
from app.db.models import Job, User
from app.db.session import init_engine, session_scope


def _init() -> None:
    cfg = get_config()
    cfg.ensure_dirs()
    upgrade_to_head(cfg.database_url)
    init_engine(cfg.database_url)


def doctor() -> int:
    cfg = get_config()
    ok = True

    def line(name: str, good: bool | None, detail: str) -> None:
        nonlocal ok
        mark = {True: "OK  ", False: "FEL ", None: "INFO"}[good]
        if good is False:
            ok = False
        print(f"[{mark}] {name:<26} {detail}")

    line("macOS", None, platform.mac_ver()[0] or platform.platform())
    line(
        "Arkitektur",
        platform.machine() == "arm64",
        platform.machine() + (" (Apple Silicon)" if platform.machine() == "arm64" else " – väntat arm64"),
    )
    line("Python", sys.version_info >= (3, 12), platform.python_version())
    for b in (cfg.ffmpeg_bin, cfg.ffprobe_bin):
        path = shutil.which(b)
        ver = ""
        if path:
            out = subprocess.run(
                [path, "-hide_banner", "-version"], capture_output=True, text=True, timeout=10, check=False
            )
            ver = out.stdout.splitlines()[0][:60] if out.stdout else ""
        line(b, path is not None, ver or "saknas – brew install ffmpeg")
    try:
        cfg.ensure_dirs()
        probe = cfg.data_dir / ".write-test"
        probe.write_text("x")
        probe.unlink()
        line("Datamapp", True, str(cfg.data_dir))
    except OSError as e:
        line("Datamapp", False, f"{cfg.data_dir}: {e}")
    du = shutil.disk_usage(cfg.data_dir)
    line(
        "Ledigt diskutrymme",
        du.free > 5 * 1024**3,
        f"{du.free / 1024**3:.1f} GB av {du.total / 1024**3:.0f} GB",
    )
    try:
        st = migration_status(cfg.database_url)
        line(
            "Databas",
            bool(st["up_to_date"]) or st["current"] is None,
            f"schema {st['current'] or 'tomt (skapas vid start)'} / senaste {st['head']}",
        )
    except Exception as e:  # noqa: BLE001
        line("Databas", False, str(e)[:100])
    try:
        import faster_whisper  # noqa: F401

        from app.services.transcription.whisper_local import FasterWhisperEngine

        init_engine(cfg.database_url)
        from app.services.settings_service import load_settings

        with session_scope() as db:
            s = load_settings(db)
        stt = FasterWhisperEngine(
            cfg.models_dir, s.transcription_model, download_allowed=s.transcription_download_allowed
        ).status()
        line(
            "Transkribering",
            bool(stt["downloaded"]) or None,
            f"faster-whisper, modell {s.transcription_model}: "
            + ("nedladdad" if stt["downloaded"] else str(stt["reason"])),
        )
        if s.llm_provider == "ollama":
            from app.services.intelligence.llm_provider import OllamaProvider

            good, reason = OllamaProvider(cfg.ollama_url, s.ollama_model).health()
            line("Ollama", good, reason or f"{s.ollama_model} tillgänglig")
        else:
            line("Lokal AI", None, "avstängd – regelbaserad heuristik används")
        line(
            "Fjärråtkomst",
            None,
            "planerad" if s.remote_access_enabled else "av (lyssnar endast på 127.0.0.1)",
        )
        with session_scope() as db:
            counts = dict(db.execute(select(Job.status, func.count()).group_by(Job.status)).all())
        line("Kö", None, json.dumps(counts) if counts else "tom")
    except ImportError:
        line("Transkribering", False, "faster-whisper saknas i venv")
    from app.config import REPO_ROOT

    dist = REPO_ROOT / "frontend" / "dist" / "index.html"
    line(
        "Frontend",
        dist.exists() or None,
        "byggd" if dist.exists() else "ej byggd – cd frontend && npm run build",
    )
    line("YouTube API-nyckel", None, "konfigurerad" if cfg.youtube_api_key else "ej konfigurerad (valfri)")
    from app.services.publishing.secret_store import get_secret_store

    line(
        "OAuth-klient",
        None,
        "konfigurerad" if cfg.google_oauth_client() else "ej konfigurerad (se docs/YOUTUBE_SETUP.md)",
    )
    store = get_secret_store(cfg)
    store_ok, store_why = store.available()
    line("Tokenlagring", store_ok, store.name + ("" if store_ok else f": {store_why}"))
    line("Redirect-URI", None, cfg.oauth_redirect_uri)
    return 0 if ok else 1


def reset_password(username: str) -> int:
    _init()
    pw = getpass.getpass("Nytt lösenord: ")
    if pw != getpass.getpass("Upprepa: "):
        print("Lösenorden matchar inte.")
        return 1
    problem = auth.password_problem(pw, username)
    if problem:
        print(problem)
        return 1
    with session_scope() as db:
        user = db.scalar(select(User).where(User.username == username))
        if user is None:
            print("Användaren finns inte.")
            return 1
        user.password_hash = auth.hash_password(pw)
        n = auth.revoke_all(db, user.id)
    print(f"Lösenordet är bytt. {n} sessioner loggades ut.")
    return 0


def status() -> int:
    _init()
    from app.services.agent import compute_status

    with session_scope() as db:
        print(json.dumps(compute_status(db), default=str, ensure_ascii=False, indent=2))
    return 0


def main(argv: list[str]) -> int:
    if not argv or argv[0] in ("-h", "--help"):
        print(__doc__)
        return 0
    if argv[0] == "doctor":
        return doctor()
    if argv[0] == "reset-password" and len(argv) == 2:
        return reset_password(argv[1])
    if argv[0] == "status":
        return status()
    print(__doc__)
    return 2


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
