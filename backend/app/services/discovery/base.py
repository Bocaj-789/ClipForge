from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Protocol

from app.db.models import Source


@dataclass
class DiscoveredItem:
    canonical_url: str
    platform: str
    external_id: str | None
    title: str | None = None
    creator_name: str | None = None
    creator_channel_id: str | None = None
    description: str | None = None
    published_at: datetime | None = None
    thumbnail_url: str | None = None
    duration_seconds: float | None = None
    media_rel_path: str | None = None  # local_folder only
    media_checksum: str | None = None
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass
class DiscoveryResult:
    items: list[DiscoveredItem]
    cursor: dict[str, Any] = field(default_factory=dict)
    notes: list[str] = field(default_factory=list)
    quota_units: int = 0


@dataclass(frozen=True)
class Capabilities:
    metadata: bool
    media_acquisition: bool
    requires_api_key: bool
    restrictions: str


class DiscoveryAdapter(Protocol):
    kind: str

    def capabilities(self) -> Capabilities: ...

    def validate_config(self, source: Source) -> str | None:
        """Return an error message or None."""
        ...

    def discover(self, source: Source) -> DiscoveryResult: ...
