"""Channel statistics for the dashboard's Statistik tab. Reads only; nothing is changed on YouTube."""

from __future__ import annotations

from fastapi import APIRouter, Depends, Request
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.api.deps import Principal, get_db, require_user
from app.db.base import utcnow
from app.db.models import Channel, ConnectionState, Publication
from app.services.publishing import channel_stats
from app.services.publishing.youtube import YouTubeError
from app.services.settings_service import load_settings

router = APIRouter(prefix="/stats", tags=["stats"])


@router.get("/channels")
def channel_statistics(
    request: Request, _: Principal = Depends(require_user), db: Session = Depends(get_db)
) -> dict:
    deps = request.app.state.publish_deps
    settings = load_settings(db)
    rows = db.scalars(
        select(Channel)
        .where(Channel.connection_state == ConnectionState.CONNECTED)
        .order_by(Channel.created_at)
    ).all()
    items: list[dict] = []
    for ch in rows:
        video_ids = list(
            db.scalars(
                select(Publication.external_video_id).where(
                    Publication.channel_id == ch.id, Publication.external_video_id.is_not(None)
                )
            )
        )
        try:
            with deps.http_factory() as http:
                api = deps.api_for(db, settings, ch, http)
                items.append(
                    channel_stats.channel_snapshot(
                        api, channel_id=ch.id, name=ch.display_name, video_ids=[str(v) for v in video_ids]
                    )
                )
        except YouTubeError as e:
            items.append(channel_stats.failed(ch.id, ch.display_name, e.safe_message))
    return {
        "items": items,
        "totals": channel_stats.totals(items),
        "fetched_at": utcnow().isoformat(),
    }
