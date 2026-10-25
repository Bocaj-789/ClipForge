from __future__ import annotations

import logging
from typing import Any

from sqlalchemy.orm import Session

from app.core.security import redact_mapping
from app.db.models import AuditEvent

log = logging.getLogger("clipforge.audit")


def record(
    db: Session,
    action: str,
    *,
    actor: str = "system",
    object_type: str | None = None,
    object_id: str | None = None,
    result: str = "ok",
    severity: str = "info",
    details: dict[str, Any] | None = None,
) -> AuditEvent:
    """Persist an audit event. Details are redacted; never pass raw tokens here."""
    safe = redact_mapping(details or {})
    ev = AuditEvent(
        actor=actor,
        action=action,
        object_type=object_type,
        object_id=object_id,
        result=result,
        severity=severity,
        safe_details_json=safe if isinstance(safe, dict) else {},
    )
    db.add(ev)
    log.log(
        logging.WARNING if severity in {"warning", "error"} else logging.INFO,
        "%s %s %s/%s -> %s",
        actor,
        action,
        object_type,
        object_id,
        result,
    )
    return ev
