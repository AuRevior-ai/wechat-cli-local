import tempfile
import unittest
from pathlib import Path

from wechat_cli.launch_ops.archive import (
    ArchiveMigrator,
    ArchiveReconciler,
    build_current_invite_tree,
)
from wechat_cli.launch_ops.models import StandardEvent
from wechat_cli.launch_ops.store import LaunchOpsStore


class ArchiveReconcilerTests(unittest.TestCase):
    def make_archive(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        store = LaunchOpsStore(Path(tmp.name) / "launch_monitor.db")
        store.upsert_room("r1", "Room 1", now=1)
        store.upsert_room("r2", "Room 2", now=1)
        store.ensure_trace_managed_room("r1", source="manual", now=1)
        store.ensure_trace_managed_room("r2", source="manual", now=1)
        return store, ArchiveReconciler(store)

    def persist_observation(
        self,
        store,
        *,
        room_id,
        key,
        members,
        observed_at,
        membership_status="complete",
        inviter_metadata_status="complete",
    ):
        observation = store.save_trace_membership_observation(
            room_id=room_id,
            source_kind="contact",
            source_state_key=f"state:{key}",
            semantic_hash=f"semantic:{key}",
            membership_status=membership_status,
            inviter_metadata_status=inviter_metadata_status,
            members=members,
            observed_at=observed_at,
        )
        if not hasattr(self, "_persisted_observations"):
            self._persisted_observations = {}
        self._persisted_observations[(room_id, key)] = observation
        return observation

    def reconcile_persisted_membership(
        self, reconciler, room_id, key, *, now
    ):
        observation = self._persisted_observations[(room_id, key)]
        return reconciler.reconcile_room(
            room_id,
            trigger="membership",
            observation_id=observation["observation_id"],
            now=now,
        )

    def test_only_complete_absence_can_close_active_episode(self):
        store, reconciler = self.make_archive()
        self.persist_observation(
            store,
            room_id="r1",
            key="present",
            members=[{"username": "wxid_b", "display_name": "B"}],
            observed_at=100,
        )
        self.reconcile_persisted_membership(
            reconciler, "r1", "present", now=100
        )
        person = store.get_trace_person_by_username("wxid_b")
        episode = store.get_active_trace_episode("r1", person["person_id"])
        self.assertIsNotNone(episode)

        self.persist_observation(
            store,
            room_id="r1",
            key="partial-empty",
            members=[],
            observed_at=110,
            membership_status="partial",
            inviter_metadata_status="unavailable",
        )
        self.reconcile_persisted_membership(
            reconciler, "r1", "partial-empty", now=110
        )
        self.assertIsNotNone(store.get_active_trace_episode("r1", person["person_id"]))

        self.persist_observation(
            store,
            room_id="r1",
            key="failed-empty",
            members=[],
            observed_at=120,
            membership_status="failed",
            inviter_metadata_status="unavailable",
        )
        self.reconcile_persisted_membership(
            reconciler, "r1", "failed-empty", now=120
        )
        self.assertIsNotNone(store.get_active_trace_episode("r1", person["person_id"]))

        self.persist_observation(
            store,
            room_id="r1",
            key="complete-empty",
            members=[],
            observed_at=130,
            membership_status="complete",
            inviter_metadata_status="unavailable",
        )
        self.reconcile_persisted_membership(
            reconciler, "r1", "complete-empty", now=130
        )

        self.assertIsNone(store.get_active_trace_episode("r1", person["person_id"]))
        history = store.list_trace_membership_episodes("r1", person["person_id"])
        self.assertEqual(len(history), 1)
        self.assertEqual(history[0]["status"], "inactive")
        self.assertEqual(history[0]["inactive_observed_at"], 130)
        self.assertIsNone(history[0]["left_event_at"])

    def test_reappearance_after_inactive_creates_episode_two(self):
        store, reconciler = self.make_archive()
        self.persist_observation(
            store, room_id="r1", key="one",
            members=[{"username": "wxid_b"}], observed_at=100,
        )
        self.reconcile_persisted_membership(reconciler, "r1", "one", now=100)
        person = store.get_trace_person_by_username("wxid_b")

        self.persist_observation(
            store, room_id="r1", key="gone", members=[], observed_at=200,
        )
        self.reconcile_persisted_membership(reconciler, "r1", "gone", now=200)
        self.persist_observation(
            store, room_id="r1", key="back",
            members=[{"username": "wxid_b"}], observed_at=300,
        )
        self.reconcile_persisted_membership(reconciler, "r1", "back", now=300)

        episodes = store.list_trace_membership_episodes("r1", person["person_id"])
        self.assertEqual([row["episode_index"] for row in episodes], [1, 2])
        self.assertEqual([row["status"] for row in episodes], ["inactive", "active"])

    def test_inviter_only_stable_wxid_creates_person_without_room_episode(self):
        store, reconciler = self.make_archive()
        self.persist_observation(
            store,
            room_id="r1",
            key="inviter-only",
            members=[{
                "username": "wxid_b",
                "display_name": "B",
                "inviter_username": "wxid_a",
            }],
            observed_at=100,
        )
        self.reconcile_persisted_membership(
            reconciler, "r1", "inviter-only", now=100
        )

        inviter = store.get_trace_person_by_username("wxid_a")
        self.assertIsNotNone(inviter)
        self.assertIsNone(store.get_active_trace_episode("r1", inviter["person_id"]))

    def test_same_wxid_reuses_global_person_across_rooms_and_duplicate_names_do_not_merge(self):
        store, reconciler = self.make_archive()
        self.persist_observation(
            store, room_id="r1", key="r1",
            members=[
                {"username": "wxid_same", "display_name": "清柠"},
                {"username": "wxid_other", "display_name": "清柠"},
            ], observed_at=100,
        )
        self.persist_observation(
            store, room_id="r2", key="r2",
            members=[{"username": "wxid_same", "display_name": "新群昵称"}],
            observed_at=101,
        )
        self.reconcile_persisted_membership(reconciler, "r1", "r1", now=100)
        self.reconcile_persisted_membership(reconciler, "r2", "r2", now=101)

        same = store.get_trace_person_by_username("wxid_same")
        other = store.get_trace_person_by_username("wxid_other")
        self.assertNotEqual(same["person_id"], other["person_id"])
        self.assertIsNotNone(store.get_active_trace_episode("r1", same["person_id"]))
        self.assertIsNotNone(store.get_active_trace_episode("r2", same["person_id"]))


    def test_conflicting_metadata_does_not_churn_manual_resolved_relation(self):
        store, reconciler = self.make_archive()
        self.persist_observation(
            store,
            room_id="r1",
            key="metadata-a",
            members=[{"username": "wxid_b", "inviter_username": "wxid_a"}],
            observed_at=100,
        )
        self.reconcile_persisted_membership(reconciler, "r1", "metadata-a", now=100)

        invitee = store.get_trace_person_by_username("wxid_b")
        inviter_a = store.get_trace_person_by_username("wxid_a")
        episode = store.get_active_trace_episode("r1", invitee["person_id"])
        store.add_trace_manual_confirmation(
            episode["episode_id"],
            inviter_person_id=inviter_a["person_id"],
            unresolved=False,
            method=None,
            event_time=None,
            reason="verified",
            operator_id="test",
            now=110,
        )
        reconciler.reconcile_room("r1", trigger="manual", now=110)
        original_relation = store.get_active_trace_relation(episode["episode_id"])

        self.persist_observation(
            store,
            room_id="r1",
            key="metadata-c",
            members=[{"username": "wxid_b", "inviter_username": "wxid_c"}],
            observed_at=120,
        )
        self.reconcile_persisted_membership(reconciler, "r1", "metadata-c", now=120)
        reconciler.reconcile_room("r1", trigger="evidence", now=121)

        active = store.get_active_trace_relation(episode["episode_id"])
        history = store.list_trace_relations(episode["episode_id"])
        conflicts = store.list_trace_conflicts(episode_id=episode["episode_id"])
        self.assertEqual(active["relation_id"], original_relation["relation_id"])
        self.assertEqual(active["inviter_person_id"], inviter_a["person_id"])
        self.assertEqual(len(history), 1)
        self.assertEqual(
            len([c for c in conflicts if c["conflict_type"] == "stable_inviter"]),
            1,
        )

    def test_conflicting_metadata_does_not_churn_system_resolved_relation(self):
        from wechat_cli.launch_ops.models import StandardEvent

        store, reconciler = self.make_archive()
        self.persist_observation(
            store,
            room_id="r1",
            key="metadata-a",
            members=[{"username": "wxid_b", "inviter_username": "wxid_a"}],
            observed_at=100,
        )
        self.reconcile_persisted_membership(reconciler, "r1", "metadata-a", now=100)
        store.insert_event(
            StandardEvent(
                event_id="invite-a-b",
                event_type="member.invited",
                room_id="r1",
                event_time=90,
                source_shard="message/message_0.db",
                source_table="Msg_test",
                source_identity="invite-a-b",
                actor_id="wxid_a",
                subject_id="wxid_b",
                payload={"method": "direct"},
            ),
            now=105,
        )
        reconciler.reconcile_room("r1", trigger="evidence", now=105)

        invitee = store.get_trace_person_by_username("wxid_b")
        inviter_a = store.get_trace_person_by_username("wxid_a")
        episode = store.get_active_trace_episode("r1", invitee["person_id"])
        original_relation = store.get_active_trace_relation(episode["episode_id"])

        self.persist_observation(
            store,
            room_id="r1",
            key="metadata-c",
            members=[{"username": "wxid_b", "inviter_username": "wxid_c"}],
            observed_at=120,
        )
        self.reconcile_persisted_membership(reconciler, "r1", "metadata-c", now=120)
        reconciler.reconcile_room("r1", trigger="evidence", now=121)

        active = store.get_active_trace_relation(episode["episode_id"])
        history = store.list_trace_relations(episode["episode_id"])
        conflicts = store.list_trace_conflicts(episode_id=episode["episode_id"])
        self.assertEqual(active["relation_id"], original_relation["relation_id"])
        self.assertEqual(active["inviter_person_id"], inviter_a["person_id"])
        self.assertEqual(len(history), 1)
        self.assertEqual(
            len([c for c in conflicts if c["conflict_type"] == "stable_inviter"]),
            1,
        )

    def test_relation_versions_only_when_resolved_inviter_changes(self):
        store, reconciler = self.make_archive()
        self.persist_observation(
            store,
            room_id="r1",
            key="metadata-a",
            members=[{"username": "wxid_b", "inviter_username": "wxid_a"}],
            observed_at=100,
        )
        self.reconcile_persisted_membership(reconciler, "r1", "metadata-a", now=100)

        invitee = store.get_trace_person_by_username("wxid_b")
        inviter_a = store.get_trace_person_by_username("wxid_a")
        episode = store.get_active_trace_episode("r1", invitee["person_id"])
        first = store.get_active_trace_relation(episode["episode_id"])
        self.assertEqual(first["inviter_person_id"], inviter_a["person_id"])

        self.persist_observation(
            store,
            room_id="r1",
            key="metadata-c",
            members=[{"username": "wxid_b", "inviter_username": "wxid_c"}],
            observed_at=120,
        )
        self.reconcile_persisted_membership(reconciler, "r1", "metadata-c", now=120)

        inviter_c = store.get_trace_person_by_username("wxid_c")
        active = store.get_active_trace_relation(episode["episode_id"])
        history = store.list_trace_relations(episode["episode_id"])
        self.assertNotEqual(active["relation_id"], first["relation_id"])
        self.assertEqual(active["inviter_person_id"], inviter_c["person_id"])
        self.assertEqual(len(history), 2)
        self.assertEqual([row["active"] for row in history], [0, 1])

    def test_store_relation_rows_use_person_ids_and_username_helper_is_joined_projection(self):
        store, reconciler = self.make_archive()
        self.persist_observation(
            store,
            room_id="r1",
            key="identity-shape",
            members=[{"username": "wxid_b", "inviter_username": "wxid_a"}],
            observed_at=100,
        )
        self.reconcile_persisted_membership(reconciler, "r1", "identity-shape", now=100)

        invitee = store.get_trace_person_by_username("wxid_b")
        inviter = store.get_trace_person_by_username("wxid_a")
        episode = store.get_active_trace_episode("r1", invitee["person_id"])
        base = store.get_active_trace_relation(episode["episode_id"])
        joined = store.get_active_trace_relation_for_username("r1", "wxid_b")

        self.assertEqual(base["invitee_person_id"], invitee["person_id"])
        self.assertEqual(base["inviter_person_id"], inviter["person_id"])
        self.assertNotIn("invitee_username", base)
        self.assertNotIn("inviter_username", base)
        self.assertEqual(joined["invitee_username"], "wxid_b")
        self.assertEqual(joined["inviter_username"], "wxid_a")
        self.assertEqual(joined["inviter_person_id"], inviter["person_id"])


    def test_old_event_is_not_attached_to_new_rejoin_episode_by_room_and_wxid(self):
        from wechat_cli.launch_ops.models import StandardEvent

        store, reconciler = self.make_archive()
        self.persist_observation(
            store, room_id="r1", key="ep1-present",
            members=[{"username": "wxid_b"}], observed_at=100,
        )
        self.reconcile_persisted_membership(reconciler, "r1", "ep1-present", now=100)
        person = store.get_trace_person_by_username("wxid_b")
        ep1 = store.get_active_trace_episode("r1", person["person_id"])

        self.persist_observation(
            store, room_id="r1", key="ep1-gone", members=[], observed_at=200,
        )
        self.reconcile_persisted_membership(reconciler, "r1", "ep1-gone", now=200)
        self.persist_observation(
            store, room_id="r1", key="ep2-present",
            members=[{"username": "wxid_b"}], observed_at=300,
        )
        self.reconcile_persisted_membership(reconciler, "r1", "ep2-present", now=300)
        ep2 = store.get_active_trace_episode("r1", person["person_id"])

        store.insert_event(
            StandardEvent(
                event_id="old",
                event_type="member.invited",
                room_id="r1",
                event_time=150,
                source_shard="message/message_0.db",
                source_table="Msg_test",
                source_identity="old",
                actor_id="wxid_a",
                subject_id="wxid_b",
                payload={"method": "direct"},
            ),
            now=400,
        )
        reconciler.reconcile_room("r1", trigger="evidence", now=400)

        evidence = store.get_trace_evidence_by_source("system_message", "old")
        self.assertEqual(evidence["attachment_state"], "attached")
        self.assertEqual(evidence["episode_id"], ep1["episode_id"])
        self.assertNotEqual(evidence["episode_id"], ep2["episode_id"])

    def test_ambiguous_history_remains_unattached_and_inert(self):
        store, reconciler = self.make_archive()
        self.persist_observation(
            store, room_id="r1", key="ep1-present",
            members=[{"username": "wxid_b", "inviter_username": "wxid_a"}],
            observed_at=100,
        )
        self.reconcile_persisted_membership(reconciler, "r1", "ep1-present", now=100)
        person = store.get_trace_person_by_username("wxid_b")
        self.persist_observation(
            store, room_id="r1", key="gone", members=[], observed_at=200,
        )
        self.reconcile_persisted_membership(reconciler, "r1", "gone", now=200)
        self.persist_observation(
            store, room_id="r1", key="ep2-present",
            members=[{"username": "wxid_b", "inviter_username": "wxid_c"}],
            observed_at=300,
        )
        self.reconcile_persisted_membership(reconciler, "r1", "ep2-present", now=300)
        ep2 = store.get_active_trace_episode("r1", person["person_id"])
        before = store.get_active_trace_relation(ep2["episode_id"])
        inviter_a = store.get_trace_person_by_username("wxid_a")

        store.insert_trace_relation_evidence(
            evidence_id="legacy-ambiguous",
            room_id="r1",
            episode_id=None,
            relation_id=None,
            attachment_state="unattached",
            attachment_reason="",
            source_type="csv_import",
            source_identity="legacy-ambiguous",
            evidence_subject_key="wxid_b",
            observed_at=400,
            inviter_person_id=inviter_a["person_id"],
            invitee_person_id=person["person_id"],
            event_time=None,
            method=None,
            raw_reference="legacy-row-1",
            authority_level=40,
        )
        reconciler.reconcile_room("r1", trigger="evidence", now=400)

        evidence = store.get_trace_evidence_by_source("csv_import", "legacy-ambiguous")
        after = store.get_active_trace_relation(ep2["episode_id"])
        self.assertEqual(evidence["attachment_state"], "ambiguous")
        self.assertIsNone(evidence["episode_id"])
        self.assertIsNone(evidence["relation_id"])
        self.assertEqual(after["relation_id"], before["relation_id"])
        self.assertEqual(after["inviter_person_id"], before["inviter_person_id"])

    def test_name_only_import_without_stable_invitee_remains_unattached_and_inert(self):
        store, reconciler = self.make_archive()
        store.insert_trace_relation_evidence(
            evidence_id="name-only",
            room_id="r1",
            episode_id=None,
            relation_id=None,
            attachment_state="unattached",
            attachment_reason="identity_unresolved",
            source_type="csv_import",
            source_identity="name-only",
            evidence_subject_key="name:清柠",
            observed_at=100,
            inviter_person_id=None,
            invitee_person_id=None,
            event_time=None,
            method=None,
            raw_reference="清柠 <- 邀请者",
            authority_level=10,
        )

        reconciler.reconcile_room("r1", trigger="evidence", now=101)

        evidence = store.get_trace_evidence_by_source("csv_import", "name-only")
        self.assertEqual(evidence["attachment_state"], "unattached")
        self.assertIsNone(evidence["episode_id"])
        self.assertIsNone(evidence["relation_id"])


    def test_manual_confirmation_invalidates_ready_and_enqueues_manual_reconcile_by_audit_id(self):
        store, reconciler = self.make_archive()
        store.set_meta("trace_archive_migration_checkpoint_v2", "ready")
        observation = self.persist_observation(
            store, room_id="r1", key="present",
            members=[{"username": "wxid_b", "inviter_username": "wxid_a"}],
            observed_at=100,
        )
        self.reconcile_persisted_membership(reconciler, "r1", "present", now=100)
        for queued in store.list_pending_trace_reconcile("r1"):
            store.ack_trace_reconcile(queued["queue_id"], now=101)
        store.mark_trace_room_projection_ready(
            "r1", observation["observation_id"], now=101
        )
        invitee = store.get_trace_person_by_username("wxid_b")
        inviter = store.get_trace_person_by_username("wxid_a")
        episode = store.get_active_trace_episode("r1", invitee["person_id"])
        self.assertTrue(
            store.get_trace_room_projection_status("r1")["use_archive_current_projection"]
        )

        audit = store.add_trace_manual_confirmation(
            episode["episode_id"],
            inviter_person_id=inviter["person_id"],
            unresolved=False,
            method=None,
            event_time=None,
            reason="verified",
            operator_id="test",
            now=110,
        )

        status = store.get_trace_room_projection_status("r1")
        self.assertEqual(status["projection_state"], "pending")
        self.assertFalse(status["use_archive_current_projection"])
        pending = store.list_pending_trace_reconcile("r1")
        manual = [row for row in pending if row["reason"] == "manual"]
        self.assertEqual(len(manual), 1)
        self.assertEqual(manual[0]["source_identity"], f"manual:{audit['audit_id']}")

    def test_new_system_evidence_invalidates_ready_projection_before_reconcile(self):
        from wechat_cli.launch_ops.models import StandardEvent

        store, reconciler = self.make_archive()
        store.set_meta("trace_archive_migration_checkpoint_v2", "ready")
        observation = self.persist_observation(
            store,
            room_id="r1",
            key="current-a",
            members=[{"username": "wxid_b", "inviter_username": "wxid_a"}],
            observed_at=100,
        )
        self.reconcile_persisted_membership(reconciler, "r1", "current-a", now=100)
        for queued in store.list_pending_trace_reconcile("r1"):
            store.ack_trace_reconcile(queued["queue_id"], now=101)
        store.mark_trace_room_projection_ready(
            "r1", observation["observation_id"], now=101
        )
        invitee = store.get_trace_person_by_username("wxid_b")
        episode = store.get_active_trace_episode("r1", invitee["person_id"])
        before = store.get_active_trace_relation(episode["episode_id"])
        self.assertTrue(store.get_trace_room_projection_status("r1")["use_archive_current_projection"])

        inserted = store.insert_event(
            StandardEvent(
                event_id="invite-c-b-new",
                event_type="member.invited",
                room_id="r1",
                event_time=90,
                source_shard="message/message_0.db",
                source_table="Msg_test",
                source_identity="invite-c-b-new",
                actor_id="wxid_c",
                subject_id="wxid_b",
                payload={"method": "direct"},
            ),
            now=110,
        )

        self.assertTrue(inserted)
        status = store.get_trace_room_projection_status("r1")
        self.assertEqual(status["projection_state"], "pending")
        self.assertFalse(status["use_archive_current_projection"])
        self.assertEqual(
            store.get_active_trace_relation(episode["episode_id"])["relation_id"],
            before["relation_id"],
        )
        pending = store.list_pending_trace_reconcile("r1")
        self.assertEqual(
            [(row["reason"], row["source_identity"]) for row in pending],
            [("evidence", "event:invite-c-b-new")],
        )

    def test_duplicate_system_evidence_does_not_invalidate_ready_projection_twice(self):
        from wechat_cli.launch_ops.models import StandardEvent
        from wechat_cli.launch_ops.watcher import LaunchOpsWatcher
        from unittest.mock import Mock

        store, reconciler = self.make_archive()
        store.set_meta("trace_archive_migration_checkpoint_v2", "ready")
        observation = self.persist_observation(
            store, room_id="r1", key="current-a",
            members=[{"username": "wxid_b", "inviter_username": "wxid_a"}],
            observed_at=100,
        )
        self.reconcile_persisted_membership(reconciler, "r1", "current-a", now=100)
        for queued in store.list_pending_trace_reconcile("r1"):
            store.ack_trace_reconcile(queued["queue_id"], now=101)
        store.mark_trace_room_projection_ready("r1", observation["observation_id"], now=101)
        event = StandardEvent(
            event_id="invite-c-b-once",
            event_type="member.invited",
            room_id="r1",
            event_time=90,
            source_shard="message/message_0.db",
            source_table="Msg_test",
            source_identity="invite-c-b-once",
            actor_id="wxid_c",
            subject_id="wxid_b",
            payload={"method": "direct"},
        )
        self.assertTrue(store.insert_event(event, now=110))

        watcher = LaunchOpsWatcher(store, Mock())
        watcher.archive_reconciler = reconciler
        watcher._drain_trace_reconcile_queue(now=111, room_id="r1")
        store.mark_trace_room_projection_ready("r1", observation["observation_id"], now=112)
        self.assertTrue(store.get_trace_room_projection_status("r1")["use_archive_current_projection"])

        self.assertFalse(store.insert_event(event, now=120))
        status = store.get_trace_room_projection_status("r1")
        self.assertEqual(status["projection_state"], "ready")
        self.assertTrue(status["use_archive_current_projection"])
        self.assertEqual(store.list_pending_trace_reconcile("r1"), [])

    def test_stopped_ready_room_keeps_raw_event_but_does_not_reconcile_archive(self):
        from unittest.mock import Mock
        from wechat_cli.launch_ops.models import StandardEvent
        from wechat_cli.launch_ops.watcher import LaunchOpsWatcher

        store, reconciler = self.make_archive()
        observation = self.persist_observation(
            store, room_id="r1", key="ready-a",
            members=[{"username": "wxid_b", "inviter_username": "wxid_a"}],
            observed_at=100,
        )
        self.reconcile_persisted_membership(reconciler, "r1", "ready-a", now=100)
        for row in store.list_pending_trace_reconcile("r1"):
            store.ack_trace_reconcile(row["queue_id"], now=101)
        store.mark_trace_room_projection_ready("r1", observation["observation_id"], now=101)
        invitee = store.get_trace_person_by_username("wxid_b")
        episode = store.get_active_trace_episode("r1", invitee["person_id"])
        before = store.get_active_trace_relation(episode["episode_id"])
        before_history = store.list_trace_relations(episode["episode_id"])

        store.set_trace_managed_room_enabled("r1", False, source="manual", now=110)
        event = StandardEvent(
            event_id="stopped-c-b",
            event_type="member.invited",
            room_id="r1",
            event_time=105,
            source_shard="message/message_0.db",
            source_table="Msg_test",
            source_identity="stopped-c-b",
            actor_id="wxid_c",
            subject_id="wxid_b",
            payload={"method": "direct"},
        )
        self.assertTrue(store.insert_event(event, now=120))
        self.assertEqual(store.list_pending_trace_reconcile("r1"), [])

        watcher = LaunchOpsWatcher(store, Mock())
        watcher.archive_reconciler.reconcile_room = Mock()
        watcher._drain_trace_reconcile_queue(now=121, room_id="r1")
        watcher.archive_reconciler.reconcile_room.assert_not_called()
        after = store.get_active_trace_relation(episode["episode_id"])
        self.assertEqual(after["relation_id"], before["relation_id"])
        self.assertEqual(store.list_trace_relations(episode["episode_id"]), before_history)

    def test_disabled_room_evidence_is_durable_without_queue_and_manual_confirmation_is_rejected(self):
        store, reconciler = self.make_archive()
        self.persist_observation(
            store, room_id="r1", key="present",
            members=[{"username": "wxid_b", "inviter_username": "wxid_a"}],
            observed_at=100,
        )
        self.reconcile_persisted_membership(reconciler, "r1", "present", now=100)
        for row in store.list_pending_trace_reconcile("r1"):
            store.ack_trace_reconcile(row["queue_id"], now=101)
        invitee = store.get_trace_person_by_username("wxid_b")
        inviter = store.get_trace_person_by_username("wxid_a")
        episode = store.get_active_trace_episode("r1", invitee["person_id"])
        store.set_trace_managed_room_enabled("r1", False, source="manual", now=110)

        with self.assertRaisesRegex(ValueError, "archive is disabled"):
            store.save_trace_membership_observation(
                room_id="r1",
                source_kind="contact",
                source_state_key="stopped-contact",
                semantic_hash="stopped-contact",
                membership_status="complete",
                inviter_metadata_status="complete",
                members=[{"username": "wxid_b"}],
                observed_at=115,
            )

        evidence = store.insert_trace_relation_evidence(
            evidence_id="disabled-import",
            room_id="r1",
            episode_id=episode["episode_id"],
            relation_id=None,
            attachment_state="attached",
            attachment_reason="explicit_import_binding",
            source_type="csv_import",
            source_identity="disabled-import",
            evidence_subject_key="wxid_b",
            observed_at=120,
            inviter_person_id=inviter["person_id"],
            invitee_person_id=invitee["person_id"],
            event_time=90,
            method=None,
            raw_reference="row:disabled",
            authority_level=40,
        )
        self.assertEqual(evidence["evidence_id"], "disabled-import")
        self.assertEqual(store.list_pending_trace_reconcile("r1"), [])

        with self.assertRaisesRegex(ValueError, "archive is disabled"):
            store.add_trace_manual_confirmation(
                episode["episode_id"],
                inviter_person_id=inviter["person_id"],
                unresolved=False,
                method=None,
                event_time=None,
                reason="must re-enable first",
                operator_id="test",
                now=130,
            )

    def test_csv_import_evidence_triggers_reconcile_without_contact_change(self):
        store, reconciler = self.make_archive()
        self.persist_observation(
            store,
            room_id="r1",
            key="current",
            members=[{"username": "wxid_b"}],
            observed_at=100,
        )
        self.reconcile_persisted_membership(reconciler, "r1", "current", now=100)
        for row in store.list_pending_trace_reconcile("r1"):
            store.ack_trace_reconcile(row["queue_id"], now=101)
        invitee = store.get_trace_person_by_username("wxid_b")
        inviter = store.ensure_trace_person("wxid_a", now=110)

        store.insert_trace_relation_evidence(
            evidence_id="csv-1",
            room_id="r1",
            episode_id=store.get_active_trace_episode(
                "r1", invitee["person_id"]
            )["episode_id"],
            relation_id=None,
            attachment_state="attached",
            attachment_reason="explicit_import_binding",
            source_type="csv_import",
            source_identity="csv-1",
            evidence_subject_key="wxid_b",
            observed_at=110,
            inviter_person_id=inviter["person_id"],
            invitee_person_id=invitee["person_id"],
            event_time=90,
            method=None,
            raw_reference="row:1",
            authority_level=40,
        )
        pending = store.list_pending_trace_reconcile("r1")
        self.assertEqual(
            [(row["reason"], row["source_identity"]) for row in pending],
            [("evidence", "evidence:csv-1")],
        )

        reconciler.reconcile_room(
            "r1",
            trigger="evidence",
            observation_id=store.get_trace_managed_room("r1")["current_observation_id"],
            now=111,
        )
        relation = store.get_active_trace_relation_for_username("r1", "wxid_b")
        self.assertEqual(relation["inviter_username"], "wxid_a")


class ArchiveInviteTreeTests(ArchiveReconcilerTests):
    def test_current_invite_tree_truncates_cycle_and_does_not_double_count(self):
        store, reconciler = self.make_archive()
        self.persist_observation(
            store,
            room_id="r1",
            key="cycle",
            members=[
                {"username": "wxid_a", "display_name": "A", "inviter_username": "wxid_c"},
                {"username": "wxid_b", "display_name": "B", "inviter_username": "wxid_a"},
                {"username": "wxid_c", "display_name": "C", "inviter_username": "wxid_b"},
            ],
            observed_at=100,
        )
        self.reconcile_persisted_membership(reconciler, "r1", "cycle", now=100)

        tree = build_current_invite_tree(store, ["r1"])

        self.assertEqual(tree["unique_people"], 3)
        self.assertEqual(tree["cycle_edges"], 1)
        person_a = store.get_trace_person_by_username("wxid_a")
        self.assertLessEqual(tree["descendant_counts"][person_a["person_id"]], 2)
        conflicts = store.list_trace_conflicts(room_id="r1")
        self.assertEqual(
            len([c for c in conflicts if c["conflict_type"] == "invite_tree_cycle"]),
            1,
        )

    def test_duplicate_display_names_with_distinct_person_ids_do_not_create_false_cycle(self):
        store, reconciler = self.make_archive()
        self.persist_observation(
            store,
            room_id="r1",
            key="duplicate-names",
            members=[
                {"username": "wxid_a", "display_name": "清柠", "inviter_username": ""},
                {"username": "wxid_b", "display_name": "清柠", "inviter_username": "wxid_a"},
            ],
            observed_at=100,
        )
        self.reconcile_persisted_membership(
            reconciler, "r1", "duplicate-names", now=100
        )

        tree = build_current_invite_tree(store, ["r1"])

        self.assertEqual(tree["unique_people"], 2)
        self.assertEqual(tree["cycle_edges"], 0)
        self.assertEqual(
            [c for c in store.list_trace_conflicts(room_id="r1")
             if c["conflict_type"] == "invite_tree_cycle"],
            [],
        )


class ArchiveMigrationTests(unittest.TestCase):
    def legacy_store_with_room(self, room_id):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        store = LaunchOpsStore(Path(tmp.name) / "launch.db")
        store.upsert_room(room_id, "Legacy Room", now=1)
        campaign = store.create_campaign("Legacy Campaign", now=2)
        store.add_room_to_campaign(campaign["campaign_id"], room_id, now=3)
        store.save_membership_snapshot(
            room_id,
            [{"username": "wxid_b", "inviter_username": "wxid_a"}],
            captured_at=10,
        )
        return store

    def test_migration_reaches_ready_on_generic_database_without_acceptance_rooms(self):
        store = self.legacy_store_with_room("unrelated@chatroom")
        migrator = ArchiveMigrator(store)

        migrator.migrate_v2(now=500)

        self.assertEqual(store.get_meta("trace_archive_schema_version"), "2")
        self.assertEqual(store.get_meta("trace_archive_migration_checkpoint_v2"), "ready")
        status = store.get_trace_room_projection_status("unrelated@chatroom")
        self.assertEqual(status["projection_state"], "pending")
        self.assertFalse(status["use_archive_current_projection"])

    def test_global_ready_allows_room_refresh_error_without_using_archive_current_projection(self):
        store = self.legacy_store_with_room("unrelated@chatroom")
        ArchiveMigrator(store).migrate_v2(now=500)
        store.mark_trace_room_refresh_error(
            "unrelated@chatroom",
            error_code="contact_read_failed",
            error_message="unavailable",
            now=510,
        )

        self.assertEqual(store.get_meta("trace_archive_migration_checkpoint_v2"), "ready")
        status = store.get_trace_room_projection_status("unrelated@chatroom")
        self.assertEqual(status["projection_state"], "refresh_error")
        self.assertFalse(status["use_archive_current_projection"])

    def test_migration_resumes_after_checkpoint_without_duplicate_archive_rows(self):
        from unittest.mock import patch

        store = self.legacy_store_with_room("unrelated@chatroom")
        migrator = ArchiveMigrator(store)

        def fail_after(checkpoint):
            if checkpoint == "durable_evidence_ingested":
                raise RuntimeError("stop")

        with patch.object(migrator, "_after_checkpoint", side_effect=fail_after):
            with self.assertRaises(RuntimeError):
                migrator.migrate_v2(now=500)

        counts_before = store.trace_archive_row_counts()
        ArchiveMigrator(store).migrate_v2(now=600)
        counts_after = store.trace_archive_row_counts()

        self.assertEqual(store.get_meta("trace_archive_migration_checkpoint_v2"), "ready")
        self.assertEqual(counts_after["people"], counts_before["people"])
        self.assertEqual(counts_after["evidence"], counts_before["evidence"])
        self.assertEqual(counts_after["observations"], counts_before["observations"])

    def test_legacy_observation_checkpoint_does_not_ingest_system_evidence_early(self):
        from unittest.mock import patch

        store = self.legacy_store_with_room("managed@chatroom")
        store.insert_event(
            StandardEvent(
                event_id="managed-invite",
                event_type="member.invited",
                room_id="managed@chatroom",
                event_time=12,
                source_shard="message/message_0.db",
                source_table="Msg_test",
                source_identity="managed-invite",
                actor_id="wxid_a",
                subject_id="wxid_b",
                payload={"method": "direct"},
            ),
            now=12,
        )
        migrator = ArchiveMigrator(store)

        def stop_after(checkpoint):
            if checkpoint == "legacy_observations_imported":
                raise RuntimeError("stop-after-legacy")

        with patch.object(migrator, "_after_checkpoint", side_effect=stop_after):
            with self.assertRaisesRegex(RuntimeError, "stop-after-legacy"):
                migrator.migrate_v2(now=500)

        self.assertEqual(
            store.get_meta("trace_archive_migration_checkpoint_v2"),
            "legacy_observations_imported",
        )
        self.assertEqual(store.trace_archive_row_counts()["evidence"], 0)

        ArchiveMigrator(store).migrate_v2(now=600)
        self.assertGreater(store.trace_archive_row_counts()["evidence"], 0)
        self.assertEqual(
            store.get_meta("trace_archive_migration_checkpoint_v2"), "ready"
        )

    def test_migration_archives_only_managed_rooms(self):
        store = self.legacy_store_with_room("managed@chatroom")
        store.upsert_room("unmanaged@chatroom", "Unmanaged", now=1)
        store.save_membership_snapshot(
            "unmanaged@chatroom",
            [{"username": "wxid_unmanaged", "inviter_username": "wxid_outside"}],
            captured_at=11,
        )
        store.insert_event(
            StandardEvent(
                event_id="unmanaged-invite",
                event_type="member.invited",
                room_id="unmanaged@chatroom",
                event_time=12,
                source_shard="message/message_0.db",
                source_table="Msg_test",
                source_identity="unmanaged-invite",
                actor_id="wxid_outside",
                subject_id="wxid_unmanaged",
                payload={"method": "direct"},
            ),
            now=12,
        )

        ArchiveMigrator(store).migrate_v2(now=500)

        with store._connect() as conn:
            unmanaged_observations = conn.execute(
                "SELECT COUNT(*) FROM trace_membership_observations WHERE room_id = ?",
                ("unmanaged@chatroom",),
            ).fetchone()[0]
            unmanaged_episodes = conn.execute(
                "SELECT COUNT(*) FROM trace_membership_episodes WHERE room_id = ?",
                ("unmanaged@chatroom",),
            ).fetchone()[0]
            unmanaged_evidence = conn.execute(
                "SELECT COUNT(*) FROM trace_relation_evidence WHERE room_id = ?",
                ("unmanaged@chatroom",),
            ).fetchone()[0]

        self.assertEqual(unmanaged_observations, 0)
        self.assertEqual(unmanaged_episodes, 0)
        self.assertEqual(unmanaged_evidence, 0)
        self.assertIsNone(store.get_trace_managed_room("unmanaged@chatroom"))

    def test_archive_migrator_contains_no_acceptance_room_ids(self):
        import inspect
        source = inspect.getsource(ArchiveMigrator)
        self.assertNotIn("50605914198@chatroom", source)
        self.assertNotIn("50364919282@chatroom", source)


if __name__ == "__main__":
    unittest.main()
