from __future__ import annotations

import json
import sqlite3
from datetime import UTC, datetime

from fastapi import APIRouter, Depends, Request
from pydantic import BaseModel, Field, ValidationError
from sqlalchemy.orm import Session

from app.api.deps import Principal, get_db, require_user
from app.core.errors import AppError, ErrorCode
from app.db.migrate import migration_status
from app.services.audit import service as audit
from app.services.settings_service import (
    CONFIRM_REQUIRED,
    TRANSCRIPTION_MODEL_INFO,
    AppSettings,
    apply_patch,
    load_settings,
    save_settings,
)
from app.services.storage.manager import invalidate_cache

router = APIRouter(prefix="/settings", tags=["settings"])
CONFIRM_PHRASE = "JA"


class PatchIn(BaseModel):
    changes: dict[str, object] = Field(max_length=60)
    confirm: str | None = Field(default=None, max_length=50)


def _out(s: AppSettings) -> dict:
    return {
        "settings": s.model_dump(),
        "transcription_models": TRANSCRIPTION_MODEL_INFO,
        "confirm_required": sorted(CONFIRM_REQUIRED),
    }


@router.get("")
def get_settings(_: Principal = Depends(require_user), db: Session = Depends(get_db)) -> dict:
    return _out(load_settings(db))


def _validated(db: Session, body: PatchIn) -> tuple[AppSettings, AppSettings]:
    cur = load_settings(db)
    try:
        new = apply_patch(cur, body.changes)
    except (ValidationError, ValueError) as e:
        msg = e.errors()[0]["msg"] if isinstance(e, ValidationError) else str(e)
        raise AppError(ErrorCode.INVALID_INPUT, f"Ogiltig inställning: {msg}") from e
    turning_on = [k for k in CONFIRM_REQUIRED if getattr(new, k) and not getattr(cur, k)]
    if turning_on and (body.confirm or "").strip().upper() != CONFIRM_PHRASE:
        raise AppError(
            ErrorCode.INVALID_INPUT,
            f'Bekräftelse krävs för: {", ".join(turning_on)}. Skicka confirm="{CONFIRM_PHRASE}".',
        )
    return cur, new


@router.post("/validate")
def validate(body: PatchIn, _: Principal = Depends(require_user), db: Session = Depends(get_db)) -> dict:
    _validated(db, body)
    return {"ok": True}


@router.patch("")
def patch(body: PatchIn, _: Principal = Depends(require_user), db: Session = Depends(get_db)) -> dict:
    cur, new = _validated(db, body)
    save_settings(db, new)
    changed = {k: getattr(new, k) for k in body.changes if getattr(cur, k) != getattr(new, k)}
    audit.record(
        db,
        "settings.updated",
        actor="user",
        object_type="settings",
        details=changed,
        severity="warning" if set(changed) & CONFIRM_REQUIRED else "info",
    )
    return _out(new)


@router.post("/backup")
def backup(request: Request, _: Principal = Depends(require_user), db: Session = Depends(get_db)) -> dict:
    """Consistent SQLite backup (online backup API) + settings JSON. Secrets are not included."""
    cfg = request.app.state.cfg
    stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    out_dir = cfg.backups_dir / f"backup-{stamp}"
    out_dir.mkdir(parents=True, exist_ok=False)
    src = sqlite3.connect(cfg.db_path)
    dst = sqlite3.connect(out_dir / "clipforge.sqlite3")
    try:
        src.backup(dst)
        # Sessions are credentials: strip them from the copy.
        dst.execute("DELETE FROM auth_sessions")
        dst.commit()
    finally:
        dst.close()
        src.close()
    meta = {
        "created_at": stamp,
        "schema": migration_status(cfg.database_url),
        "includes_secrets": False,
        "settings": load_settings(db).model_dump(),
    }
    (out_dir / "manifest.json").write_text(json.dumps(meta, indent=2, default=str), encoding="utf-8")
    invalidate_cache()
    audit.record(db, "settings.backup", actor="user", details={"name": out_dir.name})
    return {"ok": True, "name": out_dir.name}
