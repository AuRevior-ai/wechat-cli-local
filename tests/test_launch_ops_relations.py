import tempfile
import unittest
from pathlib import Path

from wechat_cli.launch_ops.models import StandardEvent
from wechat_cli.launch_ops.relations import build_hybrid_invite_relations
from wechat_cli.launch_ops.store import LaunchOpsStore


class LaunchOpsHybridRelationTests(unittest.TestCase):
    def make_store(self, path):
        return LaunchOpsStore(path)

    def save_snapshot(self, store, members, room_id="room@chatroom", captured_at=200):
        store.save_membership_snapshot(room_id, members, captured_at=captured_at)

    def insert_invite(
        self,
        store,
        *,
        event_id="invite:1",
        room_id="room@chatroom",
        event_time=100,
        actor_id="wxid_inviter",
        subject_id="wxid_member",
        inviter_name="邀请者",
        invitee_name="成员",
        method="direct",
    ):
        store.insert_event(
            StandardEvent(
                event_id=event_id,
                event_type="member.invited",
                room_id=room_id,
                event_time=event_time,
                source_shard="message/message_0.db",
                source_table="Msg_test",
                source_identity=event_id,
                actor_id=actor_id,
                subject_id=subject_id,
                payload={
                    "method": method,
                    "inviter_name_raw": inviter_name,
                    "invitee_name_raw": invitee_name,
                    "inviter_username": actor_id,
                    "invitee_username": subject_id,
                },
            ),
            now=event_time,
        )

    def test_metadata_only_current_member_becomes_unknown_time_relation(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = self.make_store(Path(tmp) / "launch.db")
            self.save_snapshot(
                store,
                [
                    {"username": "wxid_inviter", "display_name": "邀请者"},
                    {
                        "username": "wxid_member",
                        "display_name": "成员",
                        "inviter_username": "wxid_inviter",
                        "inviter_source": "chat_room.ext_buffer",
                    },
                ],
            )

            relations = build_hybrid_invite_relations(
                store, ["room@chatroom"], include_metadata=True
            )

        self.assertEqual(1, len(relations))
        relation = relations[0]
        self.assertIsNone(relation["event_time"])
        self.assertEqual("metadata", relation["payload"]["method"])
        self.assertEqual("unknown", relation["time_status"])
        self.assertEqual("chat_room.ext_buffer", relation["relation_source"])
        self.assertEqual("wxid_inviter", relation["actor_id"])
        self.assertEqual("wxid_member", relation["subject_id"])
        self.assertEqual("邀请者", relation["payload"]["inviter_name_raw"])
        self.assertEqual("成员", relation["payload"]["invitee_name_raw"])

    def test_real_event_wins_and_metadata_does_not_double_count(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = self.make_store(Path(tmp) / "launch.db")
            self.save_snapshot(
                store,
                [
                    {"username": "wxid_inviter", "display_name": "邀请者"},
                    {"username": "wxid_member", "display_name": "成员", "inviter_username": "wxid_inviter"},
                ],
            )
            self.insert_invite(store, method="qr")

            relations = build_hybrid_invite_relations(
                store, ["room@chatroom"], include_metadata=True
            )

        self.assertEqual(1, len(relations))
        self.assertEqual("invite:1", relations[0]["event_id"])
        self.assertEqual(100, relations[0]["event_time"])
        self.assertEqual("qr", relations[0]["payload"]["method"])
        self.assertEqual("event", relations[0]["relation_source"])
        self.assertEqual("known", relations[0]["time_status"])

    def test_missing_real_actor_is_enriched_from_exact_subject_metadata(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = self.make_store(Path(tmp) / "launch.db")
            self.save_snapshot(
                store,
                [
                    {"username": "wxid_inviter", "display_name": "邀请者"},
                    {"username": "wxid_member", "display_name": "成员", "inviter_username": "wxid_inviter"},
                ],
            )
            self.insert_invite(store, actor_id="", inviter_name="待确认")

            relations = build_hybrid_invite_relations(
                store, ["room@chatroom"], include_metadata=True
            )

        self.assertEqual(1, len(relations))
        self.assertEqual("wxid_inviter", relations[0]["actor_id"])
        self.assertEqual("邀请者", relations[0]["payload"]["inviter_name_raw"])
        self.assertEqual("chat_room.ext_buffer", relations[0]["payload"]["attribution_source"])

    def test_stable_conflict_keeps_real_event_and_suppresses_metadata_duplicate(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = self.make_store(Path(tmp) / "launch.db")
            self.save_snapshot(
                store,
                [
                    {"username": "wxid_meta", "display_name": "元数据邀请者"},
                    {"username": "wxid_real", "display_name": "事件邀请者"},
                    {"username": "wxid_member", "display_name": "成员", "inviter_username": "wxid_meta"},
                ],
            )
            self.insert_invite(
                store,
                actor_id="wxid_real",
                inviter_name="事件邀请者",
            )

            relations = build_hybrid_invite_relations(
                store, ["room@chatroom"], include_metadata=True
            )

        self.assertEqual(1, len(relations))
        self.assertEqual("wxid_real", relations[0]["actor_id"])
        self.assertTrue(relations[0]["metadata_conflict"])

    def test_name_only_history_consumes_matching_metadata_edge_without_assigning_ambiguous_ids(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = self.make_store(Path(tmp) / "launch.db")
            self.save_snapshot(
                store,
                [
                    {"username": "wxid_a", "display_name": "贾烟火"},
                    {"username": "wxid_b", "display_name": "贾烟火", "inviter_username": "wxid_a"},
                ],
            )
            self.insert_invite(
                store,
                actor_id="",
                subject_id="",
                inviter_name="贾烟火",
                invitee_name="贾烟火",
            )

            relations = build_hybrid_invite_relations(
                store, ["room@chatroom"], include_metadata=True
            )

        self.assertEqual(1, len(relations))
        self.assertEqual("invite:1", relations[0]["event_id"])
        self.assertEqual("", relations[0]["subject_id"])

    def test_rejoin_history_is_not_erased_by_single_current_metadata_edge(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = self.make_store(Path(tmp) / "launch.db")
            self.save_snapshot(
                store,
                [
                    {"username": "wxid_a", "display_name": "A"},
                    {"username": "wxid_b", "display_name": "B", "inviter_username": "wxid_a"},
                ],
            )
            self.insert_invite(
                store,
                event_id="invite:1",
                event_time=100,
                actor_id="wxid_a",
                subject_id="wxid_b",
                inviter_name="A",
                invitee_name="B",
            )
            self.insert_invite(
                store,
                event_id="invite:2",
                event_time=150,
                actor_id="wxid_a",
                subject_id="wxid_b",
                inviter_name="A",
                invitee_name="B",
            )

            relations = build_hybrid_invite_relations(
                store, ["room@chatroom"], include_metadata=True
            )

        self.assertEqual(2, len(relations))
        self.assertEqual({"invite:1", "invite:2"}, {item["event_id"] for item in relations})

    def test_departed_historical_invitee_remains_without_metadata(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = self.make_store(Path(tmp) / "launch.db")
            self.save_snapshot(
                store,
                [{"username": "wxid_current", "display_name": "当前成员"}],
            )
            self.insert_invite(
                store,
                subject_id="wxid_departed",
                invitee_name="已退群成员",
            )

            relations = build_hybrid_invite_relations(
                store, ["room@chatroom"], include_metadata=True
            )

        self.assertEqual(1, len(relations))
        self.assertEqual("wxid_departed", relations[0]["subject_id"])
        self.assertEqual(100, relations[0]["event_time"])

    def test_include_metadata_false_returns_only_timestamped_events(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = self.make_store(Path(tmp) / "launch.db")
            self.save_snapshot(
                store,
                [
                    {"username": "wxid_a", "display_name": "A"},
                    {"username": "wxid_b", "display_name": "B", "inviter_username": "wxid_a"},
                ],
            )

            relations = build_hybrid_invite_relations(
                store, ["room@chatroom"], include_metadata=False
            )

        self.assertEqual([], relations)


if __name__ == "__main__":
    unittest.main()
