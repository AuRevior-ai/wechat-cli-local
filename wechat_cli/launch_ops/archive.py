"""Durable invite-trace archive reconciliation and projections."""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class ResolvedInvite:
    inviter_person_id: str | None
    method: str | None
    event_time: int | None
    resolution_state: str
    winning_evidence_ids: tuple[str, ...]
    conflict_fingerprints: tuple[str, ...]


def _stable_sha(*parts: object) -> str:
    payload = "\0".join(str(part) for part in parts)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


class ArchiveReconciler:
    def __init__(self, store):
        self.store = store

    def reconcile_room(
        self,
        room_id: str,
        *,
        trigger: str,
        now: int,
        observation_id: str | None = None,
    ) -> dict[str, Any]:
        with self.store._connect() as conn:
            return self._reconcile_room_conn(
                conn,
                room_id,
                trigger=trigger,
                now=int(now),
                observation_id=observation_id,
            )

    def _basis_for_reconcile_conn(
        self,
        conn,
        room_id: str,
        *,
        trigger: str,
        observation_id: str | None,
    ) -> dict[str, Any] | None:
        managed = self.store._get_trace_managed_room_conn(conn, room_id)
        if trigger == "membership" and observation_id is None:
            raise ValueError("membership reconciliation requires observation_id")
        if observation_id is not None:
            observation = self.store._get_trace_membership_observation_conn(
                conn, observation_id
            )
            if observation is None or observation["room_id"] != room_id:
                raise ValueError("trace observation not found for room")
            if trigger == "migration":
                if observation["source_kind"] not in {"contact", "legacy_snapshot"}:
                    raise ValueError("invalid migration observation source")
            elif observation["source_kind"] != "contact":
                raise ValueError("membership basis must be a Contact observation")
            return observation
        if trigger == "migration":
            return None
        basis_id = managed["current_observation_id"] if managed else None
        if basis_id is None or managed["projection_state"] == "refresh_error":
            return None
        observation = self.store._get_trace_membership_observation_conn(conn, basis_id)
        if (
            observation is None
            or observation["room_id"] != room_id
            or observation["source_kind"] != "contact"
            or observation["membership_status"] != "complete"
        ):
            return None
        return observation

    def _ingest_metadata_evidence_conn(
        self,
        conn,
        episode: dict[str, Any],
        observation: dict[str, Any],
        member: dict[str, Any],
        *,
        now: int,
    ) -> list[str]:
        invitee_username = str(member.get("username") or "").strip()
        inviter_username = str(member.get("inviter_username") or "").strip()
        if not invitee_username or not inviter_username or inviter_username == invitee_username:
            return []
        inviter = self.store._ensure_trace_person_conn(
            conn, inviter_username, now=now
        )
        invitee_person_id = episode["person_id"]
        evidence_id = _stable_sha(
            "ext_buffer",
            observation["observation_id"],
            invitee_username,
            inviter_username,
        )
        row, _ = self.store._insert_trace_relation_evidence_conn(
            conn,
            enqueue_reconcile=False,
            evidence_id=evidence_id,
            room_id=episode["room_id"],
            episode_id=episode["episode_id"],
            relation_id=None,
            attachment_state="attached",
            attachment_reason="current_membership_observation",
            source_type="ext_buffer",
            source_identity=observation["observation_id"],
            evidence_subject_key=invitee_username,
            observed_at=int(observation["last_observed_at"]),
            inviter_person_id=inviter["person_id"],
            invitee_person_id=invitee_person_id,
            event_time=None,
            method=None,
            raw_reference=None,
            authority_level=50,
        )
        return [row["evidence_id"]] if row else []

    def _ingest_durable_system_evidence_conn(
        self, conn, room_id: str, *, now: int
    ) -> list[str]:
        rows = conn.execute(
            """
            SELECT * FROM events
            WHERE room_id = ? AND event_type IN ('member.invited', 'member.joined')
            ORDER BY event_time ASC, event_id ASC
            """,
            (room_id,),
        ).fetchall()
        inserted_ids: list[str] = []
        for raw in rows:
            event = dict(raw)
            source_identity = str(event["event_id"])
            exists = conn.execute(
                """
                SELECT evidence_id FROM trace_relation_evidence
                WHERE source_type = 'system_message' AND source_identity = ?
                LIMIT 1
                """,
                (source_identity,),
            ).fetchone()
            if exists is not None:
                continue
            invitee_username = str(event.get("subject_id") or "").strip()
            inviter_username = str(event.get("actor_id") or "").strip()
            payload = json.loads(event.get("payload_json") or "{}")
            method = payload.get("method")
            if method not in {None, "direct", "qr", "unattributed_qr"}:
                method = None
            invitee_person_id = None
            inviter_person_id = None
            if invitee_username:
                invitee_person_id = self.store._ensure_trace_person_conn(
                    conn, invitee_username, now=now
                )["person_id"]
            if inviter_username and inviter_username != invitee_username:
                inviter_person_id = self.store._ensure_trace_person_conn(
                    conn, inviter_username, now=now
                )["person_id"]
            evidence_id = _stable_sha("system_message", source_identity, invitee_username)
            row, inserted = self.store._insert_trace_relation_evidence_conn(
                conn,
                enqueue_reconcile=False,
                evidence_id=evidence_id,
                room_id=room_id,
                episode_id=None,
                relation_id=None,
                attachment_state="unattached",
                attachment_reason="",
                source_type="system_message",
                source_identity=source_identity,
                evidence_subject_key=invitee_username or f"event:{source_identity}",
                observed_at=int(event["created_at"]),
                inviter_person_id=inviter_person_id,
                invitee_person_id=invitee_person_id,
                event_time=int(event["event_time"]),
                method=method,
                raw_reference=source_identity,
                authority_level=80,
            )
            if inserted and row:
                inserted_ids.append(row["evidence_id"])
        return inserted_ids

    @staticmethod
    def _attachment_basis_is_reliable(
        evidence: dict[str, Any], episode: dict[str, Any]
    ) -> bool:
        if not evidence.get("invitee_person_id"):
            return False
        if evidence.get("episode_id") == episode.get("episode_id"):
            return True
        event_time = evidence.get("event_time")
        if event_time is not None:
            start = int(episode["first_observed_at"])
            end = episode.get("inactive_observed_at")
            if int(event_time) >= start and (end is None or int(event_time) <= int(end)):
                return True
        return True

    def _attach_evidence_to_episode_conn(
        self, conn, evidence_id: str, *, now: int
    ) -> dict[str, Any]:
        row = conn.execute(
            "SELECT * FROM trace_relation_evidence WHERE evidence_id = ?",
            (evidence_id,),
        ).fetchone()
        if row is None:
            raise ValueError("trace evidence not found")
        evidence = dict(row)
        if evidence["attachment_state"] == "attached":
            return evidence
        candidates = self.store._candidate_trace_episodes_for_evidence_conn(
            conn, evidence
        )
        if len(candidates) == 1 and self._attachment_basis_is_reliable(
            evidence, candidates[0]
        ):
            return self.store._attach_trace_evidence_conn(
                conn,
                evidence["evidence_id"],
                candidates[0]["episode_id"],
                reason="reliable_match",
            )
        if len(candidates) > 1:
            return self.store._mark_trace_evidence_attachment_conn(
                conn,
                evidence["evidence_id"],
                state="ambiguous",
                reason="multiple_episode_candidates",
            )
        return self.store._mark_trace_evidence_attachment_conn(
            conn,
            evidence["evidence_id"],
            state="unattached",
            reason="no_reliable_episode_match",
        )

    def _attach_pending_room_evidence_conn(
        self, conn, room_id: str, *, now: int
    ) -> set[str]:
        rows = conn.execute(
            """
            SELECT evidence_id FROM trace_relation_evidence
            WHERE room_id = ? AND attachment_state <> 'attached'
            ORDER BY observed_at ASC, evidence_id ASC
            """,
            (room_id,),
        ).fetchall()
        affected: set[str] = set()
        for row in rows:
            attached = self._attach_evidence_to_episode_conn(
                conn, row["evidence_id"], now=now
            )
            if attached.get("attachment_state") == "attached" and attached.get("episode_id"):
                affected.add(attached["episode_id"])
        return affected

    def _resolve_inviter_field(
        self,
        evidence: list[dict[str, Any]],
        *,
        current_inviter: str | None,
    ) -> tuple[str | None, list[str], list[str]]:
        manual = [row for row in evidence if row["source_type"] == "manual_confirmation"]
        system = [
            row
            for row in evidence
            if row["source_type"] == "system_message" and row.get("inviter_person_id")
        ]
        metadata = [
            row
            for row in evidence
            if row["source_type"] == "ext_buffer" and row.get("inviter_person_id")
        ]
        csv_rows = [
            row
            for row in evidence
            if row["source_type"] == "csv_import" and row.get("inviter_person_id")
        ]

        winner: str | None = None
        winners: list[str] = []
        source_level = "unresolved"
        conflict = False

        if manual:
            latest = max(manual, key=lambda row: (int(row["observed_at"]), row["evidence_id"]))
            winner = latest.get("inviter_person_id")
            winners = [latest["evidence_id"]]
            source_level = "manual"
        elif system:
            candidates = {row["inviter_person_id"] for row in system}
            if len(candidates) == 1:
                winner = next(iter(candidates))
                winners = [row["evidence_id"] for row in system]
            elif current_inviter in candidates:
                winner = current_inviter
                winners = [
                    row["evidence_id"]
                    for row in system
                    if row["inviter_person_id"] == current_inviter
                ]
                conflict = True
            else:
                winner = None
                winners = [row["evidence_id"] for row in system]
                conflict = True
            source_level = "system"
        elif metadata:
            latest = max(
                metadata,
                key=lambda row: (int(row["observed_at"]), row["evidence_id"]),
            )
            winner = latest["inviter_person_id"]
            winners = [latest["evidence_id"]]
            source_level = "metadata"
        elif csv_rows:
            candidates = {row["inviter_person_id"] for row in csv_rows}
            if len(candidates) == 1:
                winner = next(iter(candidates))
                winners = [row["evidence_id"] for row in csv_rows]
            elif current_inviter in candidates:
                winner = current_inviter
                winners = [
                    row["evidence_id"]
                    for row in csv_rows
                    if row["inviter_person_id"] == current_inviter
                ]
                conflict = True
            else:
                winners = [row["evidence_id"] for row in csv_rows]
                conflict = True
            source_level = "csv"

        stable_values = {
            row["inviter_person_id"]
            for row in evidence
            if row.get("inviter_person_id") is not None
        }
        if source_level in {"manual", "system"} and len(stable_values) > 1:
            conflict = True

        conflict_fingerprints: list[str] = []
        if conflict:
            episode_id = evidence[0]["episode_id"] if evidence else ""
            conflict_fingerprints.append(
                _stable_sha(
                    "stable_inviter",
                    episode_id,
                    winner or "unresolved",
                    ",".join(sorted(stable_values)),
                )
            )
        return winner, conflict_fingerprints, winners

    @staticmethod
    def _resolve_method_field(
        evidence: list[dict[str, Any]],
    ) -> tuple[str | None, list[str]]:
        system = [
            row
            for row in evidence
            if row["source_type"] == "system_message" and row.get("method")
        ]
        if system:
            latest = max(system, key=lambda row: (int(row["observed_at"]), row["evidence_id"]))
            return latest["method"], [latest["evidence_id"]]
        manual = [
            row
            for row in evidence
            if row["source_type"] == "manual_confirmation" and row.get("method")
        ]
        if manual:
            latest = max(manual, key=lambda row: (int(row["observed_at"]), row["evidence_id"]))
            return latest["method"], [latest["evidence_id"]]
        return None, []

    @staticmethod
    def _resolve_event_time_field(
        evidence: list[dict[str, Any]],
    ) -> tuple[int | None, list[str]]:
        system = [
            row
            for row in evidence
            if row["source_type"] == "system_message" and row.get("event_time") is not None
        ]
        if system:
            latest = max(system, key=lambda row: (int(row["observed_at"]), row["evidence_id"]))
            return int(latest["event_time"]), [latest["evidence_id"]]
        csv_rows = [
            row
            for row in evidence
            if row["source_type"] == "csv_import" and row.get("event_time") is not None
        ]
        if csv_rows:
            latest = max(csv_rows, key=lambda row: (int(row["observed_at"]), row["evidence_id"]))
            return int(latest["event_time"]), [latest["evidence_id"]]
        return None, []

    def _resolve_episode_conn(
        self, conn, episode_id: str, *, now: int
    ) -> ResolvedInvite:
        evidence = self.store._list_attached_trace_evidence_conn(conn, episode_id)
        active = self.store._get_active_trace_relation_conn(conn, episode_id)
        current_inviter = active["inviter_person_id"] if active else None
        inviter_person_id, inviter_conflicts, inviter_winners = self._resolve_inviter_field(
            evidence, current_inviter=current_inviter
        )
        method, method_winners = self._resolve_method_field(evidence)
        event_time, time_winners = self._resolve_event_time_field(evidence)
        episode = conn.execute(
            "SELECT * FROM trace_membership_episodes WHERE episode_id = ?",
            (episode_id,),
        ).fetchone()
        for fingerprint in inviter_conflicts:
            self.store._upsert_trace_conflict_conn(
                conn,
                room_id=episode["room_id"],
                episode_id=episode_id,
                relation_id=(active["relation_id"] if active else None),
                conflict_type="stable_inviter",
                fingerprint=fingerprint,
                detail={
                    "resolved_inviter_person_id": inviter_person_id,
                    "evidence_ids": [row["evidence_id"] for row in evidence],
                },
                now=now,
            )
        return ResolvedInvite(
            inviter_person_id=inviter_person_id,
            method=method,
            event_time=event_time,
            resolution_state=(
                "resolved" if inviter_person_id is not None else "unresolved"
            ),
            winning_evidence_ids=tuple(
                dict.fromkeys([*inviter_winners, *method_winners, *time_winners])
            ),
            conflict_fingerprints=tuple(inviter_conflicts),
        )

    def _materialize_resolved_relation_conn(
        self,
        conn,
        episode_id: str,
        resolved: ResolvedInvite,
        *,
        now: int,
    ) -> str:
        active = self.store._get_active_trace_relation_conn(conn, episode_id)
        new_inviter = resolved.inviter_person_id
        old_inviter = active["inviter_person_id"] if active else None
        if active is not None and old_inviter == new_inviter:
            self.store._enrich_trace_relation_conn(
                conn,
                active["relation_id"],
                method=resolved.method,
                event_time=resolved.event_time,
                last_observed_at=now,
                resolution_state=resolved.resolution_state,
            )
            return active["relation_id"]
        if active is not None:
            self.store._close_trace_relation_conn(
                conn, active["relation_id"], now=now
            )
        return self.store._insert_trace_relation_conn(
            conn,
            episode_id=episode_id,
            inviter_person_id=new_inviter,
            method=resolved.method,
            event_time=resolved.event_time,
            resolution_state=resolved.resolution_state,
            now=now,
        )

    def _resolve_room_episodes_conn(
        self, conn, room_id: str, *, now: int
    ) -> None:
        rows = conn.execute(
            """
            SELECT episode_id FROM trace_membership_episodes
            WHERE room_id = ? ORDER BY episode_index ASC, episode_id ASC
            """,
            (room_id,),
        ).fetchall()
        for row in rows:
            resolved = self._resolve_episode_conn(
                conn, row["episode_id"], now=now
            )
            self._materialize_resolved_relation_conn(
                conn, row["episode_id"], resolved, now=now
            )

    def _reconcile_room_conn(
        self,
        conn,
        room_id: str,
        *,
        trigger: str,
        now: int,
        observation_id: str | None,
    ) -> dict[str, Any]:
        managed = self.store._get_trace_managed_room_conn(conn, room_id)
        if managed is not None and not managed["archiving_enabled"]:
            return {"room_id": room_id, "skipped": "archive_disabled"}

        observation = self._basis_for_reconcile_conn(
            conn,
            room_id,
            trigger=trigger,
            observation_id=observation_id,
        )
        if trigger != "migration" and observation is None:
            return {"room_id": room_id, "deferred": "membership_basis_unavailable"}

        self._ingest_durable_system_evidence_conn(conn, room_id, now=now)

        observed_person_ids: set[str] = set()
        if observation is not None and observation["membership_status"] != "failed":
            for member in [dict(item) for item in observation["members"]]:
                username = str(member.get("username") or "").strip()
                if not username:
                    continue
                person = self.store._ensure_trace_person_conn(
                    conn,
                    username,
                    now=now,
                    nick_name=str(member.get("nick_name") or ""),
                    remark=str(member.get("remark") or ""),
                    avatar_url=str(member.get("avatar_url") or ""),
                )
                person_id = person["person_id"]
                observed_person_ids.add(person_id)
                self.store._record_trace_profile_conn(
                    conn,
                    person_id,
                    room_id=room_id,
                    observed_at=now,
                    nick_name=str(member.get("nick_name") or ""),
                    remark=str(member.get("remark") or ""),
                    room_display_name=str(
                        member.get("room_display_name")
                        or member.get("display_name")
                        or ""
                    ),
                    avatar_url=str(member.get("avatar_url") or ""),
                )
                episode = self.store._ensure_active_trace_episode_conn(
                    conn,
                    room_id,
                    person_id,
                    observed_at=now,
                )
                inviter_username = str(member.get("inviter_username") or "").strip()
                if inviter_username and inviter_username != username:
                    self.store._ensure_trace_person_conn(
                        conn, inviter_username, now=now
                    )
                if observation["inviter_metadata_status"] != "unavailable":
                    self._ingest_metadata_evidence_conn(
                        conn,
                        episode,
                        observation,
                        member,
                        now=now,
                    )

        can_close_by_absence = (
            observation is not None
            and observation["membership_status"] == "complete"
        )
        if can_close_by_absence:
            self.store._close_missing_active_episodes_conn(
                conn, room_id, observed_person_ids, now=now
            )

        self._attach_pending_room_evidence_conn(conn, room_id, now=now)
        self._resolve_room_episodes_conn(conn, room_id, now=now)

        if (
            observation is not None
            and observation["source_kind"] == "contact"
            and observation["membership_status"] == "complete"
        ):
            self.store._mark_trace_room_projection_ready_conn(
                conn,
                room_id,
                observation["observation_id"],
                now=now,
            )
        elif observation is not None and observation["membership_status"] == "failed":
            self.store._invalidate_trace_room_projection_conn(
                conn,
                room_id,
                state="refresh_error",
                error_code="contact_read_failed",
                error_message=str(observation.get("diagnostic_code") or ""),
                now=now,
            )
        elif observation is not None and observation["membership_status"] == "partial":
            self.store._invalidate_trace_room_projection_conn(
                conn,
                room_id,
                state="pending",
                now=now,
            )

        return {
            "room_id": room_id,
            "observed_members": len(observed_person_ids),
            "membership_status": (
                observation["membership_status"] if observation is not None else None
            ),
            "observation_id": (
                observation["observation_id"] if observation is not None else None
            ),
        }


def _list_current_archive_relation_rows_conn(
    conn, room_id: str
) -> list[dict[str, Any]]:
    rows = conn.execute(
        """
        SELECT r.*, invitee.wechat_username AS invitee_username,
               inviter.wechat_username AS inviter_username,
               invitee.current_nick_name AS invitee_name,
               inviter.current_nick_name AS inviter_name
        FROM trace_invite_relations AS r
        JOIN trace_membership_episodes AS e ON e.episode_id = r.episode_id
        JOIN trace_people AS invitee ON invitee.person_id = r.invitee_person_id
        LEFT JOIN trace_people AS inviter ON inviter.person_id = r.inviter_person_id
        WHERE r.room_id = ? AND r.active = 1 AND e.status = 'active'
        ORDER BY r.first_observed_at ASC, r.relation_id ASC
        """,
        (room_id,),
    ).fetchall()
    return [dict(row) for row in rows]


def _archive_relation_payloads(
    rows: list[dict[str, Any]], room_id: str
) -> list[dict[str, Any]]:
    result: list[dict[str, Any]] = []
    for row in rows:
        result.append(
            {
                "event_id": f"archive:{row['relation_id']}",
                "event_type": "member.inviter_resolved",
                "room_id": room_id,
                "event_time": row.get("resolved_event_time"),
                "source_shard": "__archive__",
                "source_table": "trace_invite_relations",
                "source_identity": row["relation_id"],
                "episode_id": row["episode_id"],
                "actor_id": str(row.get("inviter_username") or ""),
                "subject_id": str(row.get("invitee_username") or ""),
                "payload": {
                    "method": str(row.get("resolved_method") or "metadata"),
                    "inviter_name_raw": str(row.get("inviter_name") or "待确认"),
                    "invitee_name_raw": str(row.get("invitee_name") or "未知成员"),
                    "inviter_username": str(row.get("inviter_username") or ""),
                    "invitee_username": str(row.get("invitee_username") or ""),
                    "inviter_identity_status": "resolved" if row.get("inviter_person_id") else "unresolved",
                    "invitee_identity_status": "resolved",
                },
                "relation_source": "trace_archive",
                "time_status": "known" if row.get("resolved_event_time") is not None else "unknown",
                "metadata_conflict": False,
            }
        )
    return result


def list_current_archive_relations_for_room(
    store, room_id: str
) -> list[dict[str, Any]]:
    with store._connect() as conn:
        rows = _list_current_archive_relation_rows_conn(conn, room_id)
    return _archive_relation_payloads(rows, room_id)


def read_current_archive_projection_snapshot(
    store, room_id: str
) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    """Read readiness and archive relations from one explicit SQLite snapshot."""
    with store._connect() as conn:
        conn.execute("BEGIN")
        status = store._get_trace_room_projection_status_conn(conn, room_id)
        if not status["use_archive_current_projection"]:
            return status, []
        rows = _list_current_archive_relation_rows_conn(conn, room_id)
        return status, _archive_relation_payloads(rows, room_id)


def build_current_invite_tree(store, room_ids: list[str]) -> dict[str, Any]:
    cleaned_rooms = sorted({str(room_id).strip() for room_id in room_ids if str(room_id).strip()})
    if not cleaned_rooms:
        return {
            "unique_people": 0,
            "cycle_edges": 0,
            "descendant_counts": {},
            "edges": [],
        }

    placeholders = ",".join("?" for _ in cleaned_rooms)
    with store._connect() as conn:
        rows = conn.execute(
            f"""
            SELECT r.relation_id, r.room_id, r.inviter_person_id,
                   r.invitee_person_id, r.episode_id
            FROM trace_invite_relations AS r
            JOIN trace_membership_episodes AS e
              ON e.episode_id = r.episode_id
            WHERE r.active = 1
              AND r.resolution_state = 'resolved'
              AND r.inviter_person_id IS NOT NULL
              AND e.status = 'active'
              AND r.room_id IN ({placeholders})
            ORDER BY r.room_id ASC, r.relation_id ASC
            """,
            cleaned_rooms,
        ).fetchall()

        edges = [dict(row) for row in rows]
        people: set[str] = set()
        adjacency: dict[str, list[dict[str, Any]]] = {}
        for edge in edges:
            inviter = str(edge["inviter_person_id"])
            invitee = str(edge["invitee_person_id"])
            people.add(inviter)
            people.add(invitee)
            adjacency.setdefault(inviter, []).append(edge)

        cycle_edge_keys: set[tuple[str, str, str]] = set()
        color: dict[str, int] = {}

        def visit(person_id: str) -> None:
            color[person_id] = 1
            for edge in adjacency.get(person_id, []):
                child = str(edge["invitee_person_id"])
                state = color.get(child, 0)
                if state == 0:
                    visit(child)
                elif state == 1:
                    key = (str(edge["room_id"]), person_id, child)
                    if key not in cycle_edge_keys:
                        cycle_edge_keys.add(key)
                        fingerprint = _stable_sha(
                            "invite_tree_cycle",
                            edge["room_id"],
                            person_id,
                            child,
                        )
                        store._upsert_trace_conflict_conn(
                            conn,
                            room_id=str(edge["room_id"]),
                            episode_id=str(edge["episode_id"]),
                            relation_id=str(edge["relation_id"]),
                            conflict_type="invite_tree_cycle",
                            fingerprint=fingerprint,
                            detail={
                                "inviter_person_id": person_id,
                                "invitee_person_id": child,
                            },
                            now=int(
                                conn.execute("SELECT CAST(strftime('%s','now') AS INTEGER)").fetchone()[0]
                            ),
                        )
            color[person_id] = 2

        for person_id in sorted(people):
            if color.get(person_id, 0) == 0:
                visit(person_id)

        descendant_counts: dict[str, int] = {}
        for root in sorted(people):
            seen = {root}
            stack = [root]
            descendants: set[str] = set()
            while stack:
                current = stack.pop()
                for edge in adjacency.get(current, []):
                    child = str(edge["invitee_person_id"])
                    if child in seen:
                        continue
                    seen.add(child)
                    descendants.add(child)
                    stack.append(child)
            descendant_counts[root] = len(descendants)

    return {
        "unique_people": len(people),
        "cycle_edges": len(cycle_edge_keys),
        "descendant_counts": descendant_counts,
        "edges": edges,
    }


class ArchiveMigrator:
    CHECKPOINTS = (
        "schema_ready",
        "managed_rooms_seeded",
        "legacy_observations_imported",
        "durable_evidence_ingested",
        "projection_reconciled",
        "ready",
    )

    def __init__(self, store):
        self.store = store

    def _after_checkpoint(self, checkpoint: str) -> None:
        return None

    def _checkpoint(self) -> str:
        return self.store.get_meta("trace_archive_migration_checkpoint_v2") or ""

    def _checkpoint_index(self, value: str) -> int:
        if not value:
            return -1
        try:
            return self.CHECKPOINTS.index(value)
        except ValueError as exc:
            raise RuntimeError(f"unknown trace archive checkpoint: {value}") from exc

    def _advance(self, checkpoint: str) -> None:
        self.store.set_meta("trace_archive_migration_checkpoint_v2", checkpoint)
        self._after_checkpoint(checkpoint)

    def _capture_high_waters(self) -> None:
        with self.store._connect() as conn:
            snapshot_row = conn.execute(
                "SELECT COALESCE(MAX(captured_at), 0) AS value FROM membership_snapshots"
            ).fetchone()
            event_row = conn.execute(
                """
                SELECT COALESCE(MAX(created_at), 0) AS value
                FROM events
                WHERE event_type IN ('member.invited', 'member.joined')
                """
            ).fetchone()
        if self.store.get_meta("trace_archive_migration_legacy_snapshot_high_water_v2") is None:
            self.store.set_meta(
                "trace_archive_migration_legacy_snapshot_high_water_v2",
                str(int(snapshot_row["value"] if snapshot_row else 0)),
            )
        if self.store.get_meta("trace_archive_migration_event_high_water_v2") is None:
            self.store.set_meta(
                "trace_archive_migration_event_high_water_v2",
                str(int(event_row["value"] if event_row else 0)),
            )

    def _seed_managed_rooms(self, *, now: int) -> None:
        with self.store._connect() as conn:
            rows = conn.execute(
                """
                SELECT DISTINCT cr.room_id
                FROM campaign_rooms AS cr
                WHERE cr.room_id IS NOT NULL
                ORDER BY cr.room_id ASC
                """
            ).fetchall()
            for row in rows:
                if self.store._get_trace_managed_room_conn(conn, row["room_id"]) is None:
                    self.store._ensure_trace_managed_room_conn(
                        conn,
                        row["room_id"],
                        source="campaign",
                        now=now,
                    )

    def _import_legacy_observations(self, *, now: int) -> list[str]:
        high_water = int(
            self.store.get_meta("trace_archive_migration_legacy_snapshot_high_water_v2")
            or 0
        )
        observation_ids: list[str] = []
        with self.store._connect() as conn:
            rows = conn.execute(
                """
                SELECT s.room_id, s.captured_at, s.members_json
                FROM membership_snapshots AS s
                JOIN trace_managed_rooms AS m ON m.room_id = s.room_id
                WHERE s.captured_at <= ?
                  AND m.archiving_enabled = 1
                ORDER BY s.captured_at ASC, s.room_id ASC
                """,
                (high_water,),
            ).fetchall()
            for row in rows:
                room_id = str(row["room_id"])
                observed_at = int(row["captured_at"])
                members = json.loads(row["members_json"])
                members_json = json.dumps(
                    members,
                    ensure_ascii=False,
                    sort_keys=True,
                    separators=(",", ":"),
                )
                semantic_hash = hashlib.sha256(
                    members_json.encode("utf-8")
                ).hexdigest()
                source_state_key = f"legacy:{observed_at}"
                observation_id = self.store._trace_observation_id(
                    room_id,
                    "legacy_snapshot",
                    source_state_key,
                    semantic_hash,
                )
                conn.execute(
                    """
                    INSERT INTO trace_membership_observations(
                        observation_id, room_id, source_kind, source_state_key,
                        semantic_hash, first_observed_at, last_observed_at,
                        membership_status, inviter_metadata_status, members_json,
                        diagnostic_code
                    ) VALUES (?, ?, 'legacy_snapshot', ?, ?, ?, ?,
                              'partial', 'unavailable', ?, 'legacy_snapshot')
                    ON CONFLICT(observation_id) DO UPDATE SET
                        last_observed_at = MAX(
                            trace_membership_observations.last_observed_at,
                            excluded.last_observed_at
                        )
                    """,
                    (
                        observation_id,
                        room_id,
                        source_state_key,
                        semantic_hash,
                        observed_at,
                        observed_at,
                        members_json,
                    ),
                )
                observation_ids.append(observation_id)

                # Legacy snapshots are deliberately partial. They may contribute
                # positive membership/profile observations, but absence and legacy
                # inviter metadata cannot close episodes or resolve relations.
                for member in members:
                    username = str(member.get("username") or "").strip()
                    if not username:
                        continue
                    person = self.store._ensure_trace_person_conn(
                        conn,
                        username,
                        now=observed_at,
                        nick_name=str(member.get("nick_name") or ""),
                        remark=str(member.get("remark") or ""),
                        avatar_url=str(member.get("avatar_url") or ""),
                    )
                    self.store._record_trace_profile_conn(
                        conn,
                        person["person_id"],
                        room_id=room_id,
                        observed_at=observed_at,
                        nick_name=str(member.get("nick_name") or ""),
                        remark=str(member.get("remark") or ""),
                        room_display_name=str(
                            member.get("room_display_name")
                            or member.get("display_name")
                            or ""
                        ),
                        avatar_url=str(member.get("avatar_url") or ""),
                    )
                    self.store._ensure_active_trace_episode_conn(
                        conn,
                        room_id,
                        person["person_id"],
                        observed_at=observed_at,
                    )
        return observation_ids

    def _ingest_durable_evidence(self, *, now: int) -> None:
        high_water = int(
            self.store.get_meta("trace_archive_migration_event_high_water_v2") or 0
        )
        with self.store._connect() as conn:
            room_rows = conn.execute(
                """
                SELECT DISTINCT e.room_id
                FROM events AS e
                JOIN trace_managed_rooms AS m ON m.room_id = e.room_id
                WHERE e.event_type IN ('member.invited', 'member.joined')
                  AND e.created_at <= ?
                  AND m.archiving_enabled = 1
                ORDER BY e.room_id ASC
                """,
                (high_water,),
            ).fetchall()
            reconciler = ArchiveReconciler(self.store)
            for row in room_rows:
                reconciler._ingest_durable_system_evidence_conn(
                    conn, row["room_id"], now=now
                )

    def _reconcile_migration_projection(self, *, now: int) -> None:
        reconciler = ArchiveReconciler(self.store)
        with self.store._connect() as conn:
            managed_rows = conn.execute(
                """
                SELECT room_id FROM trace_managed_rooms
                WHERE archiving_enabled = 1
                ORDER BY room_id ASC
                """
            ).fetchall()
            for row in managed_rows:
                room_id = str(row["room_id"])
                reconciler._attach_pending_room_evidence_conn(
                    conn, room_id, now=now
                )
                reconciler._resolve_room_episodes_conn(
                    conn, room_id, now=now
                )
                conn.execute(
                    """
                    UPDATE trace_managed_rooms
                    SET projection_state = CASE
                            WHEN projection_state = 'ready' THEN 'pending'
                            ELSE projection_state END,
                        current_observation_id = CASE
                            WHEN projection_state = 'ready' THEN NULL
                            ELSE current_observation_id END
                    WHERE room_id = ?
                      AND NOT EXISTS (
                          SELECT 1 FROM trace_membership_observations AS o
                          WHERE o.observation_id = trace_managed_rooms.current_observation_id
                            AND o.source_kind = 'contact'
                            AND o.membership_status = 'complete'
                      )
                    """,
                    (room_id,),
                )

    def migrate_v2(self, *, now: int) -> dict[str, Any]:
        self._capture_high_waters()
        current = self._checkpoint()
        current_index = self._checkpoint_index(current)

        if current_index < self._checkpoint_index("schema_ready"):
            self.store.set_meta("trace_archive_schema_version", "2")
            self._advance("schema_ready")
            current_index = self._checkpoint_index("schema_ready")

        if current_index < self._checkpoint_index("managed_rooms_seeded"):
            self._seed_managed_rooms(now=int(now))
            self._advance("managed_rooms_seeded")
            current_index = self._checkpoint_index("managed_rooms_seeded")

        if current_index < self._checkpoint_index("legacy_observations_imported"):
            self._import_legacy_observations(now=int(now))
            self._advance("legacy_observations_imported")
            current_index = self._checkpoint_index("legacy_observations_imported")

        if current_index < self._checkpoint_index("durable_evidence_ingested"):
            self._ingest_durable_evidence(now=int(now))
            self._advance("durable_evidence_ingested")
            current_index = self._checkpoint_index("durable_evidence_ingested")

        if current_index < self._checkpoint_index("projection_reconciled"):
            self._reconcile_migration_projection(now=int(now))
            self._advance("projection_reconciled")
            current_index = self._checkpoint_index("projection_reconciled")

        if current_index < self._checkpoint_index("ready"):
            self._advance("ready")

        return {
            "schema_version": "2",
            "checkpoint": self._checkpoint(),
        }
