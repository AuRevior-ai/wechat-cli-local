"""Query-only invite trace reconstruction for the general CLI.

This module deliberately does not construct LaunchOpsStore. Archive access uses a
SQLite read-only URI so ordinary CLI queries cannot create or migrate archive
state.
"""

from __future__ import annotations

import csv
import io
import sqlite3
import time
from contextlib import closing
from dataclasses import dataclass, replace
from datetime import datetime
from pathlib import Path
from typing import Any

from .contacts import get_self_username
from .invite_stats import collect_group_invite_stats
from .messages import resolve_chat_context
from ..launch_ops.models import DEFAULT_STORE_PATH
from ..launch_ops.source import GroupSource, LocalWeChatSourceAdapter


@dataclass(frozen=True)
class InviteTraceRelation:
    room_id: str
    room_display_name: str
    invitee_key: str
    invitee_username: str
    invitee_name: str
    inviter_key: str
    inviter_username: str
    inviter_name: str
    membership_state: str
    episode_index: int | None
    method: str
    event_time: int | None
    resolution_state: str
    evidence_source: str
    query_source: str
    metadata_conflict: bool = False


class TraceArchiveReader:
    """Read trace archive v2 without permitting any archive mutation."""

    def __init__(self, path: str | Path = DEFAULT_STORE_PATH):
        self.path = Path(path)

    def _connect(self) -> sqlite3.Connection:
        uri = self.path.resolve().as_uri() + "?mode=ro"
        conn = sqlite3.connect(uri, uri=True, timeout=5)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA query_only = ON")
        return conn

    @staticmethod
    def _unavailable_status() -> dict[str, Any]:
        return {
            "archive_state": "unavailable",
            "projection_state": "pending",
            "global_migration_ready": False,
            "use_archive_current_projection": False,
        }

    def _projection_status_conn(
        self, conn: sqlite3.Connection, room_id: str
    ) -> dict[str, Any]:
        required = {
            "meta",
            "trace_managed_rooms",
            "trace_membership_observations",
            "trace_membership_episodes",
            "trace_invite_relations",
            "trace_people",
        }
        existing = {
            row["name"]
            for row in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            )
        }
        if not required.issubset(existing):
            return self._unavailable_status()
        global_row = conn.execute(
            "SELECT value FROM meta WHERE key = ?",
            ("trace_archive_migration_checkpoint_v2",),
        ).fetchone()
        global_ready = bool(global_row and global_row["value"] == "ready")
        managed = conn.execute(
            "SELECT * FROM trace_managed_rooms WHERE room_id = ?",
            (room_id,),
        ).fetchone()
        if managed is None:
            return {
                "archive_state": "unmanaged",
                "projection_state": "pending",
                "global_migration_ready": global_ready,
                "use_archive_current_projection": False,
            }
        observation = None
        if managed["current_observation_id"]:
            observation = conn.execute(
                "SELECT * FROM trace_membership_observations WHERE observation_id = ?",
                (managed["current_observation_id"],),
            ).fetchone()
        room_ready = bool(
            managed["archiving_enabled"]
            and managed["projection_state"] == "ready"
            and observation is not None
            and observation["source_kind"] == "contact"
            and observation["membership_status"] == "complete"
        )
        return {
            "archive_state": "managed",
            "archiving_enabled": bool(managed["archiving_enabled"]),
            "projection_state": managed["projection_state"],
            "global_migration_ready": global_ready,
            "use_archive_current_projection": bool(global_ready and room_ready),
            "current_observation_membership_status": (
                observation["membership_status"] if observation else None
            ),
            "current_observation_inviter_metadata_status": (
                observation["inviter_metadata_status"] if observation else None
            ),
        }

    def projection_status(self, room_id: str) -> dict[str, Any]:
        if not self.path.exists():
            return self._unavailable_status()
        try:
            with closing(self._connect()) as conn:
                return self._projection_status_conn(conn, room_id)
        except sqlite3.Error as exc:
            raise RuntimeError(f"archive read failed: {exc}") from exc

    def _read_relations_conn(
        self, conn: sqlite3.Connection, room_id: str
    ) -> list[InviteTraceRelation]:
        episode_rows = conn.execute(
            """
            SELECT e.episode_id, e.episode_index, e.status,
                   e.joined_event_at, e.left_event_at, e.inactive_observed_at,
                   r.relation_id, r.resolved_method, r.resolved_event_time,
                   r.resolution_state,
                   invitee.wechat_username AS invitee_username,
                   invitee.current_nick_name AS invitee_name,
                   invitee.current_remark AS invitee_remark,
                   inviter.wechat_username AS inviter_username,
                   inviter.current_nick_name AS inviter_name,
                   inviter.current_remark AS inviter_remark
            FROM trace_membership_episodes AS e
            JOIN trace_people AS invitee ON invitee.person_id = e.person_id
            LEFT JOIN trace_invite_relations AS r
              ON r.episode_id = e.episode_id AND r.active = 1
            LEFT JOIN trace_people AS inviter
              ON inviter.person_id = r.inviter_person_id
            WHERE e.room_id = ?
            ORDER BY invitee.wechat_username ASC, e.episode_index ASC
            """,
            (room_id,),
        ).fetchall()
        evidence_rows = conn.execute(
            """
            SELECT evidence_id, episode_id, source_type, observed_at,
                   event_time, authority_level, conflicts_with_resolution
            FROM trace_relation_evidence
            WHERE room_id = ? AND attachment_state = 'attached'
            ORDER BY episode_id ASC, authority_level DESC,
                     observed_at DESC, evidence_id ASC
            """,
            (room_id,),
        ).fetchall()
        best_evidence: dict[str, sqlite3.Row] = {}
        evidence_conflict: set[str] = set()
        for evidence in evidence_rows:
            episode_id = str(evidence["episode_id"] or "")
            if not episode_id:
                continue
            best_evidence.setdefault(episode_id, evidence)
            if evidence["conflicts_with_resolution"]:
                evidence_conflict.add(episode_id)
        conflict_rows = conn.execute(
            "SELECT episode_id FROM trace_conflicts "
            "WHERE room_id = ? AND resolved_at IS NULL",
            (room_id,),
        ).fetchall()
        open_conflicts = {
            str(row["episode_id"]) for row in conflict_rows if row["episode_id"]
        }
        result: list[InviteTraceRelation] = []
        for row in episode_rows:
            episode_id = str(row["episode_id"])
            evidence = best_evidence.get(episode_id)
            source_type = (
                str(evidence["source_type"])
                if evidence is not None
                else "trace_archive"
            )
            resolved_method = str(row["resolved_method"] or "")
            method = (
                resolved_method
                if resolved_method
                else "metadata"
                if source_type == "ext_buffer"
                else "unknown"
            )
            event_time = row["resolved_event_time"]
            if event_time is None and evidence is not None:
                event_time = evidence["event_time"]
            invitee_username = str(row["invitee_username"] or "")
            inviter_username = str(row["inviter_username"] or "")
            result.append(
                InviteTraceRelation(
                    room_id=room_id,
                    room_display_name="",
                    invitee_key=(
                        f"user:{invitee_username}"
                        if invitee_username
                        else f"episode:{episode_id}"
                    ),
                    invitee_username=invitee_username,
                    invitee_name=str(
                        row["invitee_remark"] or row["invitee_name"] or ""
                    ),
                    inviter_key=(
                        f"user:{inviter_username}" if inviter_username else ""
                    ),
                    inviter_username=inviter_username,
                    inviter_name=str(
                        row["inviter_remark"] or row["inviter_name"] or ""
                    ),
                    membership_state=(
                        "current" if row["status"] == "active"
                        else "departed" if row["status"] == "inactive"
                        else "unknown"
                    ),
                    episode_index=int(row["episode_index"]),
                    method=method,
                    event_time=int(event_time) if event_time is not None else None,
                    resolution_state=str(row["resolution_state"] or "unresolved"),
                    evidence_source=source_type,
                    query_source="archive",
                    metadata_conflict=(
                        episode_id in evidence_conflict or episode_id in open_conflicts
                    ),
                )
            )
        return result

    def read_relations(self, room_id: str) -> list[InviteTraceRelation]:
        try:
            with closing(self._connect()) as conn:
                return self._read_relations_conn(conn, room_id)
        except sqlite3.Error as exc:
            raise RuntimeError(f"archive read failed: {exc}") from exc

    def read_projection_snapshot(
        self, room_id: str
    ) -> tuple[dict[str, Any], list[InviteTraceRelation]]:
        if not self.path.exists():
            return self._unavailable_status(), []
        try:
            with closing(self._connect()) as conn:
                conn.execute("BEGIN")
                status = self._projection_status_conn(conn, room_id)
                if not status["use_archive_current_projection"]:
                    return status, []
                return status, self._read_relations_conn(conn, room_id)
        except sqlite3.Error as exc:
            raise RuntimeError(f"archive read failed: {exc}") from exc


def merge_live_relations(
    room_id: str,
    room_display_name: str,
    members: list[dict[str, Any]],
    legacy_result: dict[str, Any],
) -> list[InviteTraceRelation]:
    current_by_username = {
        str(member.get("username") or "").strip(): member
        for member in members
        if str(member.get("username") or "").strip()
    }
    latest_event_by_invitee: dict[str, dict[str, Any]] = {}
    for event in legacy_result.get("events", []):
        invitee = str(event.get("invitee_username") or "").strip()
        if not invitee:
            continue
        previous = latest_event_by_invitee.get(invitee)
        marker = (
            int(event.get("timestamp") or 0),
            str(event.get("event_id") or ""),
        )
        previous_marker = (
            (
                int(previous.get("timestamp") or 0),
                str(previous.get("event_id") or ""),
            )
            if previous
            else (-1, "")
        )
        if marker >= previous_marker:
            latest_event_by_invitee[invitee] = event

    rows: list[InviteTraceRelation] = []
    all_invitees = set(current_by_username) | set(latest_event_by_invitee)
    for invitee in sorted(all_invitees):
        member = current_by_username.get(invitee)
        event = latest_event_by_invitee.get(invitee)
        metadata_inviter = str(
            (member or {}).get("inviter_username") or ""
        ).strip()
        if event is not None:
            inviter = str(event.get("inviter_username") or "").strip()
            rows.append(
                InviteTraceRelation(
                    room_id=room_id,
                    room_display_name=room_display_name,
                    invitee_key=f"user:{invitee}",
                    invitee_username=invitee,
                    invitee_name=str(
                        event.get("invitee_name_raw")
                        or (member or {}).get("display_name")
                        or invitee
                    ),
                    inviter_key=str(
                        event.get("inviter_key")
                        or (f"user:{inviter}" if inviter else "")
                    ),
                    inviter_username=inviter,
                    inviter_name=str(
                        event.get("inviter_name_raw")
                        or inviter
                        or "待确认"
                    ),
                    membership_state=(
                        "current"
                        if member is not None
                        else "departed"
                        if legacy_result.get("_contact_available", True)
                        else "unknown"
                    ),
                    episode_index=None,
                    method=str(event.get("method") or "unknown"),
                    event_time=int(event.get("timestamp") or 0) or None,
                    resolution_state=(
                        "resolved" if inviter else "unresolved"
                    ),
                    evidence_source="system_message",
                    query_source="live",
                    metadata_conflict=bool(
                        metadata_inviter
                        and inviter
                        and metadata_inviter != inviter
                    ),
                )
            )
            continue

        if not metadata_inviter or member is None:
            continue
        inviter_member = current_by_username.get(metadata_inviter, {})
        rows.append(
            InviteTraceRelation(
                room_id=room_id,
                room_display_name=room_display_name,
                invitee_key=f"user:{invitee}",
                invitee_username=invitee,
                invitee_name=str(member.get("display_name") or invitee),
                inviter_key=f"user:{metadata_inviter}",
                inviter_username=metadata_inviter,
                inviter_name=str(
                    inviter_member.get("display_name") or metadata_inviter
                ),
                membership_state="current",
                episode_index=None,
                method="metadata",
                event_time=None,
                resolution_state="resolved",
                evidence_source="ext_buffer",
                query_source="live",
            )
        )
    return rows


def read_live_membership(
    ctx,
    chat_ctx: dict[str, Any],
    *,
    now: int,
):
    """Read fresh Contact/ext_buffer membership without scanning messages."""
    source = LocalWeChatSourceAdapter(ctx)
    group = GroupSource(
        room_id=chat_ctx["username"],
        display_name=chat_ctx["display_name"],
    )
    observation = source.membership_observation(
        group,
        observed_at=now,
        force=True,
    )
    return observation, list(observation.members)


def read_message_history(
    ctx,
    chat_ctx: dict[str, Any],
    members: list[dict[str, Any]],
    bindings: dict[str, str],
) -> dict[str, Any]:
    live_chat_ctx = dict(chat_ctx)
    try:
        live_chat_ctx["self_username"] = get_self_username(
            getattr(ctx, "db_dir", ""),
            ctx.cache,
            ctx.decrypted_dir,
        )
    except Exception:
        live_chat_ctx["self_username"] = ""
    return collect_group_invite_stats(
        live_chat_ctx,
        members,
        bindings,
    )


def read_live_inputs(
    ctx,
    chat_ctx: dict[str, Any],
    bindings: dict[str, str],
    *,
    now: int,
):
    """Collect Contact and message history independently; fail only if both fail."""
    observation = None
    members: list[dict[str, Any]] = []
    warnings: list[str] = []
    contact_error: Exception | None = None
    message_error: Exception | None = None
    try:
        observation, members = read_live_membership(
            ctx,
            chat_ctx,
            now=now,
        )
    except Exception as exc:
        contact_error = exc
        warnings.append(f"contact source failed: {exc}")

    try:
        legacy = read_message_history(ctx, chat_ctx, members, bindings)
    except Exception as exc:
        message_error = exc
        warnings.append(f"message history failed: {exc}")
        legacy = {
            "events": [],
            "unattributed_events": [],
            "unparsed_messages": [],
            "failures": [f"message history failed: {exc}"],
            "scope": {},
            "summary": {},
        }

    if contact_error is not None and message_error is not None:
        raise RuntimeError(
            f"contact source failed: {contact_error}; message history failed: {message_error}"
        )
    legacy = dict(legacy)
    legacy["_contact_available"] = contact_error is None
    legacy["warnings"] = [*legacy.get("warnings", []), *warnings]
    return observation, members, legacy


def format_timestamp(value: int | None) -> str:
    if value is None:
        return "时间未知"
    return datetime.fromtimestamp(int(value)).strftime("%Y-%m-%d %H:%M:%S")


def public_invitee_row(row: InviteTraceRelation) -> dict[str, Any]:
    return {
        "name": row.invitee_name,
        "username": row.invitee_username,
        "time": format_timestamp(row.event_time),
        "method": row.method,
        "membership_state": row.membership_state,
    }


def _relation_from_legacy_event(
    room_id: str,
    room_display_name: str,
    event: dict[str, Any],
    current_usernames: set[str],
    *,
    membership_available: bool,
) -> InviteTraceRelation:
    invitee_username = str(event.get("invitee_username") or "").strip()
    invitee_key = str(event.get("invitee_key") or "")
    inviter_username = str(event.get("inviter_username") or "").strip()
    inviter_key = str(event.get("inviter_key") or "")
    return InviteTraceRelation(
        room_id=room_id,
        room_display_name=room_display_name,
        invitee_key=(
            invitee_key
            or (
                f"user:{invitee_username}"
                if invitee_username
                else f"name:{event.get('invitee_name_raw', '')}"
            )
        ),
        invitee_username=invitee_username,
        invitee_name=str(
            event.get("invitee_name_raw")
            or invitee_username
            or "未知成员"
        ),
        inviter_key=(
            inviter_key
            or (f"user:{inviter_username}" if inviter_username else "")
        ),
        inviter_username=inviter_username,
        inviter_name=str(
            event.get("inviter_name_raw")
            or inviter_username
            or "待确认"
        ),
        membership_state=(
            "current"
            if invitee_username and invitee_username in current_usernames
            else "departed"
            if invitee_username and membership_available
            else "unknown"
        ),
        episode_index=None,
        method=str(event.get("method") or "unknown"),
        event_time=int(event.get("timestamp") or 0) or None,
        resolution_state=(
            "resolved"
            if event.get("inviter_identity_status") == "resolved"
            and event.get("invitee_identity_status") == "resolved"
            else "unresolved"
        ),
        evidence_source="system_message",
        query_source="live",
    )


def build_live_trace_relations(
    room_id: str,
    room_display_name: str,
    members: list[dict[str, Any]],
    legacy_result: dict[str, Any],
    ranking_relations: list[InviteTraceRelation],
) -> list[InviteTraceRelation]:
    current_usernames = {
        str(member.get("username") or "").strip()
        for member in members
        if str(member.get("username") or "").strip()
    }
    trace = [
        _relation_from_legacy_event(
            room_id,
            room_display_name,
            event,
            current_usernames,
            membership_available=bool(
                legacy_result.get("_contact_available", True)
            ),
        )
        for event in legacy_result.get("events", [])
    ]
    explicit_invitees = {row.invitee_key for row in trace if row.invitee_key}
    for row in ranking_relations:
        if (
            row.evidence_source == "ext_buffer"
            and row.invitee_key not in explicit_invitees
        ):
            trace.append(row)
        elif row.metadata_conflict:
            for index, existing in enumerate(trace):
                if existing.invitee_key == row.invitee_key:
                    trace[index] = InviteTraceRelation(
                        **{
                            **existing.__dict__,
                            "metadata_conflict": True,
                        }
                    )
    trace.sort(
        key=lambda row: (
            row.event_time is None,
            row.event_time or 0,
            row.invitee_key,
            row.inviter_key,
        )
    )
    return trace


def filter_trace_relations(
    rows: list[InviteTraceRelation],
    *,
    start_ts: int | None,
    end_ts: int | None,
) -> tuple[list[InviteTraceRelation], int]:
    if start_ts is None and end_ts is None:
        return list(rows), 0
    kept: list[InviteTraceRelation] = []
    excluded_unknown = 0
    for row in rows:
        if row.event_time is None:
            excluded_unknown += 1
            continue
        if start_ts is not None and row.event_time < start_ts:
            continue
        if end_ts is not None and row.event_time > end_ts:
            continue
        kept.append(row)
    return kept, excluded_unknown


def _public_trace_row(row: InviteTraceRelation) -> dict[str, Any]:
    return {
        "episode_index": row.episode_index,
        "inviter_name": row.inviter_name,
        "inviter_username": row.inviter_username,
        "invitee_name": row.invitee_name,
        "invitee_username": row.invitee_username,
        "membership_state": row.membership_state,
        "method": row.method,
        "event_time": row.event_time,
        "time": format_timestamp(row.event_time),
        "resolution_state": row.resolution_state,
        "evidence_source": row.evidence_source,
        "query_source": row.query_source,
        "metadata_conflict": row.metadata_conflict,
    }


def _public_event_row(row: InviteTraceRelation) -> dict[str, Any]:
    return {
        "event_id": (
            f"{row.query_source}:{row.invitee_key}:"
            f"{row.event_time or 'unknown'}:{row.inviter_key}"
        ),
        "timestamp": row.event_time,
        "time": format_timestamp(row.event_time),
        "method": row.method,
        "inviter_name_raw": row.inviter_name,
        "inviter_username": row.inviter_username,
        "inviter_key": row.inviter_key,
        "inviter_identity_status": (
            "resolved" if row.inviter_username else "unresolved"
        ),
        "inviter_identity_source": row.evidence_source,
        "invitee_name_raw": row.invitee_name,
        "invitee_username": row.invitee_username,
        "invitee_key": row.invitee_key,
        "invitee_identity_status": (
            "resolved" if row.invitee_username else "unresolved"
        ),
        "invitee_identity_source": row.evidence_source,
        "raw_text": (
            "Contact/ext_buffer metadata"
            if row.method == "metadata"
            else ""
        ),
        "membership_state": row.membership_state,
        "evidence_source": row.evidence_source,
        "metadata_conflict": row.metadata_conflict,
    }


def annotate_archive_metadata_conflicts(
    rows: list[InviteTraceRelation],
    members: list[dict[str, Any]],
) -> tuple[list[InviteTraceRelation], list[str]]:
    fresh_by_invitee = {
        str(member.get("username") or "").strip(): str(
            member.get("inviter_username") or ""
        ).strip()
        for member in members
        if str(member.get("username") or "").strip()
    }
    warnings: list[str] = []
    annotated: list[InviteTraceRelation] = []
    for row in rows:
        fresh_inviter = fresh_by_invitee.get(row.invitee_username, "")
        if (
            row.membership_state == "current"
            and fresh_inviter
            and row.inviter_username
            and fresh_inviter != row.inviter_username
        ):
            annotated.append(replace(row, metadata_conflict=True))
            warnings.append(
                "metadata conflict for "
                f"{row.invitee_username}: archive={row.inviter_username}, "
                f"fresh={fresh_inviter}"
            )
        else:
            annotated.append(row)
    return annotated, warnings


def aggregate_invite_stats(
    relations_for_ranking: list[InviteTraceRelation],
    trace_relations: list[InviteTraceRelation],
) -> list[dict[str, Any]]:
    by_invitee: dict[str, InviteTraceRelation] = {}
    for row in relations_for_ranking:
        previous = by_invitee.get(row.invitee_key)
        marker = (
            row.episode_index if row.episode_index is not None else -1,
            row.event_time if row.event_time is not None else -1,
            1 if row.membership_state == "current" else 0,
        )
        previous_marker = (
            previous.episode_index
            if previous and previous.episode_index is not None
            else -1,
            previous.event_time
            if previous and previous.event_time is not None
            else -1,
            1 if previous and previous.membership_state == "current" else 0,
        )
        if previous is None or marker >= previous_marker:
            by_invitee[row.invitee_key] = row

    owned: dict[str, list[InviteTraceRelation]] = {}
    for row in by_invitee.values():
        if row.inviter_key:
            owned.setdefault(row.inviter_key, []).append(row)

    trace_counts: dict[str, int] = {}
    for row in trace_relations:
        if row.inviter_key:
            trace_counts[row.inviter_key] = (
                trace_counts.get(row.inviter_key, 0) + 1
            )

    ranking: list[dict[str, Any]] = []
    for inviter_key, rows in owned.items():
        rows.sort(
            key=lambda row: (
                row.event_time is None,
                row.event_time or 0,
                row.invitee_key,
            )
        )
        sample = rows[0]
        known_times = [
            row.event_time for row in rows if row.event_time is not None
        ]
        ranking.append(
            {
                "rank": 0,
                "inviter_key": inviter_key,
                "inviter_username": sample.inviter_username,
                "inviter_name": sample.inviter_name,
                "historical_names": sorted(
                    {row.inviter_name for row in rows if row.inviter_name}
                ),
                "identity_status": (
                    "resolved" if sample.inviter_username else "unresolved"
                ),
                "identity_source": sample.evidence_source,
                "unique_invitee_count": len(rows),
                "historical_unique_invitee_count": len(rows),
                "current_invitee_count": sum(
                    row.membership_state == "current" for row in rows
                ),
                "departed_invitee_count": sum(
                    row.membership_state == "departed" for row in rows
                ),
                "unknown_membership_count": sum(
                    row.membership_state == "unknown" for row in rows
                ),
                "unknown_time_count": sum(
                    row.event_time is None for row in rows
                ),
                "event_count": trace_counts.get(inviter_key, 0),
                "episode_count": trace_counts.get(inviter_key, 0),
                "direct_count": sum(row.method == "direct" for row in rows),
                "qr_count": sum(row.method == "qr" for row in rows),
                "metadata_only_count": sum(
                    row.method == "metadata" for row in rows
                ),
                "first_invite_time": (
                    format_timestamp(min(known_times))
                    if known_times
                    else "时间未知"
                ),
                "last_invite_time": (
                    format_timestamp(max(known_times))
                    if known_times
                    else "时间未知"
                ),
                "invitees": [public_invitee_row(row) for row in rows],
            }
        )
    ranking.sort(
        key=lambda item: (
            -item["historical_unique_invitee_count"],
            item["inviter_name"],
            item["inviter_key"],
        )
    )
    for index, item in enumerate(ranking, 1):
        item["rank"] = index
    return ranking


def build_invite_trace_result(
    *,
    chat_ctx: dict[str, Any],
    query_source: str,
    completeness: str,
    archive_status: dict[str, Any],
    ranking: list[dict[str, Any]],
    trace_relations: list[InviteTraceRelation],
    legacy_result: dict[str, Any],
    warnings: list[str],
    excluded_unknown_time_count: int,
    start_ts: int | None,
    end_ts: int | None,
) -> dict[str, Any]:
    events = [_public_event_row(row) for row in trace_relations]
    unique_invitees = {
        invitee["username"] or invitee["name"]
        for item in ranking
        for invitee in item.get("invitees", [])
    }
    legacy_summary = legacy_result.get("summary", {})
    return {
        "chat": chat_ctx.get("display_name", ""),
        "username": chat_ctx.get("username", ""),
        "query_source": query_source,
        "archive_status": archive_status,
        "completeness": completeness,
        "scope": {
            "start_timestamp": start_ts,
            "end_timestamp": end_ts,
            "first_visible_system_time": legacy_result.get("scope", {}).get(
                "first_visible_system_time"
            ),
            "last_visible_system_time": legacy_result.get("scope", {}).get(
                "last_visible_system_time"
            ),
        },
        "summary": {
            "system_message_count": int(
                legacy_summary.get("system_message_count", 0)
            ),
            "invite_event_count": len(events),
            "attributed_event_count": sum(
                bool(row["inviter_username"]) for row in events
            ),
            "unique_invitee_count": len(unique_invitees),
            "unattributed_count": len(
                legacy_result.get("unattributed_events", [])
            ),
            "unresolved_identity_count": sum(
                row["resolution_state"] != "resolved"
                for row in map(_public_trace_row, trace_relations)
            ),
            "unparsed_count": len(
                legacy_result.get("unparsed_messages", [])
            ),
        },
        "ranking": ranking,
        "events": events,
        "trace": [_public_trace_row(row) for row in trace_relations],
        "unattributed_events": list(
            legacy_result.get("unattributed_events", [])
        ),
        "unparsed_messages": list(
            legacy_result.get("unparsed_messages", [])
        ),
        "failures": list(legacy_result.get("failures", [])),
        "warnings": list(warnings),
        "excluded_unknown_time_count": excluded_unknown_time_count,
    }


class InviteTraceQueryError(RuntimeError):
    pass


class InviteTraceQueryService:
    def __init__(
        self,
        ctx,
        *,
        archive_path: str | Path = DEFAULT_STORE_PATH,
        archive_reader: TraceArchiveReader | None = None,
    ):
        self.ctx = ctx
        self.archive_reader = archive_reader or TraceArchiveReader(archive_path)

    def _read_live(
        self,
        chat_ctx: dict[str, Any],
        bindings: dict[str, str],
        *,
        now: int,
    ):
        try:
            return read_live_inputs(
                self.ctx,
                chat_ctx,
                bindings,
                now=now,
            )
        except Exception as exc:
            raise InviteTraceQueryError(
                f"无法读取群邀请数据: {exc}"
            ) from exc

    def query(
        self,
        group_name: str,
        *,
        bindings: dict[str, str],
        start_ts: int | None,
        end_ts: int | None,
        now: int | None = None,
    ) -> dict[str, Any]:
        chat_ctx = resolve_chat_context(
            group_name,
            self.ctx.msg_db_keys,
            self.ctx.cache,
            self.ctx.decrypted_dir,
        )
        if not chat_ctx:
            raise InviteTraceQueryError(f"找不到聊天对象: {group_name}")
        if not chat_ctx.get("is_group"):
            raise InviteTraceQueryError(f"{group_name} 不是一个群聊")

        room_id = str(chat_ctx["username"])
        display_name = str(chat_ctx.get("display_name") or group_name)
        warnings: list[str] = []
        archive_probe_failed = False
        archive_relations_snapshot: list[InviteTraceRelation] = []
        try:
            archive_status, archive_relations_snapshot = (
                self.archive_reader.read_projection_snapshot(room_id)
            )
        except RuntimeError as exc:
            archive_probe_failed = True
            warnings.append(str(exc))
            archive_status = {
                "archive_state": "error",
                "projection_state": "pending",
                "global_migration_ready": False,
                "use_archive_current_projection": False,
            }

        legacy_result: dict[str, Any] = {
            "events": [],
            "unattributed_events": [],
            "unparsed_messages": [],
            "failures": [],
            "scope": {},
            "summary": {},
        }
        trace_relations: list[InviteTraceRelation]
        relations_for_ranking: list[InviteTraceRelation]
        query_source = "live"
        completeness = "live_complete_for_available_local_sources"
        observed_at = int(time.time() if now is None else now)

        if archive_status.get("use_archive_current_projection"):
            try:
                trace_relations = list(archive_relations_snapshot)
                relations_for_ranking = list(trace_relations)
                query_source = "archive"
                completeness = "archive_complete"
                try:
                    _, fresh_members = read_live_membership(
                        self.ctx,
                        chat_ctx,
                        now=observed_at,
                    )
                    trace_relations, conflict_warnings = (
                        annotate_archive_metadata_conflicts(
                            trace_relations, fresh_members
                        )
                    )
                    relations_for_ranking = list(trace_relations)
                    warnings.extend(conflict_warnings)
                except Exception as exc:
                    warnings.append(f"live contact supplement failed: {exc}")
            except RuntimeError as exc:
                warnings.append(str(exc))
                observation, members, legacy_result = self._read_live(
                    chat_ctx,
                    bindings,
                    now=observed_at,
                )
                relations_for_ranking = merge_live_relations(
                    room_id,
                    display_name,
                    members,
                    legacy_result,
                )
                trace_relations = build_live_trace_relations(
                    room_id,
                    display_name,
                    members,
                    legacy_result,
                    relations_for_ranking,
                )
                completeness = "degraded"
        else:
            observation, members, legacy_result = self._read_live(
                chat_ctx,
                bindings,
                now=observed_at,
            )
            relations_for_ranking = merge_live_relations(
                room_id,
                display_name,
                members,
                legacy_result,
            )
            trace_relations = build_live_trace_relations(
                room_id,
                display_name,
                members,
                legacy_result,
                relations_for_ranking,
            )
            warnings.extend(legacy_result.get("warnings", []))
            if observation is None:
                completeness = "degraded"
            elif (
                observation.membership_status != "complete"
                or observation.inviter_metadata_status != "complete"
            ):
                completeness = "partial"
            if legacy_result.get("failures") or archive_probe_failed:
                completeness = "degraded"

        filtered_trace, excluded_unknown = filter_trace_relations(
            trace_relations,
            start_ts=start_ts,
            end_ts=end_ts,
        )
        filtered_ranking, _ = filter_trace_relations(
            relations_for_ranking,
            start_ts=start_ts,
            end_ts=end_ts,
        )
        ranking = aggregate_invite_stats(filtered_ranking, filtered_trace)
        return build_invite_trace_result(
            chat_ctx=chat_ctx,
            query_source=query_source,
            completeness=completeness,
            archive_status=archive_status,
            ranking=ranking,
            trace_relations=filtered_trace,
            legacy_result=legacy_result,
            warnings=warnings,
            excluded_unknown_time_count=excluded_unknown,
            start_ts=start_ts,
            end_ts=end_ts,
        )


def format_invite_trace_text(result: dict[str, Any]) -> str:
    lines = [
        f'{result.get("chat", "")} 邀请溯源',
        (
            f'数据源: {result.get("query_source", "unknown")}；'
            f'完整度: {result.get("completeness", "unknown")}'
        ),
        "",
    ]
    for row in result.get("trace", []):
        episode = row.get("episode_index")
        prefix = f"#{episode}" if episode is not None else "#-"
        flags = []
        if row.get("metadata_conflict"):
            flags.append("conflict")
        if row.get("resolution_state") != "resolved":
            flags.append("unresolved")
        suffix = (
            " " + " ".join(f"[{flag}]" for flag in flags)
            if flags
            else ""
        )
        lines.append(
            f'{prefix} {row.get("inviter_name") or "待确认"} -> '
            f'{row.get("invitee_name") or "未知成员"} '
            f'{row.get("membership_state", "unknown")} '
            f'{row.get("time", "时间未知")} '
            f'{row.get("method", "unknown")} '
            f'{row.get("evidence_source", "unknown")}{suffix}'
        )
    for warning in result.get("warnings", []):
        lines.append(f"警告: {warning}")
    return "\n".join(lines)


def format_invite_trace_csv(result: dict[str, Any]) -> str:
    stream = io.StringIO(newline="")
    stream.write("\ufeff")
    writer = csv.writer(stream)
    writer.writerow([
        "群聊", "群聊ID", "Episode", "邀请人", "邀请人账号",
        "被邀请人", "被邀请人账号", "成员状态", "入群时间",
        "邀请方式", "解析状态", "证据来源", "查询来源", "冲突",
    ])
    for row in result.get("trace", []):
        writer.writerow([
            result.get("chat", ""),
            result.get("username", ""),
            (
                row.get("episode_index")
                if row.get("episode_index") is not None
                else ""
            ),
            row.get("inviter_name", ""),
            row.get("inviter_username", ""),
            row.get("invitee_name", ""),
            row.get("invitee_username", ""),
            row.get("membership_state", "unknown"),
            row.get("time", "时间未知"),
            row.get("method", "unknown"),
            row.get("resolution_state", "unresolved"),
            row.get("evidence_source", "unknown"),
            result.get("query_source", ""),
            "yes" if row.get("metadata_conflict") else "",
        ])
    return stream.getvalue()
