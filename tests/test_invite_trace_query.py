import hashlib
import sqlite3
import tempfile
import unittest
from contextlib import closing
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from wechat_cli.core.invite_trace import (
    InviteTraceQueryError,
    InviteTraceQueryService,
    InviteTraceRelation,
    TraceArchiveReader,
    aggregate_invite_stats,
    build_invite_trace_result,
    merge_live_relations,
)


MIN_ARCHIVE_SCHEMA = """
CREATE TABLE meta(key TEXT PRIMARY KEY, value TEXT NOT NULL);
CREATE TABLE trace_membership_observations(
    observation_id TEXT PRIMARY KEY,
    room_id TEXT NOT NULL,
    source_kind TEXT NOT NULL,
    membership_status TEXT NOT NULL,
    inviter_metadata_status TEXT NOT NULL
);
CREATE TABLE trace_managed_rooms(
    room_id TEXT PRIMARY KEY,
    archiving_enabled INTEGER NOT NULL,
    projection_state TEXT NOT NULL,
    current_observation_id TEXT,
    last_refresh_at INTEGER,
    last_success_at INTEGER,
    last_reconciled_at INTEGER,
    last_error_code TEXT NOT NULL DEFAULT '',
    last_error_message TEXT NOT NULL DEFAULT ''
);
CREATE TABLE trace_people(
    person_id TEXT PRIMARY KEY,
    wechat_username TEXT NOT NULL,
    current_nick_name TEXT NOT NULL DEFAULT '',
    current_remark TEXT NOT NULL DEFAULT ''
);
CREATE TABLE trace_membership_episodes(
    episode_id TEXT PRIMARY KEY,
    room_id TEXT NOT NULL,
    person_id TEXT NOT NULL,
    episode_index INTEGER NOT NULL,
    joined_event_at INTEGER,
    left_event_at INTEGER,
    inactive_observed_at INTEGER,
    status TEXT NOT NULL
);
CREATE TABLE trace_invite_relations(
    relation_id TEXT PRIMARY KEY,
    room_id TEXT NOT NULL,
    episode_id TEXT NOT NULL,
    invitee_person_id TEXT NOT NULL,
    inviter_person_id TEXT,
    active INTEGER NOT NULL,
    resolved_method TEXT,
    resolved_event_time INTEGER,
    resolution_state TEXT NOT NULL
);
CREATE TABLE trace_relation_evidence(
    evidence_id TEXT PRIMARY KEY,
    room_id TEXT NOT NULL,
    episode_id TEXT,
    attachment_state TEXT NOT NULL,
    source_type TEXT NOT NULL,
    observed_at INTEGER NOT NULL,
    event_time INTEGER,
    authority_level INTEGER NOT NULL,
    conflicts_with_resolution INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE trace_conflicts(
    conflict_id TEXT PRIMARY KEY,
    room_id TEXT NOT NULL,
    episode_id TEXT,
    conflict_type TEXT NOT NULL,
    resolved_at INTEGER
);
"""


def sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


class TraceArchiveReaderTests(unittest.TestCase):
    def _ready_db(self, path: Path) -> None:
        with closing(sqlite3.connect(path)) as conn:
            conn.executescript(MIN_ARCHIVE_SCHEMA)
            conn.execute(
                "INSERT INTO meta(key,value) VALUES(?,?)",
                ("trace_archive_migration_checkpoint_v2", "ready"),
            )
            conn.execute(
                "INSERT INTO trace_membership_observations VALUES(?,?,?,?,?)",
                ("obs1", "r1", "contact", "complete", "complete"),
            )
            conn.execute(
                "INSERT INTO trace_managed_rooms VALUES(?,?,?,?,?,?,?,?,?)",
                ("r1", 1, "ready", "obs1", 10, 10, 10, "", ""),
            )
            conn.execute(
                "INSERT INTO trace_people VALUES(?,?,?,?)",
                ("p1", "wxid_b", "乙", ""),
            )
            conn.execute(
                "INSERT INTO trace_people VALUES(?,?,?,?)",
                ("p2", "wxid_a", "甲", ""),
            )
            conn.execute(
                "INSERT INTO trace_membership_episodes VALUES(?,?,?,?,?,?,?,?)",
                ("ep1", "r1", "p1", 1, 100, 200, 200, "inactive"),
            )
            conn.execute(
                "INSERT INTO trace_invite_relations VALUES(?,?,?,?,?,?,?,?,?)",
                ("rel1", "r1", "ep1", "p1", "p2", 1, "direct", 100, "resolved"),
            )
            conn.execute(
                "INSERT INTO trace_relation_evidence VALUES(?,?,?,?,?,?,?,?,?)",
                ("ev1", "r1", "ep1", "attached", "system_message", 100, 100, 90, 0),
            )
            conn.commit()

    def test_ready_projection_and_departed_relation_are_read_without_mutation(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "launch.db"
            self._ready_db(path)
            before = sha256(path)

            reader = TraceArchiveReader(path)
            status = reader.projection_status("r1")
            rows = reader.read_relations("r1")
            after = sha256(path)

        self.assertTrue(status["use_archive_current_projection"])
        self.assertEqual(rows[0].membership_state, "departed")
        self.assertEqual(rows[0].inviter_username, "wxid_a")
        self.assertEqual(rows[0].invitee_username, "wxid_b")
        self.assertEqual(rows[0].event_time, 100)
        self.assertEqual(rows[0].evidence_source, "system_message")
        self.assertEqual(before, after)

    def test_missing_archive_is_unavailable_without_creating_file(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "missing.db"
            status = TraceArchiveReader(path).projection_status("r1")
            self.assertFalse(path.exists())
        self.assertFalse(status["use_archive_current_projection"])
        self.assertEqual(status["archive_state"], "unavailable")

    def test_ready_to_pending_before_snapshot_never_returns_archive_authority(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "launch.db"
            self._ready_db(path)
            reader = TraceArchiveReader(path)
            self.assertTrue(
                reader.projection_status("r1")["use_archive_current_projection"]
            )
            with closing(sqlite3.connect(path)) as conn:
                conn.execute(
                    "UPDATE trace_managed_rooms SET projection_state='pending' "
                    "WHERE room_id='r1'"
                )
                conn.commit()

            status, rows = reader.read_projection_snapshot("r1")

        self.assertFalse(status["use_archive_current_projection"])
        self.assertEqual(status["projection_state"], "pending")
        self.assertEqual(rows, [])

    def test_source_contains_no_launch_ops_store_construction(self):
        import inspect
        import wechat_cli.core.invite_trace as module

        source = inspect.getsource(module)
        self.assertNotIn("LaunchOpsStore(", source)
        self.assertNotIn("migrate_v2(", source)


class LiveInviteTraceMergeTests(unittest.TestCase):
    def test_contact_metadata_recovers_187_relations_even_when_history_has_two_events(self):
        members = [
            {
                "username": f"wxid_member_{index}",
                "display_name": f"成员{index}",
                "inviter_username": "wxid_inviter",
            }
            for index in range(187)
        ]
        legacy = {
            "events": [
                {
                    "event_id": "1:0",
                    "timestamp": 100,
                    "time": "1970-01-01 00:01",
                    "method": "direct",
                    "inviter_name_raw": "邀请人",
                    "inviter_username": "wxid_inviter",
                    "inviter_key": "user:wxid_inviter",
                    "inviter_identity_status": "resolved",
                    "inviter_identity_source": "notice_username",
                    "invitee_name_raw": "成员0",
                    "invitee_username": "wxid_member_0",
                    "invitee_key": "user:wxid_member_0",
                    "invitee_identity_status": "resolved",
                    "invitee_identity_source": "notice_username",
                    "raw_text": "notice",
                },
                {
                    "event_id": "2:0",
                    "timestamp": 101,
                    "time": "1970-01-01 00:01",
                    "method": "qr",
                    "inviter_name_raw": "邀请人",
                    "inviter_username": "wxid_inviter",
                    "inviter_key": "user:wxid_inviter",
                    "inviter_identity_status": "resolved",
                    "inviter_identity_source": "notice_username",
                    "invitee_name_raw": "成员1",
                    "invitee_username": "wxid_member_1",
                    "invitee_key": "user:wxid_member_1",
                    "invitee_identity_status": "resolved",
                    "invitee_identity_source": "notice_username",
                    "raw_text": "notice",
                },
            ],
            "unattributed_events": [],
        }

        rows = merge_live_relations("r1", "测试群", members, legacy)

        self.assertEqual(len(rows), 187)
        self.assertEqual(
            sum(row.evidence_source == "system_message" for row in rows),
            2,
        )
        self.assertEqual(
            sum(row.evidence_source == "ext_buffer" for row in rows),
            185,
        )

    def test_system_event_wins_conflicting_metadata_and_marks_conflict(self):
        members = [{
            "username": "wxid_b",
            "display_name": "乙",
            "inviter_username": "wxid_metadata",
        }]
        legacy = {
            "events": [{
                "event_id": "10:0",
                "timestamp": 100,
                "time": "1970-01-01 00:01",
                "method": "direct",
                "inviter_name_raw": "事件邀请人",
                "inviter_username": "wxid_event",
                "inviter_key": "user:wxid_event",
                "inviter_identity_status": "resolved",
                "inviter_identity_source": "notice_username",
                "invitee_name_raw": "乙",
                "invitee_username": "wxid_b",
                "invitee_key": "user:wxid_b",
                "invitee_identity_status": "resolved",
                "invitee_identity_source": "notice_username",
                "raw_text": "notice",
            }],
            "unattributed_events": [],
        }

        rows = merge_live_relations("r1", "测试群", members, legacy)

        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0].inviter_username, "wxid_event")
        self.assertEqual(rows[0].evidence_source, "system_message")
        self.assertTrue(rows[0].metadata_conflict)

    def test_stable_system_event_for_absent_member_is_departed(self):
        legacy = {
            "events": [{
                "event_id": "20:0",
                "timestamp": 100,
                "time": "1970-01-01 00:01",
                "method": "qr",
                "inviter_name_raw": "甲",
                "inviter_username": "wxid_a",
                "inviter_key": "user:wxid_a",
                "inviter_identity_status": "resolved",
                "inviter_identity_source": "notice_username",
                "invitee_name_raw": "已退群成员",
                "invitee_username": "wxid_departed",
                "invitee_key": "user:wxid_departed",
                "invitee_identity_status": "resolved",
                "invitee_identity_source": "notice_username",
                "raw_text": "notice",
            }],
            "unattributed_events": [],
        }

        rows = merge_live_relations("r1", "测试群", [], legacy)

        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0].membership_state, "departed")


class FakeReader:
    def __init__(self, status, rows=None, *, probe_error=None, read_error=None):
        self.status = status
        self.rows = rows or []
        self.probe_error = probe_error
        self.read_error = read_error

    def projection_status(self, room_id):
        if self.probe_error:
            raise RuntimeError(self.probe_error)
        return dict(self.status)

    def read_relations(self, room_id):
        if self.read_error:
            raise RuntimeError(self.read_error)
        return list(self.rows)

    def read_projection_snapshot(self, room_id):
        if self.probe_error:
            raise RuntimeError(self.probe_error)
        status = dict(self.status)
        if status.get("use_archive_current_projection") and self.read_error:
            raise RuntimeError(self.read_error)
        return (
            status,
            list(self.rows)
            if status.get("use_archive_current_projection")
            else [],
        )


class InviteTraceRoutingTests(unittest.TestCase):
    def setUp(self):
        self.ctx = SimpleNamespace(
            msg_db_keys=[], cache=object(), decrypted_dir="decrypted"
        )
        self.chat_ctx = {
            "display_name": "测试群",
            "username": "r1",
            "is_group": True,
            "message_tables": [],
        }
        self.complete_observation = SimpleNamespace(
            membership_status="complete",
            inviter_metadata_status="complete",
        )

    def relation(self, *, event_time=150, method="direct", source="system_message"):
        return InviteTraceRelation(
            room_id="r1",
            room_display_name="测试群",
            invitee_key="user:wxid_b",
            invitee_username="wxid_b",
            invitee_name="乙",
            inviter_key="user:wxid_a",
            inviter_username="wxid_a",
            inviter_name="甲",
            membership_state="current",
            episode_index=1 if source == "archive" else None,
            method=method,
            event_time=event_time,
            resolution_state="resolved",
            evidence_source=("system_message" if source == "archive" else source),
            query_source=("archive" if source == "archive" else "live"),
        )

    def _service(self, reader):
        return InviteTraceQueryService(self.ctx, archive_reader=reader)

    def _live_result(self, *, include_metadata=False, failures=None):
        members = [{
            "username": "wxid_b",
            "display_name": "乙",
            "inviter_username": "wxid_a",
        }]
        events = [{
            "event_id": "1:0",
            "timestamp": 150,
            "time": "1970-01-01 00:02",
            "method": "direct",
            "inviter_name_raw": "甲",
            "inviter_username": "wxid_a",
            "inviter_key": "user:wxid_a",
            "inviter_identity_status": "resolved",
            "invitee_name_raw": "乙",
            "invitee_username": "wxid_b",
            "invitee_key": "user:wxid_b",
            "invitee_identity_status": "resolved",
            "raw_text": "notice",
        }]
        if include_metadata:
            members.append({
                "username": "wxid_c",
                "display_name": "丙",
                "inviter_username": "wxid_a",
            })
        legacy = {
            "events": events,
            "unattributed_events": [],
            "unparsed_messages": [],
            "failures": list(failures or []),
            "scope": {},
            "summary": {},
        }
        return self.complete_observation, members, legacy

    @patch("wechat_cli.core.invite_trace.resolve_chat_context")
    @patch("wechat_cli.core.invite_trace.read_live_inputs")
    def test_archive_ready_uses_archive_and_does_not_use_live_relations_as_authority(
        self, live, resolve
    ):
        resolve.return_value = self.chat_ctx
        archive_row = self.relation(source="archive")
        reader = FakeReader({
            "archive_state": "managed",
            "projection_state": "ready",
            "global_migration_ready": True,
            "use_archive_current_projection": True,
        }, [archive_row])

        result = self._service(reader).query(
            "测试群", bindings={}, start_ts=None, end_ts=None, now=1000
        )

        self.assertEqual(result["query_source"], "archive")
        self.assertEqual(result["completeness"], "archive_complete")
        self.assertEqual(len(result["trace"]), 1)
        live.assert_not_called()

    @patch("wechat_cli.core.invite_trace.resolve_chat_context")
    @patch("wechat_cli.core.invite_trace.read_live_inputs")
    def test_pending_uses_live_reconstruction(self, live, resolve):
        resolve.return_value = self.chat_ctx
        live.return_value = self._live_result()
        reader = FakeReader({
            "archive_state": "managed", "projection_state": "pending",
            "global_migration_ready": True,
            "use_archive_current_projection": False,
        })
        result = self._service(reader).query(
            "测试群", bindings={}, start_ts=None, end_ts=None, now=1000
        )
        self.assertEqual(result["query_source"], "live")
        self.assertEqual(result["completeness"], "live_complete_for_available_local_sources")
        self.assertEqual(len(result["trace"]), 1)

    @patch("wechat_cli.core.invite_trace.resolve_chat_context")
    @patch("wechat_cli.core.invite_trace.read_live_inputs")
    def test_refresh_error_uses_live_reconstruction(self, live, resolve):
        resolve.return_value = self.chat_ctx
        live.return_value = self._live_result()
        reader = FakeReader({
            "archive_state": "managed", "projection_state": "refresh_error",
            "global_migration_ready": True,
            "use_archive_current_projection": False,
        })
        result = self._service(reader).query(
            "测试群", bindings={}, start_ts=None, end_ts=None, now=1000
        )
        self.assertEqual(result["query_source"], "live")
        self.assertEqual(len(result["trace"]), 1)

    @patch("wechat_cli.core.invite_trace.resolve_chat_context")
    @patch("wechat_cli.core.invite_trace.read_live_inputs")
    def test_unmanaged_uses_live_reconstruction(self, live, resolve):
        resolve.return_value = self.chat_ctx
        live.return_value = self._live_result()
        reader = FakeReader({
            "archive_state": "unmanaged", "projection_state": "pending",
            "global_migration_ready": True,
            "use_archive_current_projection": False,
        })
        result = self._service(reader).query(
            "测试群", bindings={}, start_ts=None, end_ts=None, now=1000
        )
        self.assertEqual(result["query_source"], "live")
        self.assertEqual(result["archive_status"]["archive_state"], "unmanaged")

    @patch("wechat_cli.core.invite_trace.resolve_chat_context")
    @patch("wechat_cli.core.invite_trace.read_live_inputs")
    def test_archive_read_failure_falls_back_live_and_marks_degraded(self, live, resolve):
        resolve.return_value = self.chat_ctx
        live.return_value = self._live_result()
        reader = FakeReader({
            "archive_state": "managed", "projection_state": "ready",
            "global_migration_ready": True,
            "use_archive_current_projection": True,
        }, read_error="archive broken")
        result = self._service(reader).query(
            "测试群", bindings={}, start_ts=None, end_ts=None, now=1000
        )
        self.assertEqual(result["query_source"], "live")
        self.assertEqual(result["completeness"], "degraded")
        self.assertTrue(any("archive broken" in item for item in result["warnings"]))

    @patch("wechat_cli.core.invite_trace.resolve_chat_context")
    @patch("wechat_cli.core.invite_trace.read_live_inputs")
    def test_unknown_time_metadata_is_included_without_range(self, live, resolve):
        resolve.return_value = self.chat_ctx
        live.return_value = self._live_result(include_metadata=True)
        reader = FakeReader({
            "archive_state": "unmanaged", "projection_state": "pending",
            "global_migration_ready": True,
            "use_archive_current_projection": False,
        })
        result = self._service(reader).query(
            "测试群", bindings={}, start_ts=None, end_ts=None, now=1000
        )
        self.assertEqual(len(result["trace"]), 2)
        self.assertEqual(result["excluded_unknown_time_count"], 0)

    @patch("wechat_cli.core.invite_trace.resolve_chat_context")
    @patch("wechat_cli.core.invite_trace.read_live_inputs")
    def test_unknown_time_metadata_is_excluded_and_counted_with_range(self, live, resolve):
        resolve.return_value = self.chat_ctx
        live.return_value = self._live_result(include_metadata=True)
        reader = FakeReader({
            "archive_state": "unmanaged", "projection_state": "pending",
            "global_migration_ready": True,
            "use_archive_current_projection": False,
        })
        result = self._service(reader).query(
            "测试群", bindings={}, start_ts=100, end_ts=200, now=1000
        )
        self.assertEqual(len(result["trace"]), 1)
        self.assertEqual(result["trace"][0]["event_time"], 150)
        self.assertEqual(result["excluded_unknown_time_count"], 1)

    @patch("wechat_cli.core.invite_trace.resolve_chat_context")
    @patch("wechat_cli.core.invite_trace.read_live_inputs")
    def test_both_live_sources_unusable_raises_query_error(self, live, resolve):
        resolve.return_value = self.chat_ctx
        live.side_effect = RuntimeError("live unavailable")
        reader = FakeReader({
            "archive_state": "unmanaged", "projection_state": "pending",
            "global_migration_ready": True,
            "use_archive_current_projection": False,
        })
        with self.assertRaises(InviteTraceQueryError):
            self._service(reader).query(
                "测试群", bindings={}, start_ts=None, end_ts=None, now=1000
            )


class InviteTraceAggregationTests(unittest.TestCase):
    @staticmethod
    def relation(
        invitee,
        inviter,
        *,
        episode,
        state="current",
        event_time=None,
    ):
        return InviteTraceRelation(
            room_id="r1",
            room_display_name="测试群",
            invitee_key=f"user:{invitee}",
            invitee_username=invitee,
            invitee_name=invitee,
            inviter_key=f"user:{inviter}",
            inviter_username=inviter,
            inviter_name=inviter,
            membership_state=state,
            episode_index=episode,
            method="direct",
            event_time=event_time,
            resolution_state="resolved",
            evidence_source="system_message",
            query_source="archive",
        )

    def test_same_stable_invitee_multiple_same_inviter_episodes_counts_once(self):
        trace = [
            self.relation(
                "wxid_z", "wxid_a", episode=1,
                state="departed", event_time=100,
            ),
            self.relation(
                "wxid_z", "wxid_a", episode=2,
                state="current", event_time=200,
            ),
        ]
        result = {"ranking": aggregate_invite_stats(trace, trace)}
        self.assertEqual(result["ranking"][0]["unique_invitee_count"], 1)
        self.assertEqual(result["ranking"][0]["episode_count"], 2)

    def test_latest_valid_episode_owns_unique_invitee_when_inviter_changes(self):
        trace = [
            self.relation(
                "wxid_z", "wxid_a", episode=1,
                state="departed", event_time=100,
            ),
            self.relation(
                "wxid_z", "wxid_b", episode=2,
                state="current", event_time=200,
            ),
        ]
        result = {"ranking": aggregate_invite_stats(trace, trace)}
        self.assertEqual(result["ranking"][0]["inviter_username"], "wxid_b")
        self.assertEqual(result["ranking"][0]["unique_invitee_count"], 1)
        self.assertFalse(any(
            row["inviter_username"] == "wxid_a"
            and row["unique_invitee_count"]
            for row in result["ranking"]
        ))

    def test_departed_members_remain_in_historical_total(self):
        trace = [
            self.relation(
                "wxid_y", "wxid_a", episode=1,
                state="departed", event_time=100,
            ),
            self.relation(
                "wxid_z", "wxid_a", episode=1,
                state="current", event_time=200,
            ),
        ]
        item = aggregate_invite_stats(trace, trace)[0]
        self.assertEqual(item["historical_unique_invitee_count"], 2)
        self.assertEqual(item["current_invitee_count"], 1)
        self.assertEqual(item["departed_invitee_count"], 1)


class InviteTraceSafetyTests(unittest.TestCase):
    def setUp(self):
        self.fake_ctx = SimpleNamespace(
            msg_db_keys=[], cache=object(), decrypted_dir="decrypted"
        )
        self.chat_ctx = {
            "display_name": "测试群",
            "username": "r1",
            "is_group": True,
            "message_tables": [],
        }
        self.complete_observation = SimpleNamespace(
            membership_status="complete",
            inviter_metadata_status="complete",
        )
        self.members = [{
            "username": "wxid_b",
            "display_name": "乙",
            "inviter_username": "wxid_a",
        }]
        self.legacy_result = {
            "events": [],
            "unattributed_events": [],
            "unparsed_messages": [],
            "failures": [],
            "scope": {},
            "summary": {},
        }

    def test_unmanaged_query_keeps_archive_bytes_identical_and_room_unmanaged(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "launch.db"
            with closing(sqlite3.connect(path)) as conn:
                conn.executescript(MIN_ARCHIVE_SCHEMA)
                conn.execute(
                    "INSERT INTO meta(key,value) VALUES(?,?)",
                    ("trace_archive_migration_checkpoint_v2", "ready"),
                )
                conn.commit()
            before = sha256(path)
            reader = TraceArchiveReader(path)
            service = InviteTraceQueryService(
                self.fake_ctx, archive_reader=reader
            )
            with patch(
                "wechat_cli.core.invite_trace.resolve_chat_context",
                return_value=self.chat_ctx,
            ), patch(
                "wechat_cli.core.invite_trace.read_live_inputs",
                return_value=(
                    self.complete_observation,
                    self.members,
                    self.legacy_result,
                ),
            ):
                result = service.query(
                    "测试群",
                    bindings={},
                    start_ts=None,
                    end_ts=None,
                    now=1000,
                )
            after = sha256(path)
            with closing(sqlite3.connect(path)) as conn:
                row = conn.execute(
                    "SELECT 1 FROM trace_managed_rooms WHERE room_id = ?",
                    ("r1",),
                ).fetchone()

        self.assertEqual(before, after)
        self.assertIsNone(row)
        self.assertEqual(result["query_source"], "live")

    def test_query_module_contains_no_insert_update_delete_or_schema_exec_for_archive(self):
        import inspect

        source = inspect.getsource(TraceArchiveReader).upper()
        for forbidden in (
            "INSERT ", "UPDATE ", "DELETE ", "CREATE TABLE", "EXECUTESCRIPT",
        ):
            self.assertNotIn(forbidden, source)
        self.assertIn("PRAGMA QUERY_ONLY", source)

    def test_query_does_not_call_archive_migrator(self):
        import inspect
        import wechat_cli.core.invite_trace as module

        source = inspect.getsource(module)
        self.assertNotIn("ArchiveMigrator(", source)
        self.assertNotIn("migrate_v2(", source)

    @patch("wechat_cli.core.invite_trace.resolve_chat_context")
    @patch("wechat_cli.core.invite_trace.read_live_inputs")
    def test_message_shard_failure_keeps_contact_relations_and_marks_degraded(
        self, live, resolve
    ):
        resolve.return_value = self.chat_ctx
        degraded_legacy = dict(self.legacy_result)
        degraded_legacy["failures"] = ["message_1.db: malformed"]
        live.return_value = (
            self.complete_observation,
            self.members,
            degraded_legacy,
        )
        reader = FakeReader({
            "archive_state": "unmanaged",
            "projection_state": "pending",
            "global_migration_ready": True,
            "use_archive_current_projection": False,
        })

        result = InviteTraceQueryService(
            self.fake_ctx, archive_reader=reader
        ).query(
            "测试群", bindings={}, start_ts=None, end_ts=None, now=1000
        )

        self.assertEqual(result["completeness"], "degraded")
        self.assertEqual(len(result["trace"]), 1)
        self.assertEqual(result["trace"][0]["evidence_source"], "ext_buffer")
        self.assertEqual(result["failures"], ["message_1.db: malformed"])

    @patch("wechat_cli.core.invite_trace.resolve_chat_context")
    @patch("wechat_cli.core.invite_trace.read_live_inputs")
    def test_partial_inviter_metadata_keeps_available_rows_and_marks_partial(
        self, live, resolve
    ):
        resolve.return_value = self.chat_ctx
        partial = SimpleNamespace(
            membership_status="complete",
            inviter_metadata_status="partial",
        )
        live.return_value = (partial, self.members, self.legacy_result)
        reader = FakeReader({
            "archive_state": "unmanaged",
            "projection_state": "pending",
            "global_migration_ready": True,
            "use_archive_current_projection": False,
        })

        result = InviteTraceQueryService(
            self.fake_ctx, archive_reader=reader
        ).query(
            "测试群", bindings={}, start_ts=None, end_ts=None, now=1000
        )

        self.assertEqual(result["completeness"], "partial")
        self.assertEqual(len(result["trace"]), 1)

    def test_contact_failure_still_returns_system_history_with_degraded_warning(self):
        system_legacy = {
            "events": [{
                "event_id": "sys:1",
                "timestamp": 150,
                "time": "1970-01-01 00:02",
                "method": "direct",
                "inviter_name_raw": "甲",
                "inviter_username": "wxid_a",
                "inviter_key": "user:wxid_a",
                "inviter_identity_status": "resolved",
                "invitee_name_raw": "乙",
                "invitee_username": "wxid_b",
                "invitee_key": "user:wxid_b",
                "invitee_identity_status": "resolved",
                "raw_text": "notice",
            }],
            "unattributed_events": [],
            "unparsed_messages": [],
            "failures": [],
            "scope": {},
            "summary": {"system_message_count": 1},
        }
        reader = FakeReader({
            "archive_state": "unmanaged", "projection_state": "pending",
            "global_migration_ready": True,
            "use_archive_current_projection": False,
        })
        with patch(
            "wechat_cli.core.invite_trace.resolve_chat_context",
            return_value=self.chat_ctx,
        ), patch(
            "wechat_cli.core.invite_trace.read_live_membership",
            side_effect=RuntimeError("contact unavailable"),
        ), patch(
            "wechat_cli.core.invite_trace.read_message_history",
            return_value=system_legacy,
            create=True,
        ):
            result = InviteTraceQueryService(
                self.fake_ctx, archive_reader=reader
            ).query(
                "测试群", bindings={}, start_ts=None, end_ts=None, now=1000
            )

        self.assertEqual(result["query_source"], "live")
        self.assertEqual(result["completeness"], "degraded")
        self.assertEqual(len(result["trace"]), 1)
        self.assertEqual(result["trace"][0]["evidence_source"], "system_message")
        self.assertEqual(result["trace"][0]["membership_state"], "unknown")
        self.assertTrue(any(
            "contact unavailable" in warning for warning in result["warnings"]
        ))

    def test_archive_ready_fresh_metadata_matches_current_episode_without_flagging_departed_history(self):
        departed = InviteTraceRelation(
            room_id="r1", room_display_name="测试群",
            invitee_key="user:wxid_b", invitee_username="wxid_b",
            invitee_name="乙", inviter_key="user:wxid_a",
            inviter_username="wxid_a", inviter_name="甲",
            membership_state="departed", episode_index=1,
            method="direct", event_time=100, resolution_state="resolved",
            evidence_source="system_message", query_source="archive",
        )
        current = InviteTraceRelation(
            room_id="r1", room_display_name="测试群",
            invitee_key="user:wxid_b", invitee_username="wxid_b",
            invitee_name="乙", inviter_key="user:wxid_c",
            inviter_username="wxid_c", inviter_name="丙",
            membership_state="current", episode_index=2,
            method="direct", event_time=200, resolution_state="resolved",
            evidence_source="system_message", query_source="archive",
        )
        reader = FakeReader({
            "archive_state": "managed", "projection_state": "ready",
            "global_migration_ready": True,
            "use_archive_current_projection": True,
        }, [departed, current])
        fresh_members = [{
            "username": "wxid_b", "display_name": "乙",
            "inviter_username": "wxid_c",
        }]
        with patch(
            "wechat_cli.core.invite_trace.resolve_chat_context",
            return_value=self.chat_ctx,
        ), patch(
            "wechat_cli.core.invite_trace.read_live_membership",
            return_value=(self.complete_observation, fresh_members),
        ):
            result = InviteTraceQueryService(
                self.fake_ctx, archive_reader=reader
            ).query(
                "测试群", bindings={}, start_ts=None, end_ts=None, now=1000
            )

        by_episode = {row["episode_index"]: row for row in result["trace"]}
        self.assertFalse(by_episode[1]["metadata_conflict"])
        self.assertFalse(by_episode[2]["metadata_conflict"])
        self.assertFalse(any(
            "metadata conflict" in warning for warning in result["warnings"]
        ))

    def test_archive_metadata_annotation_preserves_historical_flags_without_new_warning(self):
        from wechat_cli.core.invite_trace import annotate_archive_metadata_conflicts

        for state in ("departed", "unknown"):
            with self.subTest(state=state):
                historical = InviteTraceRelation(
                    room_id="r1", room_display_name="测试群",
                    invitee_key="user:wxid_b", invitee_username="wxid_b",
                    invitee_name="乙", inviter_key="user:wxid_a",
                    inviter_username="wxid_a", inviter_name="甲",
                    membership_state=state, episode_index=1,
                    method="direct", event_time=100,
                    resolution_state="resolved",
                    evidence_source="system_message", query_source="archive",
                    metadata_conflict=True,
                )
                annotated, warnings = annotate_archive_metadata_conflicts(
                    [historical],
                    [{
                        "username": "wxid_b", "display_name": "乙",
                        "inviter_username": "wxid_c",
                    }],
                )

                self.assertTrue(annotated[0].metadata_conflict)
                self.assertEqual(warnings, [])

    def test_archive_ready_fresh_metadata_conflict_is_reported_without_overriding_archive(self):
        archive_row = InviteTraceRelation(
            room_id="r1", room_display_name="测试群",
            invitee_key="user:wxid_b", invitee_username="wxid_b",
            invitee_name="乙", inviter_key="user:wxid_a",
            inviter_username="wxid_a", inviter_name="甲",
            membership_state="current", episode_index=1,
            method="direct", event_time=100, resolution_state="resolved",
            evidence_source="system_message", query_source="archive",
        )
        reader = FakeReader({
            "archive_state": "managed", "projection_state": "ready",
            "global_migration_ready": True,
            "use_archive_current_projection": True,
        }, [archive_row])
        fresh_members = [{
            "username": "wxid_b", "display_name": "乙",
            "inviter_username": "wxid_c",
        }]
        with patch(
            "wechat_cli.core.invite_trace.resolve_chat_context",
            return_value=self.chat_ctx,
        ), patch(
            "wechat_cli.core.invite_trace.read_live_membership",
            return_value=(self.complete_observation, fresh_members),
        ):
            result = InviteTraceQueryService(
                self.fake_ctx, archive_reader=reader
            ).query(
                "测试群", bindings={}, start_ts=None, end_ts=None, now=1000
            )

        self.assertEqual(result["query_source"], "archive")
        self.assertEqual(result["trace"][0]["inviter_username"], "wxid_a")
        self.assertTrue(result["trace"][0]["metadata_conflict"])
        self.assertTrue(any(
            "wxid_b" in warning and "metadata conflict" in warning
            for warning in result["warnings"]
        ))

    @patch("wechat_cli.core.invite_trace.resolve_chat_context")
    @patch(
        "wechat_cli.core.invite_trace.read_live_membership",
        side_effect=RuntimeError("contact supplement failed"),
    )
    def test_archive_ready_supplement_failure_keeps_archive_complete_with_warning(
        self, supplement, resolve
    ):
        resolve.return_value = self.chat_ctx
        archive_row = InviteTraceRelation(
            room_id="r1",
            room_display_name="测试群",
            invitee_key="user:wxid_b",
            invitee_username="wxid_b",
            invitee_name="乙",
            inviter_key="user:wxid_a",
            inviter_username="wxid_a",
            inviter_name="甲",
            membership_state="current",
            episode_index=1,
            method="direct",
            event_time=100,
            resolution_state="resolved",
            evidence_source="system_message",
            query_source="archive",
        )
        reader = FakeReader({
            "archive_state": "managed",
            "projection_state": "ready",
            "global_migration_ready": True,
            "use_archive_current_projection": True,
        }, [archive_row])

        result = InviteTraceQueryService(
            self.fake_ctx, archive_reader=reader
        ).query(
            "测试群", bindings={}, start_ts=None, end_ts=None, now=1000
        )

        self.assertEqual(result["query_source"], "archive")
        self.assertEqual(result["completeness"], "archive_complete")
        self.assertTrue(any(
            "contact supplement failed" in warning
            for warning in result["warnings"]
        ))
        supplement.assert_called_once()


class InviteTraceWebCompatibilityTests(unittest.TestCase):
    def test_unified_result_keeps_invite_stats_web_fields(self):
        relation = InviteTraceRelation(
            room_id="r1",
            room_display_name="测试群",
            invitee_key="user:wxid_b",
            invitee_username="wxid_b",
            invitee_name="乙",
            inviter_key="user:wxid_a",
            inviter_username="wxid_a",
            inviter_name="甲",
            membership_state="current",
            episode_index=None,
            method="direct",
            event_time=100,
            resolution_state="resolved",
            evidence_source="system_message",
            query_source="live",
        )
        ranking = aggregate_invite_stats([relation], [relation])
        result = build_invite_trace_result(
            chat_ctx={"display_name": "测试群", "username": "r1"},
            query_source="live",
            completeness="live_complete_for_available_local_sources",
            archive_status={"projection_state": "pending"},
            ranking=ranking,
            trace_relations=[relation],
            legacy_result={
                "summary": {"system_message_count": 1},
                "scope": {},
                "unattributed_events": [],
                "unparsed_messages": [],
                "failures": [],
            },
            warnings=[],
            excluded_unknown_time_count=0,
            start_ts=None,
            end_ts=None,
        )

        self.assertTrue({
            "invite_event_count",
            "attributed_event_count",
            "unique_invitee_count",
            "unattributed_count",
            "unresolved_identity_count",
            "unparsed_count",
        }.issubset(result["summary"]))
        self.assertTrue({
            "unique_invitee_count",
            "direct_count",
            "qr_count",
            "invitees",
        }.issubset(result["ranking"][0]))
        self.assertTrue({
            "time",
            "inviter_name_raw",
            "invitee_name_raw",
            "method",
        }.issubset(result["events"][0]))


if __name__ == "__main__":
    unittest.main()
