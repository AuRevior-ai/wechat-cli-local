import json
import tempfile
import threading
import time
import unittest
from collections import defaultdict
from pathlib import Path
from unittest.mock import Mock, patch

from wechat_cli.launch_ops.source import (
    GroupSource,
    MessageSource,
    RawMessageRow,
    SourceStamp,
    TableHead,
)
from wechat_cli.launch_ops.store import LaunchOpsStore
from wechat_cli.launch_ops.watcher import LaunchOpsWatcher


class FakeSourceAdapter:
    def __init__(self, groups, sources_by_room, rows_by_shard=None):
        self.groups = groups
        self.sources_by_room = sources_by_room
        self.rows_by_shard = rows_by_shard or {}
        self.stamps = {
            source.source_shard: SourceStamp(1, 0, 10, 0)
            for sources in sources_by_room.values()
            for source in sources
        }
        self.refresh_calls = defaultdict(int)
        self.read_calls = defaultdict(int)
        self.head_calls = defaultdict(int)
        self.fail_discovery = None

    def discover_groups(self):
        if self.fail_discovery:
            raise self.fail_discovery
        return list(self.groups)

    def resolve_sources(self, group):
        return list(self.sources_by_room.get(group.room_id, []))

    def stamp(self, source):
        return self.stamps[source.source_shard]

    def refresh(self, source):
        self.refresh_calls[source.source_shard] += 1
        return source

    def table_heads(self, sources):
        result = {}
        seen_shards = set()
        for source in sources:
            if source.source_shard not in seen_shards:
                self.head_calls[source.source_shard] += 1
                seen_shards.add(source.source_shard)
            rows = list(self.rows_by_shard.get(source.source_shard, []))
            if rows:
                row = max(rows, key=lambda item: item.local_id)
                head = TableHead(row.local_id, row.create_time)
            else:
                head = TableHead(0, 0)
            result[(source.source_shard, source.table_name)] = head
        return result

    def read_rows(self, source, *, start_time, start_local_id, overlap_seconds):
        self.read_calls[source.source_shard] += 1
        rows = list(self.rows_by_shard.get(source.source_shard, []))
        threshold = start_time - overlap_seconds
        result = []
        for row in rows:
            if row.create_time < threshold:
                continue
            if overlap_seconds <= 0 and row.create_time == start_time and row.local_id <= start_local_id:
                continue
            result.append(row)
        return result

    def read_rows_between(self, source, *, start_time, end_time):
        rows = list(self.rows_by_shard.get(source.source_shard, []))
        return [row for row in rows if start_time <= row.create_time <= end_time]

    def members(self, group):
        return []


class CountingRoomStateStore(LaunchOpsStore):
    def __init__(self, path):
        super().__init__(path)
        self.room_upserts = 0
        self.snapshot_saves = 0
        self.membership_meta_reads = 0

    def upsert_room(self, *args, **kwargs):
        self.room_upserts += 1
        return super().upsert_room(*args, **kwargs)

    def save_membership_snapshot(self, *args, **kwargs):
        self.snapshot_saves += 1
        return super().save_membership_snapshot(*args, **kwargs)

    def get_meta(self, key):
        if str(key).startswith("membership_signature:"):
            self.membership_meta_reads += 1
        return super().get_meta(key)


class FailBatchOnceStore(LaunchOpsStore):
    def __init__(self, path):
        super().__init__(path)
        self.fail_batch_once = True

    def apply_source_batch(self, *args, **kwargs):
        if self.fail_batch_once:
            self.fail_batch_once = False
            raise RuntimeError("source batch failed")
        return super().apply_source_batch(*args, **kwargs)


class LaunchOpsWatcherTests(unittest.TestCase):
    def make_source(self, shard="message/message_0.db", room="room@chatroom"):
        return MessageSource(
            room_id=room,
            source_shard=shard,
            db_path=f"{shard}.decrypted",
            table_name="Msg_" + ("a" if shard.endswith("0.db") else "b") * 32,
        )

    def make_row(self, local_id=1, server_id=101, create_time=100, text="hello"):
        return RawMessageRow(
            local_id=local_id,
            server_id=server_id,
            local_type=1,
            create_time=create_time,
            real_sender_id=0,
            message_content=text,
            content_compression=0,
        )

    def test_unchanged_shard_is_not_refreshed_twice(self):
        source = self.make_source()
        adapter = FakeSourceAdapter(
            [GroupSource(source.room_id, "测试群")],
            {source.room_id: [source]},
            {source.source_shard: [self.make_row()]},
        )
        with tempfile.TemporaryDirectory() as tmp:
            watcher = LaunchOpsWatcher(
                LaunchOpsStore(Path(tmp) / "launch.db"), adapter
            )
            watcher.scan_once(now=1000)
            watcher.scan_once(now=1003)

        self.assertEqual(1, adapter.refresh_calls[source.source_shard])
        self.assertEqual(1, adapter.read_calls[source.source_shard])

    def test_restart_skips_source_when_durable_stamp_is_unchanged(self):
        source = self.make_source()
        adapter = FakeSourceAdapter(
            [GroupSource(source.room_id, "测试群")],
            {source.room_id: [source]},
            {source.source_shard: [self.make_row()]},
        )
        stamp = adapter.stamps[source.source_shard]
        with tempfile.TemporaryDirectory() as tmp:
            store = LaunchOpsStore(Path(tmp) / "launch.db")
            store.apply_source_batch(
                source_shard=source.source_shard,
                source_table=source.table_name,
                source_stamp={
                    "db_mtime_ns": stamp.db_mtime_ns,
                    "wal_mtime_ns": stamp.wal_mtime_ns,
                    "db_size": stamp.db_size,
                    "wal_size": stamp.wal_size,
                },
                events=[],
                messages=[],
                cursor=None,
                now=900,
            )
            watcher = LaunchOpsWatcher(store, adapter)
            result = watcher.scan_once(now=1000)

        self.assertEqual(0, adapter.refresh_calls[source.source_shard])
        self.assertEqual(0, adapter.read_calls[source.source_shard])
        self.assertEqual(0, result.scanned_sources)
        self.assertEqual(1, result.skipped_sources)

    def test_restart_scans_source_when_durable_stamp_changed(self):
        source = self.make_source()
        adapter = FakeSourceAdapter(
            [GroupSource(source.room_id, "测试群")],
            {source.room_id: [source]},
            {source.source_shard: [self.make_row()]},
        )
        with tempfile.TemporaryDirectory() as tmp:
            store = LaunchOpsStore(Path(tmp) / "launch.db")
            store.apply_source_batch(
                source_shard=source.source_shard,
                source_table=source.table_name,
                source_stamp={
                    "db_mtime_ns": 0,
                    "wal_mtime_ns": 0,
                    "db_size": 0,
                    "wal_size": 0,
                },
                events=[],
                messages=[],
                cursor=None,
                now=900,
            )
            watcher = LaunchOpsWatcher(store, adapter)
            result = watcher.scan_once(now=1000)

        self.assertEqual(1, adapter.refresh_calls[source.source_shard])
        self.assertEqual(1, adapter.read_calls[source.source_shard])
        self.assertEqual(1, result.scanned_sources)

    def test_raw_stamp_change_without_table_head_change_skips_message_read(self):
        source = self.make_source()
        adapter = FakeSourceAdapter(
            [GroupSource(source.room_id, "测试群")],
            {source.room_id: [source]},
            {source.source_shard: [self.make_row()]},
        )
        with tempfile.TemporaryDirectory() as tmp:
            store = LaunchOpsStore(Path(tmp) / "launch.db")
            watcher = LaunchOpsWatcher(store, adapter)
            watcher.scan_once(now=1000)
            self.assertEqual(1, adapter.read_calls[source.source_shard])

            adapter.stamps[source.source_shard] = SourceStamp(2, 3, 11, 12)
            result = watcher.scan_once(now=1003)

            self.assertEqual(1, adapter.read_calls[source.source_shard])
            self.assertEqual(1, result.skipped_sources)
            self.assertEqual(2, adapter.head_calls[source.source_shard])

    def test_raw_stamp_and_table_head_change_still_reads_new_rows(self):
        source = self.make_source()
        adapter = FakeSourceAdapter(
            [GroupSource(source.room_id, "测试群")],
            {source.room_id: [source]},
            {source.source_shard: [self.make_row()]},
        )
        with tempfile.TemporaryDirectory() as tmp:
            store = LaunchOpsStore(Path(tmp) / "launch.db")
            watcher = LaunchOpsWatcher(store, adapter)
            watcher.scan_once(now=1000)
            adapter.rows_by_shard[source.source_shard].append(
                self.make_row(local_id=2, server_id=102, create_time=101)
            )
            adapter.stamps[source.source_shard] = SourceStamp(2, 3, 11, 12)
            result = watcher.scan_once(now=1003)

            self.assertEqual(2, adapter.read_calls[source.source_shard])
            self.assertEqual(1, result.scanned_sources)
            self.assertEqual(
                2, len(store.list_events(event_type="message.created"))
            )

    def test_only_changed_shard_is_refreshed_on_next_scan(self):
        source0 = self.make_source("message/message_0.db", "room0@chatroom")
        source1 = self.make_source("message/message_1.db", "room1@chatroom")
        adapter = FakeSourceAdapter(
            [GroupSource(source0.room_id, "群0"), GroupSource(source1.room_id, "群1")],
            {source0.room_id: [source0], source1.room_id: [source1]},
        )
        with tempfile.TemporaryDirectory() as tmp:
            watcher = LaunchOpsWatcher(
                LaunchOpsStore(Path(tmp) / "launch.db"), adapter
            )
            watcher.scan_once(now=1000)
            adapter.rows_by_shard[source0.source_shard] = [
                self.make_row(local_id=1, server_id=201, create_time=101)
            ]
            adapter.stamps[source0.source_shard] = SourceStamp(1, 2, 10, 5)
            watcher.scan_once(now=1003)

        self.assertEqual(2, adapter.refresh_calls[source0.source_shard])
        self.assertEqual(1, adapter.refresh_calls[source1.source_shard])

    def test_same_second_rows_both_survive(self):
        source = self.make_source()
        rows = [
            self.make_row(local_id=1, server_id=101, create_time=100),
            self.make_row(local_id=2, server_id=102, create_time=100),
        ]
        adapter = FakeSourceAdapter(
            [GroupSource(source.room_id, "测试群")],
            {source.room_id: [source]},
            {source.source_shard: rows},
        )
        with tempfile.TemporaryDirectory() as tmp:
            store = LaunchOpsStore(Path(tmp) / "launch.db")
            watcher = LaunchOpsWatcher(store, adapter)
            watcher.scan_once(now=1000)
            cursor = store.get_cursor(source.source_shard, source.table_name)

            self.assertEqual(
                2, len(store.list_events(event_type="message.created"))
            )
            self.assertEqual(100, cursor["create_time"])
            self.assertEqual(2, cursor["local_id"])

    def test_invite_backfill_is_bounded_idempotent_and_cursor_neutral(self):
        source = self.make_source()
        qr_xml = """<sysmsg type="sysmsgtemplate"><sysmsgtemplate>
        <content_template type="tmpl_type_profile">
          <template><![CDATA["$adder$"通过扫描"$from$"分享的二维码加入群聊]]></template>
          <link_list>
            <link name="adder" type="link_profile"><memberlist><member>
              <username>wxid_invitee</username><nickname>诚美</nickname>
            </member></memberlist></link>
            <link name="from" type="link_profile"><memberlist><member>
              <username>wxid_inviter</username><nickname>清柠</nickname>
            </member></memberlist></link>
          </link_list>
        </content_template></sysmsgtemplate></sysmsg>"""
        rows = [
            self.make_row(1, 701, 90, "outside-before"),
            RawMessageRow(2, 702, 10000, 100, 0, qr_xml, 0),
            self.make_row(3, 703, 110, "outside-after"),
        ]
        adapter = FakeSourceAdapter(
            [GroupSource(source.room_id, "测试群")],
            {source.room_id: [source]},
            {source.source_shard: rows},
        )
        with tempfile.TemporaryDirectory() as tmp:
            store = LaunchOpsStore(Path(tmp) / "launch.db")
            store.set_cursor(
                source.source_shard,
                source.table_name,
                create_time=110,
                local_id=3,
                source_identity="server:703",
            )
            store.apply_source_batch(
                source_shard=source.source_shard,
                source_table=source.table_name,
                source_stamp={
                    "db_mtime_ns": 1,
                    "wal_mtime_ns": 0,
                    "db_size": 10,
                    "wal_size": 0,
                },
                table_head={"local_id": 3, "create_time": 110},
                events=[],
                messages=[],
                cursor=None,
                now=150,
            )
            cursor_before = store.get_cursor(source.source_shard, source.table_name)
            stamp_before = store.get_source_stamp(source.source_shard, source.table_name)
            head_before = store.get_table_head(source.source_shard, source.table_name)
            store.upsert_room(source.room_id, "测试群", now=150)
            store.ensure_trace_managed_room(source.room_id, source="manual", now=150)
            observation = store.save_trace_membership_observation(
                room_id=source.room_id,
                source_kind="contact",
                source_state_key="backfill-current",
                semantic_hash="backfill-current",
                membership_status="complete",
                inviter_metadata_status="unavailable",
                members=[{"username": "wxid_invitee"}],
                observed_at=150,
            )
            watcher = LaunchOpsWatcher(store, adapter)
            watcher.archive_reconciler.reconcile_room(
                source.room_id,
                trigger="membership",
                observation_id=observation["observation_id"],
                now=150,
            )

            first = watcher.backfill_invites(
                [source.room_id], start_time=95, end_time=105, now=200
            )
            cursor_after_first = store.get_cursor(source.source_shard, source.table_name)
            stamp_after_first = store.get_source_stamp(source.source_shard, source.table_name)
            head_after_first = store.get_table_head(source.source_shard, source.table_name)
            second = watcher.backfill_invites(
                [source.room_id], start_time=95, end_time=105, now=201
            )
            cursor_after_second = store.get_cursor(source.source_shard, source.table_name)
            stamp_after_second = store.get_source_stamp(source.source_shard, source.table_name)
            head_after_second = store.get_table_head(source.source_shard, source.table_name)

            invited = store.list_events(event_type="member.invited")
            joined = store.list_events(event_type="member.joined")
            relation = store.get_active_trace_relation_for_username(
                source.room_id, "wxid_invitee"
            )
            evidence = [
                row for row in store.list_trace_evidence_for_room(source.room_id)
                if row["source_type"] == "system_message"
                and row["source_identity"] == "invite:server:702:0"
            ]
            pending = store.list_pending_trace_reconcile(source.room_id)

        self.assertEqual(1, first["inserted_invite_events"])
        self.assertEqual(1, first["inserted_join_events"])
        self.assertEqual(0, second["inserted_invite_events"])
        self.assertEqual(0, second["inserted_join_events"])
        self.assertEqual(cursor_before, cursor_after_first)
        self.assertEqual(cursor_before, cursor_after_second)
        self.assertEqual(stamp_before, stamp_after_first)
        self.assertEqual(stamp_before, stamp_after_second)
        self.assertEqual(head_before, head_after_first)
        self.assertEqual(head_before, head_after_second)
        self.assertEqual(["invite:server:702:0"], [item["event_id"] for item in invited])
        self.assertEqual(["join:server:702:0"], [item["event_id"] for item in joined])
        self.assertEqual("wxid_inviter", relation["inviter_username"])
        self.assertEqual([], pending)
        self.assertEqual(1, len(evidence))

    def test_restart_after_source_batch_failure_replays_without_duplicate_event(self):
        from unittest.mock import Mock
        from wechat_cli.launch_ops.source import MembershipObservation

        source = self.make_source()
        qr_xml = """<sysmsg type="sysmsgtemplate"><sysmsgtemplate>
        <content_template type="tmpl_type_profile">
          <template><![CDATA["$adder$"通过扫描"$from$"分享的二维码加入群聊]]></template>
          <link_list>
            <link name="adder" type="link_profile"><memberlist><member>
              <username>wxid_invitee</username><nickname>诚美</nickname>
            </member></memberlist></link>
            <link name="from" type="link_profile"><memberlist><member>
              <username>wxid_inviter</username><nickname>清柠</nickname>
            </member></memberlist></link>
          </link_list>
        </content_template></sysmsgtemplate></sysmsg>"""
        row = RawMessageRow(1, 702, 10000, 100, 0, qr_xml, 0)
        adapter = FakeSourceAdapter(
            [GroupSource(source.room_id, "测试群")],
            {source.room_id: [source]},
            {source.source_shard: [row]},
        )
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "launch.db"
            store = FailBatchOnceStore(path)
            store.upsert_room(source.room_id, "测试群", now=900)
            store.ensure_trace_managed_room(source.room_id, source="manual", now=900)
            store.save_trace_membership_observation(
                room_id=source.room_id,
                source_kind="contact",
                source_state_key="restart-current",
                semantic_hash="restart-current",
                membership_status="complete",
                inviter_metadata_status="unavailable",
                members=[{"username": "wxid_invitee"}],
                observed_at=900,
            )
            watcher = LaunchOpsWatcher(store, adapter)
            with self.assertRaisesRegex(RuntimeError, "source batch failed"):
                watcher.scan_once(now=1000)
            self.assertEqual(0, len(store.list_events(event_type="member.invited")))
            self.assertEqual([], store.list_trace_evidence_for_room(source.room_id))
            self.assertIsNone(store.get_cursor(source.source_shard, source.table_name))

            adapter2 = FakeSourceAdapter(
                [GroupSource(source.room_id, "测试群")],
                {source.room_id: [source]},
                {source.source_shard: [row]},
            )
            adapter2.membership_observation = Mock(
                return_value=MembershipObservation(
                    room_id=source.room_id,
                    observed_at=1003,
                    source_state_key="restart-current",
                    semantic_hash="restart-current",
                    membership_status="complete",
                    inviter_metadata_status="unavailable",
                    members=({"username": "wxid_invitee"},),
                    diagnostic_code="",
                )
            )
            restarted = LaunchOpsWatcher(LaunchOpsStore(path), adapter2)
            restarted.scan_once(now=1003)

            self.assertEqual(
                1,
                len(restarted.store.list_events(event_type="member.invited")),
            )
            system_evidence = [
                item
                for item in restarted.store.list_trace_evidence_for_room(source.room_id)
                if item["source_type"] == "system_message"
            ]
            self.assertEqual(2, len(system_evidence))
            self.assertEqual(
                1,
                len([
                    item for item in system_evidence
                    if item["source_identity"] == "invite:server:702:0"
                ]),
            )
            self.assertEqual(
                1,
                len([
                    item for item in system_evidence
                    if item["source_identity"] == "join:server:702:0"
                ]),
            )
            self.assertEqual([], restarted.store.list_pending_trace_reconcile(source.room_id))
            self.assertIsNotNone(
                restarted.store.get_cursor(source.source_shard, source.table_name)
            )

    def test_first_discovery_and_changed_membership_emit_lightweight_events(self):
        source = self.make_source()
        adapter = FakeSourceAdapter(
            [GroupSource(source.room_id, "测试群")],
            {source.room_id: [source]},
        )
        adapter.members = lambda group: [{"username": "u1"}, {"username": "u2"}]
        with tempfile.TemporaryDirectory() as tmp:
            store = LaunchOpsStore(Path(tmp) / "launch.db")
            watcher = LaunchOpsWatcher(store, adapter)
            watcher.scan_once(now=1000)
            watcher.scan_once(now=1003)
            adapter.members = lambda group: [
                {"username": "u1"},
                {"username": "u2"},
                {"username": "u3"},
            ]
            watcher.scan_once(now=1006)

            discovered = store.list_events(event_type="room.discovered")
            snapshots = store.list_events(event_type="room.membership_snapshot")
            health = store.list_events(event_type="watcher.health")

        self.assertEqual(1, len(discovered))
        self.assertEqual(2, len(snapshots))
        self.assertEqual([2, 3], sorted(item["payload"]["member_count"] for item in snapshots))
        self.assertEqual(1, len([item for item in health if item["payload"]["status"] == "running"]))

    def test_inviter_metadata_change_emits_new_membership_snapshot(self):
        source = self.make_source()
        adapter = FakeSourceAdapter(
            [GroupSource(source.room_id, "测试群")],
            {source.room_id: [source]},
        )
        current = [
            {"username": "u1", "inviter_username": "owner"},
            {"username": "u2", "inviter_username": "u1"},
        ]
        adapter.members = lambda group: [dict(member) for member in current]
        with tempfile.TemporaryDirectory() as tmp:
            store = CountingRoomStateStore(Path(tmp) / "launch.db")
            watcher = LaunchOpsWatcher(store, adapter)
            watcher.scan_once(now=1000)
            current[1]["inviter_username"] = "owner"
            watcher.scan_once(now=1003)
            watcher.scan_once(now=1006)

            snapshots = store.list_events(event_type="room.membership_snapshot")
            latest = store.latest_membership_snapshot(source.room_id)

        self.assertEqual(2, len(snapshots))
        self.assertEqual(2, store.snapshot_saves)
        by_username = {member["username"]: member for member in latest["members"]}
        self.assertEqual("owner", by_username["u2"]["inviter_username"])

    def test_scan_persists_room_avatar_for_store_only_viewer(self):
        source = self.make_source()
        adapter = FakeSourceAdapter(
            [
                GroupSource(
                    source.room_id,
                    "测试群",
                    avatar_url="https://wx.qlogo.cn/room-avatar",
                )
            ],
            {source.room_id: [source]},
        )
        with tempfile.TemporaryDirectory() as tmp:
            store = LaunchOpsStore(Path(tmp) / "launch.db")
            watcher = LaunchOpsWatcher(store, adapter)
            watcher.scan_once(now=1000)

            profile = store.get_room_profile(source.room_id)

        self.assertIsNotNone(profile)
        self.assertEqual("https://wx.qlogo.cn/room-avatar", profile["avatar_url"])
        self.assertEqual(1000, profile["observed_at"])

    def test_health_event_records_error_transition_once(self):
        source = self.make_source()
        adapter = FakeSourceAdapter(
            [GroupSource(source.room_id, "测试群")],
            {source.room_id: [source]},
        )
        with tempfile.TemporaryDirectory() as tmp:
            store = LaunchOpsStore(Path(tmp) / "launch.db")
            watcher = LaunchOpsWatcher(store, adapter)
            watcher.scan_once(now=1000)
            adapter.fail_discovery = FileNotFoundError("wechat db missing")
            for now in (1003, 1006):
                with self.assertRaises(FileNotFoundError):
                    watcher.scan_once(now=now)

            events = store.list_events(event_type="watcher.health")

        states = [item["payload"]["status"] for item in reversed(events)]
        self.assertEqual(["running", "error"], states)

    def test_health_meta_persists_full_snapshot_without_repeating_running_event(self):
        source = self.make_source()
        adapter = FakeSourceAdapter(
            [GroupSource(source.room_id, "测试群")],
            {source.room_id: [source]},
        )
        with tempfile.TemporaryDirectory() as tmp:
            store = LaunchOpsStore(Path(tmp) / "launch.db")
            watcher = LaunchOpsWatcher(store, adapter)
            watcher.scan_once(now=1000)
            watcher.scan_once(now=1003)

            raw = store.get_meta("watcher_health_state")
            health_events = store.list_events(event_type="watcher.health")

        payload = json.loads(raw)
        self.assertEqual("running", payload["status"])
        self.assertEqual(1003, payload["last_attempt_at"])
        self.assertEqual(1003, payload["last_success_at"])
        self.assertIsNone(payload["last_event_at"])
        self.assertEqual("", payload["error_code"])
        self.assertEqual("", payload["error_message"])
        self.assertEqual(
            1,
            len(
                [
                    item
                    for item in health_events
                    if item["payload"]["status"] == "running"
                ]
            ),
        )

    def test_member_snapshot_updates_current_room_member_count(self):
        source = self.make_source()
        adapter = FakeSourceAdapter(
            [GroupSource(source.room_id, "测试群")],
            {source.room_id: [source]},
        )
        adapter.members = lambda group: [
            {"username": "u1"},
            {"username": "u2"},
            {"username": "u3"},
        ]
        with tempfile.TemporaryDirectory() as tmp:
            store = LaunchOpsStore(Path(tmp) / "launch.db")
            watcher = LaunchOpsWatcher(store, adapter)
            watcher.scan_once(now=1000)

            rooms = store.list_rooms()

        self.assertEqual(3, rooms[0]["member_count"])

    @patch("wechat_cli.launch_ops.watcher.time.time", side_effect=[1000, 1020])
    def test_real_clock_health_uses_scan_completion_time(self, _time):
        adapter = FakeSourceAdapter([], {})
        with tempfile.TemporaryDirectory() as tmp:
            watcher = LaunchOpsWatcher(
                LaunchOpsStore(Path(tmp) / "launch.db"), adapter
            )
            watcher.scan_once()
            health = watcher.health(now=1021)

        self.assertEqual(1020, health.last_success_at)
        self.assertFalse(health.stale)
        self.assertEqual("running", health.status)

    def test_background_health_reports_scanning_before_first_scan_completes(self):
        entered = threading.Event()
        release = threading.Event()

        class BlockingAdapter(FakeSourceAdapter):
            def discover_groups(self):
                entered.set()
                release.wait(1.0)
                return []

        adapter = BlockingAdapter([], {})
        with tempfile.TemporaryDirectory() as tmp:
            watcher = LaunchOpsWatcher(
                LaunchOpsStore(Path(tmp) / "launch.db"), adapter
            )
            watcher.start()
            self.assertTrue(entered.wait(0.5))
            health = watcher.health()
            release.set()
            watcher.stop(timeout=2.0)

        self.assertEqual("scanning", health.status)
        self.assertIsNotNone(health.last_attempt_at)
        self.assertIsNone(health.last_success_at)

    def test_concurrent_scan_requests_are_serialized(self):
        class ProbeAdapter(FakeSourceAdapter):
            def __init__(self):
                super().__init__([], {})
                self.entered = threading.Event()
                self.release = threading.Event()
                self.guard = threading.Lock()
                self.active = 0
                self.max_active = 0
                self.calls = 0

            def discover_groups(self):
                with self.guard:
                    self.calls += 1
                    self.active += 1
                    self.max_active = max(self.max_active, self.active)
                    call_number = self.calls
                try:
                    if call_number == 1:
                        self.entered.set()
                        self.release.wait(1.0)
                    return []
                finally:
                    with self.guard:
                        self.active -= 1

        adapter = ProbeAdapter()
        with tempfile.TemporaryDirectory() as tmp:
            watcher = LaunchOpsWatcher(
                LaunchOpsStore(Path(tmp) / "launch.db"), adapter
            )
            first = threading.Thread(target=watcher.scan_once)
            second_done = threading.Event()

            def second_scan():
                watcher.scan_once()
                second_done.set()

            second = threading.Thread(target=second_scan)
            first.start()
            self.assertTrue(adapter.entered.wait(0.5))
            second.start()
            time.sleep(0.05)
            max_active_during_overlap = adapter.max_active
            second_finished_early = second_done.is_set()

            adapter.release.set()
            first.join(1.0)
            second.join(1.0)

        self.assertEqual(1, max_active_during_overlap)
        self.assertFalse(second_finished_early)
        self.assertFalse(first.is_alive())
        self.assertFalse(second.is_alive())
        self.assertEqual(2, adapter.calls)
        self.assertEqual(1, adapter.max_active)

    def test_unchanged_second_scan_avoids_rewriting_room_membership_state(self):
        source = self.make_source()
        adapter = FakeSourceAdapter(
            [GroupSource(source.room_id, "测试群")],
            {source.room_id: [source]},
        )
        adapter.members = lambda group: [
            {"username": "u1"},
            {"username": "u2"},
        ]
        with tempfile.TemporaryDirectory() as tmp:
            store = CountingRoomStateStore(Path(tmp) / "launch.db")
            watcher = LaunchOpsWatcher(store, adapter)
            watcher.scan_once(now=1000)
            after_first = (
                store.room_upserts,
                store.snapshot_saves,
                store.membership_meta_reads,
            )
            watcher.scan_once(now=1003)
            after_second = (
                store.room_upserts,
                store.snapshot_saves,
                store.membership_meta_reads,
            )

        self.assertEqual(after_first, after_second)

    def test_health_reports_failure_and_staleness(self):
        source = self.make_source()
        adapter = FakeSourceAdapter(
            [GroupSource(source.room_id, "测试群")],
            {source.room_id: [source]},
        )
        with tempfile.TemporaryDirectory() as tmp:
            watcher = LaunchOpsWatcher(
                LaunchOpsStore(Path(tmp) / "launch.db"), adapter
            )
            watcher.scan_once(now=1000)
            healthy = watcher.health(now=1003)
            stale = watcher.health(now=1016)
            adapter.fail_discovery = FileNotFoundError("wechat db missing")
            with self.assertRaises(FileNotFoundError):
                watcher.scan_once(now=1020)
            failed = watcher.health(now=1020)

        self.assertEqual("running", healthy.status)
        self.assertFalse(healthy.stale)
        self.assertTrue(stale.stale)
        self.assertEqual("wechat_data_unavailable", failed.error_code)
        self.assertEqual(1000, failed.last_success_at)

    def test_background_start_and_stop_are_bounded(self):
        source = self.make_source()
        adapter = FakeSourceAdapter(
            [GroupSource(source.room_id, "测试群")],
            {source.room_id: [source]},
        )
        with tempfile.TemporaryDirectory() as tmp:
            watcher = LaunchOpsWatcher(
                LaunchOpsStore(Path(tmp) / "launch.db"),
                adapter,
                scan_interval_seconds=0.01,
            )
            watcher.start()
            time.sleep(0.04)
            watcher.stop(timeout=1.0)

        self.assertFalse(watcher.is_running)
        self.assertGreaterEqual(adapter.read_calls[source.source_shard], 1)


    def test_force_refresh_attempts_fresh_contact_observation_and_drains_target_room(self):
        from wechat_cli.launch_ops.source import MembershipObservation

        with tempfile.TemporaryDirectory() as tmp:
            store = LaunchOpsStore(Path(tmp) / "launch.db")
            store.upsert_room("r1", "Room", now=1)
            store.ensure_trace_managed_room("r1", source="manual", now=1)
            source = Mock()
            source.discover_groups.return_value = [GroupSource("r1", "Room")]
            source.membership_observation.return_value = MembershipObservation(
                room_id="r1",
                observed_at=100,
                source_state_key="state-a",
                semantic_hash="sem-a",
                membership_status="complete",
                inviter_metadata_status="complete",
                members=({"username": "wxid_b"},),
                diagnostic_code="",
            )
            watcher = LaunchOpsWatcher(store, source)

            result = watcher.force_refresh_room("r1", now=100)

            self.assertTrue(result["reconciled"])
            self.assertEqual(source.membership_observation.call_count, 1)
            self.assertTrue(source.membership_observation.call_args.kwargs["force"])
            self.assertEqual(store.list_pending_trace_reconcile("r1"), [])

    def test_reconcile_queue_is_acknowledged_only_after_success(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = LaunchOpsStore(Path(tmp) / "launch.db")
            store.upsert_room("r1", "Room", now=1)
            store.ensure_trace_managed_room("r1", source="manual", now=1)
            observation = store.save_trace_membership_observation(
                room_id="r1", source_kind="contact",
                source_state_key="state-a", semantic_hash="sem-a",
                membership_status="complete", inviter_metadata_status="complete",
                members=[{"username": "wxid_b"}], observed_at=90,
            )
            watcher = LaunchOpsWatcher(store, Mock())
            watcher._drain_trace_reconcile_queue(now=91, room_id="r1")
            row = store.enqueue_trace_reconcile(
                "r1", "manual", "manual:test", now=100
            )
            watcher.archive_reconciler.reconcile_room = Mock(
                side_effect=RuntimeError("boom")
            )

            with self.assertRaises(RuntimeError):
                watcher._drain_trace_reconcile_queue(now=101, room_id="r1")
            self.assertEqual(
                [item["queue_id"] for item in store.list_pending_trace_reconcile("r1")],
                [row["queue_id"]],
            )

            watcher.archive_reconciler.reconcile_room = Mock(
                return_value={"room_id": "r1"}
            )
            store.mark_trace_room_projection_ready(
                "r1", observation["observation_id"], now=102
            )
            watcher._drain_trace_reconcile_queue(now=102, room_id="r1")
            self.assertEqual(store.list_pending_trace_reconcile("r1"), [])

    def test_stopped_room_pending_queue_is_fenced_and_reenable_resumes_drain(self):
        from unittest.mock import Mock
        from wechat_cli.launch_ops.models import StandardEvent

        with tempfile.TemporaryDirectory() as tmp:
            store = LaunchOpsStore(Path(tmp) / "launch.db")
            store.upsert_room("r1", "Room", now=1)
            store.ensure_trace_managed_room("r1", source="manual", now=1)
            complete = store.save_trace_membership_observation(
                room_id="r1",
                source_kind="contact",
                source_state_key="state-a",
                semantic_hash="sem-a",
                membership_status="complete",
                inviter_metadata_status="complete",
                members=[{"username": "wxid_b"}],
                observed_at=5,
            )
            for row in store.list_pending_trace_reconcile("r1"):
                store.ack_trace_reconcile(row["queue_id"], now=6)
            store.mark_trace_room_projection_ready(
                "r1", complete["observation_id"], now=6
            )
            queued = store.enqueue_trace_reconcile(
                "r1", "evidence", "event:before-stop", now=10
            )
            store.set_trace_managed_room_enabled("r1", False, source="manual", now=20)

            source = Mock()
            source.discover_groups.return_value = []
            watcher = LaunchOpsWatcher(store, source)
            watcher.archive_reconciler.reconcile_room = Mock()

            watcher.scan_once(now=30)
            watcher.archive_reconciler.reconcile_room.assert_not_called()
            self.assertEqual(
                [row["queue_id"] for row in store.list_pending_trace_reconcile("r1")],
                [queued["queue_id"]],
            )

            store.set_trace_managed_room_enabled("r1", True, source="manual", now=40)
            complete_again = store.save_trace_membership_observation(
                room_id="r1",
                source_kind="contact",
                source_state_key="state-a",
                semantic_hash="sem-a",
                membership_status="complete",
                inviter_metadata_status="complete",
                members=[{"username": "wxid_b"}],
                observed_at=40,
            )
            self.assertEqual(
                complete_again["observation_id"], complete["observation_id"]
            )
            store.insert_event(
                StandardEvent(
                    event_id="after-reenable",
                    event_type="member.invited",
                    room_id="r1",
                    event_time=41,
                    source_shard="message/message_0.db",
                    source_table="Msg_test",
                    source_identity="after-reenable",
                    actor_id="wxid_a",
                    subject_id="wxid_b",
                    payload={"method": "direct"},
                ),
                now=41,
            )

            def reconcile_with_basis(
                room_id, *, trigger, observation_id=None, now
            ):
                self.assertEqual(observation_id, complete["observation_id"])
                if trigger == "membership":
                    store.mark_trace_room_projection_ready(
                        room_id, observation_id, now=now
                    )
                return {"room_id": room_id}

            watcher.archive_reconciler.reconcile_room = Mock(
                side_effect=reconcile_with_basis
            )
            watcher.scan_once(now=42)

            self.assertEqual(watcher.archive_reconciler.reconcile_room.call_count, 3)
            self.assertEqual(store.list_pending_trace_reconcile("r1"), [])

    def test_ordinary_scan_persists_managed_v2_observations_without_force_refresh(self):
        from wechat_cli.launch_ops.source import MembershipObservation

        group = GroupSource("r1", "Room")
        adapter = FakeSourceAdapter([group], {"r1": []})
        adapter.membership_observation = Mock(
            side_effect=[
                MembershipObservation(
                    room_id="r1", observed_at=100,
                    source_state_key="state-a", semantic_hash="sem-a",
                    membership_status="complete",
                    inviter_metadata_status="complete",
                    members=({"username": "wxid_b", "inviter_username": "wxid_a"},),
                    diagnostic_code="",
                ),
                MembershipObservation(
                    room_id="r1", observed_at=101,
                    source_state_key="state-a", semantic_hash="sem-a",
                    membership_status="complete",
                    inviter_metadata_status="complete",
                    members=({"username": "wxid_b", "inviter_username": "wxid_a"},),
                    diagnostic_code="",
                ),
                MembershipObservation(
                    room_id="r1", observed_at=102,
                    source_state_key="state-b", semantic_hash="sem-b",
                    membership_status="complete",
                    inviter_metadata_status="complete",
                    members=({"username": "wxid_b", "inviter_username": "wxid_c"},),
                    diagnostic_code="",
                ),
            ]
        )

        with tempfile.TemporaryDirectory() as tmp:
            store = LaunchOpsStore(Path(tmp) / "launch.db")
            store.upsert_room("r1", "Room", now=1)
            store.ensure_trace_managed_room("r1", source="manual", now=1)
            watcher = LaunchOpsWatcher(store, adapter)

            watcher.scan_once(now=100)
            watcher.scan_once(now=101)
            watcher.scan_once(now=102)

            observations = store.list_trace_membership_observations("r1")
            self.assertEqual(
                {row["semantic_hash"] for row in observations},
                {"sem-a", "sem-b"},
            )
            self.assertEqual(adapter.membership_observation.call_count, 3)
            self.assertEqual(store.list_pending_trace_reconcile("r1"), [])
            status = store.get_trace_room_projection_status("r1")
            self.assertEqual(status["projection_state"], "ready")

    def test_ordinary_scan_does_not_create_archive_observation_for_unmanaged_or_stopped_room(self):
        for stopped in (False, True):
            with self.subTest(stopped=stopped), tempfile.TemporaryDirectory() as tmp:
                group = GroupSource("r1", "Room")
                adapter = FakeSourceAdapter([group], {"r1": []})
                adapter.membership_observation = Mock()
                store = LaunchOpsStore(Path(tmp) / "launch.db")
                store.upsert_room("r1", "Room", now=1)
                if stopped:
                    store.ensure_trace_managed_room("r1", source="manual", now=1)
                    store.set_trace_managed_room_enabled(
                        "r1", False, source="manual", now=2
                    )
                watcher = LaunchOpsWatcher(store, adapter)

                watcher.scan_once(now=100)

                adapter.membership_observation.assert_not_called()
                self.assertEqual(store.list_trace_membership_observations("r1"), [])
                self.assertIsNotNone(store.latest_membership_snapshot("r1"))

    def test_stop_between_precheck_and_reconcile_never_acks_queue_and_reenable_resumes(self):
        for reason in ("membership", "evidence", "manual", "force_refresh"):
            with self.subTest(reason=reason), tempfile.TemporaryDirectory() as tmp:
                store = LaunchOpsStore(Path(tmp) / "launch.db")
                store.upsert_room("r1", "Room", now=1)
                store.ensure_trace_managed_room("r1", source="manual", now=1)
                observation = store.save_trace_membership_observation(
                    room_id="r1", source_kind="contact",
                    source_state_key="state-a", semantic_hash="sem-a",
                    membership_status="complete", inviter_metadata_status="complete",
                    members=[{"username": "wxid_b"}], observed_at=10,
                )
                watcher = LaunchOpsWatcher(store, Mock())
                membership_row = next(
                    row for row in store.list_pending_trace_reconcile("r1")
                    if row["reason"] == "membership"
                )
                if reason == "membership":
                    queued = membership_row
                else:
                    watcher.archive_reconciler.reconcile_room = Mock(
                        return_value={"room_id": "r1"}
                    )
                    watcher._drain_trace_reconcile_queue(now=11, room_id="r1")
                    store.mark_trace_room_projection_ready(
                        "r1", observation["observation_id"], now=12
                    )
                    queued = store.enqueue_trace_reconcile(
                        "r1", reason, f"{reason}:race", now=13
                    )

                def stop_during_reconcile(*args, **kwargs):
                    store.set_trace_managed_room_enabled(
                        "r1", False, source="manual", now=20
                    )
                    return {"room_id": "r1", "skipped": "archive_disabled"}

                watcher.archive_reconciler.reconcile_room = Mock(
                    side_effect=stop_during_reconcile
                )
                watcher._drain_trace_reconcile_queue(now=20, room_id="r1")
                self.assertEqual(
                    [row["queue_id"] for row in store.list_pending_trace_reconcile("r1")],
                    [queued["queue_id"]],
                )

                store.set_trace_managed_room_enabled(
                    "r1", True, source="manual", now=30
                )
                if reason != "membership":
                    store.mark_trace_room_projection_ready(
                        "r1", observation["observation_id"], now=31
                    )
                watcher.archive_reconciler.reconcile_room = Mock(
                    return_value={"room_id": "r1"}
                )
                watcher._drain_trace_reconcile_queue(now=32, room_id="r1")
                self.assertEqual(store.list_pending_trace_reconcile("r1"), [])


if __name__ == "__main__":
    unittest.main()
