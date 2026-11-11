"""Stable value objects and constants for Launch Ops."""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from ..core.config import STATE_DIR

DEFAULT_STORE_PATH = Path(STATE_DIR) / "launch_monitor.db"
DEFAULT_CAMPAIGN_NAME = "OPC大课发售"
DEFAULT_CAMPAIGN_GROUP_PATTERN = re.compile(
    r"^🙌青年OPC全域增长群(?P<number>\d+)$"
)
FULL_MESSAGE_RETENTION_SECONDS = 30 * 24 * 60 * 60


@dataclass(frozen=True)
class StandardEvent:
    event_id: str
    event_type: str
    room_id: str
    event_time: int
    source_shard: str
    source_table: str
    source_identity: str
    actor_id: str
    subject_id: str
    payload: dict[str, Any]


@dataclass(frozen=True)
class WatcherHealth:
    status: str
    stale: bool
    last_attempt_at: int | None
    last_success_at: int | None
    last_event_at: int | None
    error_code: str
    error_message: str


@dataclass(frozen=True)
class ScanResult:
    scanned_sources: int
    skipped_sources: int
    inserted_events: int
    inserted_messages: int
    discovered_rooms: int
    last_event_at: int | None
