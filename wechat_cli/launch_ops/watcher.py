"""Incremental Launch Ops watcher with durable cursors and health state."""

from __future__ import annotations

import hashlib
import json
import threading
import time
import uuid
from typing import Any

from .archive import ArchiveReconciler
from .models import ScanResult, StandardEvent, WatcherHealth
from .normalizer import normalize_invite_events, normalize_message

SCAN_INTERVAL_SECONDS = 3.0
OVERLAP_SECONDS = 5
INITIAL_BACKFILL_SECONDS = 30 * 24 * 60 * 60
STALE_AFTER_SECONDS = 15


class LaunchOpsWatcher:
    def __init__(
        self,
        store,
        source_adapter,
        *,
        scan_interval_seconds: float = SCAN_INTERVAL_SECONDS,
        overlap_seconds: int = OVERLAP_SECONDS,
        initial_backfill_seconds: int = INITIAL_BACKFILL_SECONDS,
        stale_after_seconds: int = STALE_AFTER_SECONDS,
    ):
        self.store = store
        self.source = source_adapter
        self.scan_interval_seconds = float(scan_interval_seconds)
        self.overlap_seconds = int(overlap_seconds)
        self.initial_backfill_seconds = int(initial_backfill_seconds)
        self.stale_after_seconds = int(stale_after_seconds)
        self._last_stamps: dict[tuple[str, str, str], Any] = {}
        self._last_heads: dict[tuple[str, str, str], dict[str, int]] = {}
        self._room_names: dict[str, str] = {}
        self._membership_signatures: dict[str, str] = {}
        self._last_attempt_at: int | None = None
        self._last_success_at: int | None = None
        self._last_event_at: int | None = None
        self._scan_in_progress = False
        self._error_code = ""
        self._error_message = ""
        self._stop_event = threading.Event()
        self._thread: threading.Thread | None = None
        self._state_lock = threading.Lock()
        self._scan_lock = threading.Lock()
        self.archive_reconciler = ArchiveReconciler(store)

    @property
    def is_running(self) -> bool:
        thread = self._thread
        return bool(thread and thread.is_alive())

    def _record_health_transition(
        self,
        status: str,
        now: int,
        *,
        error_code: str = "",
        error_message: str = "",
    ) -> None:
        meta_key = "watcher_health_state"
        try:
            previous_raw = self.store.get_meta(meta_key) or ""
            previous_status = previous_raw
            if previous_raw:
                try:
                    previous_payload = json.loads(previous_raw)
                except (TypeError, json.JSONDecodeError):
                    previous_payload = None
                if isinstance(previous_payload, dict):
                    previous_status = str(previous_payload.get("status") or "")

            with self._state_lock:
                snapshot = {
                    "status": status,
                    "last_attempt_at": self._last_attempt_at,
                    "last_success_at": self._last_success_at,
                    "last_event_at": self._last_event_at,
                    "error_code": error_code,
                    "error_message": error_message,
                }

            if previous_status != status:
                self.store.insert_event(
                    StandardEvent(
                        event_id=f"watcher:health:{status}:{now}",
                        event_type="watcher.health",
                        room_id="__watcher__",
                        event_time=now,
                        source_shard="__watcher__",
                        source_table="__watcher__",
                        source_identity=f"{status}:{now}",
                        actor_id="",
                        subject_id="",
                        payload={
                            "status": status,
                            "error_code": error_code,
                            "error_message": error_message,
                        },
                    ),
                    protected=False,
                    now=now,
                )
            self.store.set_meta(
                meta_key,
                json.dumps(snapshot, ensure_ascii=False, sort_keys=True),
            )
        except Exception:
            # Health telemetry must never hide the original watcher outcome.
            return

    def _set_failure(self, exc: Exception, now: int) -> None:
        if isinstance(exc, FileNotFoundError):
            code = "wechat_data_unavailable"
        elif isinstance(exc, PermissionError):
            code = "wechat_data_unreadable"
        elif isinstance(exc, RuntimeError) and "密钥" in str(exc):
            code = "key_refresh_required"
        else:
            code = "scan_failed"
        message = str(exc)
        with self._state_lock:
            self._last_attempt_at = now
            self._scan_in_progress = False
            self._error_code = code
            self._error_message = message
        self._record_health_transition(
            "error",
            now,
            error_code=code,
            error_message=message,
        )

    def _set_success(self, now: int, last_event_at: int | None) -> None:
        with self._state_lock:
            self._last_attempt_at = now
            self._last_success_at = now
            self._scan_in_progress = False
            if last_event_at is not None:
                self._last_event_at = last_event_at
            self._error_code = ""
            self._error_message = ""
        self._record_health_transition("running", now)

    @staticmethod
    def _membership_observation_id_from_source_identity(
        source_identity: str,
    ) -> str:
        prefix = "membership:"
        if not str(source_identity).startswith(prefix):
            raise ValueError("invalid membership queue source_identity")
        observation_id = str(source_identity)[len(prefix):]
        if not observation_id:
            raise ValueError("invalid membership queue source_identity")
        return observation_id

    def _drain_trace_reconcile_queue(
        self, *, now: int, room_id: str | None = None
    ) -> list[dict[str, Any]]:
        pending = self.store.list_pending_trace_reconcile(room_id)
        room_ids = []
        for row in pending:
            if row["room_id"] not in room_ids:
                room_ids.append(row["room_id"])
        results: list[dict[str, Any]] = []
        for current_room_id in room_ids:
            managed = self.store.get_trace_managed_room(current_room_id)
            if managed is None or not managed["archiving_enabled"]:
                continue
            room_pending = self.store.list_pending_trace_reconcile(current_room_id)
            membership_rows = [
                row for row in room_pending if row["reason"] == "membership"
            ]
            if membership_rows:
                queued = membership_rows[0]
                observation_id = self._membership_observation_id_from_source_identity(
                    queued["source_identity"]
                )
                observation = self.store.get_trace_membership_observation(
                    observation_id
                )
                if observation is None or observation["room_id"] != current_room_id:
                    raise ValueError("trace observation not found for room")
                try:
                    result = self.archive_reconciler.reconcile_room(
                        current_room_id,
                        trigger="membership",
                        observation_id=observation_id,
                        now=int(now),
                    )
                except Exception as exc:
                    self.store.mark_trace_room_refresh_error(
                        current_room_id,
                        error_code="archive_reconcile_failed",
                        error_message=str(exc),
                        now=int(now),
                    )
                    raise
                results.append(result)
                if self._reconcile_result_archive_disabled(result):
                    continue
                if observation["membership_status"] == "complete":
                    if not self.store.ack_trace_reconcile_if_room_enabled(
                        queued["queue_id"], current_room_id, now=int(now)
                    ):
                        continue
                else:
                    continue

            room_pending = self.store.list_pending_trace_reconcile(current_room_id)
            if any(row["reason"] == "membership" for row in room_pending):
                continue
            managed = self.store.get_trace_managed_room(current_room_id)
            basis_id = managed.get("current_observation_id") if managed else None
            if not basis_id or managed.get("projection_state") == "refresh_error":
                continue
            basis = self.store.get_trace_membership_observation(basis_id)
            if (
                basis is None
                or basis["room_id"] != current_room_id
                or basis["source_kind"] != "contact"
                or basis["membership_status"] != "complete"
            ):
                continue
            for queued in list(self.store.list_pending_trace_reconcile(current_room_id)):
                if queued["reason"] == "membership":
                    continue
                try:
                    result = self.archive_reconciler.reconcile_room(
                        current_room_id,
                        trigger=queued["reason"],
                        observation_id=basis_id,
                        now=int(now),
                    )
                except Exception as exc:
                    self.store.mark_trace_room_refresh_error(
                        current_room_id,
                        error_code="archive_reconcile_failed",
                        error_message=str(exc),
                        now=int(now),
                    )
                    raise
                if self._reconcile_result_archive_disabled(result):
                    results.append(result)
                    continue
                if not self.store.ack_trace_reconcile_if_room_enabled(
                    queued["queue_id"], current_room_id, now=int(now)
                ):
                    results.append(result)
                    continue
                results.append(result)
        return results

    @staticmethod
    def _reconcile_result_archive_disabled(result: dict[str, Any] | None) -> bool:
        if not isinstance(result, dict):
            return False
        skipped = str(result.get("skipped") or "").strip().lower()
        status = str(result.get("status") or "").strip().lower()
        return skipped.startswith("archive_disabled") or status in {
            "archive_disabled",
            "disabled",
        }

    def scan_once(self, now: int | None = None) -> ScanResult:
        with self._scan_lock:
            return self._scan_once_unlocked(now=now)

    def force_refresh_room(
        self, room_id: str, *, now: int
    ) -> dict[str, Any]:
        managed = self.store.get_trace_managed_room(room_id)
        if managed is None or not managed["archiving_enabled"]:
            raise ValueError("archive is disabled")
        groups = {
            group.room_id: group for group in self.source.discover_groups()
        }
        group = groups.get(room_id)
        if group is None:
            raise ValueError("room not found")
        observation = self.source.membership_observation(
            group,
            observed_at=int(now),
            force=True,
        )
        saved = self.store.save_trace_membership_observation(
            room_id=observation.room_id,
            source_kind="contact",
            source_state_key=observation.source_state_key,
            semantic_hash=observation.semantic_hash,
            membership_status=observation.membership_status,
            inviter_metadata_status=observation.inviter_metadata_status,
            members=list(observation.members),
            diagnostic_code=observation.diagnostic_code,
            observed_at=observation.observed_at,
        )
        request_id = uuid.uuid4().hex
        self.store.enqueue_trace_reconcile(
            room_id,
            "force_refresh",
            f"force_refresh:{request_id}",
            now=int(now),
        )
        results = self._drain_trace_reconcile_queue(
            now=int(now), room_id=room_id
        )
        status = self.store.get_trace_room_projection_status(room_id)
        return {
            "room_id": room_id,
            "observation_id": saved["observation_id"],
            "membership_status": saved["membership_status"],
            "reconciled": status["projection_state"] == "ready",
            "projection_state": status["projection_state"],
            "results": results,
        }

    def backfill_invites(
        self,
        room_ids: list[str],
        *,
        start_time: int,
        end_time: int,
        now: int | None = None,
    ) -> dict[str, int]:
        start = int(start_time)
        end = int(end_time)
        if start < 0 or end < start:
            raise ValueError("invalid invite backfill time window")
        requested = {
            str(room_id).strip()
            for room_id in room_ids
            if str(room_id).strip()
        }
        if not requested:
            return {
                "scanned_rooms": 0,
                "scanned_sources": 0,
                "candidate_system_messages": 0,
                "inserted_invite_events": 0,
                "inserted_join_events": 0,
            }
        with self._scan_lock:
            scan_time = int(time.time() if now is None else now)
            groups = {
                group.room_id: group
                for group in self.source.discover_groups()
                if group.room_id in requested
            }
            scanned_sources = 0
            candidate_system_messages = 0
            inserted_invites = 0
            inserted_joins = 0
            affected_archive_rooms: set[str] = set()
            for room_id in sorted(requested):
                group = groups.get(room_id)
                if group is None:
                    continue
                for source in self.source.resolve_sources(group):
                    refreshed = self.source.refresh(source)
                    rows = self.source.read_rows_between(
                        refreshed,
                        start_time=start,
                        end_time=end,
                    )
                    scanned_sources += 1
                    for row in rows:
                        parent = normalize_message(refreshed, row)
                        if parent.message_type != "system":
                            continue
                        candidate_system_messages += 1
                        for event in normalize_invite_events(refreshed, row, parent):
                            inserted = self.store.insert_event(event, now=scan_time)
                            if not inserted:
                                continue
                            if event.event_type == "member.invited":
                                inserted_invites += 1
                            elif event.event_type == "member.joined":
                                inserted_joins += 1
                            managed = self.store.get_trace_managed_room(room_id)
                            if managed is not None and managed["archiving_enabled"]:
                                affected_archive_rooms.add(room_id)
            for room_id in sorted(affected_archive_rooms):
                self._drain_trace_reconcile_queue(
                    now=scan_time, room_id=room_id
                )
            return {
                "scanned_rooms": len(groups),
                "scanned_sources": scanned_sources,
                "candidate_system_messages": candidate_system_messages,
                "inserted_invite_events": inserted_invites,
                "inserted_join_events": inserted_joins,
            }

    def _scan_once_unlocked(self, now: int | None = None) -> ScanResult:
        explicit_now = now is not None
        scan_time = int(time.time() if now is None else now)
        scanned_sources = 0
        skipped_sources = 0
        inserted_events = 0
        inserted_messages = 0
        last_event_at: int | None = None
        with self._state_lock:
            self._last_attempt_at = scan_time
            self._scan_in_progress = True
        try:
            groups = self.source.discover_groups()
            sources_by_room: dict[str, list[Any]] = {}
            sources_by_shard: dict[str, list[Any]] = {}
            for group in groups:
                group_sources = self.source.resolve_sources(group)
                sources_by_room[group.room_id] = group_sources
                for source in group_sources:
                    sources_by_shard.setdefault(source.source_shard, []).append(source)
            head_cache: dict[tuple[str, str], Any] = {}

            for group in groups:
                room_known = group.room_id in self._room_names
                room_renamed = self._room_names.get(group.room_id) != group.display_name
                if not room_known:
                    if self.store.insert_event(
                        StandardEvent(
                            event_id=f"room:discovered:{group.room_id}",
                            event_type="room.discovered",
                            room_id=group.room_id,
                            event_time=scan_time,
                            source_shard="__contact__",
                            source_table="contact",
                            source_identity=group.room_id,
                            actor_id="",
                            subject_id=group.room_id,
                            payload={"display_name": group.display_name},
                        ),
                        protected=False,
                        now=scan_time,
                    ):
                        inserted_events += 1
                if not room_known or room_renamed:
                    self.store.upsert_room(
                        group.room_id,
                        group.display_name,
                        health_status="running",
                        now=scan_time,
                    )
                self.store.upsert_room_profile(
                    group.room_id,
                    group.avatar_url,
                    observed_at=scan_time,
                )
                self._room_names[group.room_id] = group.display_name
                sources = sources_by_room.get(group.room_id, [])
                for source in sources:
                    stamp = self.source.stamp(source)
                    stamp_key = (
                        source.room_id,
                        source.source_shard,
                        source.table_name,
                    )
                    stamp_payload = {
                        "db_mtime_ns": int(stamp.db_mtime_ns),
                        "wal_mtime_ns": int(stamp.wal_mtime_ns),
                        "db_size": int(stamp.db_size),
                        "wal_size": int(stamp.wal_size),
                    }
                    previous_stamp = self._last_stamps.get(stamp_key)
                    durable_stamp = None
                    stamp_already_processed = False
                    if previous_stamp is None:
                        durable_stamp = self.store.get_source_stamp(
                            source.source_shard,
                            source.table_name,
                        )
                        if durable_stamp == stamp_payload:
                            durable_head = self.store.get_table_head(
                                source.source_shard,
                                source.table_name,
                            )
                            if durable_head is not None:
                                self._last_stamps[stamp_key] = stamp
                                self._last_heads[stamp_key] = durable_head
                                skipped_sources += 1
                                continue
                            stamp_already_processed = True
                    elif previous_stamp == stamp:
                        skipped_sources += 1
                        continue

                    if source.source_shard not in {
                        key[0] for key in head_cache
                    }:
                        head_cache.update(
                            self.source.table_heads(
                                sources_by_shard.get(source.source_shard, [source])
                            )
                        )
                    current_head = head_cache.get(
                        (source.source_shard, source.table_name)
                    )
                    head_payload = {
                        "local_id": int(current_head.local_id) if current_head else 0,
                        "create_time": int(current_head.create_time) if current_head else 0,
                    }
                    previous_head = self._last_heads.get(stamp_key)
                    if previous_head is None:
                        previous_head = self.store.get_table_head(
                            source.source_shard,
                            source.table_name,
                        )

                    if stamp_already_processed or (
                        previous_head is not None and previous_head == head_payload
                    ):
                        self.store.apply_source_batch(
                            source_shard=source.source_shard,
                            source_table=source.table_name,
                            source_stamp=stamp_payload,
                            table_head=head_payload,
                            events=[],
                            messages=[],
                            cursor=None,
                            now=scan_time,
                        )
                        self._last_stamps[stamp_key] = stamp
                        self._last_heads[stamp_key] = head_payload
                        skipped_sources += 1
                        continue

                    refreshed = self.source.refresh(source)
                    cursor = self.store.get_cursor(
                        refreshed.source_shard,
                        refreshed.table_name,
                    )
                    if cursor is None:
                        start_time = scan_time - self.initial_backfill_seconds
                        start_local_id = 0
                        overlap_seconds = 0
                    else:
                        start_time = int(cursor["create_time"])
                        start_local_id = int(cursor["local_id"])
                        overlap_seconds = self.overlap_seconds

                    rows = self.source.read_rows(
                        refreshed,
                        start_time=start_time,
                        start_local_id=start_local_id,
                        overlap_seconds=overlap_seconds,
                    )
                    max_row = None
                    source_events = []
                    source_messages = []
                    for row in rows:
                        normalized = normalize_message(refreshed, row)
                        source_events.append(normalized.event)
                        source_messages.append(
                            {
                                "message_id": normalized.message_id,
                                "event_id": normalized.event.event_id,
                                "room_id": normalized.event.room_id,
                                "event_time": normalized.event.event_time,
                                "message_type": normalized.message_type,
                                "sender_id": normalized.sender_id,
                                "text_content": normalized.text_content,
                                "payload": normalized.payload,
                            }
                        )
                        source_events.extend(
                            normalize_invite_events(
                                refreshed,
                                row,
                                normalized,
                            )
                        )
                        if max_row is None or (
                            row.create_time,
                            row.local_id,
                        ) > (
                            max_row.create_time,
                            max_row.local_id,
                        ):
                            max_row = row
                        if last_event_at is None or row.create_time > last_event_at:
                            last_event_at = row.create_time

                    cursor_update = None
                    if max_row is not None:
                        normalized_max = normalize_message(refreshed, max_row)
                        cursor_update = {
                            "create_time": max_row.create_time,
                            "local_id": max_row.local_id,
                            "source_identity": normalized_max.event.source_identity,
                        }
                    batch_result = self.store.apply_source_batch(
                        source_shard=refreshed.source_shard,
                        source_table=refreshed.table_name,
                        source_stamp=stamp_payload,
                        table_head=head_payload,
                        events=source_events,
                        messages=source_messages,
                        cursor=cursor_update,
                        now=scan_time,
                    )
                    inserted_events += batch_result["inserted_events"]
                    inserted_messages += batch_result["inserted_messages"]
                    if max_row is not None:
                        self.store.upsert_room(
                            group.room_id,
                            group.display_name,
                            last_event_at=max_row.create_time,
                            health_status="running",
                            now=scan_time,
                        )
                    self._last_stamps[stamp_key] = stamp
                    self._last_heads[stamp_key] = head_payload
                    scanned_sources += 1

                managed = self.store.get_trace_managed_room(group.room_id)
                if managed is not None and bool(managed["archiving_enabled"]):
                    observation = self.source.membership_observation(
                        group,
                        observed_at=scan_time,
                        force=False,
                    )
                    self.store.save_trace_membership_observation(
                        room_id=observation.room_id,
                        source_kind="contact",
                        source_state_key=observation.source_state_key,
                        semantic_hash=observation.semantic_hash,
                        membership_status=observation.membership_status,
                        inviter_metadata_status=observation.inviter_metadata_status,
                        members=list(observation.members),
                        diagnostic_code=observation.diagnostic_code,
                        observed_at=observation.observed_at,
                    )
                    members = (
                        list(observation.members)
                        if observation.membership_status != "failed"
                        else self.source.members(group)
                    )
                else:
                    members = self.source.members(group)
                canonical_members = sorted(
                    (
                        str(
                            member.get("username")
                            or member.get("display_name")
                            or ""
                        ),
                        str(member.get("inviter_username") or ""),
                    )
                    for member in members
                )
                signature = hashlib.sha256(
                    json.dumps(
                        canonical_members,
                        ensure_ascii=False,
                        separators=(",", ":"),
                    ).encode("utf-8")
                ).hexdigest()
                signature_key = f"membership_signature:{group.room_id}"
                if group.room_id not in self._membership_signatures:
                    self._membership_signatures[group.room_id] = (
                        self.store.get_meta(signature_key) or ""
                    )
                if self._membership_signatures[group.room_id] != signature:
                    self.store.upsert_room(
                        group.room_id,
                        group.display_name,
                        member_count=len(members),
                        health_status="running",
                        now=scan_time,
                    )
                    snapshot_identity = (
                        f"membership:{group.room_id}:{signature}:{scan_time}"
                    )
                    if self.store.insert_event(
                        StandardEvent(
                            event_id=snapshot_identity,
                            event_type="room.membership_snapshot",
                            room_id=group.room_id,
                            event_time=scan_time,
                            source_shard="__contact__",
                            source_table="chatroom_member",
                            source_identity=snapshot_identity,
                            actor_id="",
                            subject_id=group.room_id,
                            payload={
                                "member_count": len(members),
                                "signature": signature,
                            },
                        ),
                        now=scan_time,
                    ):
                        inserted_events += 1
                    self.store.set_meta(signature_key, signature)
                    self.store.save_membership_snapshot(
                        group.room_id,
                        members,
                        captured_at=scan_time,
                    )
                    self._membership_signatures[group.room_id] = signature

            self._drain_trace_reconcile_queue(now=scan_time)
            self.store.purge_expired_messages(now=scan_time)
            completed_at = scan_time if explicit_now else int(time.time())
            self._set_success(completed_at, last_event_at)
            return ScanResult(
                scanned_sources=scanned_sources,
                skipped_sources=skipped_sources,
                inserted_events=inserted_events,
                inserted_messages=inserted_messages,
                discovered_rooms=len(groups),
                last_event_at=last_event_at,
            )
        except Exception as exc:
            failed_at = scan_time if explicit_now else int(time.time())
            self._set_failure(exc, failed_at)
            raise

    def health(self, now: int | None = None) -> WatcherHealth:
        now = int(time.time() if now is None else now)
        with self._state_lock:
            last_attempt = self._last_attempt_at
            last_success = self._last_success_at
            last_event = self._last_event_at
            scanning = self._scan_in_progress
            error_code = self._error_code
            error_message = self._error_message
        stale = bool(
            last_success is not None
            and now - last_success > self.stale_after_seconds
        )
        if scanning:
            status = "scanning"
        elif error_code:
            status = "error"
        elif last_success is None:
            status = "starting"
        else:
            status = "running"
        return WatcherHealth(
            status=status,
            stale=stale,
            last_attempt_at=last_attempt,
            last_success_at=last_success,
            last_event_at=last_event,
            error_code=error_code,
            error_message=error_message,
        )

    def _run_loop(self) -> None:
        while not self._stop_event.is_set():
            try:
                self.scan_once()
            except Exception:
                pass
            if self._stop_event.wait(self.scan_interval_seconds):
                break

    def start(self) -> None:
        if self.is_running:
            return
        self._stop_event.clear()
        self._thread = threading.Thread(
            target=self._run_loop,
            name="wechat-cli-launch-ops-watcher",
            daemon=True,
        )
        self._thread.start()

    def stop(self, timeout: float = 5.0) -> None:
        self._stop_event.set()
        thread = self._thread
        if thread and thread.is_alive():
            thread.join(timeout=max(0.0, timeout))
        self._thread = None
