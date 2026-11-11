"""SQLite persistence for Launch Ops.

The store owns only Launch Ops state. It never writes to WeChat databases.
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
import time
import uuid
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator

from .models import FULL_MESSAGE_RETENTION_SECONDS, StandardEvent


_SCHEMA = """
CREATE TABLE IF NOT EXISTS meta (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS campaigns (
    campaign_id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    created_at INTEGER NOT NULL,
    ended_at INTEGER,
    access_policy TEXT NOT NULL DEFAULT 'local',
    remote_access_enabled INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS campaign_rooms (
    campaign_id TEXT NOT NULL,
    room_id TEXT NOT NULL,
    joined_campaign_at INTEGER NOT NULL,
    removed_campaign_at INTEGER,
    PRIMARY KEY (campaign_id, room_id, joined_campaign_at)
);
CREATE INDEX IF NOT EXISTS idx_campaign_rooms_active
    ON campaign_rooms(campaign_id, room_id, removed_campaign_at);
CREATE TABLE IF NOT EXISTS rooms (
    room_id TEXT PRIMARY KEY,
    display_name TEXT NOT NULL,
    member_count INTEGER,
    discovered_at INTEGER NOT NULL,
    last_seen_at INTEGER NOT NULL,
    last_event_at INTEGER,
    health_status TEXT NOT NULL DEFAULT 'unknown'
);
CREATE TABLE IF NOT EXISTS room_profiles (
    room_id TEXT PRIMARY KEY,
    avatar_url TEXT NOT NULL DEFAULT '',
    observed_at INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS watcher_cursors (
    source_shard TEXT NOT NULL,
    source_table TEXT NOT NULL,
    create_time INTEGER NOT NULL,
    local_id INTEGER NOT NULL,
    source_identity TEXT NOT NULL,
    PRIMARY KEY (source_shard, source_table)
);
CREATE TABLE IF NOT EXISTS watcher_source_state (
    source_shard TEXT NOT NULL,
    source_table TEXT NOT NULL,
    db_mtime_ns INTEGER NOT NULL,
    wal_mtime_ns INTEGER NOT NULL,
    db_size INTEGER NOT NULL,
    wal_size INTEGER NOT NULL,
    processed_at INTEGER NOT NULL,
    PRIMARY KEY (source_shard, source_table)
);
CREATE TABLE IF NOT EXISTS watcher_table_heads (
    source_shard TEXT NOT NULL,
    source_table TEXT NOT NULL,
    local_id INTEGER NOT NULL,
    create_time INTEGER NOT NULL,
    processed_at INTEGER NOT NULL,
    PRIMARY KEY (source_shard, source_table)
);
CREATE TABLE IF NOT EXISTS events (
    event_id TEXT PRIMARY KEY,
    event_type TEXT NOT NULL,
    room_id TEXT NOT NULL,
    event_time INTEGER NOT NULL,
    source_shard TEXT NOT NULL,
    source_table TEXT NOT NULL,
    source_identity TEXT NOT NULL,
    actor_id TEXT NOT NULL,
    subject_id TEXT NOT NULL,
    payload_json TEXT NOT NULL,
    protected INTEGER NOT NULL DEFAULT 0,
    created_at INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_events_room_time
    ON events(room_id, event_time);
CREATE INDEX IF NOT EXISTS idx_events_type_time
    ON events(event_type, event_time);
CREATE TABLE IF NOT EXISTS messages (
    message_id TEXT PRIMARY KEY,
    event_id TEXT NOT NULL UNIQUE,
    room_id TEXT NOT NULL,
    event_time INTEGER NOT NULL,
    message_type TEXT NOT NULL,
    sender_id TEXT NOT NULL,
    text_content TEXT NOT NULL,
    payload_json TEXT NOT NULL,
    protected INTEGER NOT NULL DEFAULT 0,
    created_at INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_messages_room_time
    ON messages(room_id, event_time);
CREATE TABLE IF NOT EXISTS membership_snapshots (
    room_id TEXT NOT NULL,
    captured_at INTEGER NOT NULL,
    members_json TEXT NOT NULL,
    PRIMARY KEY (room_id, captured_at)
);
CREATE TABLE IF NOT EXISTS viewer_publications (
    publication_id TEXT PRIMARY KEY,
    campaign_id TEXT NOT NULL,
    path_slug TEXT NOT NULL UNIQUE,
    state TEXT NOT NULL,
    created_at INTEGER NOT NULL,
    published_at INTEGER NOT NULL,
    ended_at INTEGER,
    review_expires_at INTEGER,
    revoked_at INTEGER,
    config_revision INTEGER NOT NULL DEFAULT 1
);
CREATE TABLE IF NOT EXISTS viewer_credentials (
    credential_id TEXT PRIMARY KEY,
    publication_id TEXT NOT NULL,
    credential_type TEXT NOT NULL,
    algorithm TEXT NOT NULL,
    iterations INTEGER NOT NULL,
    salt_hex TEXT NOT NULL,
    verifier_hex TEXT NOT NULL,
    version INTEGER NOT NULL,
    created_at INTEGER NOT NULL,
    revoked_at INTEGER,
    FOREIGN KEY(publication_id) REFERENCES viewer_publications(publication_id)
);
CREATE INDEX IF NOT EXISTS idx_viewer_credentials_publication
    ON viewer_credentials(publication_id, version);
CREATE TABLE IF NOT EXISTS viewer_sessions (
    session_id TEXT PRIMARY KEY,
    publication_id TEXT NOT NULL,
    credential_version INTEGER NOT NULL,
    token_digest TEXT NOT NULL UNIQUE,
    created_at INTEGER NOT NULL,
    expires_at INTEGER NOT NULL,
    revoked_at INTEGER,
    last_seen_at INTEGER NOT NULL,
    FOREIGN KEY(publication_id) REFERENCES viewer_publications(publication_id)
);
CREATE INDEX IF NOT EXISTS idx_viewer_sessions_publication
    ON viewer_sessions(publication_id, expires_at);
"""

_TRACE_ARCHIVE_V2_SCHEMA = """
CREATE TABLE IF NOT EXISTS trace_membership_observations (
    observation_id TEXT PRIMARY KEY,
    room_id TEXT NOT NULL,
    source_kind TEXT NOT NULL CHECK(source_kind IN ('contact', 'legacy_snapshot')),
    source_state_key TEXT NOT NULL,
    semantic_hash TEXT NOT NULL,
    first_observed_at INTEGER NOT NULL,
    last_observed_at INTEGER NOT NULL,
    membership_status TEXT NOT NULL
        CHECK(membership_status IN ('complete', 'partial', 'failed')),
    inviter_metadata_status TEXT NOT NULL
        CHECK(inviter_metadata_status IN ('complete', 'partial', 'unavailable')),
    members_json TEXT NOT NULL,
    diagnostic_code TEXT NOT NULL DEFAULT '',
    UNIQUE(room_id, source_kind, source_state_key, semantic_hash),
    CHECK(last_observed_at >= first_observed_at)
);
CREATE TABLE IF NOT EXISTS trace_people (
    person_id TEXT PRIMARY KEY,
    wechat_username TEXT NOT NULL UNIQUE,
    first_seen_at INTEGER NOT NULL,
    last_seen_at INTEGER NOT NULL,
    current_nick_name TEXT NOT NULL DEFAULT '',
    current_remark TEXT NOT NULL DEFAULT '',
    current_avatar_url TEXT NOT NULL DEFAULT '',
    CHECK(last_seen_at >= first_seen_at)
);
CREATE TABLE IF NOT EXISTS trace_person_profiles (
    profile_id TEXT PRIMARY KEY,
    person_id TEXT NOT NULL,
    room_id TEXT,
    observed_at INTEGER NOT NULL,
    nick_name TEXT NOT NULL DEFAULT '',
    remark TEXT NOT NULL DEFAULT '',
    room_display_name TEXT NOT NULL DEFAULT '',
    avatar_url TEXT NOT NULL DEFAULT '',
    profile_hash TEXT NOT NULL,
    FOREIGN KEY(person_id) REFERENCES trace_people(person_id) ON DELETE RESTRICT,
    FOREIGN KEY(room_id) REFERENCES rooms(room_id) ON DELETE RESTRICT
);
CREATE UNIQUE INDEX IF NOT EXISTS uq_trace_profile_global
    ON trace_person_profiles(person_id, profile_hash)
    WHERE room_id IS NULL;
CREATE UNIQUE INDEX IF NOT EXISTS uq_trace_profile_room
    ON trace_person_profiles(person_id, room_id, profile_hash)
    WHERE room_id IS NOT NULL;
CREATE INDEX IF NOT EXISTS idx_trace_profiles_person_time
    ON trace_person_profiles(person_id, observed_at);
CREATE TABLE IF NOT EXISTS trace_membership_episodes (
    episode_id TEXT PRIMARY KEY,
    room_id TEXT NOT NULL,
    person_id TEXT NOT NULL,
    episode_index INTEGER NOT NULL CHECK(episode_index > 0),
    first_observed_at INTEGER NOT NULL,
    last_observed_at INTEGER NOT NULL,
    joined_event_at INTEGER,
    left_event_at INTEGER,
    inactive_observed_at INTEGER,
    status TEXT NOT NULL CHECK(status IN ('active', 'inactive')),
    UNIQUE(room_id, person_id, episode_index),
    FOREIGN KEY(room_id) REFERENCES rooms(room_id) ON DELETE RESTRICT,
    FOREIGN KEY(person_id) REFERENCES trace_people(person_id) ON DELETE RESTRICT,
    CHECK(last_observed_at >= first_observed_at),
    CHECK(inactive_observed_at IS NULL OR inactive_observed_at >= first_observed_at),
    CHECK(left_event_at IS NULL OR joined_event_at IS NULL OR left_event_at >= joined_event_at),
    CHECK(
        (status = 'active' AND inactive_observed_at IS NULL)
        OR
        (status = 'inactive' AND inactive_observed_at IS NOT NULL)
    )
);
CREATE UNIQUE INDEX IF NOT EXISTS uq_trace_episode_active
    ON trace_membership_episodes(room_id, person_id)
    WHERE status = 'active';
CREATE INDEX IF NOT EXISTS idx_trace_episode_room_status
    ON trace_membership_episodes(room_id, status, person_id);
CREATE TABLE IF NOT EXISTS trace_invite_relations (
    relation_id TEXT PRIMARY KEY,
    room_id TEXT NOT NULL,
    episode_id TEXT NOT NULL,
    invitee_person_id TEXT NOT NULL,
    inviter_person_id TEXT,
    valid_from_observation INTEGER NOT NULL,
    valid_until_observation INTEGER,
    first_observed_at INTEGER NOT NULL,
    last_observed_at INTEGER NOT NULL,
    active INTEGER NOT NULL CHECK(active IN (0, 1)),
    resolved_method TEXT
        CHECK(resolved_method IS NULL OR resolved_method IN ('direct', 'qr', 'unattributed_qr')),
    resolved_event_time INTEGER,
    resolution_state TEXT NOT NULL CHECK(resolution_state IN ('resolved', 'unresolved')),
    FOREIGN KEY(room_id) REFERENCES rooms(room_id) ON DELETE RESTRICT,
    FOREIGN KEY(episode_id) REFERENCES trace_membership_episodes(episode_id) ON DELETE RESTRICT,
    FOREIGN KEY(invitee_person_id) REFERENCES trace_people(person_id) ON DELETE RESTRICT,
    FOREIGN KEY(inviter_person_id) REFERENCES trace_people(person_id) ON DELETE RESTRICT,
    CHECK(last_observed_at >= first_observed_at),
    CHECK(valid_until_observation IS NULL OR valid_until_observation >= valid_from_observation),
    CHECK(
        (active = 1 AND valid_until_observation IS NULL)
        OR
        (active = 0 AND valid_until_observation IS NOT NULL)
    ),
    CHECK(
        (resolution_state = 'resolved' AND inviter_person_id IS NOT NULL)
        OR
        (resolution_state = 'unresolved' AND inviter_person_id IS NULL)
    )
);
CREATE UNIQUE INDEX IF NOT EXISTS uq_trace_relation_active
    ON trace_invite_relations(episode_id)
    WHERE active = 1;
CREATE INDEX IF NOT EXISTS idx_trace_relation_room_inviter
    ON trace_invite_relations(room_id, inviter_person_id, active);
CREATE TABLE IF NOT EXISTS trace_relation_evidence (
    evidence_id TEXT PRIMARY KEY,
    room_id TEXT NOT NULL,
    episode_id TEXT,
    relation_id TEXT,
    attachment_state TEXT NOT NULL
        CHECK(attachment_state IN ('attached', 'ambiguous', 'unattached')),
    attachment_reason TEXT NOT NULL DEFAULT '',
    source_type TEXT NOT NULL
        CHECK(source_type IN ('ext_buffer', 'system_message', 'csv_import', 'manual_confirmation')),
    source_identity TEXT NOT NULL,
    evidence_subject_key TEXT NOT NULL,
    observed_at INTEGER NOT NULL,
    inviter_person_id TEXT,
    invitee_person_id TEXT,
    event_time INTEGER,
    method TEXT CHECK(method IS NULL OR method IN ('direct', 'qr', 'unattributed_qr')),
    raw_reference TEXT,
    authority_level INTEGER NOT NULL CHECK(authority_level BETWEEN 0 AND 100),
    conflicts_with_resolution INTEGER NOT NULL DEFAULT 0
        CHECK(conflicts_with_resolution IN (0, 1)),
    UNIQUE(source_type, source_identity, evidence_subject_key),
    FOREIGN KEY(room_id) REFERENCES rooms(room_id) ON DELETE RESTRICT,
    FOREIGN KEY(episode_id) REFERENCES trace_membership_episodes(episode_id) ON DELETE RESTRICT,
    FOREIGN KEY(relation_id) REFERENCES trace_invite_relations(relation_id) ON DELETE RESTRICT,
    FOREIGN KEY(inviter_person_id) REFERENCES trace_people(person_id) ON DELETE RESTRICT,
    FOREIGN KEY(invitee_person_id) REFERENCES trace_people(person_id) ON DELETE RESTRICT,
    CHECK(
        attachment_state <> 'attached'
        OR (episode_id IS NOT NULL AND invitee_person_id IS NOT NULL)
    ),
    CHECK(relation_id IS NULL OR episode_id IS NOT NULL)
);
CREATE INDEX IF NOT EXISTS idx_trace_evidence_episode
    ON trace_relation_evidence(episode_id, source_type, event_time);
CREATE INDEX IF NOT EXISTS idx_trace_evidence_invitee
    ON trace_relation_evidence(room_id, invitee_person_id, attachment_state);
CREATE TABLE IF NOT EXISTS trace_manual_audit (
    audit_id TEXT PRIMARY KEY,
    room_id TEXT NOT NULL,
    episode_id TEXT NOT NULL,
    relation_id TEXT,
    manual_evidence_id TEXT NOT NULL UNIQUE,
    operator_id TEXT NOT NULL DEFAULT 'local',
    changed_at INTEGER NOT NULL,
    action TEXT NOT NULL CHECK(action IN ('confirm', 'mark_unresolved', 'attach_evidence')),
    previous_inviter_person_id TEXT,
    new_inviter_person_id TEXT,
    previous_method TEXT
        CHECK(previous_method IS NULL OR previous_method IN ('direct', 'qr', 'unattributed_qr')),
    new_method TEXT
        CHECK(new_method IS NULL OR new_method IN ('direct', 'qr', 'unattributed_qr')),
    previous_event_time INTEGER,
    new_event_time INTEGER,
    reason TEXT NOT NULL CHECK(length(trim(reason)) > 0),
    FOREIGN KEY(room_id) REFERENCES rooms(room_id) ON DELETE RESTRICT,
    FOREIGN KEY(episode_id) REFERENCES trace_membership_episodes(episode_id) ON DELETE RESTRICT,
    FOREIGN KEY(relation_id) REFERENCES trace_invite_relations(relation_id) ON DELETE RESTRICT,
    FOREIGN KEY(manual_evidence_id) REFERENCES trace_relation_evidence(evidence_id) ON DELETE RESTRICT,
    FOREIGN KEY(previous_inviter_person_id) REFERENCES trace_people(person_id) ON DELETE RESTRICT,
    FOREIGN KEY(new_inviter_person_id) REFERENCES trace_people(person_id) ON DELETE RESTRICT
);
CREATE INDEX IF NOT EXISTS idx_trace_manual_audit_episode_time
    ON trace_manual_audit(episode_id, changed_at);
CREATE TABLE IF NOT EXISTS trace_conflicts (
    conflict_id TEXT PRIMARY KEY,
    room_id TEXT NOT NULL,
    episode_id TEXT,
    relation_id TEXT,
    conflict_type TEXT NOT NULL
        CHECK(conflict_type IN ('stable_inviter', 'identity', 'manual_resolution', 'invite_tree_cycle')),
    fingerprint TEXT NOT NULL UNIQUE,
    detail_json TEXT NOT NULL,
    created_at INTEGER NOT NULL,
    last_seen_at INTEGER NOT NULL,
    resolved_at INTEGER,
    FOREIGN KEY(room_id) REFERENCES rooms(room_id) ON DELETE RESTRICT,
    FOREIGN KEY(episode_id) REFERENCES trace_membership_episodes(episode_id) ON DELETE RESTRICT,
    FOREIGN KEY(relation_id) REFERENCES trace_invite_relations(relation_id) ON DELETE RESTRICT,
    CHECK(last_seen_at >= created_at),
    CHECK(resolved_at IS NULL OR resolved_at >= created_at)
);
CREATE INDEX IF NOT EXISTS idx_trace_conflicts_room_open
    ON trace_conflicts(room_id, conflict_type, resolved_at);
CREATE TABLE IF NOT EXISTS trace_managed_rooms (
    room_id TEXT PRIMARY KEY,
    managed_since INTEGER NOT NULL,
    archiving_enabled INTEGER NOT NULL CHECK(archiving_enabled IN (0, 1)),
    archiving_stopped_at INTEGER,
    management_source TEXT NOT NULL CHECK(management_source IN ('campaign', 'manual')),
    last_refresh_at INTEGER,
    last_success_at INTEGER,
    last_error_code TEXT NOT NULL DEFAULT '',
    last_error_message TEXT NOT NULL DEFAULT '',
    projection_state TEXT NOT NULL DEFAULT 'pending'
        CHECK(projection_state IN ('pending', 'ready', 'refresh_error')),
    current_observation_id TEXT,
    last_reconciled_at INTEGER,
    FOREIGN KEY(room_id) REFERENCES rooms(room_id) ON DELETE RESTRICT,
    FOREIGN KEY(current_observation_id)
        REFERENCES trace_membership_observations(observation_id) ON DELETE RESTRICT,
    CHECK(
        (archiving_enabled = 1 AND archiving_stopped_at IS NULL)
        OR
        (archiving_enabled = 0 AND archiving_stopped_at IS NOT NULL)
    ),
    CHECK(projection_state <> 'ready' OR current_observation_id IS NOT NULL),
    CHECK(archiving_enabled = 1 OR projection_state <> 'ready')
);
CREATE INDEX IF NOT EXISTS idx_trace_managed_rooms_enabled_state
    ON trace_managed_rooms(archiving_enabled, projection_state);
CREATE TABLE IF NOT EXISTS trace_reconcile_queue (
    queue_id TEXT PRIMARY KEY,
    room_id TEXT NOT NULL,
    reason TEXT NOT NULL CHECK(reason IN ('membership','evidence','manual','force_refresh','migration')),
    source_identity TEXT NOT NULL,
    created_at INTEGER NOT NULL,
    acknowledged_at INTEGER,
    UNIQUE(room_id, reason, source_identity)
);
"""


class LaunchOpsStore:
    def __init__(self, path: str | Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self._connect() as conn:
            conn.executescript(_SCHEMA)
            self._migrate_trace_archive_v2_conn(conn)

    def _migrate_trace_archive_v2_conn(self, conn: sqlite3.Connection) -> None:
        conn.executescript(_TRACE_ARCHIVE_V2_SCHEMA)

    @contextmanager
    def _connect(self) -> Iterator[sqlite3.Connection]:
        conn = sqlite3.connect(self.path, timeout=30)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys = ON")
        try:
            yield conn
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

    @staticmethod
    def _row_dict(row: sqlite3.Row | None) -> dict[str, Any] | None:
        if row is None:
            return None
        result = dict(row)
        if "remote_access_enabled" in result:
            result["remote_access_enabled"] = bool(result["remote_access_enabled"])
        return result

    def ensure_campaign(
        self,
        *,
        campaign_id: str,
        name: str,
        now: int | None = None,
    ) -> dict[str, Any]:
        cleaned_id = str(campaign_id or "").strip()
        cleaned_name = str(name or "").strip()
        if not cleaned_id or not cleaned_name:
            raise ValueError("campaign id and name are required")
        ts = int(time.time() if now is None else now)
        with self._connect() as conn:
            conn.execute(
                """
                INSERT INTO campaigns(
                    campaign_id, name, created_at, ended_at,
                    access_policy, remote_access_enabled
                ) VALUES (?, ?, ?, NULL, 'local', 0)
                ON CONFLICT(campaign_id) DO NOTHING
                """,
                (cleaned_id, cleaned_name, ts),
            )
            row = conn.execute(
                "SELECT * FROM campaigns WHERE campaign_id = ?",
                (cleaned_id,),
            ).fetchone()
        return self._row_dict(row) or {}

    def create_campaign(self, name: str, *, now: int | None = None) -> dict[str, Any]:
        cleaned = (name or "").strip()
        if not cleaned:
            raise ValueError("campaign name is required")
        ts = int(time.time() if now is None else now)
        campaign_id = uuid.uuid4().hex
        with self._connect() as conn:
            conn.execute(
                """
                INSERT INTO campaigns(
                    campaign_id, name, created_at, ended_at,
                    access_policy, remote_access_enabled
                ) VALUES (?, ?, ?, NULL, 'local', 0)
                """,
                (campaign_id, cleaned, ts),
            )
            row = conn.execute(
                "SELECT * FROM campaigns WHERE campaign_id = ?",
                (campaign_id,),
            ).fetchone()
        return self._row_dict(row) or {}

    def list_campaigns(self, *, include_ended: bool = True) -> list[dict[str, Any]]:
        sql = "SELECT * FROM campaigns"
        params: tuple[Any, ...] = ()
        if not include_ended:
            sql += " WHERE ended_at IS NULL"
        sql += " ORDER BY created_at ASC, campaign_id ASC"
        with self._connect() as conn:
            rows = conn.execute(sql, params).fetchall()
        return [self._row_dict(row) or {} for row in rows]

    def end_campaign(self, campaign_id: str, *, now: int | None = None) -> None:
        ts = int(time.time() if now is None else now)
        with self._connect() as conn:
            cur = conn.execute(
                "UPDATE campaigns SET ended_at = COALESCE(ended_at, ?) WHERE campaign_id = ?",
                (ts, campaign_id),
            )
            if cur.rowcount != 1:
                raise ValueError("campaign not found")

    def end_campaign_and_review_viewers(
        self,
        campaign_id: str,
        *,
        now: int,
        review_expires_at: int,
    ) -> None:
        with self._connect() as conn:
            cur = conn.execute(
                "UPDATE campaigns SET ended_at = COALESCE(ended_at, ?) WHERE campaign_id = ?",
                (int(now), campaign_id),
            )
            if cur.rowcount != 1:
                raise ValueError("campaign not found")
            conn.execute(
                """
                UPDATE viewer_publications
                SET state = 'review',
                    ended_at = COALESCE(ended_at, ?),
                    review_expires_at = ?,
                    config_revision = config_revision + 1
                WHERE campaign_id = ? AND revoked_at IS NULL
                """,
                (int(now), int(review_expires_at), campaign_id),
            )

    def add_room_to_campaign(
        self,
        campaign_id: str,
        room_id: str,
        *,
        now: int | None = None,
    ) -> dict[str, Any]:
        if not room_id:
            raise ValueError("room_id is required")
        ts = int(time.time() if now is None else now)
        with self._connect() as conn:
            campaign = conn.execute(
                "SELECT 1 FROM campaigns WHERE campaign_id = ?",
                (campaign_id,),
            ).fetchone()
            if not campaign:
                raise ValueError("campaign not found")
            active = conn.execute(
                """
                SELECT * FROM campaign_rooms
                WHERE campaign_id = ? AND room_id = ? AND removed_campaign_at IS NULL
                ORDER BY joined_campaign_at DESC LIMIT 1
                """,
                (campaign_id, room_id),
            ).fetchone()
            if active:
                return dict(active)
            conn.execute(
                """
                INSERT INTO rooms(
                    room_id, display_name, member_count, discovered_at,
                    last_seen_at, last_event_at, health_status
                ) VALUES (?, ?, NULL, ?, ?, NULL, 'unknown')
                ON CONFLICT(room_id) DO NOTHING
                """,
                (room_id, room_id, ts, ts),
            )
            conn.execute(
                """
                INSERT INTO campaign_rooms(
                    campaign_id, room_id, joined_campaign_at, removed_campaign_at
                ) VALUES (?, ?, ?, NULL)
                """,
                (campaign_id, room_id, ts),
            )
            self._ensure_trace_managed_room_conn(
                conn, room_id, source="campaign", now=ts
            )
            row = conn.execute(
                """
                SELECT * FROM campaign_rooms
                WHERE campaign_id = ? AND room_id = ? AND joined_campaign_at = ?
                """,
                (campaign_id, room_id, ts),
            ).fetchone()
        return dict(row) if row else {}

    def remove_room_from_campaign(
        self,
        campaign_id: str,
        room_id: str,
        *,
        now: int | None = None,
    ) -> None:
        ts = int(time.time() if now is None else now)
        with self._connect() as conn:
            cur = conn.execute(
                """
                UPDATE campaign_rooms
                SET removed_campaign_at = ?
                WHERE campaign_id = ? AND room_id = ? AND removed_campaign_at IS NULL
                """,
                (ts, campaign_id, room_id),
            )
            if cur.rowcount == 0:
                raise ValueError("active campaign room not found")

    def list_campaign_rooms(
        self,
        campaign_id: str,
        *,
        include_removed: bool = False,
    ) -> list[dict[str, Any]]:
        sql = """
            SELECT cr.*, r.display_name, r.member_count, r.last_event_at, r.health_status
            FROM campaign_rooms AS cr
            LEFT JOIN rooms AS r ON r.room_id = cr.room_id
            WHERE cr.campaign_id = ?
        """
        if not include_removed:
            sql += " AND cr.removed_campaign_at IS NULL"
        sql += " ORDER BY cr.joined_campaign_at ASC, cr.room_id ASC"
        with self._connect() as conn:
            rows = conn.execute(sql, (campaign_id,)).fetchall()
        return [dict(row) for row in rows]

    @staticmethod
    def _room_is_campaign_covered_conn(
        conn: sqlite3.Connection,
        room_id: str,
        event_time: int,
    ) -> bool:
        row = conn.execute(
            """
            SELECT 1
            FROM campaign_rooms
            WHERE room_id = ?
              AND joined_campaign_at <= ?
              AND (removed_campaign_at IS NULL OR removed_campaign_at > ?)
            LIMIT 1
            """,
            (room_id, event_time, event_time),
        ).fetchone()
        return row is not None

    def room_is_campaign_covered(self, room_id: str, event_time: int) -> bool:
        with self._connect() as conn:
            return self._room_is_campaign_covered_conn(conn, room_id, event_time)

    def _insert_event_conn(
        self,
        conn: sqlite3.Connection,
        event: StandardEvent,
        *,
        protected: bool,
        now: int,
    ) -> bool:
        cur = conn.execute(
            """
            INSERT OR IGNORE INTO events(
                event_id, event_type, room_id, event_time,
                source_shard, source_table, source_identity,
                actor_id, subject_id, payload_json, protected, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                event.event_id,
                event.event_type,
                event.room_id,
                event.event_time,
                event.source_shard,
                event.source_table,
                event.source_identity,
                event.actor_id,
                event.subject_id,
                json.dumps(event.payload, ensure_ascii=False, sort_keys=True),
                int(bool(protected)),
                now,
            ),
        )
        inserted = cur.rowcount == 1
        if (
            inserted
            and event.event_type in {"member.invited", "member.joined"}
            and self._trace_room_archiving_enabled_conn(conn, event.room_id)
        ):
            self._invalidate_trace_room_projection_conn(
                conn, event.room_id, state="pending", now=now
            )
            self._enqueue_trace_reconcile_conn(
                conn,
                event.room_id,
                "evidence",
                f"event:{event.event_id}",
                now=now,
            )
        return inserted

    def insert_event(
        self,
        event: StandardEvent,
        *,
        protected: bool | None = None,
        now: int | None = None,
    ) -> bool:
        ts = int(time.time() if now is None else now)
        with self._connect() as conn:
            if protected is None:
                protected = self._room_is_campaign_covered_conn(
                    conn,
                    event.room_id,
                    event.event_time,
                )
            return self._insert_event_conn(
                conn,
                event,
                protected=bool(protected),
                now=ts,
            )

    def count_events(self) -> int:
        with self._connect() as conn:
            row = conn.execute("SELECT COUNT(*) AS n FROM events").fetchone()
        return int(row["n"] if row else 0)

    def get_cursor(self, source_shard: str, source_table: str) -> dict[str, Any] | None:
        with self._connect() as conn:
            row = conn.execute(
                """
                SELECT * FROM watcher_cursors
                WHERE source_shard = ? AND source_table = ?
                """,
                (source_shard, source_table),
            ).fetchone()
        return dict(row) if row else None

    def get_source_stamp(
        self,
        source_shard: str,
        source_table: str,
    ) -> dict[str, int] | None:
        with self._connect() as conn:
            row = conn.execute(
                """
                SELECT db_mtime_ns, wal_mtime_ns, db_size, wal_size
                FROM watcher_source_state
                WHERE source_shard = ? AND source_table = ?
                """,
                (source_shard, source_table),
            ).fetchone()
        if row is None:
            return None
        return {
            "db_mtime_ns": int(row["db_mtime_ns"]),
            "wal_mtime_ns": int(row["wal_mtime_ns"]),
            "db_size": int(row["db_size"]),
            "wal_size": int(row["wal_size"]),
        }

    def get_table_head(
        self,
        source_shard: str,
        source_table: str,
    ) -> dict[str, int] | None:
        with self._connect() as conn:
            row = conn.execute(
                """
                SELECT local_id, create_time
                FROM watcher_table_heads
                WHERE source_shard = ? AND source_table = ?
                """,
                (source_shard, source_table),
            ).fetchone()
        if row is None:
            return None
        return {
            "local_id": int(row["local_id"]),
            "create_time": int(row["create_time"]),
        }

    @staticmethod
    def _set_table_head_conn(
        conn: sqlite3.Connection,
        source_shard: str,
        source_table: str,
        table_head: dict[str, Any],
        *,
        processed_at: int,
    ) -> None:
        conn.execute(
            """
            INSERT INTO watcher_table_heads(
                source_shard, source_table, local_id, create_time, processed_at
            ) VALUES (?, ?, ?, ?, ?)
            ON CONFLICT(source_shard, source_table) DO UPDATE SET
                local_id = excluded.local_id,
                create_time = excluded.create_time,
                processed_at = excluded.processed_at
            """,
            (
                source_shard,
                source_table,
                int(table_head["local_id"]),
                int(table_head["create_time"]),
                int(processed_at),
            ),
        )

    @staticmethod
    def _set_source_stamp_conn(
        conn: sqlite3.Connection,
        source_shard: str,
        source_table: str,
        source_stamp: dict[str, Any],
        *,
        processed_at: int,
    ) -> None:
        conn.execute(
            """
            INSERT INTO watcher_source_state(
                source_shard, source_table,
                db_mtime_ns, wal_mtime_ns, db_size, wal_size,
                processed_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(source_shard, source_table) DO UPDATE SET
                db_mtime_ns = excluded.db_mtime_ns,
                wal_mtime_ns = excluded.wal_mtime_ns,
                db_size = excluded.db_size,
                wal_size = excluded.wal_size,
                processed_at = excluded.processed_at
            """,
            (
                source_shard,
                source_table,
                int(source_stamp["db_mtime_ns"]),
                int(source_stamp["wal_mtime_ns"]),
                int(source_stamp["db_size"]),
                int(source_stamp["wal_size"]),
                int(processed_at),
            ),
        )

    @staticmethod
    def _set_cursor_conn(
        conn: sqlite3.Connection,
        source_shard: str,
        source_table: str,
        *,
        create_time: int,
        local_id: int,
        source_identity: str,
    ) -> None:
        conn.execute(
            """
            INSERT INTO watcher_cursors(
                source_shard, source_table, create_time, local_id, source_identity
            ) VALUES (?, ?, ?, ?, ?)
            ON CONFLICT(source_shard, source_table) DO UPDATE SET
                create_time = excluded.create_time,
                local_id = excluded.local_id,
                source_identity = excluded.source_identity
            """,
            (
                source_shard,
                source_table,
                int(create_time),
                int(local_id),
                source_identity,
            ),
        )

    def set_cursor(
        self,
        source_shard: str,
        source_table: str,
        *,
        create_time: int,
        local_id: int,
        source_identity: str,
    ) -> None:
        with self._connect() as conn:
            self._set_cursor_conn(
                conn,
                source_shard,
                source_table,
                create_time=create_time,
                local_id=local_id,
                source_identity=source_identity,
            )

    @staticmethod
    def _insert_message_conn(
        conn: sqlite3.Connection,
        *,
        message_id: str,
        event_id: str,
        room_id: str,
        event_time: int,
        message_type: str,
        sender_id: str,
        text_content: str,
        payload: dict[str, Any],
        protected: bool,
        now: int,
    ) -> bool:
        cur = conn.execute(
            """
            INSERT OR IGNORE INTO messages(
                message_id, event_id, room_id, event_time,
                message_type, sender_id, text_content,
                payload_json, protected, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                message_id,
                event_id,
                room_id,
                int(event_time),
                message_type,
                sender_id,
                text_content,
                json.dumps(payload, ensure_ascii=False, sort_keys=True),
                int(bool(protected)),
                now,
            ),
        )
        return cur.rowcount == 1

    def insert_message(
        self,
        *,
        message_id: str,
        event_id: str,
        room_id: str,
        event_time: int,
        message_type: str,
        sender_id: str,
        text_content: str,
        payload: dict[str, Any],
        protected: bool | None = None,
        now: int | None = None,
    ) -> bool:
        ts = int(time.time() if now is None else now)
        with self._connect() as conn:
            if protected is None:
                protected = self._room_is_campaign_covered_conn(
                    conn,
                    room_id,
                    event_time,
                )
            return self._insert_message_conn(
                conn,
                message_id=message_id,
                event_id=event_id,
                room_id=room_id,
                event_time=event_time,
                message_type=message_type,
                sender_id=sender_id,
                text_content=text_content,
                payload=payload,
                protected=bool(protected),
                now=ts,
            )

    def apply_source_batch(
        self,
        *,
        source_shard: str,
        source_table: str,
        events: list[StandardEvent],
        source_stamp: dict[str, Any] | None = None,
        table_head: dict[str, Any] | None = None,
        messages: list[dict[str, Any]],
        cursor: dict[str, Any] | None,
        now: int | None = None,
    ) -> dict[str, int]:
        """Atomically persist one source scan and advance its durable cursor."""
        ts = int(time.time() if now is None else now)
        inserted_events = 0
        inserted_messages = 0
        protection_cache: dict[tuple[str, int], bool] = {}

        def is_protected(conn: sqlite3.Connection, room_id: str, event_time: int) -> bool:
            key = (room_id, int(event_time))
            if key not in protection_cache:
                protection_cache[key] = self._room_is_campaign_covered_conn(
                    conn,
                    room_id,
                    int(event_time),
                )
            return protection_cache[key]

        with self._connect() as conn:
            for event in events:
                if event.source_shard != source_shard or event.source_table != source_table:
                    raise ValueError("event source does not match source batch")
                if self._insert_event_conn(
                    conn,
                    event,
                    protected=is_protected(conn, event.room_id, event.event_time),
                    now=ts,
                ):
                    inserted_events += 1

            for message in messages:
                room_id = message["room_id"]
                event_time = int(message["event_time"])
                if self._insert_message_conn(
                    conn,
                    message_id=message["message_id"],
                    event_id=message["event_id"],
                    room_id=room_id,
                    event_time=event_time,
                    message_type=message["message_type"],
                    sender_id=message["sender_id"],
                    text_content=message["text_content"],
                    payload=message["payload"],
                    protected=is_protected(conn, room_id, event_time),
                    now=ts,
                ):
                    inserted_messages += 1

            if cursor is not None:
                self._set_cursor_conn(
                    conn,
                    source_shard,
                    source_table,
                    create_time=int(cursor["create_time"]),
                    local_id=int(cursor["local_id"]),
                    source_identity=str(cursor["source_identity"]),
                )
            if source_stamp is not None:
                self._set_source_stamp_conn(
                    conn,
                    source_shard,
                    source_table,
                    source_stamp,
                    processed_at=ts,
                )
            if table_head is not None:
                self._set_table_head_conn(
                    conn,
                    source_shard,
                    source_table,
                    table_head,
                    processed_at=ts,
                )

        return {
            "inserted_events": inserted_events,
            "inserted_messages": inserted_messages,
        }

    def get_message(self, message_id: str) -> dict[str, Any] | None:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT * FROM messages WHERE message_id = ?",
                (message_id,),
            ).fetchone()
        if not row:
            return None
        result = dict(row)
        result["payload"] = json.loads(result.pop("payload_json"))
        result["protected"] = bool(result["protected"])
        return result

    def purge_expired_messages(self, *, now: int | None = None) -> int:
        ts = int(time.time() if now is None else now)
        cutoff = ts - FULL_MESSAGE_RETENTION_SECONDS
        with self._connect() as conn:
            cur = conn.execute(
                "DELETE FROM messages WHERE protected = 0 AND event_time < ?",
                (cutoff,),
            )
        return cur.rowcount

    def upsert_room(
        self,
        room_id: str,
        display_name: str,
        *,
        member_count: int | None = None,
        last_event_at: int | None = None,
        health_status: str = "unknown",
        now: int | None = None,
    ) -> None:
        ts = int(time.time() if now is None else now)
        with self._connect() as conn:
            conn.execute(
                """
                INSERT INTO rooms(
                    room_id, display_name, member_count, discovered_at,
                    last_seen_at, last_event_at, health_status
                ) VALUES (?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(room_id) DO UPDATE SET
                    display_name = excluded.display_name,
                    member_count = COALESCE(excluded.member_count, rooms.member_count),
                    last_seen_at = excluded.last_seen_at,
                    last_event_at = COALESCE(excluded.last_event_at, rooms.last_event_at),
                    health_status = excluded.health_status
                """,
                (
                    room_id,
                    display_name,
                    member_count,
                    ts,
                    ts,
                    last_event_at,
                    health_status,
                ),
            )

    def upsert_room_profile(
        self,
        room_id: str,
        avatar_url: str,
        *,
        observed_at: int,
    ) -> None:
        with self._connect() as conn:
            conn.execute(
                """
                INSERT INTO room_profiles(room_id, avatar_url, observed_at)
                VALUES (?, ?, ?)
                ON CONFLICT(room_id) DO UPDATE SET
                    avatar_url = excluded.avatar_url,
                    observed_at = excluded.observed_at
                """,
                (room_id, str(avatar_url or ""), int(observed_at)),
            )

    def get_room_profile(self, room_id: str) -> dict[str, Any] | None:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT room_id, avatar_url, observed_at FROM room_profiles WHERE room_id = ?",
                (room_id,),
            ).fetchone()
        return dict(row) if row else None

    def _get_trace_managed_room_conn(
        self, conn: sqlite3.Connection, room_id: str
    ) -> dict[str, Any] | None:
        row = conn.execute(
            "SELECT * FROM trace_managed_rooms WHERE room_id = ?",
            (room_id,),
        ).fetchone()
        if row is None:
            return None
        result = dict(row)
        result["archiving_enabled"] = bool(result["archiving_enabled"])
        return result

    def get_trace_managed_room(self, room_id: str) -> dict[str, Any] | None:
        with self._connect() as conn:
            return self._get_trace_managed_room_conn(conn, room_id)

    def _ensure_trace_managed_room_conn(
        self,
        conn: sqlite3.Connection,
        room_id: str,
        *,
        source: str,
        now: int,
    ) -> dict[str, Any]:
        if source not in {"campaign", "manual"}:
            raise ValueError("trace management source is invalid")
        room = conn.execute(
            "SELECT 1 FROM rooms WHERE room_id = ?",
            (room_id,),
        ).fetchone()
        if room is None:
            raise ValueError("room not found")
        existing = self._get_trace_managed_room_conn(conn, room_id)
        if existing is None:
            conn.execute(
                """
                INSERT INTO trace_managed_rooms(
                    room_id, managed_since, archiving_enabled,
                    archiving_stopped_at, management_source, projection_state
                ) VALUES (?, ?, 1, NULL, ?, 'pending')
                """,
                (room_id, int(now), source),
            )
        else:
            conn.execute(
                """
                UPDATE trace_managed_rooms
                SET archiving_enabled = 1,
                    archiving_stopped_at = NULL,
                    management_source = ?,
                    projection_state = 'pending',
                    last_error_code = '',
                    last_error_message = ''
                WHERE room_id = ?
                """,
                (source, room_id),
            )
        return self._get_trace_managed_room_conn(conn, room_id) or {}

    def ensure_trace_managed_room(
        self, room_id: str, *, source: str, now: int
    ) -> dict[str, Any]:
        with self._connect() as conn:
            return self._ensure_trace_managed_room_conn(
                conn, room_id, source=source, now=int(now)
            )

    def _set_trace_managed_room_enabled_conn(
        self,
        conn: sqlite3.Connection,
        room_id: str,
        enabled: bool,
        *,
        source: str,
        now: int,
    ) -> dict[str, Any]:
        if source not in {"campaign", "manual"}:
            raise ValueError("trace management source is invalid")
        existing = self._get_trace_managed_room_conn(conn, room_id)
        if existing is None:
            if not enabled:
                raise ValueError("trace managed room not found")
            return self._ensure_trace_managed_room_conn(
                conn, room_id, source=source, now=int(now)
            )
        if enabled:
            conn.execute(
                """
                UPDATE trace_managed_rooms
                SET archiving_enabled = 1,
                    archiving_stopped_at = NULL,
                    management_source = ?,
                    projection_state = 'pending',
                    last_error_code = '',
                    last_error_message = ''
                WHERE room_id = ?
                """,
                (source, room_id),
            )
        else:
            conn.execute(
                """
                UPDATE trace_managed_rooms
                SET archiving_enabled = 0,
                    archiving_stopped_at = ?,
                    management_source = ?,
                    projection_state = 'pending',
                    last_error_code = '',
                    last_error_message = ''
                WHERE room_id = ?
                """,
                (int(now), source, room_id),
            )
        return self._get_trace_managed_room_conn(conn, room_id) or {}

    def set_trace_managed_room_enabled(
        self,
        room_id: str,
        enabled: bool,
        *,
        source: str,
        now: int,
    ) -> dict[str, Any]:
        with self._connect() as conn:
            return self._set_trace_managed_room_enabled_conn(
                conn,
                room_id,
                bool(enabled),
                source=source,
                now=int(now),
            )

    def list_trace_managed_rooms(
        self, enabled: bool | None = None
    ) -> list[dict[str, Any]]:
        sql = "SELECT * FROM trace_managed_rooms"
        params: list[Any] = []
        if enabled is not None:
            sql += " WHERE archiving_enabled = ?"
            params.append(int(bool(enabled)))
        sql += " ORDER BY managed_since ASC, room_id ASC"
        with self._connect() as conn:
            rows = conn.execute(sql, params).fetchall()
        result = []
        for row in rows:
            item = dict(row)
            item["archiving_enabled"] = bool(item["archiving_enabled"])
            result.append(item)
        return result

    @staticmethod
    def _trace_observation_id(
        room_id: str,
        source_kind: str,
        source_state_key: str,
        semantic_hash: str,
    ) -> str:
        raw = (
            "trace-membership-v2\0"
            + room_id
            + "\0"
            + source_kind
            + "\0"
            + source_state_key
            + "\0"
            + semantic_hash
        )
        return hashlib.sha256(raw.encode("utf-8")).hexdigest()

    @staticmethod
    def _decode_trace_observation_row(
        row: sqlite3.Row | None,
    ) -> dict[str, Any] | None:
        if row is None:
            return None
        result = dict(row)
        result["members"] = json.loads(result.pop("members_json"))
        return result

    def save_trace_membership_observation(
        self,
        *,
        room_id: str,
        source_kind: str,
        source_state_key: str,
        semantic_hash: str,
        membership_status: str,
        inviter_metadata_status: str,
        members: list[dict] | tuple[dict, ...],
        observed_at: int,
        diagnostic_code: str = "",
    ) -> dict[str, Any]:
        observation_id = self._trace_observation_id(
            room_id, source_kind, source_state_key, semantic_hash
        )
        ts = int(observed_at)
        members_json = json.dumps(
            list(members), ensure_ascii=False, sort_keys=True, separators=(",", ":")
        )
        with self._connect() as conn:
            if source_kind == "contact":
                managed = self._get_trace_managed_room_conn(conn, room_id)
                if managed is None or not managed["archiving_enabled"]:
                    raise ValueError("archive is disabled")
            conn.execute(
                """
                INSERT INTO trace_membership_observations(
                    observation_id, room_id, source_kind, source_state_key,
                    semantic_hash, first_observed_at, last_observed_at,
                    membership_status, inviter_metadata_status, members_json,
                    diagnostic_code
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(observation_id) DO UPDATE SET
                    last_observed_at = MAX(
                        trace_membership_observations.last_observed_at,
                        excluded.last_observed_at
                    )
                """,
                (
                    observation_id,
                    room_id,
                    source_kind,
                    source_state_key,
                    semantic_hash,
                    ts,
                    ts,
                    membership_status,
                    inviter_metadata_status,
                    members_json,
                    str(diagnostic_code or ""),
                ),
            )
            if source_kind == "contact":
                self._apply_trace_contact_attempt_conn(
                    conn,
                    room_id=room_id,
                    observation_id=observation_id,
                    membership_status=membership_status,
                    observed_at=ts,
                )
            row = conn.execute(
                "SELECT * FROM trace_membership_observations WHERE observation_id = ?",
                (observation_id,),
            ).fetchone()
        return self._decode_trace_observation_row(row) or {}

    def _get_trace_membership_observation_conn(
        self, conn: sqlite3.Connection, observation_id: str
    ) -> dict[str, Any] | None:
        row = conn.execute(
            "SELECT * FROM trace_membership_observations WHERE observation_id = ?",
            (observation_id,),
        ).fetchone()
        return self._decode_trace_observation_row(row)

    def get_trace_membership_observation(
        self, observation_id: str
    ) -> dict[str, Any] | None:
        with self._connect() as conn:
            return self._get_trace_membership_observation_conn(conn, observation_id)

    def list_trace_membership_observations(
        self, room_id: str
    ) -> list[dict[str, Any]]:
        with self._connect() as conn:
            rows = conn.execute(
                """
                SELECT * FROM trace_membership_observations
                WHERE room_id = ?
                ORDER BY first_observed_at ASC, observation_id ASC
                """,
                (room_id,),
            ).fetchall()
        return [self._decode_trace_observation_row(row) or {} for row in rows]

    def latest_trace_membership_observation(
        self, room_id: str
    ) -> dict[str, Any] | None:
        with self._connect() as conn:
            row = conn.execute(
                """
                SELECT * FROM trace_membership_observations
                WHERE room_id = ?
                ORDER BY last_observed_at DESC, observation_id DESC
                LIMIT 1
                """,
                (room_id,),
            ).fetchone()
        return self._decode_trace_observation_row(row)

    def _ensure_trace_person_conn(
        self,
        conn: sqlite3.Connection,
        wechat_username: str,
        *,
        now: int,
        nick_name: str = "",
        remark: str = "",
        avatar_url: str = "",
    ) -> dict[str, Any]:
        username = str(wechat_username or "").strip()
        if not username:
            raise ValueError("wechat_username is required")
        row = conn.execute(
            "SELECT * FROM trace_people WHERE wechat_username = ?",
            (username,),
        ).fetchone()
        if row is None:
            person_id = uuid.uuid4().hex
            conn.execute(
                """
                INSERT INTO trace_people(
                    person_id, wechat_username, first_seen_at, last_seen_at,
                    current_nick_name, current_remark, current_avatar_url
                ) VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    person_id,
                    username,
                    int(now),
                    int(now),
                    str(nick_name or ""),
                    str(remark or ""),
                    str(avatar_url or ""),
                ),
            )
        else:
            conn.execute(
                """
                UPDATE trace_people
                SET last_seen_at = MAX(last_seen_at, ?),
                    current_nick_name = CASE WHEN ? <> '' THEN ? ELSE current_nick_name END,
                    current_remark = CASE WHEN ? <> '' THEN ? ELSE current_remark END,
                    current_avatar_url = CASE WHEN ? <> '' THEN ? ELSE current_avatar_url END
                WHERE wechat_username = ?
                """,
                (
                    int(now),
                    str(nick_name or ""),
                    str(nick_name or ""),
                    str(remark or ""),
                    str(remark or ""),
                    str(avatar_url or ""),
                    str(avatar_url or ""),
                    username,
                ),
            )
        current = conn.execute(
            "SELECT * FROM trace_people WHERE wechat_username = ?",
            (username,),
        ).fetchone()
        return dict(current) if current else {}

    def ensure_trace_person(
        self,
        wechat_username: str,
        *,
        now: int,
        nick_name: str = "",
        remark: str = "",
        avatar_url: str = "",
    ) -> dict[str, Any]:
        with self._connect() as conn:
            return self._ensure_trace_person_conn(
                conn,
                wechat_username,
                now=int(now),
                nick_name=nick_name,
                remark=remark,
                avatar_url=avatar_url,
            )

    def get_trace_person_by_username(
        self, wechat_username: str
    ) -> dict[str, Any] | None:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT * FROM trace_people WHERE wechat_username = ?",
                (str(wechat_username or "").strip(),),
            ).fetchone()
        return dict(row) if row else None

    def _record_trace_profile_conn(
        self,
        conn: sqlite3.Connection,
        person_id: str,
        *,
        room_id: str | None,
        observed_at: int,
        nick_name: str = "",
        remark: str = "",
        room_display_name: str = "",
        avatar_url: str = "",
    ) -> dict[str, Any]:
        payload = json.dumps(
            {
                "nick_name": str(nick_name or ""),
                "remark": str(remark or ""),
                "room_display_name": str(room_display_name or ""),
                "avatar_url": str(avatar_url or ""),
            },
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
        profile_hash = hashlib.sha256(payload.encode("utf-8")).hexdigest()
        if room_id is None:
            row = conn.execute(
                """
                SELECT * FROM trace_person_profiles
                WHERE person_id = ? AND room_id IS NULL AND profile_hash = ?
                """,
                (person_id, profile_hash),
            ).fetchone()
        else:
            row = conn.execute(
                """
                SELECT * FROM trace_person_profiles
                WHERE person_id = ? AND room_id = ? AND profile_hash = ?
                """,
                (person_id, room_id, profile_hash),
            ).fetchone()
        if row is None:
            profile_id = uuid.uuid4().hex
            conn.execute(
                """
                INSERT INTO trace_person_profiles(
                    profile_id, person_id, room_id, observed_at,
                    nick_name, remark, room_display_name, avatar_url, profile_hash
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    profile_id,
                    person_id,
                    room_id,
                    int(observed_at),
                    str(nick_name or ""),
                    str(remark or ""),
                    str(room_display_name or ""),
                    str(avatar_url or ""),
                    profile_hash,
                ),
            )
            row = conn.execute(
                "SELECT * FROM trace_person_profiles WHERE profile_id = ?",
                (profile_id,),
            ).fetchone()
        return dict(row) if row else {}

    def get_trace_membership_episode(
        self, episode_id: str
    ) -> dict[str, Any] | None:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT * FROM trace_membership_episodes WHERE episode_id = ?",
                (episode_id,),
            ).fetchone()
        return dict(row) if row else None

    def _get_active_trace_episode_conn(
        self, conn: sqlite3.Connection, room_id: str, person_id: str
    ) -> dict[str, Any] | None:
        row = conn.execute(
            """
            SELECT * FROM trace_membership_episodes
            WHERE room_id = ? AND person_id = ? AND status = 'active'
            """,
            (room_id, person_id),
        ).fetchone()
        return dict(row) if row else None

    def get_active_trace_episode(
        self, room_id: str, person_id: str
    ) -> dict[str, Any] | None:
        with self._connect() as conn:
            return self._get_active_trace_episode_conn(conn, room_id, person_id)

    def list_trace_membership_episodes(
        self, room_id: str, person_id: str
    ) -> list[dict[str, Any]]:
        with self._connect() as conn:
            rows = conn.execute(
                """
                SELECT * FROM trace_membership_episodes
                WHERE room_id = ? AND person_id = ?
                ORDER BY episode_index ASC
                """,
                (room_id, person_id),
            ).fetchall()
        return [dict(row) for row in rows]

    def _ensure_active_trace_episode_conn(
        self,
        conn: sqlite3.Connection,
        room_id: str,
        person_id: str,
        *,
        observed_at: int,
    ) -> dict[str, Any]:
        active = self._get_active_trace_episode_conn(conn, room_id, person_id)
        if active is not None:
            conn.execute(
                """
                UPDATE trace_membership_episodes
                SET last_observed_at = MAX(last_observed_at, ?)
                WHERE episode_id = ?
                """,
                (int(observed_at), active["episode_id"]),
            )
            row = conn.execute(
                "SELECT * FROM trace_membership_episodes WHERE episode_id = ?",
                (active["episode_id"],),
            ).fetchone()
            return dict(row) if row else {}
        next_index = conn.execute(
            """
            SELECT COALESCE(MAX(episode_index), 0) + 1
            FROM trace_membership_episodes
            WHERE room_id = ? AND person_id = ?
            """,
            (room_id, person_id),
        ).fetchone()[0]
        episode_id = uuid.uuid4().hex
        conn.execute(
            """
            INSERT INTO trace_membership_episodes(
                episode_id, room_id, person_id, episode_index,
                first_observed_at, last_observed_at, status
            ) VALUES (?, ?, ?, ?, ?, ?, 'active')
            """,
            (
                episode_id,
                room_id,
                person_id,
                int(next_index),
                int(observed_at),
                int(observed_at),
            ),
        )
        row = conn.execute(
            "SELECT * FROM trace_membership_episodes WHERE episode_id = ?",
            (episode_id,),
        ).fetchone()
        return dict(row) if row else {}

    def _close_missing_active_episodes_conn(
        self,
        conn: sqlite3.Connection,
        room_id: str,
        observed_person_ids: set[str],
        *,
        now: int,
    ) -> None:
        rows = conn.execute(
            """
            SELECT episode_id, person_id
            FROM trace_membership_episodes
            WHERE room_id = ? AND status = 'active'
            """,
            (room_id,),
        ).fetchall()
        for row in rows:
            if row["person_id"] in observed_person_ids:
                continue
            conn.execute(
                """
                UPDATE trace_membership_episodes
                SET status = 'inactive',
                    inactive_observed_at = ?,
                    last_observed_at = MAX(last_observed_at, ?)
                WHERE episode_id = ?
                """,
                (int(now), int(now), row["episode_id"]),
            )

    def _get_active_trace_relation_conn(
        self, conn: sqlite3.Connection, episode_id: str
    ) -> dict[str, Any] | None:
        row = conn.execute(
            """
            SELECT * FROM trace_invite_relations
            WHERE episode_id = ? AND active = 1
            """,
            (episode_id,),
        ).fetchone()
        return dict(row) if row else None

    def get_active_trace_relation(
        self, episode_id: str
    ) -> dict[str, Any] | None:
        with self._connect() as conn:
            return self._get_active_trace_relation_conn(conn, episode_id)

    def list_trace_relations(self, episode_id: str) -> list[dict[str, Any]]:
        with self._connect() as conn:
            rows = conn.execute(
                """
                SELECT * FROM trace_invite_relations
                WHERE episode_id = ?
                ORDER BY valid_from_observation ASC, relation_id ASC
                """,
                (episode_id,),
            ).fetchall()
        return [dict(row) for row in rows]

    def get_active_trace_relation_for_username(
        self, room_id: str, invitee_username: str
    ) -> dict[str, Any] | None:
        with self._connect() as conn:
            person = conn.execute(
                "SELECT * FROM trace_people WHERE wechat_username = ?",
                (str(invitee_username or "").strip(),),
            ).fetchone()
            if person is None:
                return None
            episode = self._get_active_trace_episode_conn(
                conn, room_id, person["person_id"]
            )
            if episode is None:
                return None
            relation = self._get_active_trace_relation_conn(
                conn, episode["episode_id"]
            )
            if relation is None:
                return None
            result = dict(relation)
            result["invitee_username"] = person["wechat_username"]
            if relation["inviter_person_id"] is None:
                result["inviter_username"] = None
            else:
                inviter = conn.execute(
                    "SELECT wechat_username FROM trace_people WHERE person_id = ?",
                    (relation["inviter_person_id"],),
                ).fetchone()
                result["inviter_username"] = (
                    str(inviter["wechat_username"]) if inviter else None
                )
            return result

    def _insert_trace_relation_conn(
        self,
        conn: sqlite3.Connection,
        *,
        episode_id: str,
        inviter_person_id: str | None,
        method: str | None,
        event_time: int | None,
        resolution_state: str,
        now: int,
    ) -> str:
        episode = conn.execute(
            "SELECT * FROM trace_membership_episodes WHERE episode_id = ?",
            (episode_id,),
        ).fetchone()
        if episode is None:
            raise ValueError("membership episode not found")
        if resolution_state == "resolved" and inviter_person_id is None:
            raise ValueError("resolved relation requires inviter")
        if resolution_state == "unresolved" and inviter_person_id is not None:
            raise ValueError("unresolved relation cannot have inviter")
        relation_id = uuid.uuid4().hex
        conn.execute(
            """
            INSERT INTO trace_invite_relations(
                relation_id, room_id, episode_id, invitee_person_id,
                inviter_person_id, valid_from_observation,
                valid_until_observation, first_observed_at,
                last_observed_at, active, resolved_method,
                resolved_event_time, resolution_state
            ) VALUES (?, ?, ?, ?, ?, ?, NULL, ?, ?, 1, ?, ?, ?)
            """,
            (
                relation_id,
                episode["room_id"],
                episode_id,
                episode["person_id"],
                inviter_person_id,
                int(now),
                int(now),
                int(now),
                method,
                event_time,
                resolution_state,
            ),
        )
        return relation_id

    def _close_trace_relation_conn(
        self, conn: sqlite3.Connection, relation_id: str, *, now: int
    ) -> None:
        conn.execute(
            """
            UPDATE trace_invite_relations
            SET active = 0,
                valid_until_observation = ?,
                last_observed_at = MAX(last_observed_at, ?)
            WHERE relation_id = ? AND active = 1
            """,
            (int(now), int(now), relation_id),
        )

    def _enrich_trace_relation_conn(
        self,
        conn: sqlite3.Connection,
        relation_id: str,
        *,
        method: str | None,
        event_time: int | None,
        last_observed_at: int,
        resolution_state: str,
    ) -> None:
        conn.execute(
            """
            UPDATE trace_invite_relations
            SET resolved_method = CASE
                    WHEN ? IS NOT NULL THEN ? ELSE resolved_method END,
                resolved_event_time = CASE
                    WHEN ? IS NOT NULL THEN ? ELSE resolved_event_time END,
                last_observed_at = MAX(last_observed_at, ?),
                resolution_state = ?
            WHERE relation_id = ?
            """,
            (
                method,
                method,
                event_time,
                event_time,
                int(last_observed_at),
                resolution_state,
                relation_id,
            ),
        )

    def _list_attached_trace_evidence_conn(
        self, conn: sqlite3.Connection, episode_id: str
    ) -> list[dict[str, Any]]:
        rows = conn.execute(
            """
            SELECT * FROM trace_relation_evidence
            WHERE episode_id = ? AND attachment_state = 'attached'
            ORDER BY observed_at ASC, evidence_id ASC
            """,
            (episode_id,),
        ).fetchall()
        return [dict(row) for row in rows]

    def _insert_trace_relation_evidence_conn(
        self,
        conn: sqlite3.Connection,
        *,
        enqueue_reconcile: bool,
        **evidence_fields,
    ) -> tuple[dict[str, Any], bool]:
        fields = dict(evidence_fields)
        episode_id = fields.get("episode_id")
        relation_id = fields.get("relation_id")
        room_id = fields["room_id"]
        invitee_person_id = fields.get("invitee_person_id")
        if episode_id is not None:
            episode = conn.execute(
                "SELECT * FROM trace_membership_episodes WHERE episode_id = ?",
                (episode_id,),
            ).fetchone()
            if episode is None:
                raise ValueError("membership episode not found")
            if episode["room_id"] != room_id:
                raise ValueError("evidence room does not match episode")
            if invitee_person_id is not None and episode["person_id"] != invitee_person_id:
                raise ValueError("evidence invitee does not match episode")
            if relation_id is not None:
                relation = conn.execute(
                    "SELECT * FROM trace_invite_relations WHERE relation_id = ?",
                    (relation_id,),
                ).fetchone()
                if relation is None or relation["episode_id"] != episode_id:
                    raise ValueError("evidence relation does not match episode")
        before = conn.total_changes
        conn.execute(
            """
            INSERT OR IGNORE INTO trace_relation_evidence(
                evidence_id, room_id, episode_id, relation_id,
                attachment_state, attachment_reason, source_type,
                source_identity, evidence_subject_key, observed_at,
                inviter_person_id, invitee_person_id, event_time, method,
                raw_reference, authority_level, conflicts_with_resolution
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                fields["evidence_id"],
                room_id,
                episode_id,
                relation_id,
                fields["attachment_state"],
                fields.get("attachment_reason", ""),
                fields["source_type"],
                fields["source_identity"],
                fields["evidence_subject_key"],
                int(fields["observed_at"]),
                fields.get("inviter_person_id"),
                invitee_person_id,
                fields.get("event_time"),
                fields.get("method"),
                fields.get("raw_reference"),
                int(fields["authority_level"]),
                int(bool(fields.get("conflicts_with_resolution", False))),
            ),
        )
        inserted = conn.total_changes > before
        row = conn.execute(
            """
            SELECT * FROM trace_relation_evidence
            WHERE source_type = ? AND source_identity = ?
              AND evidence_subject_key = ?
            """,
            (
                fields["source_type"],
                fields["source_identity"],
                fields["evidence_subject_key"],
            ),
        ).fetchone()
        result = dict(row) if row else {}
        if (
            inserted
            and enqueue_reconcile
            and self._trace_room_archiving_enabled_conn(conn, room_id)
        ):
            self._invalidate_trace_room_projection_conn(
                conn, room_id, state="pending", now=int(fields["observed_at"])
            )
            self._enqueue_trace_reconcile_conn(
                conn,
                room_id,
                "evidence",
                f"evidence:{result['evidence_id']}",
                now=int(fields["observed_at"]),
            )
        return result, inserted

    def insert_trace_relation_evidence(
        self,
        *,
        evidence_id: str,
        room_id: str,
        episode_id: str | None,
        relation_id: str | None,
        attachment_state: str,
        attachment_reason: str,
        source_type: str,
        source_identity: str,
        evidence_subject_key: str,
        observed_at: int,
        inviter_person_id: str | None,
        invitee_person_id: str | None,
        event_time: int | None,
        method: str | None,
        raw_reference: str | None,
        authority_level: int,
    ) -> dict[str, Any]:
        with self._connect() as conn:
            row, _ = self._insert_trace_relation_evidence_conn(
                conn,
                enqueue_reconcile=True,
                evidence_id=evidence_id,
                room_id=room_id,
                episode_id=episode_id,
                relation_id=relation_id,
                attachment_state=attachment_state,
                attachment_reason=attachment_reason,
                source_type=source_type,
                source_identity=source_identity,
                evidence_subject_key=evidence_subject_key,
                observed_at=int(observed_at),
                inviter_person_id=inviter_person_id,
                invitee_person_id=invitee_person_id,
                event_time=event_time,
                method=method,
                raw_reference=raw_reference,
                authority_level=int(authority_level),
            )
            return row

    def get_trace_evidence_by_source(
        self, source_type: str, source_identity: str
    ) -> dict[str, Any] | None:
        with self._connect() as conn:
            row = conn.execute(
                """
                SELECT * FROM trace_relation_evidence
                WHERE source_type = ? AND source_identity = ?
                ORDER BY observed_at ASC, evidence_id ASC LIMIT 1
                """,
                (source_type, source_identity),
            ).fetchone()
        return dict(row) if row else None

    def list_trace_evidence_for_room(
        self, room_id: str
    ) -> list[dict[str, Any]]:
        with self._connect() as conn:
            rows = conn.execute(
                """
                SELECT * FROM trace_relation_evidence
                WHERE room_id = ? ORDER BY observed_at ASC, evidence_id ASC
                """,
                (room_id,),
            ).fetchall()
        return [dict(row) for row in rows]

    def _mark_trace_evidence_attachment_conn(
        self,
        conn: sqlite3.Connection,
        evidence_id: str,
        *,
        state: str,
        reason: str,
    ) -> dict[str, Any]:
        if state not in {"ambiguous", "unattached"}:
            raise ValueError("invalid evidence attachment state")
        conn.execute(
            """
            UPDATE trace_relation_evidence
            SET attachment_state = ?, attachment_reason = ?,
                episode_id = NULL, relation_id = NULL
            WHERE evidence_id = ?
            """,
            (state, reason, evidence_id),
        )
        row = conn.execute(
            "SELECT * FROM trace_relation_evidence WHERE evidence_id = ?",
            (evidence_id,),
        ).fetchone()
        return dict(row) if row else {}

    def _attach_trace_evidence_conn(
        self,
        conn: sqlite3.Connection,
        evidence_id: str,
        episode_id: str,
        *,
        reason: str,
    ) -> dict[str, Any]:
        evidence = conn.execute(
            "SELECT * FROM trace_relation_evidence WHERE evidence_id = ?",
            (evidence_id,),
        ).fetchone()
        episode = conn.execute(
            "SELECT * FROM trace_membership_episodes WHERE episode_id = ?",
            (episode_id,),
        ).fetchone()
        if evidence is None or episode is None:
            raise ValueError("evidence or episode not found")
        if evidence["room_id"] != episode["room_id"]:
            raise ValueError("evidence room does not match episode")
        if evidence["invitee_person_id"] != episode["person_id"]:
            raise ValueError("evidence invitee does not match episode")
        conn.execute(
            """
            UPDATE trace_relation_evidence
            SET attachment_state = 'attached', attachment_reason = ?,
                episode_id = ?, relation_id = NULL
            WHERE evidence_id = ?
            """,
            (reason, episode_id, evidence_id),
        )
        row = conn.execute(
            "SELECT * FROM trace_relation_evidence WHERE evidence_id = ?",
            (evidence_id,),
        ).fetchone()
        return dict(row) if row else {}

    def _candidate_trace_episodes_for_evidence_conn(
        self, conn: sqlite3.Connection, evidence: dict[str, Any]
    ) -> list[dict[str, Any]]:
        invitee_person_id = evidence.get("invitee_person_id")
        if not invitee_person_id:
            return []
        rows = conn.execute(
            """
            SELECT * FROM trace_membership_episodes
            WHERE room_id = ? AND person_id = ?
            ORDER BY episode_index ASC
            """,
            (evidence["room_id"], invitee_person_id),
        ).fetchall()
        episodes = [dict(row) for row in rows]
        event_time = evidence.get("event_time")
        if event_time is None:
            return episodes
        matching = []
        for episode in episodes:
            start = int(episode["first_observed_at"])
            end = episode["inactive_observed_at"]
            if int(event_time) >= start and (end is None or int(event_time) <= int(end)):
                matching.append(episode)
        if matching:
            return matching
        if len(episodes) == 1:
            return episodes
        return []

    def _upsert_trace_conflict_conn(
        self,
        conn: sqlite3.Connection,
        *,
        room_id: str,
        episode_id: str | None,
        relation_id: str | None,
        conflict_type: str,
        fingerprint: str,
        detail: dict[str, Any],
        now: int,
    ) -> dict[str, Any]:
        conn.execute(
            """
            INSERT INTO trace_conflicts(
                conflict_id, room_id, episode_id, relation_id, conflict_type,
                fingerprint, detail_json, created_at, last_seen_at, resolved_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, NULL)
            ON CONFLICT(fingerprint) DO UPDATE SET
                last_seen_at = excluded.last_seen_at,
                detail_json = excluded.detail_json,
                relation_id = COALESCE(excluded.relation_id, trace_conflicts.relation_id)
            """,
            (
                uuid.uuid4().hex,
                room_id,
                episode_id,
                relation_id,
                conflict_type,
                fingerprint,
                json.dumps(detail, ensure_ascii=False, sort_keys=True),
                int(now),
                int(now),
            ),
        )
        row = conn.execute(
            "SELECT * FROM trace_conflicts WHERE fingerprint = ?",
            (fingerprint,),
        ).fetchone()
        result = dict(row) if row else {}
        if result and "detail_json" in result:
            result["detail"] = json.loads(result["detail_json"])
        return result

    def list_trace_conflicts(
        self,
        *,
        room_id: str | None = None,
        episode_id: str | None = None,
    ) -> list[dict[str, Any]]:
        clauses = []
        params: list[Any] = []
        if room_id is not None:
            clauses.append("room_id = ?")
            params.append(room_id)
        if episode_id is not None:
            clauses.append("episode_id = ?")
            params.append(episode_id)
        sql = "SELECT * FROM trace_conflicts"
        if clauses:
            sql += " WHERE " + " AND ".join(clauses)
        sql += " ORDER BY created_at ASC, conflict_id ASC"
        with self._connect() as conn:
            rows = conn.execute(sql, params).fetchall()
        result = []
        for row in rows:
            item = dict(row)
            item["detail"] = json.loads(item["detail_json"])
            result.append(item)
        return result

    def _add_trace_manual_confirmation_conn(
        self,
        conn: sqlite3.Connection,
        episode_id: str,
        *,
        inviter_person_id: str | None,
        unresolved: bool,
        method: str | None,
        event_time: int | None,
        reason: str,
        operator_id: str,
        now: int,
    ) -> dict[str, Any]:
        episode = conn.execute(
            "SELECT * FROM trace_membership_episodes WHERE episode_id = ?",
            (episode_id,),
        ).fetchone()
        if episode is None:
            raise ValueError("membership episode not found")
        managed = self._get_trace_managed_room_conn(conn, episode["room_id"])
        if managed is None or not managed["archiving_enabled"]:
            raise ValueError("archive is disabled")
        if not str(reason or "").strip():
            raise ValueError("manual confirmation reason is required")
        if unresolved:
            inviter_person_id = None
        elif inviter_person_id is None:
            raise ValueError("manual confirmation requires inviter")
        active = self._get_active_trace_relation_conn(conn, episode_id)
        audit_id = uuid.uuid4().hex
        evidence_id = uuid.uuid4().hex
        self._insert_trace_relation_evidence_conn(
            conn,
            enqueue_reconcile=False,
            evidence_id=evidence_id,
            room_id=episode["room_id"],
            episode_id=episode_id,
            relation_id=(active["relation_id"] if active else None),
            attachment_state="attached",
            attachment_reason="manual_confirmation",
            source_type="manual_confirmation",
            source_identity=f"manual:{audit_id}",
            evidence_subject_key=str(episode["person_id"]),
            observed_at=int(now),
            inviter_person_id=inviter_person_id,
            invitee_person_id=episode["person_id"],
            event_time=event_time,
            method=method,
            raw_reference=None,
            authority_level=100,
        )
        conn.execute(
            """
            INSERT INTO trace_manual_audit(
                audit_id, room_id, episode_id, relation_id, manual_evidence_id,
                operator_id, changed_at, action, previous_inviter_person_id,
                new_inviter_person_id, previous_method, new_method,
                previous_event_time, new_event_time, reason
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                audit_id,
                episode["room_id"],
                episode_id,
                (active["relation_id"] if active else None),
                evidence_id,
                str(operator_id or "local"),
                int(now),
                "mark_unresolved" if unresolved else "confirm",
                (active["inviter_person_id"] if active else None),
                inviter_person_id,
                (active["resolved_method"] if active else None),
                method,
                (active["resolved_event_time"] if active else None),
                event_time,
                str(reason).strip(),
            ),
        )
        self._invalidate_trace_room_projection_conn(
            conn, episode["room_id"], state="pending", now=int(now)
        )
        self._enqueue_trace_reconcile_conn(
            conn,
            episode["room_id"],
            "manual",
            f"manual:{audit_id}",
            now=int(now),
        )
        row = conn.execute(
            "SELECT * FROM trace_manual_audit WHERE audit_id = ?",
            (audit_id,),
        ).fetchone()
        return dict(row) if row else {}

    def add_trace_manual_confirmation(
        self,
        episode_id: str,
        *,
        inviter_person_id: str | None,
        unresolved: bool,
        method: str | None,
        event_time: int | None,
        reason: str,
        operator_id: str,
        now: int,
    ) -> dict[str, Any]:
        with self._connect() as conn:
            return self._add_trace_manual_confirmation_conn(
                conn,
                episode_id,
                inviter_person_id=inviter_person_id,
                unresolved=bool(unresolved),
                method=method,
                event_time=event_time,
                reason=reason,
                operator_id=operator_id,
                now=int(now),
            )

    def _trace_room_archiving_enabled_conn(
        self, conn: sqlite3.Connection, room_id: str
    ) -> bool:
        row = conn.execute(
            "SELECT archiving_enabled FROM trace_managed_rooms WHERE room_id = ?",
            (room_id,),
        ).fetchone()
        return bool(row and row["archiving_enabled"])

    def _supersede_pending_membership_reconcile_conn(
        self,
        conn: sqlite3.Connection,
        room_id: str,
        *,
        keep_source_identity: str,
        now: int,
    ) -> None:
        conn.execute(
            """
            UPDATE trace_reconcile_queue
            SET acknowledged_at = ?
            WHERE room_id = ?
              AND reason = 'membership'
              AND acknowledged_at IS NULL
              AND source_identity <> ?
            """,
            (int(now), room_id, keep_source_identity),
        )

    def _enqueue_trace_reconcile_conn(
        self,
        conn: sqlite3.Connection,
        room_id: str,
        reason: str,
        source_identity: str,
        *,
        now: int,
        reactivate_acknowledged: bool = False,
    ) -> dict[str, Any] | None:
        if not self._trace_room_archiving_enabled_conn(conn, room_id):
            return None
        if reason not in {"membership", "evidence", "manual", "force_refresh", "migration"}:
            raise ValueError("invalid trace reconcile reason")
        existing = conn.execute(
            """
            SELECT * FROM trace_reconcile_queue
            WHERE room_id = ? AND reason = ? AND source_identity = ?
            """,
            (room_id, reason, source_identity),
        ).fetchone()
        if existing is not None:
            if reactivate_acknowledged and existing["acknowledged_at"] is not None:
                conn.execute(
                    """
                    UPDATE trace_reconcile_queue
                    SET acknowledged_at = NULL, created_at = ?
                    WHERE queue_id = ?
                    """,
                    (int(now), existing["queue_id"]),
                )
                existing = conn.execute(
                    "SELECT * FROM trace_reconcile_queue WHERE queue_id = ?",
                    (existing["queue_id"],),
                ).fetchone()
            return dict(existing) if existing else None
        queue_id = uuid.uuid4().hex
        conn.execute(
            """
            INSERT INTO trace_reconcile_queue(
                queue_id, room_id, reason, source_identity, created_at, acknowledged_at
            ) VALUES (?, ?, ?, ?, ?, NULL)
            """,
            (queue_id, room_id, reason, source_identity, int(now)),
        )
        row = conn.execute(
            "SELECT * FROM trace_reconcile_queue WHERE queue_id = ?",
            (queue_id,),
        ).fetchone()
        return dict(row) if row else None

    def enqueue_trace_reconcile(
        self, room_id: str, reason: str, source_identity: str, *, now: int
    ) -> dict[str, Any] | None:
        with self._connect() as conn:
            return self._enqueue_trace_reconcile_conn(
                conn, room_id, reason, source_identity, now=int(now)
            )

    def list_pending_trace_reconcile(
        self, room_id: str | None = None
    ) -> list[dict[str, Any]]:
        sql = "SELECT * FROM trace_reconcile_queue WHERE acknowledged_at IS NULL"
        params: list[Any] = []
        if room_id is not None:
            sql += " AND room_id = ?"
            params.append(room_id)
        sql += " ORDER BY created_at ASC, queue_id ASC"
        with self._connect() as conn:
            rows = conn.execute(sql, params).fetchall()
        return [dict(row) for row in rows]

    def ack_trace_reconcile(self, queue_id: str, *, now: int) -> None:
        with self._connect() as conn:
            conn.execute(
                """
                UPDATE trace_reconcile_queue
                SET acknowledged_at = COALESCE(acknowledged_at, ?)
                WHERE queue_id = ?
                """,
                (int(now), queue_id),
            )

    def ack_trace_reconcile_if_room_enabled(
        self, queue_id: str, room_id: str, *, now: int
    ) -> bool:
        """ACK only if the queue row still belongs to an enabled archive room."""
        with self._connect() as conn:
            cursor = conn.execute(
                """
                UPDATE trace_reconcile_queue
                SET acknowledged_at = ?
                WHERE queue_id = ?
                  AND room_id = ?
                  AND acknowledged_at IS NULL
                  AND EXISTS (
                      SELECT 1
                      FROM trace_managed_rooms AS managed
                      WHERE managed.room_id = trace_reconcile_queue.room_id
                        AND managed.archiving_enabled = 1
                  )
                """,
                (int(now), queue_id, room_id),
            )
            return bool(cursor.rowcount)

    def _apply_trace_contact_attempt_conn(
        self,
        conn: sqlite3.Connection,
        *,
        room_id: str,
        observation_id: str,
        membership_status: str,
        observed_at: int,
    ) -> str:
        managed = self._get_trace_managed_room_conn(conn, room_id)
        if managed is None or not managed["archiving_enabled"]:
            return "archive_disabled_noop"
        if (
            membership_status == "complete"
            and managed["projection_state"] == "ready"
            and managed["current_observation_id"] == observation_id
        ):
            return "ready_same_observation_noop"
        if membership_status == "failed":
            self._invalidate_trace_room_projection_conn(
                conn,
                room_id,
                state="refresh_error",
                error_code="contact_read_failed",
                error_message="current Contact membership attempt failed",
                now=observed_at,
            )
            result = "refresh_error_reconcile"
        else:
            self._invalidate_trace_room_projection_conn(
                conn, room_id, state="pending", now=observed_at
            )
            result = "pending_reconcile"
        source_identity = f"membership:{observation_id}"
        self._supersede_pending_membership_reconcile_conn(
            conn,
            room_id,
            keep_source_identity=source_identity,
            now=observed_at,
        )
        self._enqueue_trace_reconcile_conn(
            conn,
            room_id,
            "membership",
            source_identity,
            now=observed_at,
            reactivate_acknowledged=True,
        )
        return result

    def _invalidate_trace_room_projection_conn(
        self,
        conn: sqlite3.Connection,
        room_id: str,
        *,
        state: str,
        now: int,
        error_code: str = "",
        error_message: str = "",
    ) -> None:
        if state not in {"pending", "refresh_error"}:
            raise ValueError("invalid trace projection state")
        conn.execute(
            """
            UPDATE trace_managed_rooms
            SET projection_state = ?,
                last_refresh_at = ?,
                last_error_code = ?,
                last_error_message = ?
            WHERE room_id = ? AND archiving_enabled = 1
            """,
            (
                state,
                int(now),
                str(error_code or ""),
                str(error_message or ""),
                room_id,
            ),
        )

    def _mark_trace_room_projection_ready_conn(
        self,
        conn: sqlite3.Connection,
        room_id: str,
        observation_id: str,
        *,
        now: int,
    ) -> None:
        observation = self._get_trace_membership_observation_conn(conn, observation_id)
        if observation is None or observation["room_id"] != room_id:
            raise ValueError("trace observation not found for room")
        if observation["source_kind"] != "contact":
            raise ValueError("room projection requires contact observation")
        if observation["membership_status"] != "complete":
            raise ValueError("room projection requires complete membership observation")
        conn.execute(
            """
            UPDATE trace_managed_rooms
            SET projection_state = 'ready',
                current_observation_id = ?,
                last_refresh_at = ?,
                last_success_at = ?,
                last_reconciled_at = ?,
                last_error_code = '',
                last_error_message = ''
            WHERE room_id = ? AND archiving_enabled = 1
            """,
            (observation_id, int(now), int(now), int(now), room_id),
        )

    def mark_trace_room_projection_ready(
        self, room_id: str, observation_id: str, *, now: int
    ) -> None:
        with self._connect() as conn:
            self._mark_trace_room_projection_ready_conn(
                conn, room_id, observation_id, now=int(now)
            )

    def mark_trace_room_refresh_error(
        self,
        room_id: str,
        *,
        error_code: str,
        error_message: str,
        now: int,
    ) -> None:
        with self._connect() as conn:
            self._invalidate_trace_room_projection_conn(
                conn,
                room_id,
                state="refresh_error",
                error_code=error_code,
                error_message=error_message,
                now=int(now),
            )

    def trace_archive_row_counts(self) -> dict[str, int]:
        mapping = {
            "people": "trace_people",
            "profiles": "trace_person_profiles",
            "episodes": "trace_membership_episodes",
            "relations": "trace_invite_relations",
            "evidence": "trace_relation_evidence",
            "observations": "trace_membership_observations",
            "conflicts": "trace_conflicts",
        }
        result: dict[str, int] = {}
        with self._connect() as conn:
            for key, table in mapping.items():
                row = conn.execute(f"SELECT COUNT(*) AS n FROM {table}").fetchone()
                result[key] = int(row["n"] if row else 0)
        return result

    def _get_trace_room_projection_status_conn(
        self, conn: sqlite3.Connection, room_id: str
    ) -> dict[str, Any]:
            managed = self._get_trace_managed_room_conn(conn, room_id)
            global_row = conn.execute(
                "SELECT value FROM meta WHERE key = 'trace_archive_migration_checkpoint_v2'"
            ).fetchone()
            global_ready = bool(global_row and global_row["value"] == "ready")
            if managed is None:
                return {
                    "room_id": room_id,
                    "archiving_enabled": False,
                    "projection_state": "pending",
                    "current_observation_id": None,
                    "current_observation_membership_status": None,
                    "current_observation_inviter_metadata_status": None,
                    "last_refresh_at": None,
                    "last_success_at": None,
                    "last_reconciled_at": None,
                    "last_error_code": "",
                    "last_error_message": "",
                    "global_migration_ready": global_ready,
                    "use_archive_current_projection": False,
                }
            observation = None
            if managed.get("current_observation_id"):
                observation = self._get_trace_membership_observation_conn(
                    conn, managed["current_observation_id"]
                )
            room_ready = bool(
                managed["archiving_enabled"]
                and managed["projection_state"] == "ready"
                and observation is not None
                and observation["source_kind"] == "contact"
                and observation["membership_status"] == "complete"
            )
            return {
                "room_id": room_id,
                "archiving_enabled": bool(managed["archiving_enabled"]),
                "projection_state": managed["projection_state"],
                "current_observation_id": managed["current_observation_id"],
                "current_observation_membership_status": (
                    observation["membership_status"] if observation else None
                ),
                "current_observation_inviter_metadata_status": (
                    observation["inviter_metadata_status"] if observation else None
                ),
                "last_refresh_at": managed["last_refresh_at"],
                "last_success_at": managed["last_success_at"],
                "last_reconciled_at": managed["last_reconciled_at"],
                "last_error_code": managed["last_error_code"],
                "last_error_message": managed["last_error_message"],
                "global_migration_ready": global_ready,
                "use_archive_current_projection": bool(global_ready and room_ready),
            }

    def get_trace_room_projection_status(self, room_id: str) -> dict[str, Any]:
        with self._connect() as conn:
            return self._get_trace_room_projection_status_conn(conn, room_id)

    def trace_room_projection_ready(self, room_id: str) -> bool:
        return bool(self.get_trace_room_projection_status(room_id)["use_archive_current_projection"])

    def save_membership_snapshot(
        self,
        room_id: str,
        members: list[dict[str, Any]],
        *,
        captured_at: int,
    ) -> None:
        with self._connect() as conn:
            conn.execute(
                """
                INSERT OR REPLACE INTO membership_snapshots(
                    room_id, captured_at, members_json
                ) VALUES (?, ?, ?)
                """,
                (
                    room_id,
                    int(captured_at),
                    json.dumps(members, ensure_ascii=False, sort_keys=True),
                ),
            )

    def latest_membership_snapshot(self, room_id: str) -> dict[str, Any] | None:
        with self._connect() as conn:
            row = conn.execute(
                """
                SELECT captured_at, members_json
                FROM membership_snapshots
                WHERE room_id = ?
                ORDER BY captured_at DESC
                LIMIT 1
                """,
                (room_id,),
            ).fetchone()
        if row is None:
            return None
        return {
            "room_id": room_id,
            "captured_at": int(row["captured_at"]),
            "members": json.loads(row["members_json"]),
        }

    def apply_operator_control_state(
        self,
        *,
        revision: int,
        campaign_id: str,
        campaign_name: str,
        campaign_state: str,
        rooms: list[dict[str, str]],
        publication: dict[str, Any] | None,
        now: int,
    ) -> str:
        if revision <= 0 or not campaign_id or not campaign_name:
            raise ValueError("operator control state is invalid")
        if campaign_state not in {"active", "ended"}:
            raise ValueError("operator campaign state is invalid")
        with self._connect() as conn:
            row = conn.execute(
                "SELECT value FROM meta WHERE key = 'operator_control_revision'"
            ).fetchone()
            current_revision = int(row["value"] if row else 0)
            if revision < current_revision:
                return "stale"
            if revision == current_revision:
                return "unchanged"

            conn.execute(
                """
                INSERT INTO campaigns(
                    campaign_id, name, created_at, ended_at,
                    access_policy, remote_access_enabled
                ) VALUES (?, ?, ?, NULL, 'local', 0)
                ON CONFLICT(campaign_id) DO UPDATE SET name = excluded.name
                """,
                (campaign_id, campaign_name, int(now)),
            )
            if campaign_state == "ended":
                conn.execute(
                    "UPDATE campaigns SET ended_at = COALESCE(ended_at, ?) WHERE campaign_id = ?",
                    (int(now), campaign_id),
                )

            for room in rooms:
                room_id = str(room["room_id"])
                display_name = str(room["display_name"])
                conn.execute(
                    """
                    INSERT INTO rooms(
                        room_id, display_name, member_count, discovered_at,
                        last_seen_at, last_event_at, health_status
                    ) VALUES (?, ?, NULL, ?, ?, NULL, 'unknown')
                    ON CONFLICT(room_id) DO UPDATE SET
                        display_name = excluded.display_name,
                        last_seen_at = excluded.last_seen_at
                    """,
                    (room_id, display_name, int(now), int(now)),
                )
                active = conn.execute(
                    """
                    SELECT 1 FROM campaign_rooms
                    WHERE campaign_id = ? AND room_id = ? AND removed_campaign_at IS NULL
                    """,
                    (campaign_id, room_id),
                ).fetchone()
                if active is None:
                    conn.execute(
                        """
                        INSERT INTO campaign_rooms(
                            campaign_id, room_id, joined_campaign_at, removed_campaign_at
                        ) VALUES (?, ?, ?, NULL)
                        """,
                        (campaign_id, room_id, int(now)),
                    )
                    self._ensure_trace_managed_room_conn(
                        conn,
                        room_id,
                        source="campaign",
                        now=int(now),
                    )

            if publication is not None:
                publication_id = str(publication["publication_id"])
                path_slug = str(publication["path_slug"])
                state = str(publication["state"])
                if state not in {"published", "review", "revoked"}:
                    raise ValueError("viewer publication state is invalid")
                review_expires_at = publication.get("review_expires_at")
                existing = conn.execute(
                    "SELECT * FROM viewer_publications WHERE publication_id = ?",
                    (publication_id,),
                ).fetchone()
                if existing is None:
                    conn.execute(
                        """
                        INSERT INTO viewer_publications(
                            publication_id, campaign_id, path_slug, state,
                            created_at, published_at, ended_at, review_expires_at,
                            revoked_at, config_revision
                        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                        """,
                        (
                            publication_id,
                            campaign_id,
                            path_slug,
                            state,
                            int(now),
                            int(now),
                            int(now) if state == "review" else None,
                            int(review_expires_at) if review_expires_at is not None else None,
                            int(now) if state == "revoked" else None,
                            int(revision),
                        ),
                    )
                else:
                    conn.execute(
                        """
                        UPDATE viewer_publications
                        SET campaign_id = ?, path_slug = ?, state = ?,
                            ended_at = CASE WHEN ? = 'review' THEN COALESCE(ended_at, ?) ELSE ended_at END,
                            review_expires_at = ?,
                            revoked_at = CASE WHEN ? = 'revoked' THEN COALESCE(revoked_at, ?) ELSE revoked_at END,
                            config_revision = ?
                        WHERE publication_id = ?
                        """,
                        (
                            campaign_id,
                            path_slug,
                            state,
                            state,
                            int(now),
                            int(review_expires_at) if review_expires_at is not None else None,
                            state,
                            int(now),
                            int(revision),
                            publication_id,
                        ),
                    )

                if state == "revoked":
                    conn.execute(
                        "UPDATE viewer_credentials SET revoked_at = COALESCE(revoked_at, ?) WHERE publication_id = ?",
                        (int(now), publication_id),
                    )
                    conn.execute(
                        "UPDATE viewer_sessions SET revoked_at = COALESCE(revoked_at, ?) WHERE publication_id = ?",
                        (int(now), publication_id),
                    )
                else:
                    version = int(publication["credential_version"])
                    verifier = publication["verifier"]
                    active_credential = conn.execute(
                        """
                        SELECT * FROM viewer_credentials
                        WHERE publication_id = ? AND revoked_at IS NULL
                        ORDER BY version DESC LIMIT 1
                        """,
                        (publication_id,),
                    ).fetchone()
                    if active_credential is not None and int(active_credential["version"]) == version:
                        if (
                            str(active_credential["algorithm"]) != str(verifier["algorithm"])
                            or int(active_credential["iterations"]) != int(verifier["iterations"])
                            or str(active_credential["salt_hex"]) != str(verifier["salt_hex"])
                            or str(active_credential["verifier_hex"]) != str(verifier["verifier_hex"])
                        ):
                            raise ValueError("viewer credential version conflicts with local state")
                    else:
                        conn.execute(
                            "UPDATE viewer_credentials SET revoked_at = ? WHERE publication_id = ? AND revoked_at IS NULL",
                            (int(now), publication_id),
                        )
                        conn.execute(
                            "UPDATE viewer_sessions SET revoked_at = ? WHERE publication_id = ? AND revoked_at IS NULL",
                            (int(now), publication_id),
                        )
                        conn.execute(
                            """
                            INSERT INTO viewer_credentials(
                                credential_id, publication_id, credential_type,
                                algorithm, iterations, salt_hex, verifier_hex,
                                version, created_at, revoked_at
                            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, NULL)
                            """,
                            (
                                f"control:{publication_id}:{version}",
                                publication_id,
                                str(publication.get("credential_type") or "shared"),
                                str(verifier["algorithm"]),
                                int(verifier["iterations"]),
                                str(verifier["salt_hex"]),
                                str(verifier["verifier_hex"]),
                                version,
                                int(now),
                            ),
                        )

            conn.execute(
                """
                INSERT INTO meta(key, value) VALUES ('operator_control_revision', ?)
                ON CONFLICT(key) DO UPDATE SET value = excluded.value
                """,
                (str(int(revision)),),
            )
        return "applied"

    def create_viewer_publication(
        self,
        *,
        publication_id: str,
        campaign_id: str,
        path_slug: str,
        now: int,
    ) -> dict[str, Any]:
        with self._connect() as conn:
            if conn.execute(
                "SELECT 1 FROM campaigns WHERE campaign_id = ?",
                (campaign_id,),
            ).fetchone() is None:
                raise ValueError("campaign not found")
            conn.execute(
                """
                INSERT INTO viewer_publications(
                    publication_id, campaign_id, path_slug, state,
                    created_at, published_at, ended_at, review_expires_at,
                    revoked_at, config_revision
                ) VALUES (?, ?, ?, 'published', ?, ?, NULL, NULL, NULL, 1)
                """,
                (publication_id, campaign_id, path_slug, int(now), int(now)),
            )
            row = conn.execute(
                "SELECT * FROM viewer_publications WHERE publication_id = ?",
                (publication_id,),
            ).fetchone()
        return dict(row) if row else {}

    def list_viewer_publications(self, campaign_id: str) -> list[dict[str, Any]]:
        with self._connect() as conn:
            rows = conn.execute(
                """
                SELECT * FROM viewer_publications
                WHERE campaign_id = ?
                ORDER BY created_at DESC, publication_id DESC
                """,
                (campaign_id,),
            ).fetchall()
        return [dict(row) for row in rows]

    def get_viewer_publication(self, publication_id: str) -> dict[str, Any] | None:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT * FROM viewer_publications WHERE publication_id = ?",
                (publication_id,),
            ).fetchone()
        return dict(row) if row else None

    def get_viewer_publication_by_slug(self, path_slug: str) -> dict[str, Any] | None:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT * FROM viewer_publications WHERE path_slug = ?",
                (path_slug,),
            ).fetchone()
        return dict(row) if row else None

    def create_viewer_credential(
        self,
        *,
        credential_id: str,
        publication_id: str,
        credential_type: str,
        algorithm: str,
        iterations: int,
        salt_hex: str,
        verifier_hex: str,
        version: int,
        now: int,
    ) -> None:
        with self._connect() as conn:
            conn.execute(
                """
                INSERT INTO viewer_credentials(
                    credential_id, publication_id, credential_type,
                    algorithm, iterations, salt_hex, verifier_hex,
                    version, created_at, revoked_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, NULL)
                """,
                (
                    credential_id,
                    publication_id,
                    credential_type,
                    algorithm,
                    int(iterations),
                    salt_hex,
                    verifier_hex,
                    int(version),
                    int(now),
                ),
            )

    def revoke_viewer_publication(self, publication_id: str, *, now: int) -> None:
        with self._connect() as conn:
            cur = conn.execute(
                """
                UPDATE viewer_publications
                SET state = 'revoked', revoked_at = COALESCE(revoked_at, ?),
                    config_revision = config_revision + 1
                WHERE publication_id = ?
                """,
                (int(now), publication_id),
            )
            if cur.rowcount != 1:
                raise ValueError("viewer publication not found")
            conn.execute(
                """
                UPDATE viewer_credentials
                SET revoked_at = COALESCE(revoked_at, ?)
                WHERE publication_id = ?
                """,
                (int(now), publication_id),
            )
            conn.execute(
                """
                UPDATE viewer_sessions
                SET revoked_at = COALESCE(revoked_at, ?)
                WHERE publication_id = ?
                """,
                (int(now), publication_id),
            )

    def rotate_viewer_credential(
        self,
        *,
        credential_id: str,
        publication_id: str,
        credential_type: str,
        algorithm: str,
        iterations: int,
        salt_hex: str,
        verifier_hex: str,
        now: int,
    ) -> int:
        with self._connect() as conn:
            publication = conn.execute(
                "SELECT 1 FROM viewer_publications WHERE publication_id = ?",
                (publication_id,),
            ).fetchone()
            if publication is None:
                raise ValueError("viewer publication not found")
            row = conn.execute(
                """
                SELECT MAX(version) AS max_version
                FROM viewer_credentials
                WHERE publication_id = ?
                """,
                (publication_id,),
            ).fetchone()
            version = int(row["max_version"] or 0) + 1
            conn.execute(
                """
                UPDATE viewer_credentials
                SET revoked_at = ?
                WHERE publication_id = ? AND revoked_at IS NULL
                """,
                (int(now), publication_id),
            )
            conn.execute(
                """
                UPDATE viewer_sessions
                SET revoked_at = ?
                WHERE publication_id = ? AND revoked_at IS NULL
                """,
                (int(now), publication_id),
            )
            conn.execute(
                """
                INSERT INTO viewer_credentials(
                    credential_id, publication_id, credential_type,
                    algorithm, iterations, salt_hex, verifier_hex,
                    version, created_at, revoked_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, NULL)
                """,
                (
                    credential_id,
                    publication_id,
                    credential_type,
                    algorithm,
                    int(iterations),
                    salt_hex,
                    verifier_hex,
                    version,
                    int(now),
                ),
            )
            conn.execute(
                """
                UPDATE viewer_publications
                SET config_revision = config_revision + 1
                WHERE publication_id = ?
                """,
                (publication_id,),
            )
        return version

    def get_active_viewer_credential(self, publication_id: str) -> dict[str, Any] | None:
        with self._connect() as conn:
            row = conn.execute(
                """
                SELECT * FROM viewer_credentials
                WHERE publication_id = ? AND revoked_at IS NULL
                ORDER BY version DESC
                LIMIT 1
                """,
                (publication_id,),
            ).fetchone()
        return dict(row) if row else None

    def create_viewer_session(
        self,
        *,
        session_id: str,
        publication_id: str,
        credential_version: int,
        token_digest: str,
        created_at: int,
        expires_at: int,
    ) -> None:
        with self._connect() as conn:
            conn.execute(
                """
                INSERT INTO viewer_sessions(
                    session_id, publication_id, credential_version,
                    token_digest, created_at, expires_at, revoked_at, last_seen_at
                ) VALUES (?, ?, ?, ?, ?, ?, NULL, ?)
                """,
                (
                    session_id,
                    publication_id,
                    int(credential_version),
                    token_digest,
                    int(created_at),
                    int(expires_at),
                    int(created_at),
                ),
            )

    def get_viewer_session_by_digest(self, token_digest: str) -> dict[str, Any] | None:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT * FROM viewer_sessions WHERE token_digest = ?",
                (token_digest,),
            ).fetchone()
        return dict(row) if row else None

    def get_meta(self, key: str) -> str | None:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT value FROM meta WHERE key = ?",
                (key,),
            ).fetchone()
        return str(row["value"]) if row else None

    def set_meta(self, key: str, value: str) -> None:
        with self._connect() as conn:
            conn.execute(
                """
                INSERT INTO meta(key, value) VALUES (?, ?)
                ON CONFLICT(key) DO UPDATE SET value = excluded.value
                """,
                (key, value),
            )

    def get_campaign(self, campaign_id: str) -> dict[str, Any] | None:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT * FROM campaigns WHERE campaign_id = ?",
                (campaign_id,),
            ).fetchone()
        return self._row_dict(row)

    def list_rooms(
        self,
        *,
        campaign_id: str | None = None,
        query: str = "",
        health: str = "",
    ) -> list[dict[str, Any]]:
        clauses = []
        params: list[Any] = []
        if campaign_id:
            clauses.append(
                "EXISTS (SELECT 1 FROM campaign_rooms cr "
                "WHERE cr.room_id = r.room_id AND cr.campaign_id = ? "
                "AND cr.removed_campaign_at IS NULL)"
            )
            params.append(campaign_id)
        if query:
            clauses.append("r.display_name LIKE ?")
            params.append(f"%{query}%")
        if health:
            clauses.append("r.health_status = ?")
            params.append(health)
        sql = "SELECT r.* FROM rooms r"
        if clauses:
            sql += " WHERE " + " AND ".join(clauses)
        sql += " ORDER BY r.display_name COLLATE NOCASE ASC, r.room_id ASC"
        with self._connect() as conn:
            rows = conn.execute(sql, params).fetchall()
        return [dict(row) for row in rows]

    def list_events(
        self,
        *,
        room_ids: list[str] | None = None,
        event_type: str | None = None,
        start_ts: int | None = None,
        end_ts: int | None = None,
        limit: int = 5000,
    ) -> list[dict[str, Any]]:
        clauses = []
        params: list[Any] = []
        if room_ids is not None:
            if not room_ids:
                return []
            placeholders = ",".join("?" for _ in room_ids)
            clauses.append(f"room_id IN ({placeholders})")
            params.extend(room_ids)
        if event_type:
            clauses.append("event_type = ?")
            params.append(event_type)
        if start_ts is not None:
            clauses.append("event_time >= ?")
            params.append(int(start_ts))
        if end_ts is not None:
            clauses.append("event_time <= ?")
            params.append(int(end_ts))
        sql = "SELECT * FROM events"
        if clauses:
            sql += " WHERE " + " AND ".join(clauses)
        sql += " ORDER BY event_time DESC, event_id ASC LIMIT ?"
        params.append(max(1, min(int(limit), 50_000)))
        with self._connect() as conn:
            rows = conn.execute(sql, params).fetchall()
        result = []
        for row in rows:
            item = dict(row)
            item["payload"] = json.loads(item.pop("payload_json"))
            item["protected"] = bool(item["protected"])
            result.append(item)
        return result

    def list_messages(
        self,
        *,
        room_id: str,
        sender_id: str = "",
        start_ts: int | None = None,
        end_ts: int | None = None,
        limit: int = 200,
    ) -> list[dict[str, Any]]:
        clauses = ["room_id = ?"]
        params: list[Any] = [room_id]
        if sender_id:
            clauses.append("sender_id = ?")
            params.append(sender_id)
        if start_ts is not None:
            clauses.append("event_time >= ?")
            params.append(int(start_ts))
        if end_ts is not None:
            clauses.append("event_time <= ?")
            params.append(int(end_ts))
        sql = (
            "SELECT * FROM messages WHERE "
            + " AND ".join(clauses)
            + " ORDER BY event_time DESC, message_id ASC LIMIT ?"
        )
        params.append(max(1, min(int(limit), 5000)))
        with self._connect() as conn:
            rows = conn.execute(sql, params).fetchall()
        result = []
        for row in rows:
            item = dict(row)
            item["payload"] = json.loads(item.pop("payload_json"))
            item["protected"] = bool(item["protected"])
            result.append(item)
        return result
