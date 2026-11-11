import tempfile
import unittest
from datetime import datetime
from pathlib import Path

from wechat_cli.launch_ops.models import StandardEvent
from wechat_cli.launch_ops.service import LaunchOpsService
from wechat_cli.launch_ops.source import GroupSource
from wechat_cli.launch_ops.store import LaunchOpsStore


class FakeSource:
    def __init__(self, groups=None, members_by_room=None, avatars=None):
        self.groups = list(groups or [])
        self.members_by_room = dict(members_by_room or {})
        self.avatars = dict(avatars or {})

    def discover_groups(self):
        return list(self.groups)

    def members(self, group):
        return list(self.members_by_room.get(group.room_id, []))

    def avatar_url(self, username="", display_name="", room_id=""):
        return (
            self.avatars.get((room_id, username))
            or self.avatars.get((room_id, display_name))
            or self.avatars.get(username)
            or self.avatars.get(display_name)
            or ""
        )


class FakeBackfillWatcher:
    def __init__(self):
        self.calls = []

    def backfill_invites(self, room_ids, *, start_time, end_time):
        self.calls.append((list(room_ids), start_time, end_time))
        return {
            "scanned_rooms": len(room_ids),
            "scanned_sources": len(room_ids),
            "candidate_system_messages": 3,
            "inserted_invite_events": 3,
            "inserted_join_events": 3,
        }


class LaunchOpsServiceTests(unittest.TestCase):
    def make_service(self, tmp, groups=None, watcher=None):
        store = LaunchOpsStore(Path(tmp) / "launch.db")
        return LaunchOpsService(store, FakeSource(groups), watcher=watcher), store


    def test_bootstrap_creates_opc_campaign_with_only_existing_numeric_groups(self):
        groups = [
            GroupSource("r9", "普通群"),
            GroupSource("r7", "🙌青年OPC全域增长群7"),
            GroupSource("r2", "🙌青年OPC全域增长群2"),
            GroupSource("r6", "🙌青年OPC全域增长群6"),
            GroupSource("r_bad", "🙌青年OPC全域增长群A"),
        ]
        with tempfile.TemporaryDirectory() as tmp:
            service, store = self.make_service(tmp, groups)

            campaign = service.ensure_default_campaign(now=100)
            campaigns = store.list_campaigns()
            rooms = store.list_campaign_rooms(campaign["campaign_id"])

        self.assertEqual(["OPC大课发售"], [item["name"] for item in campaigns])
        self.assertEqual(["r2", "r6", "r7"], [item["room_id"] for item in rooms])

    def test_bootstrap_is_one_time_and_does_not_silently_add_future_matching_group(self):
        groups = [GroupSource("r2", "🙌青年OPC全域增长群2")]
        with tempfile.TemporaryDirectory() as tmp:
            service, store = self.make_service(tmp, groups)
            campaign = service.ensure_default_campaign(now=100)
            service.source.groups.append(
                GroupSource("r8", "🙌青年OPC全域增长群8")
            )

            second = service.ensure_default_campaign(now=200)
            rooms = store.list_campaign_rooms(campaign["campaign_id"])

        self.assertEqual(campaign["campaign_id"], second["campaign_id"])
        self.assertEqual(["r2"], [item["room_id"] for item in rooms])

    def test_backfill_campaign_invites_scopes_to_active_campaign_rooms(self):
        watcher = FakeBackfillWatcher()
        with tempfile.TemporaryDirectory() as tmp:
            service, store = self.make_service(tmp, watcher=watcher)
            campaign_a = store.create_campaign("活动A", now=10)
            campaign_b = store.create_campaign("活动B", now=20)
            for room_id in ("r1", "r2", "r3"):
                store.upsert_room(room_id, room_id, now=30)
            store.add_room_to_campaign(campaign_a["campaign_id"], "r1", now=40)
            store.add_room_to_campaign(campaign_a["campaign_id"], "r2", now=41)
            store.add_room_to_campaign(campaign_b["campaign_id"], "r3", now=42)

            result = service.backfill_campaign_invites(
                campaign_a["campaign_id"],
                start_ts=100,
                end_ts=200,
            )

        self.assertEqual([(["r1", "r2"], 100, 200)], watcher.calls)
        self.assertEqual(campaign_a["campaign_id"], result["campaign_id"])
        self.assertEqual(100, result["start_ts"])
        self.assertEqual(200, result["end_ts"])
        self.assertEqual(3, result["inserted_invite_events"])
        self.assertNotIn("r3", watcher.calls[0][0])

    def test_dashboard_uses_real_campaign_rooms_and_invite_events(self):
        now = int(datetime(2026, 8, 19, 12, 0, 0).timestamp())
        day_start = int(datetime(2026, 8, 19, 0, 0, 0).timestamp())
        with tempfile.TemporaryDirectory() as tmp:
            service, store = self.make_service(tmp)
            campaign = store.create_campaign("活动", now=day_start - 10)
            for room_id, name, members in (
                ("r1", "群1", 10),
                ("r2", "群2", 20),
            ):
                store.upsert_room(room_id, name, member_count=members, now=day_start)
                store.add_room_to_campaign(
                    campaign["campaign_id"], room_id, now=day_start
                )

            self._insert_invite(
                store, "r1", "e1", day_start + 3600, "u-a", "A", "u-1", "甲", "direct"
            )
            self._insert_invite(
                store, "r1", "e2", day_start + 7200, "u-a", "A", "u-2", "乙", "direct"
            )
            self._insert_invite(
                store, "r2", "e3", day_start + 7200, "", "", "u-3", "丙", "unattributed_qr"
            )

            dashboard = service.dashboard(campaign["campaign_id"], now=now)

        self.assertEqual(2, dashboard["room_count"])
        self.assertEqual(30, dashboard["current_members"])
        self.assertEqual(3, dashboard["today_joined"])
        self.assertEqual(
            {"available": False, "value": None}, dashboard["today_left"]
        )
        self.assertEqual("A", dashboard["invite_ranking"][0]["name"])
        self.assertEqual(2, dashboard["invite_ranking"][0]["count"])
        self.assertEqual("campaign", dashboard["invite_window"])
        hourly = {item["hour"]: item["count"] for item in dashboard["hourly_joined"]}
        self.assertEqual(1, hourly[1])
        self.assertEqual(2, hourly[2])

    def test_dashboard_invite_ranking_defaults_to_campaign_history_and_supports_windows(self):
        now = int(datetime(2026, 8, 19, 12, 0, 0).timestamp())
        day_start = int(datetime(2026, 8, 19, 0, 0, 0).timestamp())
        with tempfile.TemporaryDirectory() as tmp:
            service, store = self.make_service(tmp)
            campaign = store.create_campaign("活动", now=day_start - 40 * 24 * 3600)
            store.upsert_room("r1", "群1", member_count=10, now=day_start)
            store.add_room_to_campaign(campaign["campaign_id"], "r1", now=day_start)
            self._insert_invite(
                store, "r1", "old", day_start - 20 * 24 * 3600,
                "u-old", "历史邀请人", "u-1", "甲", "direct", joined=False
            )
            self._insert_invite(
                store, "r1", "week", day_start - 2 * 24 * 3600,
                "u-week", "近七天邀请人", "u-2", "乙", "direct", joined=False
            )
            self._insert_invite(
                store, "r1", "today", day_start + 3600,
                "u-today", "今日邀请人", "u-3", "丙", "direct", joined=False
            )

            cumulative = service.dashboard(campaign["campaign_id"], now=now)
            week = service.dashboard(campaign["campaign_id"], now=now, invite_window="7d")
            today = service.dashboard(campaign["campaign_id"], now=now, invite_window="today")

        self.assertEqual(
            {"历史邀请人", "近七天邀请人", "今日邀请人"},
            {item["name"] for item in cumulative["invite_ranking"]},
        )
        self.assertEqual("campaign", cumulative["invite_window"])
        self.assertEqual(
            {"近七天邀请人", "今日邀请人"},
            {item["name"] for item in week["invite_ranking"]},
        )
        self.assertEqual("7d", week["invite_window"])
        self.assertEqual(["今日邀请人"], [item["name"] for item in today["invite_ranking"]])
        self.assertEqual("today", today["invite_window"])
        self.assertEqual(0, cumulative["today_joined"])
        self.assertEqual(0, week["today_joined"])
        self.assertEqual(0, today["today_joined"])

    def test_metadata_only_current_relation_enriches_default_views_without_inflating_history(self):
        now = int(datetime(2026, 9, 15, 18, 0, 0).timestamp())
        with tempfile.TemporaryDirectory() as tmp:
            store = LaunchOpsStore(Path(tmp) / "launch.db")
            source = FakeSource(
                avatars={
                    "u-a": "https://wx.qlogo.cn/a/132",
                    "u-b": "https://wx.qlogo.cn/b/132",
                }
            )
            service = LaunchOpsService(store, source)
            campaign = store.create_campaign("活动", now=now - 1000)
            store.upsert_room("r1", "群1", member_count=2, now=now - 1000)
            store.add_room_to_campaign(campaign["campaign_id"], "r1", now=now - 1000)
            store.save_membership_snapshot(
                "r1",
                [
                    {"username": "u-a", "display_name": "邀请者", "avatar_url": "https://wx.qlogo.cn/a/132"},
                    {
                        "username": "u-b",
                        "display_name": "历史成员",
                        "avatar_url": "https://wx.qlogo.cn/b/132",
                        "inviter_username": "u-a",
                        "inviter_source": "chat_room.ext_buffer",
                    },
                ],
                captured_at=now,
            )

            cumulative = service.dashboard(campaign["campaign_id"], now=now)
            today = service.dashboard(campaign["campaign_id"], now=now, invite_window="today")
            week = service.dashboard(campaign["campaign_id"], now=now, invite_window="7d")
            rooms = service.list_rooms(campaign_id=campaign["campaign_id"], now=now)
            trace = service.trace(campaign["campaign_id"])
            bounded_trace = service.trace(
                campaign["campaign_id"], {"start_ts": now - 7 * 24 * 3600}
            )
            detail = service.member_detail(campaign["campaign_id"], "r1", "u-b")

        self.assertEqual([("邀请者", 1)], [(item["name"], item["count"]) for item in cumulative["invite_ranking"]])
        self.assertEqual([], today["invite_ranking"])
        self.assertEqual([], week["invite_ranking"])
        self.assertEqual(0, rooms[0]["invite_event_count"])
        self.assertEqual(1, len(trace["items"]))
        self.assertIsNone(trace["items"][0]["event_time"])
        self.assertEqual("metadata", trace["items"][0]["method"])
        self.assertEqual("当前在群", trace["items"][0]["status"])
        self.assertEqual("https://wx.qlogo.cn/a/132", trace["items"][0]["inviter_avatar_url"])
        self.assertEqual([], bounded_trace["items"])
        invite_items = detail["sections"]["invite"]["items"]
        self.assertEqual(1, len(invite_items))
        self.assertIsNone(invite_items[0]["event_time"])
        self.assertEqual("metadata", invite_items[0]["payload"]["method"])
        self.assertEqual("https://wx.qlogo.cn/a/132", invite_items[0]["inviter_avatar_url"])

    def test_dashboard_uses_room_scoped_avatar_to_disambiguate_duplicate_names(self):
        now = int(datetime(2026, 8, 19, 12, 0, 0).timestamp())
        with tempfile.TemporaryDirectory() as tmp:
            store = LaunchOpsStore(Path(tmp) / "launch.db")
            source = FakeSource(
                avatars={
                    ("r1", "小米"): "https://wx.qlogo.cn/xiaomi-r1/132",
                    ("r2", "小米"): "https://wx.qlogo.cn/xiaomi-r2/132",
                }
            )
            service = LaunchOpsService(store, source)
            campaign = store.create_campaign("活动", now=now - 100)
            for room_id in ("r1", "r2"):
                store.upsert_room(room_id, room_id, member_count=1, now=now - 100)
                store.add_room_to_campaign(campaign["campaign_id"], room_id, now=now - 100)
            self._insert_invite(
                store, "r1", "xiaomi-r1", now - 10,
                "", "小米", "u-1", "甲", "direct", joined=False
            )
            dashboard = service.dashboard(campaign["campaign_id"], now=now)

        xiaomi = next(item for item in dashboard["invite_ranking"] if item["name"] == "小米")
        self.assertEqual("https://wx.qlogo.cn/xiaomi-r1/132", xiaomi["avatar_url"])

    def test_dashboard_does_not_choose_one_avatar_when_same_name_resolves_to_multiple_people(self):
        now = int(datetime(2026, 8, 19, 12, 0, 0).timestamp())
        with tempfile.TemporaryDirectory() as tmp:
            store = LaunchOpsStore(Path(tmp) / "launch.db")
            source = FakeSource(
                avatars={
                    ("r1", "小米"): "https://wx.qlogo.cn/xiaomi-r1/132",
                    ("r2", "小米"): "https://wx.qlogo.cn/xiaomi-r2/132",
                }
            )
            service = LaunchOpsService(store, source)
            campaign = store.create_campaign("活动", now=now - 100)
            for room_id in ("r1", "r2"):
                store.upsert_room(room_id, room_id, member_count=1, now=now - 100)
                store.add_room_to_campaign(campaign["campaign_id"], room_id, now=now - 100)
            self._insert_invite(
                store, "r1", "xiaomi-r1", now - 10,
                "", "小米", "u-1", "甲", "direct", joined=False
            )
            self._insert_invite(
                store, "r2", "xiaomi-r2", now - 9,
                "", "小米", "u-2", "乙", "direct", joined=False
            )
            dashboard = service.dashboard(campaign["campaign_id"], now=now)

        xiaomi = next(item for item in dashboard["invite_ranking"] if item["name"] == "小米")
        self.assertEqual("", xiaomi["avatar_url"])

    def test_dashboard_rejects_unknown_invite_window(self):
        with tempfile.TemporaryDirectory() as tmp:
            service, store = self.make_service(tmp)
            campaign = store.create_campaign("活动", now=100)
            with self.assertRaisesRegex(ValueError, "invite_window"):
                service.dashboard(campaign["campaign_id"], now=200, invite_window="month")

    def test_dashboard_counts_member_joined_not_invitation_relationship_only(self):
        now = int(datetime(2026, 8, 19, 12, 0, 0).timestamp())
        day_start = int(datetime(2026, 8, 19, 0, 0, 0).timestamp())
        with tempfile.TemporaryDirectory() as tmp:
            service, store = self.make_service(tmp)
            campaign = store.create_campaign("活动", now=day_start - 10)
            store.upsert_room("r1", "群1", member_count=10, now=day_start)
            store.add_room_to_campaign(campaign["campaign_id"], "r1", now=day_start)
            self._insert_invite(
                store, "r1", "invite-only", day_start + 3600,
                "u-a", "A", "u-1", "甲", "direct", joined=False
            )
            store.insert_event(
                StandardEvent(
                    event_id="join-real",
                    event_type="member.joined",
                    room_id="r1",
                    event_time=day_start + 7200,
                    source_shard="message/message_0.db",
                    source_table="Msg_" + "a" * 32,
                    source_identity="join-real",
                    actor_id="u-b",
                    subject_id="u-2",
                    payload={"method": "direct", "invitee_name_raw": "乙"},
                ),
                now=day_start + 7200,
            )

            dashboard = service.dashboard(campaign["campaign_id"], now=now)

        self.assertEqual(1, dashboard["today_joined"])
        hourly = {item["hour"]: item["count"] for item in dashboard["hourly_joined"]}
        self.assertEqual(0, hourly[1])
        self.assertEqual(1, hourly[2])
        self.assertEqual(1, dashboard["invite_ranking"][0]["count"])

    def test_service_enriches_rooms_rankings_trace_and_member_detail_with_real_avatars(self):
        now = int(datetime(2026, 8, 19, 12, 0, 0).timestamp())
        group = GroupSource("r1", "群1", "https://wx.qlogo.cn/room/132")
        source = FakeSource(
            [group],
            members_by_room={
                "r1": [
                    {"username": "u-a", "display_name": "邀请人", "avatar_url": "https://wx.qlogo.cn/a/132"},
                    {"username": "u-1", "display_name": "成员甲", "avatar_url": "https://wx.qlogo.cn/u1/132"},
                ]
            },
            avatars={
                "r1": "https://wx.qlogo.cn/room/132",
                "u-a": "https://wx.qlogo.cn/a/132",
                "u-1": "https://wx.qlogo.cn/u1/132",
            },
        )
        with tempfile.TemporaryDirectory() as tmp:
            store = LaunchOpsStore(Path(tmp) / "launch.db")
            service = LaunchOpsService(store, source)
            campaign = store.create_campaign("活动", now=now - 1000)
            store.upsert_room("r1", "群1", member_count=2, now=now - 1000)
            store.add_room_to_campaign(campaign["campaign_id"], "r1", now=now - 1000)
            self._insert_invite(
                store, "r1", "e1", now - 10, "u-a", "邀请人", "u-1", "成员甲", "direct"
            )

            rooms = service.list_rooms(campaign_id=campaign["campaign_id"], now=now)
            dashboard = service.dashboard(campaign["campaign_id"], now=now)
            trace = service.trace(campaign["campaign_id"])
            detail = service.member_detail(campaign["campaign_id"], "r1", "u-1")

        self.assertEqual("https://wx.qlogo.cn/room/132", rooms[0]["avatar_url"])
        self.assertEqual("https://wx.qlogo.cn/a/132", dashboard["invite_ranking"][0]["avatar_url"])
        self.assertEqual("https://wx.qlogo.cn/u1/132", trace["items"][0]["member_avatar_url"])
        self.assertEqual("https://wx.qlogo.cn/a/132", trace["items"][0]["inviter_avatar_url"])
        self.assertEqual("https://wx.qlogo.cn/room/132", trace["items"][0]["room_avatar_url"])
        self.assertEqual("https://wx.qlogo.cn/u1/132", detail["member_avatar_url"])
        self.assertEqual("https://wx.qlogo.cn/room/132", detail["room_avatar_url"])

    def test_list_rooms_enriches_today_joined_and_invite_event_counts(self):
        now = int(datetime(2026, 8, 19, 12, 0, 0).timestamp())
        day_start = int(datetime(2026, 8, 19, 0, 0, 0).timestamp())
        with tempfile.TemporaryDirectory() as tmp:
            service, store = self.make_service(tmp)
            campaign = store.create_campaign("活动", now=day_start - 10)
            store.upsert_room("r1", "群1", member_count=10, now=day_start)
            store.add_room_to_campaign(campaign["campaign_id"], "r1", now=day_start)
            self._insert_invite(
                store, "r1", "today", day_start + 3600,
                "u-a", "A", "u-1", "甲", "direct"
            )
            self._insert_invite(
                store, "r1", "old", day_start - 3600,
                "u-b", "B", "u-2", "乙", "direct"
            )

            rooms = service.list_rooms(
                campaign_id=campaign["campaign_id"], now=now
            )

        self.assertEqual(1, len(rooms))
        self.assertEqual(10, rooms[0]["member_count"])
        self.assertEqual(1, rooms[0]["today_joined"])
        self.assertEqual(2, rooms[0]["invite_event_count"])

    def test_room_members_returns_current_source_members_for_campaign_room(self):
        now = int(datetime(2026, 8, 19, 12, 0, 0).timestamp())
        groups = [GroupSource("r2", "🙌青年OPC全域增长群2")]
        members = {
            "r2": [
                {"username": "u1", "display_name": "甲"},
                {"username": "u2", "display_name": "乙"},
            ]
        }
        with tempfile.TemporaryDirectory() as tmp:
            store = LaunchOpsStore(Path(tmp) / "launch.db")
            source = FakeSource(groups, members_by_room=members)
            service = LaunchOpsService(store, source)
            campaign = store.create_campaign("活动", now=now - 10)
            store.upsert_room("r2", groups[0].display_name, member_count=2, now=now)
            store.add_room_to_campaign(campaign["campaign_id"], "r2", now=now - 10)

            result = service.room_members(campaign["campaign_id"], "r2")

        self.assertEqual("r2", result["room"]["room_id"])
        self.assertEqual(["u1", "u2"], [m["username"] for m in result["members"]])

    def test_ended_campaign_rejects_room_membership_mutation(self):
        with tempfile.TemporaryDirectory() as tmp:
            service, store = self.make_service(tmp)
            campaign = store.create_campaign("活动", now=100)
            store.upsert_room("r1", "群1", member_count=1, now=100)
            store.add_room_to_campaign(campaign["campaign_id"], "r1", now=100)
            store.upsert_room("r2", "群2", member_count=1, now=100)
            store.end_campaign(campaign["campaign_id"], now=200)

            with self.assertRaisesRegex(ValueError, "campaign has ended"):
                service.add_rooms(campaign["campaign_id"], ["r2"], now=300)
            with self.assertRaisesRegex(ValueError, "campaign has ended"):
                service.remove_room(campaign["campaign_id"], "r1", now=300)

            self.assertEqual(
                ["r1"],
                [room["room_id"] for room in store.list_campaign_rooms(campaign["campaign_id"])],
            )

    def test_trace_preserves_unattributed_qr_and_filters_by_method(self):
        now = int(datetime(2026, 8, 19, 12, 0, 0).timestamp())
        with tempfile.TemporaryDirectory() as tmp:
            service, store = self.make_service(tmp)
            campaign = store.create_campaign("活动", now=now - 1000)
            store.upsert_room("r1", "群1", member_count=10, now=now - 1000)
            store.add_room_to_campaign(campaign["campaign_id"], "r1", now=now - 1000)
            self._insert_invite(
                store, "r1", "e1", now - 10, "", "", "u-1", "甲", "unattributed_qr"
            )
            self._insert_invite(
                store, "r1", "e2", now - 9, "u-a", "A", "u-2", "乙", "direct"
            )

            result = service.trace(
                campaign["campaign_id"], {"method": "unattributed_qr"}
            )

        self.assertEqual(1, len(result["items"]))
        self.assertEqual("unattributed_qr", result["items"][0]["method"])
        self.assertEqual("待归因", result["items"][0]["inviter_name"])

    def test_member_detail_has_exact_four_approved_sections(self):
        now = int(datetime(2026, 8, 19, 12, 0, 0).timestamp())
        with tempfile.TemporaryDirectory() as tmp:
            service, store = self.make_service(tmp)
            campaign = store.create_campaign("活动", now=now - 1000)
            store.upsert_room("r1", "群1", member_count=10, now=now - 1000)
            store.add_room_to_campaign(campaign["campaign_id"], "r1", now=now - 1000)
            self._insert_invite(
                store, "r1", "e1", now - 10, "u-a", "A", "u-1", "甲", "direct"
            )
            store.insert_message(
                message_id="m1",
                event_id="message-e1",
                room_id="r1",
                event_time=now - 5,
                message_type="text",
                sender_id="u-1",
                text_content="老师，请问录播会保留多久？",
                payload={},
                now=now,
            )

            detail = service.member_detail(
                campaign["campaign_id"], "r1", "u-1"
            )

        self.assertEqual(
            ["invite", "messages", "media", "ai"], list(detail["sections"])
        )
        self.assertEqual(1, len(detail["sections"]["invite"]["items"]))
        self.assertEqual(1, len(detail["sections"]["messages"]["items"]))
        self.assertFalse(detail["sections"]["ai"]["available"])
        self.assertEqual("即将开放", detail["sections"]["ai"]["label"])

    def test_current_projection_requires_global_ready_and_room_ready_per_room(self):
        from unittest.mock import Mock, patch

        with tempfile.TemporaryDirectory() as tmp:
            store = LaunchOpsStore(Path(tmp) / "launch.db")
            for room_id in ("ready@chatroom", "error@chatroom"):
                store.upsert_room(room_id, room_id, now=1)
                store.ensure_trace_managed_room(room_id, source="manual", now=1)
            ready_obs = store.save_trace_membership_observation(
                room_id="ready@chatroom", source_kind="contact",
                source_state_key="ready-state", semantic_hash="ready-semantic",
                membership_status="complete", inviter_metadata_status="complete",
                members=[{"username": "wxid_ready"}], observed_at=100,
            )
            store.mark_trace_room_projection_ready(
                "ready@chatroom", ready_obs["observation_id"], now=101
            )
            store.mark_trace_room_refresh_error(
                "error@chatroom", error_code="contact_read_failed",
                error_message="unavailable", now=101,
            )
            store.set_meta("trace_archive_migration_checkpoint_v2", "ready")
            service = LaunchOpsService(store, Mock())

            with patch(
                "wechat_cli.launch_ops.service.build_hybrid_invite_relations",
                return_value=[{"marker": "hybrid"}],
            ) as hybrid_projection:
                ready = service._current_relations_for_room("ready@chatroom", now=200)
                error = service._current_relations_for_room("error@chatroom", now=200)

            self.assertEqual(ready["source"], "archive")
            self.assertEqual(error["source"], "hybrid")
            hybrid_projection.assert_called_once()

    def test_ready_to_pending_between_old_status_check_and_read_uses_hybrid_snapshot(self):
        from unittest.mock import Mock, patch

        with tempfile.TemporaryDirectory() as tmp:
            store = LaunchOpsStore(Path(tmp) / "launch.db")
            store.upsert_room("r1", "Room", now=1)
            store.ensure_trace_managed_room("r1", source="manual", now=1)
            observation = store.save_trace_membership_observation(
                room_id="r1", source_kind="contact", source_state_key="state-a",
                semantic_hash="sem-a", membership_status="complete",
                inviter_metadata_status="complete", members=[{"username": "wxid_b"}],
                observed_at=100,
            )
            store.mark_trace_room_projection_ready(
                "r1", observation["observation_id"], now=101
            )
            store.set_meta("trace_archive_migration_checkpoint_v2", "ready")
            self.assertTrue(
                store.get_trace_room_projection_status("r1")["use_archive_current_projection"]
            )
            store.save_trace_membership_observation(
                room_id="r1", source_kind="contact", source_state_key="state-b",
                semantic_hash="sem-b", membership_status="complete",
                inviter_metadata_status="complete", members=[{"username": "wxid_b"}],
                observed_at=102,
            )
            with patch(
                "wechat_cli.launch_ops.service.build_hybrid_invite_relations",
                return_value=[{"marker": "hybrid"}],
            ):
                result = LaunchOpsService(store, Mock())._current_relations_for_room(
                    "r1", now=103
                )

            self.assertEqual(result["source"], "hybrid")
            self.assertEqual(result["relations"], [{"marker": "hybrid"}])
            self.assertFalse(
                result["archive_status"]["use_archive_current_projection"]
            )

    def test_room_ready_still_uses_hybrid_until_global_migration_ready(self):
        from unittest.mock import Mock, patch

        with tempfile.TemporaryDirectory() as tmp:
            store = LaunchOpsStore(Path(tmp) / "launch.db")
            store.upsert_room("r1", "Room", now=1)
            store.ensure_trace_managed_room("r1", source="manual", now=1)
            observation = store.save_trace_membership_observation(
                room_id="r1", source_kind="contact", source_state_key="state",
                semantic_hash="semantic", membership_status="complete",
                inviter_metadata_status="complete", members=[{"username": "wxid_b"}],
                observed_at=100,
            )
            store.mark_trace_room_projection_ready("r1", observation["observation_id"], now=101)
            store.set_meta("trace_archive_migration_checkpoint_v2", "projection_reconciled")
            service = LaunchOpsService(store, Mock())
            with patch(
                "wechat_cli.launch_ops.service.build_hybrid_invite_relations",
                return_value=[{"marker": "hybrid"}],
            ):
                result = service._current_relations_for_room("r1", now=200)
            self.assertEqual(result["source"], "hybrid")

    def test_archive_room_exposes_refresh_state_without_internal_identity(self):
        import json
        from unittest.mock import Mock

        with tempfile.TemporaryDirectory() as tmp:
            store = LaunchOpsStore(Path(tmp) / "launch.db")
            store.upsert_room("r1", "Room", now=1)
            store.ensure_trace_managed_room("r1", source="manual", now=1)
            store.mark_trace_room_refresh_error(
                "r1", error_code="contact_read_failed", error_message="unavailable", now=10
            )
            store.set_meta("trace_archive_migration_checkpoint_v2", "ready")
            payload = LaunchOpsService(store, Mock()).archive_room("r1")
            self.assertEqual(payload["archive_projection_state"], "refresh_error")
            self.assertEqual(payload["archive_current_source"], "hybrid")
            self.assertEqual(payload["archive_refresh_error_code"], "contact_read_failed")
            encoded = json.dumps(payload, ensure_ascii=False)
            self.assertNotIn("person_id", encoded)
            self.assertNotIn("wxid_", encoded)

    def test_new_system_evidence_for_ready_room_uses_hybrid_until_reconciled(self):
        from unittest.mock import Mock, patch
        from wechat_cli.launch_ops.archive import ArchiveReconciler

        with tempfile.TemporaryDirectory() as tmp:
            store = LaunchOpsStore(Path(tmp) / "launch.db")
            store.upsert_room("r1", "Room", now=1)
            store.ensure_trace_managed_room("r1", source="manual", now=1)
            observation = store.save_trace_membership_observation(
                room_id="r1", source_kind="contact", source_state_key="state-a",
                semantic_hash="semantic-a", membership_status="complete",
                inviter_metadata_status="complete",
                members=[{"username": "wxid_b", "inviter_username": "wxid_a"}],
                observed_at=100,
            )
            ArchiveReconciler(store).reconcile_room(
                "r1", trigger="membership", observation_id=observation["observation_id"], now=100
            )
            for queued in store.list_pending_trace_reconcile("r1"):
                store.ack_trace_reconcile(queued["queue_id"], now=101)
            store.mark_trace_room_projection_ready("r1", observation["observation_id"], now=101)
            store.set_meta("trace_archive_migration_checkpoint_v2", "ready")
            service = LaunchOpsService(store, Mock())
            store.insert_event(
                StandardEvent(
                    event_id="new-c-b", event_type="member.invited", room_id="r1",
                    event_time=95, source_shard="message/message_0.db",
                    source_table="Msg_test", source_identity="new-c-b",
                    actor_id="wxid_c", subject_id="wxid_b", payload={"method": "direct"},
                ),
                now=110,
            )
            with patch(
                "wechat_cli.launch_ops.service.build_hybrid_invite_relations",
                return_value=[{"marker": "hybrid-after-evidence"}],
            ):
                result = service._current_relations_for_room("r1", now=111)
            self.assertEqual(result["source"], "hybrid")
            self.assertEqual(store.get_trace_room_projection_status("r1")["projection_state"], "pending")

    def test_manual_confirm_returns_only_after_current_relation_is_reconciled(self):
        from unittest.mock import Mock
        from wechat_cli.launch_ops.archive import ArchiveReconciler
        from wechat_cli.launch_ops.watcher import LaunchOpsWatcher

        with tempfile.TemporaryDirectory() as tmp:
            store = LaunchOpsStore(Path(tmp) / "launch.db")
            store.upsert_room("r1", "Room", now=1)
            store.ensure_trace_managed_room("r1", source="manual", now=1)
            observation = store.save_trace_membership_observation(
                room_id="r1", source_kind="contact", source_state_key="state-a",
                semantic_hash="semantic-a", membership_status="complete",
                inviter_metadata_status="complete",
                members=[{"username": "wxid_b", "inviter_username": "wxid_a"}],
                observed_at=100,
            )
            ArchiveReconciler(store).reconcile_room(
                "r1", trigger="membership", observation_id=observation["observation_id"], now=100
            )
            for queued in store.list_pending_trace_reconcile("r1"):
                store.ack_trace_reconcile(queued["queue_id"], now=101)
            store.mark_trace_room_projection_ready("r1", observation["observation_id"], now=101)
            store.set_meta("trace_archive_migration_checkpoint_v2", "ready")
            invitee = store.get_trace_person_by_username("wxid_b")
            episode = store.get_active_trace_episode("r1", invitee["person_id"])
            source = Mock()
            watcher = LaunchOpsWatcher(store, source)
            service = LaunchOpsService(store, source, watcher=watcher)
            result = service.manual_confirm_archive(
                "r1", episode["episode_id"], inviter_username="wxid_c",
                unresolved=False, method="direct", event_time=90,
                reason="operator verified",
            )
            inviter_c = store.get_trace_person_by_username("wxid_c")
            active = store.get_active_trace_relation(episode["episode_id"])
            self.assertEqual(active["inviter_person_id"], inviter_c["person_id"])
            self.assertEqual(result["projection_state"], "ready")
            self.assertEqual(store.list_pending_trace_reconcile("r1"), [])

    def test_manual_confirm_reconcile_failure_does_not_return_success(self):
        from unittest.mock import Mock
        from wechat_cli.launch_ops.archive import ArchiveReconciler
        from wechat_cli.launch_ops.watcher import LaunchOpsWatcher

        with tempfile.TemporaryDirectory() as tmp:
            store = LaunchOpsStore(Path(tmp) / "launch.db")
            store.upsert_room("r1", "Room", now=1)
            store.ensure_trace_managed_room("r1", source="manual", now=1)
            observation = store.save_trace_membership_observation(
                room_id="r1", source_kind="contact", source_state_key="state-a",
                semantic_hash="semantic-a", membership_status="complete",
                inviter_metadata_status="complete",
                members=[{"username": "wxid_b", "inviter_username": "wxid_a"}],
                observed_at=100,
            )
            ArchiveReconciler(store).reconcile_room(
                "r1", trigger="membership", observation_id=observation["observation_id"], now=100
            )
            for queued in store.list_pending_trace_reconcile("r1"):
                store.ack_trace_reconcile(queued["queue_id"], now=101)
            store.mark_trace_room_projection_ready("r1", observation["observation_id"], now=101)
            invitee = store.get_trace_person_by_username("wxid_b")
            episode = store.get_active_trace_episode("r1", invitee["person_id"])
            source = Mock()
            watcher = LaunchOpsWatcher(store, source)
            watcher.archive_reconciler.reconcile_room = Mock(side_effect=RuntimeError("reconcile boom"))
            service = LaunchOpsService(store, source, watcher=watcher)
            with self.assertRaisesRegex(RuntimeError, "reconcile boom"):
                service.manual_confirm_archive(
                    "r1", episode["episode_id"], inviter_username="wxid_c",
                    unresolved=False, method=None, event_time=None, reason="operator verified",
                )
            status = store.get_trace_room_projection_status("r1")
            self.assertEqual(status["projection_state"], "refresh_error")
            self.assertEqual([row["reason"] for row in store.list_pending_trace_reconcile("r1")], ["manual"])

    def _insert_invite(
        self,
        store,
        room_id,
        event_id,
        event_time,
        inviter_username,
        inviter_name,
        invitee_username,
        invitee_name,
        method,
        joined=True,
    ):
        payload = {
            "method": method,
            "inviter_username": inviter_username,
            "inviter_name_raw": inviter_name,
            "invitee_username": invitee_username,
            "invitee_name_raw": invitee_name,
            "inviter_identity_status": (
                "unattributed" if method == "unattributed_qr" else "resolved"
            ),
            "invitee_identity_status": "resolved",
            "raw_text": "notice",
        }
        store.insert_event(
            StandardEvent(
                event_id=event_id,
                event_type="member.invited",
                room_id=room_id,
                event_time=event_time,
                source_shard="message/message_0.db",
                source_table="Msg_" + "a" * 32,
                source_identity=event_id,
                actor_id=inviter_username,
                subject_id=invitee_username,
                payload=payload,
            ),
            now=event_time,
        )
        if joined:
            joined_payload = dict(payload)
            joined_payload["provenance"] = "invite_system_notice"
            store.insert_event(
                StandardEvent(
                    event_id=f"join-{event_id}",
                    event_type="member.joined",
                    room_id=room_id,
                    event_time=event_time,
                    source_shard="message/message_0.db",
                    source_table="Msg_" + "a" * 32,
                    source_identity=f"join-{event_id}",
                    actor_id=inviter_username,
                    subject_id=invitee_username,
                    payload=joined_payload,
                ),
                now=event_time,
            )


if __name__ == "__main__":
    unittest.main()
