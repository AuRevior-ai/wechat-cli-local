import tempfile
import unittest
from pathlib import Path

from wechat_cli.launch_ops.models import StandardEvent
from wechat_cli.launch_ops.store import LaunchOpsStore


class LaunchOpsStoreTests(unittest.TestCase):
    def make_store(self, root):
        return LaunchOpsStore(Path(root) / "launch_monitor.db")

    def test_list_viewer_publications_is_campaign_scoped_and_newest_first(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = self.make_store(tmp)
            campaign_a = store.create_campaign("A", now=100)
            campaign_b = store.create_campaign("B", now=100)
            first = store.create_viewer_publication(
                publication_id="pub-a-1",
                campaign_id=campaign_a["campaign_id"],
                path_slug="a-first",
                now=101,
            )
            second = store.create_viewer_publication(
                publication_id="pub-a-2",
                campaign_id=campaign_a["campaign_id"],
                path_slug="a-second",
                now=102,
            )
            store.create_viewer_publication(
                publication_id="pub-b-1",
                campaign_id=campaign_b["campaign_id"],
                path_slug="b-first",
                now=103,
            )

            rows = store.list_viewer_publications(campaign_a["campaign_id"])

        self.assertEqual(
            [second["publication_id"], first["publication_id"]],
            [row["publication_id"] for row in rows],
        )

    def test_creates_schema_and_campaign_history(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = self.make_store(tmp)
            campaign = store.create_campaign("OPC大课发售", now=100)
            self.assertFalse(campaign["remote_access_enabled"])
            store.add_room_to_campaign(campaign["campaign_id"], "room-2", now=100)
            store.remove_room_from_campaign(campaign["campaign_id"], "room-2", now=200)

            history = store.list_campaign_rooms(
                campaign["campaign_id"], include_removed=True
            )

        self.assertEqual("room-2", history[0]["room_id"])
        self.assertEqual(100, history[0]["joined_campaign_at"])
        self.assertEqual(200, history[0]["removed_campaign_at"])

    def test_event_insert_is_idempotent(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = self.make_store(tmp)
            event = StandardEvent(
                event_id="message:room:1",
                event_type="message.created",
                room_id="room",
                event_time=100,
                source_shard="message/message_0.db",
                source_table="Msg_" + "a" * 32,
                source_identity="1",
                actor_id="",
                subject_id="",
                payload={"type": "text"},
            )

            self.assertTrue(store.insert_event(event, now=110))
            self.assertFalse(store.insert_event(event, now=111))
            self.assertEqual(1, store.count_events())

    def test_cursor_round_trip_is_per_source_table(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = self.make_store(tmp)
            self.assertIsNone(store.get_cursor("message/message_0.db", "Msg_" + "a" * 32))

            store.set_cursor(
                "message/message_0.db",
                "Msg_" + "a" * 32,
                create_time=123,
                local_id=7,
                source_identity="server:99",
            )

            cursor = store.get_cursor(
                "message/message_0.db", "Msg_" + "a" * 32
            )

        self.assertEqual(123, cursor["create_time"])
        self.assertEqual(7, cursor["local_id"])
        self.assertEqual("server:99", cursor["source_identity"])

    def test_retention_keeps_recent_and_protected_messages(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = self.make_store(tmp)
            cutoff = 4_000_000 - 30 * 24 * 60 * 60
            store.insert_message(
                message_id="expired-unprotected",
                event_id="e1",
                room_id="r1",
                event_time=cutoff - 1,
                message_type="text",
                sender_id="u1",
                text_content="old",
                payload={},
                protected=False,
                now=1,
            )
            store.insert_message(
                message_id="expired-protected",
                event_id="e2",
                room_id="r1",
                event_time=cutoff - 1,
                message_type="text",
                sender_id="u1",
                text_content="protected",
                payload={},
                protected=True,
                now=1,
            )
            store.insert_message(
                message_id="recent-unprotected",
                event_id="e3",
                room_id="r1",
                event_time=cutoff + 1,
                message_type="text",
                sender_id="u1",
                text_content="recent",
                payload={},
                protected=False,
                now=1,
            )

            removed = store.purge_expired_messages(now=4_000_000)

            self.assertEqual(1, removed)
            self.assertIsNone(store.get_message("expired-unprotected"))
            self.assertIsNotNone(store.get_message("expired-protected"))
            self.assertIsNotNone(store.get_message("recent-unprotected"))

    def test_source_batch_commits_events_messages_and_cursor_atomically(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = self.make_store(tmp)
            event = StandardEvent(
                event_id="msg:server:101",
                event_type="message.created",
                room_id="room",
                event_time=100,
                source_shard="message/message_0.db",
                source_table="Msg_" + "a" * 32,
                source_identity="server:101",
                actor_id="u1",
                subject_id="",
                payload={"message_type": "text"},
            )
            source_stamp = {
                "db_mtime_ns": 11,
                "wal_mtime_ns": 22,
                "db_size": 33,
                "wal_size": 44,
            }
            table_head = {"local_id": 7, "create_time": 100}
            result = store.apply_source_batch(
                source_shard=event.source_shard,
                source_table=event.source_table,
                source_stamp=source_stamp,
                table_head=table_head,
                events=[event],
                messages=[{
                    "message_id": event.event_id,
                    "event_id": event.event_id,
                    "room_id": "room",
                    "event_time": 100,
                    "message_type": "text",
                    "sender_id": "u1",
                    "text_content": "hello",
                    "payload": {"message_type": "text"},
                }],
                cursor={
                    "create_time": 100,
                    "local_id": 1,
                    "source_identity": "server:101",
                },
                now=110,
            )

            cursor = store.get_cursor(event.source_shard, event.source_table)
            persisted_stamp = store.get_source_stamp(
                event.source_shard, event.source_table
            )
            persisted_head = store.get_table_head(
                event.source_shard, event.source_table
            )
            event_count = store.count_events()
            message = store.get_message(event.event_id)

        self.assertEqual({"inserted_events": 1, "inserted_messages": 1}, result)
        self.assertEqual(1, event_count)
        self.assertIsNotNone(message)
        self.assertEqual(100, cursor["create_time"])
        self.assertEqual(1, cursor["local_id"])
        self.assertEqual(source_stamp, persisted_stamp)
        self.assertEqual(table_head, persisted_head)

    def test_source_batch_persists_stamp_without_new_rows(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = self.make_store(tmp)
            stamp = {
                "db_mtime_ns": 101,
                "wal_mtime_ns": 202,
                "db_size": 303,
                "wal_size": 404,
            }

            result = store.apply_source_batch(
                source_shard="message/message_0.db",
                source_table="Msg_" + "a" * 32,
                source_stamp=stamp,
                table_head={"local_id": 0, "create_time": 0},
                events=[],
                messages=[],
                cursor=None,
                now=110,
            )

            persisted = store.get_source_stamp(
                "message/message_0.db", "Msg_" + "a" * 32
            )
            head = store.get_table_head(
                "message/message_0.db", "Msg_" + "a" * 32
            )

        self.assertEqual({"inserted_events": 0, "inserted_messages": 0}, result)
        self.assertEqual(stamp, persisted)
        self.assertEqual({"local_id": 0, "create_time": 0}, head)

    def test_source_batch_rolls_back_everything_on_mid_batch_failure(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = self.make_store(tmp)
            event = StandardEvent(
                event_id="msg:server:102",
                event_type="message.created",
                room_id="room",
                event_time=100,
                source_shard="message/message_0.db",
                source_table="Msg_" + "a" * 32,
                source_identity="server:102",
                actor_id="u1",
                subject_id="",
                payload={"message_type": "text"},
            )

            with self.assertRaises(KeyError):
                store.apply_source_batch(
                    source_shard=event.source_shard,
                    source_table=event.source_table,
                    source_stamp={
                        "db_mtime_ns": 1,
                        "wal_mtime_ns": 2,
                        "db_size": 3,
                        "wal_size": 4,
                    },
                    table_head={"local_id": 9, "create_time": 100},
                    events=[event],
                    messages=[{"message_id": "broken"}],
                    cursor={
                        "create_time": 100,
                        "local_id": 1,
                        "source_identity": "server:102",
                    },
                    now=110,
                )

            self.assertEqual(0, store.count_events())
            self.assertIsNone(store.get_message("broken"))
            self.assertIsNone(store.get_cursor(event.source_shard, event.source_table))
            self.assertIsNone(
                store.get_source_stamp(event.source_shard, event.source_table)
            )
            self.assertIsNone(
                store.get_table_head(event.source_shard, event.source_table)
            )

    def test_source_batch_is_idempotent_on_overlap_replay(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = self.make_store(tmp)
            event = StandardEvent(
                event_id="msg:server:103",
                event_type="message.created",
                room_id="room",
                event_time=100,
                source_shard="message/message_0.db",
                source_table="Msg_" + "a" * 32,
                source_identity="server:103",
                actor_id="u1",
                subject_id="",
                payload={"message_type": "text"},
            )
            kwargs = dict(
                source_shard=event.source_shard,
                source_table=event.source_table,
                events=[event],
                messages=[{
                    "message_id": event.event_id,
                    "event_id": event.event_id,
                    "room_id": "room",
                    "event_time": 100,
                    "message_type": "text",
                    "sender_id": "u1",
                    "text_content": "hello",
                    "payload": {},
                }],
                cursor={
                    "create_time": 100,
                    "local_id": 1,
                    "source_identity": "server:103",
                },
                now=110,
            )

            first = store.apply_source_batch(**kwargs)
            second = store.apply_source_batch(**kwargs)

            self.assertEqual(1, store.count_events())
            self.assertIsNotNone(store.get_message(event.event_id))

        self.assertEqual({"inserted_events": 1, "inserted_messages": 1}, first)
        self.assertEqual({"inserted_events": 0, "inserted_messages": 0}, second)

    def test_room_profile_and_latest_membership_snapshot_round_trip(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = self.make_store(tmp)
            self.assertTrue(
                hasattr(store, "upsert_room_profile"),
                "Launch Ops store must persist Viewer-safe room profiles",
            )
            store.upsert_room_profile(
                "room",
                "https://wx.qlogo.cn/room-avatar",
                observed_at=123,
            )
            store.save_membership_snapshot(
                "room",
                [{"username": "u1", "display_name": "成员", "avatar_url": "avatar"}],
                captured_at=124,
            )

            profile = store.get_room_profile("room")
            snapshot = store.latest_membership_snapshot("room")

        self.assertEqual("https://wx.qlogo.cn/room-avatar", profile["avatar_url"])
        self.assertEqual(123, profile["observed_at"])
        self.assertEqual(124, snapshot["captured_at"])
        self.assertEqual("成员", snapshot["members"][0]["display_name"])

    def test_campaign_coverage_protects_events_in_historical_window(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = self.make_store(tmp)
            campaign = store.create_campaign("Campaign", now=100)
            store.add_room_to_campaign(campaign["campaign_id"], "room", now=120)
            store.remove_room_from_campaign(campaign["campaign_id"], "room", now=200)

            self.assertFalse(store.room_is_campaign_covered("room", 119))
            self.assertTrue(store.room_is_campaign_covered("room", 120))
            self.assertTrue(store.room_is_campaign_covered("room", 199))
            self.assertFalse(store.room_is_campaign_covered("room", 200))

    def test_ensure_campaign_preserves_control_plane_campaign_id(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = self.make_store(tmp)
            self.assertTrue(
                hasattr(store, "ensure_campaign"),
                "exact campaign-id ensure is not implemented",
            )
            first = store.ensure_campaign(
                campaign_id="campaign-opc",
                name="OPC大课发售",
                now=100,
            )
            second = store.ensure_campaign(
                campaign_id="campaign-opc",
                name="OPC大课发售",
                now=200,
            )
            self.assertEqual("campaign-opc", first["campaign_id"])
            self.assertEqual(first, second)
            self.assertFalse(first["remote_access_enabled"])


    def test_trace_membership_observation_identity_is_append_safe_and_idempotent(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = LaunchOpsStore(Path(tmp) / "launch_monitor.db")
            store.upsert_room("r1", "Room", now=1)
            store.ensure_trace_managed_room("r1", source="manual", now=1)
            first = store.save_trace_membership_observation(
                room_id="r1",
                source_kind="contact",
                source_state_key="state-a",
                semantic_hash="sem-a",
                membership_status="complete",
                inviter_metadata_status="complete",
                members=[{"username": "wxid_b", "inviter_username": "wxid_a"}],
                observed_at=100,
            )
            replay = store.save_trace_membership_observation(
                room_id="r1",
                source_kind="contact",
                source_state_key="state-a",
                semantic_hash="sem-a",
                membership_status="complete",
                inviter_metadata_status="complete",
                members=[{"username": "wxid_b", "inviter_username": "wxid_a"}],
                observed_at=101,
            )
            same_second_different = store.save_trace_membership_observation(
                room_id="r1",
                source_kind="contact",
                source_state_key="state-b",
                semantic_hash="sem-b",
                membership_status="complete",
                inviter_metadata_status="complete",
                members=[{"username": "wxid_b", "inviter_username": "wxid_c"}],
                observed_at=100,
            )

            self.assertEqual(first["observation_id"], replay["observation_id"])
            self.assertEqual(replay["first_observed_at"], 100)
            self.assertEqual(replay["last_observed_at"], 101)
            self.assertNotEqual(first["observation_id"], same_second_different["observation_id"])
            self.assertEqual(len(store.list_trace_membership_observations("r1")), 2)

    def test_v2_observations_do_not_replace_same_second_semantic_changes(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = LaunchOpsStore(Path(tmp) / "launch_monitor.db")
            store.upsert_room("r1", "Room", now=1)
            store.ensure_trace_managed_room("r1", source="manual", now=1)
            store.save_trace_membership_observation(
                room_id="r1", source_kind="contact", source_state_key="a",
                semantic_hash="one", membership_status="complete",
                inviter_metadata_status="complete", members=[{"username": "wxid_1"}],
                observed_at=200,
            )
            store.save_trace_membership_observation(
                room_id="r1", source_kind="contact", source_state_key="b",
                semantic_hash="two", membership_status="complete",
                inviter_metadata_status="complete", members=[{"username": "wxid_2"}],
                observed_at=200,
            )
            rows = store.list_trace_membership_observations("r1")
            self.assertEqual({row["semantic_hash"] for row in rows}, {"one", "two"})
            self.assertEqual(len(rows), 2)

    def test_trace_archive_v2_core_schema_and_partial_unique_indexes_are_frozen(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = LaunchOpsStore(Path(tmp) / "launch_monitor.db")
            with store._connect() as conn:
                tables = {
                    row[0]
                    for row in conn.execute(
                        "SELECT name FROM sqlite_master WHERE type='table'"
                    ).fetchall()
                }
                self.assertTrue({
                    "trace_managed_rooms",
                    "trace_membership_observations",
                    "trace_people",
                    "trace_person_profiles",
                    "trace_membership_episodes",
                    "trace_invite_relations",
                    "trace_relation_evidence",
                    "trace_manual_audit",
                    "trace_conflicts",
                    "trace_reconcile_queue",
                }.issubset(tables))

                episode_indexes = {
                    row[1] for row in conn.execute(
                        "PRAGMA index_list('trace_membership_episodes')"
                    ).fetchall()
                }
                relation_indexes = {
                    row[1] for row in conn.execute(
                        "PRAGMA index_list('trace_invite_relations')"
                    ).fetchall()
                }
                self.assertIn("uq_trace_episode_active", episode_indexes)
                self.assertIn("uq_trace_relation_active", relation_indexes)

    def test_trace_archive_v2_constraints_reject_invalid_status_and_duplicate_active_rows(self):
        import sqlite3

        with tempfile.TemporaryDirectory() as tmp:
            store = LaunchOpsStore(Path(tmp) / "launch_monitor.db")
            store.upsert_room("r1", "Room", now=1)
            with store._connect() as conn:
                with self.assertRaises(sqlite3.IntegrityError):
                    conn.execute(
                        """
                        INSERT INTO trace_membership_observations(
                            observation_id, room_id, source_kind, source_state_key,
                            semantic_hash, first_observed_at, last_observed_at,
                            membership_status, inviter_metadata_status, members_json,
                            diagnostic_code
                        ) VALUES ('bad', 'r1', 'contact', 's', 'h', 1, 1,
                                  'unknown', 'complete', '[]', '')
                        """
                    )

                conn.execute(
                    "INSERT INTO trace_people(person_id, wechat_username, first_seen_at, last_seen_at) "
                    "VALUES ('p1', 'wxid_p1', 1, 1)"
                )
                conn.execute(
                    """
                    INSERT INTO trace_membership_episodes(
                        episode_id, room_id, person_id, episode_index,
                        first_observed_at, last_observed_at, status
                    ) VALUES ('ep1', 'r1', 'p1', 1, 1, 1, 'active')
                    """
                )
                with self.assertRaises(sqlite3.IntegrityError):
                    conn.execute(
                        """
                        INSERT INTO trace_membership_episodes(
                            episode_id, room_id, person_id, episode_index,
                            first_observed_at, last_observed_at, status
                        ) VALUES ('ep2', 'r1', 'p1', 2, 2, 2, 'active')
                        """
                    )

                conn.execute(
                    "INSERT INTO trace_people(person_id, wechat_username, first_seen_at, last_seen_at) "
                    "VALUES ('p2', 'wxid_p2', 1, 1)"
                )
                conn.execute(
                    """
                    INSERT INTO trace_invite_relations(
                        relation_id, room_id, episode_id, invitee_person_id,
                        inviter_person_id, valid_from_observation,
                        first_observed_at, last_observed_at, active,
                        resolution_state
                    ) VALUES ('rel1', 'r1', 'ep1', 'p1', 'p2', 1, 1, 1, 1, 'resolved')
                    """
                )
                with self.assertRaises(sqlite3.IntegrityError):
                    conn.execute(
                        """
                        INSERT INTO trace_invite_relations(
                            relation_id, room_id, episode_id, invitee_person_id,
                            inviter_person_id, valid_from_observation,
                            first_observed_at, last_observed_at, active,
                            resolution_state
                        ) VALUES ('rel2', 'r1', 'ep1', 'p1', 'p2', 2, 2, 2, 1, 'resolved')
                        """
                    )


    def test_new_v2_observation_enqueues_membership_reconcile_once(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = LaunchOpsStore(Path(tmp) / "launch_monitor.db")
            store.upsert_room("r1", "Room", now=1)
            store.ensure_trace_managed_room("r1", source="manual", now=1)
            first = store.save_trace_membership_observation(
                room_id="r1",
                source_kind="contact",
                source_state_key="state-a",
                semantic_hash="sem-a",
                membership_status="complete",
                inviter_metadata_status="complete",
                members=[{"username": "wxid_b"}],
                observed_at=100,
            )
            store.save_trace_membership_observation(
                room_id="r1",
                source_kind="contact",
                source_state_key="state-a",
                semantic_hash="sem-a",
                membership_status="complete",
                inviter_metadata_status="complete",
                members=[{"username": "wxid_b"}],
                observed_at=101,
            )
            pending = store.list_pending_trace_reconcile("r1")
            self.assertEqual(len(pending), 1)
            self.assertEqual(pending[0]["reason"], "membership")
            self.assertEqual(
                pending[0]["source_identity"],
                f"membership:{first['observation_id']}",
            )

    def test_new_durable_invite_event_enqueues_evidence_reconcile_once(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = LaunchOpsStore(Path(tmp) / "launch_monitor.db")
            store.upsert_room("r1", "Room", now=1)
            store.ensure_trace_managed_room("r1", source="manual", now=1)
            event = StandardEvent(
                event_id="invite-new",
                event_type="member.invited",
                room_id="r1",
                event_time=95,
                source_shard="message/message_0.db",
                source_table="Msg_test",
                source_identity="invite-new",
                actor_id="wxid_a",
                subject_id="wxid_b",
                payload={"method": "direct"},
            )
            self.assertTrue(store.insert_event(event, now=100))
            self.assertFalse(store.insert_event(event, now=101))

            pending = store.list_pending_trace_reconcile("r1")
            evidence_rows = [row for row in pending if row["reason"] == "evidence"]
            self.assertEqual(len(evidence_rows), 1)
            self.assertEqual(evidence_rows[0]["source_identity"], "event:invite-new")


    def test_reused_failed_attempt_invalidates_ready_projection(self):
        from unittest.mock import Mock
        from wechat_cli.launch_ops.watcher import LaunchOpsWatcher

        with tempfile.TemporaryDirectory() as tmp:
            store = LaunchOpsStore(Path(tmp) / "launch.db")
            store.upsert_room("r1", "Room", now=1)
            store.ensure_trace_managed_room("r1", source="manual", now=1)
            watcher = LaunchOpsWatcher(store, Mock())

            failed = store.save_trace_membership_observation(
                room_id="r1", source_kind="contact",
                source_state_key="state-f", semantic_hash="sem-f",
                membership_status="failed",
                inviter_metadata_status="unavailable",
                members=[], observed_at=100,
                diagnostic_code="contact_read_failed",
            )
            watcher._drain_trace_reconcile_queue(now=101, room_id="r1")
            self.assertEqual(
                store.get_trace_room_projection_status("r1")["projection_state"],
                "refresh_error",
            )

            complete = store.save_trace_membership_observation(
                room_id="r1", source_kind="contact",
                source_state_key="state-a", semantic_hash="sem-a",
                membership_status="complete",
                inviter_metadata_status="complete",
                members=[{"username": "wxid_b"}], observed_at=110,
            )
            watcher._drain_trace_reconcile_queue(now=111, room_id="r1")
            status = store.get_trace_room_projection_status("r1")
            self.assertEqual(status["projection_state"], "ready")
            self.assertEqual(status["current_observation_id"], complete["observation_id"])

            failed_again = store.save_trace_membership_observation(
                room_id="r1", source_kind="contact",
                source_state_key="state-f", semantic_hash="sem-f",
                membership_status="failed",
                inviter_metadata_status="unavailable",
                members=[], observed_at=120,
                diagnostic_code="contact_read_failed",
            )

            self.assertEqual(failed_again["observation_id"], failed["observation_id"])
            status = store.get_trace_room_projection_status("r1")
            self.assertEqual(status["projection_state"], "refresh_error")
            self.assertFalse(status["use_archive_current_projection"])
            self.assertEqual(
                [(q["reason"], q["source_identity"]) for q in store.list_pending_trace_reconcile("r1")],
                [("membership", f"membership:{failed['observation_id']}")],
            )

    def test_reused_complete_attempt_rearms_membership_and_recovers_from_refresh_error(self):
        from unittest.mock import Mock
        from wechat_cli.launch_ops.watcher import LaunchOpsWatcher

        with tempfile.TemporaryDirectory() as tmp:
            store = LaunchOpsStore(Path(tmp) / "launch.db")
            store.upsert_room("r1", "Room", now=1)
            store.ensure_trace_managed_room("r1", source="manual", now=1)
            watcher = LaunchOpsWatcher(store, Mock())

            complete = store.save_trace_membership_observation(
                room_id="r1", source_kind="contact",
                source_state_key="state-a", semantic_hash="sem-a",
                membership_status="complete",
                inviter_metadata_status="complete",
                members=[{"username": "wxid_b"}], observed_at=100,
            )
            watcher._drain_trace_reconcile_queue(now=101, room_id="r1")
            self.assertEqual(
                store.get_trace_room_projection_status("r1")["projection_state"],
                "ready",
            )

            store.save_trace_membership_observation(
                room_id="r1", source_kind="contact",
                source_state_key="state-f", semantic_hash="sem-f",
                membership_status="failed",
                inviter_metadata_status="unavailable",
                members=[], observed_at=110,
                diagnostic_code="contact_read_failed",
            )
            watcher._drain_trace_reconcile_queue(now=111, room_id="r1")
            self.assertEqual(
                store.get_trace_room_projection_status("r1")["projection_state"],
                "refresh_error",
            )

            complete_again = store.save_trace_membership_observation(
                room_id="r1", source_kind="contact",
                source_state_key="state-a", semantic_hash="sem-a",
                membership_status="complete",
                inviter_metadata_status="complete",
                members=[{"username": "wxid_b"}], observed_at=120,
            )
            self.assertEqual(complete_again["observation_id"], complete["observation_id"])
            self.assertEqual(
                store.get_trace_room_projection_status("r1")["projection_state"],
                "pending",
            )
            self.assertEqual(
                [(q["reason"], q["source_identity"]) for q in store.list_pending_trace_reconcile("r1")],
                [("membership", f"membership:{complete['observation_id']}")],
            )

            watcher._drain_trace_reconcile_queue(now=121, room_id="r1")
            status = store.get_trace_room_projection_status("r1")
            self.assertEqual(status["projection_state"], "ready")
            self.assertEqual(status["current_observation_id"], complete["observation_id"])

    def test_same_timestamp_complete_then_failed_uses_call_order_not_observation_hash(self):
        from unittest.mock import Mock
        from wechat_cli.launch_ops.watcher import LaunchOpsWatcher

        with tempfile.TemporaryDirectory() as tmp:
            store = LaunchOpsStore(Path(tmp) / "launch.db")
            store.upsert_room("r1", "Room", now=1)
            store.ensure_trace_managed_room("r1", source="manual", now=1)
            watcher = LaunchOpsWatcher(store, Mock())

            complete = store.save_trace_membership_observation(
                room_id="r1", source_kind="contact",
                source_state_key="a0", semantic_hash="asem0",
                membership_status="complete",
                inviter_metadata_status="complete",
                members=[{"username": "wxid_b"}], observed_at=500,
            )
            failed = store.save_trace_membership_observation(
                room_id="r1", source_kind="contact",
                source_state_key="f2", semantic_hash="fsem2",
                membership_status="failed",
                inviter_metadata_status="unavailable",
                members=[], observed_at=500,
                diagnostic_code="contact_read_failed",
            )

            self.assertGreater(complete["observation_id"], failed["observation_id"])
            pending = [
                q for q in store.list_pending_trace_reconcile("r1")
                if q["reason"] == "membership"
            ]
            self.assertEqual(
                [q["source_identity"] for q in pending],
                [f"membership:{failed['observation_id']}"],
            )

            watcher._drain_trace_reconcile_queue(now=501, room_id="r1")

            status = store.get_trace_room_projection_status("r1")
            self.assertEqual(status["projection_state"], "refresh_error")
            self.assertFalse(status["use_archive_current_projection"])
            self.assertEqual(
                [q["source_identity"] for q in store.list_pending_trace_reconcile("r1")
                 if q["reason"] == "membership"],
                [f"membership:{failed['observation_id']}"],
            )

    def test_same_timestamp_failed_then_complete_reconciles_exact_complete_basis_not_observation_hash(self):
        from unittest.mock import Mock
        from wechat_cli.launch_ops.watcher import LaunchOpsWatcher

        with tempfile.TemporaryDirectory() as tmp:
            store = LaunchOpsStore(Path(tmp) / "launch.db")
            store.upsert_room("r1", "Room", now=1)
            store.ensure_trace_managed_room("r1", source="manual", now=1)
            watcher = LaunchOpsWatcher(store, Mock())

            failed = store.save_trace_membership_observation(
                room_id="r1", source_kind="contact",
                source_state_key="f0", semantic_hash="fsem0",
                membership_status="failed",
                inviter_metadata_status="unavailable",
                members=[], observed_at=500,
                diagnostic_code="contact_read_failed",
            )
            complete = store.save_trace_membership_observation(
                room_id="r1", source_kind="contact",
                source_state_key="a0", semantic_hash="asem0",
                membership_status="complete",
                inviter_metadata_status="complete",
                members=[{"username": "wxid_b"}], observed_at=500,
            )

            self.assertGreater(failed["observation_id"], complete["observation_id"])
            pending = [
                q for q in store.list_pending_trace_reconcile("r1")
                if q["reason"] == "membership"
            ]
            self.assertEqual(
                [q["source_identity"] for q in pending],
                [f"membership:{complete['observation_id']}"],
            )

            watcher._drain_trace_reconcile_queue(now=501, room_id="r1")

            status = store.get_trace_room_projection_status("r1")
            self.assertEqual(status["projection_state"], "ready")
            self.assertEqual(status["current_observation_id"], complete["observation_id"])
            self.assertEqual(store.list_pending_trace_reconcile("r1"), [])
            person = store.get_trace_person_by_username("wxid_b")
            self.assertIsNotNone(person)
            self.assertIsNotNone(
                store.get_active_trace_episode("r1", person["person_id"])
            )


    def test_room_projection_ready_rejects_non_contact_or_incomplete_observation(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = LaunchOpsStore(Path(tmp) / "launch.db")
            store.upsert_room("r1", "Room", now=1)
            store.ensure_trace_managed_room("r1", source="manual", now=1)
            partial = store.save_trace_membership_observation(
                room_id="r1", source_kind="contact", source_state_key="partial",
                semantic_hash="partial", membership_status="partial",
                inviter_metadata_status="unavailable", members=[{"username": "wxid_b"}],
                observed_at=100,
            )
            legacy = store.save_trace_membership_observation(
                room_id="r1", source_kind="legacy_snapshot", source_state_key="legacy:100:x",
                semantic_hash="legacy", membership_status="partial",
                inviter_metadata_status="unavailable", members=[{"username": "wxid_b"}],
                observed_at=100,
            )
            with self.assertRaises(ValueError):
                store.mark_trace_room_projection_ready("r1", partial["observation_id"], now=101)
            with self.assertRaises(ValueError):
                store.mark_trace_room_projection_ready("r1", legacy["observation_id"], now=101)

    def test_new_contact_observation_invalidates_prior_ready_projection_until_reconciled(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = LaunchOpsStore(Path(tmp) / "launch.db")
            store.upsert_room("r1", "Room", now=1)
            store.ensure_trace_managed_room("r1", source="manual", now=1)
            first = store.save_trace_membership_observation(
                room_id="r1", source_kind="contact", source_state_key="state-1",
                semantic_hash="semantic-1", membership_status="complete",
                inviter_metadata_status="complete", members=[{"username": "wxid_b"}],
                observed_at=100,
            )
            store.mark_trace_room_projection_ready("r1", first["observation_id"], now=101)
            self.assertTrue(store.trace_room_projection_ready("r1") is False)
            store.set_meta("trace_archive_migration_checkpoint_v2", "ready")
            self.assertTrue(store.trace_room_projection_ready("r1"))
            second = store.save_trace_membership_observation(
                room_id="r1", source_kind="contact", source_state_key="state-2",
                semantic_hash="semantic-2", membership_status="partial",
                inviter_metadata_status="unavailable", members=[{"username": "wxid_b"}],
                observed_at=110,
            )
            status = store.get_trace_room_projection_status("r1")
            self.assertNotEqual(second["observation_id"], first["observation_id"])
            self.assertEqual(status["projection_state"], "pending")
            self.assertFalse(status["use_archive_current_projection"])


if __name__ == "__main__":
    unittest.main()
