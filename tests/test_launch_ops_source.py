import os
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch



def _pb_varint(value):
    out = bytearray()
    while True:
        byte = value & 0x7F
        value >>= 7
        if value:
            out.append(byte | 0x80)
        else:
            out.append(byte)
            return bytes(out)


def _pb_field_bytes(field, value):
    raw = value.encode("utf-8") if isinstance(value, str) else bytes(value)
    return _pb_varint((field << 3) | 2) + _pb_varint(len(raw)) + raw


def _pb_field_varint(field, value):
    return _pb_varint((field << 3) | 0) + _pb_varint(value)


def _pb_member(username, inviter="", room_name="", member_flag=1):
    payload = _pb_field_bytes(1, username)
    if room_name:
        payload += _pb_field_bytes(2, room_name)
    payload += _pb_field_varint(3, member_flag)
    if inviter:
        payload += _pb_field_bytes(4, inviter)
    return _pb_field_bytes(1, payload)


from wechat_cli.launch_ops.source import (
    GroupSource,
    LocalWeChatSourceAdapter,
    MessageSource,
    SourceStamp,
    TableHead,
    discover_group_members,
    discover_groups,
    discover_message_sources,
    read_incremental_rows,
    read_rows_between,
    resolve_message_sources,
    source_stamp,
)


class FakeContext:
    def __init__(self, contact_db, db_dir=None):
        self.decrypted_dir = str(Path(contact_db).parent)
        self.db_dir = str(db_dir or Path(contact_db).parent)

        class Cache:
            def get(_, rel_key):
                if rel_key.replace("\\", "/") == "contact/contact.db":
                    return str(contact_db)
                return None

        self.cache = Cache()


class LaunchOpsSourceTests(unittest.TestCase):
    def _membership_contract_adapter(self):
        ctx = type(
            "Ctx",
            (),
            {
                "cache": object(),
                "db_dir": "db-root",
                "decrypted_dir": "decrypted-root",
                "msg_db_keys": [],
                "refresh_message_shards": lambda self: False,
            },
        )()
        return LocalWeChatSourceAdapter(ctx), GroupSource("r1", "Room")

    @patch.object(LocalWeChatSourceAdapter, "_read_membership_source")
    def test_membership_observation_complete_members_and_complete_inviter_metadata(self, read_mock):
        from wechat_cli.launch_ops.source import MembershipSourceRead

        read_mock.return_value = MembershipSourceRead(
            members=(
                {"username": "wxid_a", "inviter_username": ""},
                {"username": "wxid_b", "inviter_username": "wxid_a"},
            ),
            membership_status="complete",
            inviter_metadata_status="complete",
            diagnostic_code="",
            source_stamp=SourceStamp(1, 2, 3, 4),
        )
        adapter, group = self._membership_contract_adapter()

        observation = adapter.membership_observation(group, observed_at=100, force=False)

        self.assertEqual(observation.membership_status, "complete")
        self.assertEqual(observation.inviter_metadata_status, "complete")
        self.assertEqual([m["username"] for m in observation.members], ["wxid_a", "wxid_b"])

    @patch.object(LocalWeChatSourceAdapter, "_read_membership_source")
    def test_membership_observation_complete_members_when_inviter_metadata_unavailable(self, read_mock):
        from wechat_cli.launch_ops.source import MembershipSourceRead

        read_mock.return_value = MembershipSourceRead(
            members=(
                {"username": "wxid_a", "inviter_username": ""},
                {"username": "wxid_b", "inviter_username": ""},
            ),
            membership_status="complete",
            inviter_metadata_status="unavailable",
            diagnostic_code="ext_buffer_unavailable",
            source_stamp=SourceStamp(1, 2, 3, 4),
        )
        adapter, group = self._membership_contract_adapter()

        observation = adapter.membership_observation(group, observed_at=100, force=False)

        self.assertEqual(observation.membership_status, "complete")
        self.assertEqual(observation.inviter_metadata_status, "unavailable")
        self.assertEqual([m["username"] for m in observation.members], ["wxid_a", "wxid_b"])

    @patch.object(LocalWeChatSourceAdapter, "_read_membership_source")
    def test_membership_observation_complete_members_with_partial_inviter_metadata(self, read_mock):
        from wechat_cli.launch_ops.source import MembershipSourceRead

        read_mock.return_value = MembershipSourceRead(
            members=(
                {"username": "wxid_a", "inviter_username": ""},
                {"username": "wxid_b", "inviter_username": "wxid_a"},
            ),
            membership_status="complete",
            inviter_metadata_status="partial",
            diagnostic_code="ext_buffer_partial",
            source_stamp=SourceStamp(1, 2, 3, 4),
        )
        adapter, group = self._membership_contract_adapter()

        observation = adapter.membership_observation(group, observed_at=100, force=False)

        self.assertEqual(observation.membership_status, "complete")
        self.assertEqual(observation.inviter_metadata_status, "partial")

    @patch.object(LocalWeChatSourceAdapter, "_read_membership_source")
    def test_membership_observation_failed_member_read_is_not_empty_complete_group(self, read_mock):
        from wechat_cli.launch_ops.source import MembershipSourceRead

        read_mock.return_value = MembershipSourceRead(
            members=(),
            membership_status="failed",
            inviter_metadata_status="unavailable",
            diagnostic_code="contact_read_failed",
            source_stamp=None,
        )
        adapter, group = self._membership_contract_adapter()

        observation = adapter.membership_observation(group, observed_at=100, force=False)

        self.assertEqual(observation.membership_status, "failed")
        self.assertEqual(observation.inviter_metadata_status, "unavailable")
        self.assertEqual(observation.members, ())

    @patch.object(LocalWeChatSourceAdapter, "_read_membership_source")
    def test_membership_observation_force_refresh_bypasses_cached_contact_state(self, read_mock):
        from wechat_cli.launch_ops.source import MembershipSourceRead

        read_mock.return_value = MembershipSourceRead(
            members=({"username": "wxid_a", "inviter_username": ""},),
            membership_status="complete",
            inviter_metadata_status="complete",
            diagnostic_code="",
            source_stamp=SourceStamp(1, 2, 3, 4),
        )
        adapter, group = self._membership_contract_adapter()

        adapter.membership_observation(group, observed_at=100, force=False)
        adapter.membership_observation(group, observed_at=101, force=True)

        self.assertEqual(read_mock.call_args_list[0].kwargs["force"], False)
        self.assertEqual(read_mock.call_args_list[1].kwargs["force"], True)

    def test_discovers_only_real_local_chatrooms(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "contact.db"
            conn = sqlite3.connect(path)
            try:
                conn.execute(
                    "CREATE TABLE contact (username TEXT, nick_name TEXT, remark TEXT, small_head_url TEXT, big_head_url TEXT)"
                )
                conn.executemany(
                    "INSERT INTO contact(username, nick_name, remark, small_head_url, big_head_url) VALUES (?, ?, ?, ?, ?)",
                    [
                        ("friend", "普通朋友", "", "https://wx.qlogo.cn/friend/132", ""),
                        ("room7@chatroom", "🙌青年OPC全域增长群7", "", "https://wx.qlogo.cn/r7/132", ""),
                        ("room2@chatroom", "🙌青年OPC全域增长群2", "", "https://wx.qlogo.cn/r2/132", ""),
                        ("room6@chatroom", "原昵称", "🙌青年OPC全域增长群6", "https://wx.qlogo.cn/r6/132", ""),
                    ],
                )
                conn.commit()
            finally:
                conn.close()

            groups = discover_groups(FakeContext(path))

        self.assertEqual(
            [
                ("room2@chatroom", "🙌青年OPC全域增长群2", "https://wx.qlogo.cn/r2/132"),
                ("room6@chatroom", "🙌青年OPC全域增长群6", "https://wx.qlogo.cn/r6/132"),
                ("room7@chatroom", "🙌青年OPC全域增长群7", "https://wx.qlogo.cn/r7/132"),
            ],
            [
                (group.room_id, group.display_name, group.avatar_url)
                for group in groups
                if group.display_name.startswith("🙌青年OPC")
            ],
        )

    @patch("wechat_cli.launch_ops.source.resolve_chat_context")
    def test_resolves_message_sources_with_originating_shard(self, resolve_mock):
        resolve_mock.return_value = {
            "message_tables": [
                {
                    "rel_key": "message/message_0.db",
                    "db_path": "decrypted-message.db",
                    "table_name": "Msg_" + "a" * 32,
                }
            ]
        }
        ctx = type(
            "Ctx",
            (),
            {"msg_db_keys": ["message/message_0.db"], "cache": object(), "decrypted_dir": "x"},
        )()
        room = GroupSource("room@chatroom", "群")

        sources = resolve_message_sources(ctx, room)

        self.assertEqual("message/message_0.db", sources[0].source_shard)
        self.assertEqual("room@chatroom", sources[0].room_id)
        resolve_mock.assert_called_once_with(
            "room@chatroom", ctx.msg_db_keys, ctx.cache, ctx.decrypted_dir
        )

    def test_local_adapter_refreshes_long_lived_shards_before_discovery(self):
        class Context:
            decrypted_dir = "decrypted"
            db_dir = "db"
            msg_db_keys = []
            cache = object()

            def __init__(self):
                self.refresh_calls = 0

            def refresh_message_shards(self):
                self.refresh_calls += 1
                return True

        ctx = Context()
        adapter = LocalWeChatSourceAdapter(ctx)
        adapter._source_cache["room@chatroom"] = [
            MessageSource(
                room_id="room@chatroom",
                source_shard="message/message_0.db",
                db_path="old.db",
                table_name="Msg_" + "a" * 32,
            )
        ]
        with patch("wechat_cli.launch_ops.source.contact_stamp", return_value=None), \
                patch("wechat_cli.launch_ops.source.discover_groups", return_value=[]), \
                patch("wechat_cli.launch_ops.source.discover_group_members", return_value={}):
            groups = adapter.discover_groups()

        self.assertEqual([], groups)
        self.assertEqual(1, ctx.refresh_calls)
        self.assertEqual({}, adapter._source_cache)

    def test_local_adapter_refreshes_only_requested_shard_and_reads_cached_members(self):
        class Cache:
            def __init__(self):
                self.calls = []

            def get(self, rel_key):
                self.calls.append(rel_key)
                return "fresh.db"

        ctx = type(
            "Ctx",
            (),
            {
                "cache": Cache(),
                "db_dir": "db-root",
                "decrypted_dir": "decrypted-root",
                "msg_db_keys": [],
            },
        )()
        adapter = LocalWeChatSourceAdapter(ctx)
        adapter._members_cache["room@chatroom"] = [{"username": "u1"}]
        source = MessageSource(
            room_id="room@chatroom",
            source_shard="message/message_0.db",
            db_path="old.db",
            table_name="Msg_" + "a" * 32,
        )

        refreshed = adapter.refresh(source)
        members = adapter.members(GroupSource("room@chatroom", "群"))

        self.assertEqual("fresh.db", refreshed.db_path)
        self.assertEqual(["message/message_0.db"], ctx.cache.calls)
        self.assertEqual([{"username": "u1"}], members)

    def test_discovers_group_members_with_ext_buffer_inviter_metadata(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "contact.db"
            conn = sqlite3.connect(path)
            try:
                conn.execute(
                    "CREATE TABLE contact (id INTEGER PRIMARY KEY, username TEXT, nick_name TEXT, remark TEXT, small_head_url TEXT, big_head_url TEXT)"
                )
                conn.execute("CREATE TABLE chatroom_member (room_id INTEGER, member_id INTEGER)")
                conn.execute("CREATE TABLE chat_room (id INTEGER PRIMARY KEY, ext_buffer BLOB)")
                conn.executemany(
                    "INSERT INTO contact(id, username, nick_name, remark, small_head_url, big_head_url) VALUES (?, ?, ?, ?, ?, ?)",
                    [
                        (1, "room@chatroom", "测试群", "", "", ""),
                        (2, "wxid_inviter", "邀请者", "", "", ""),
                        (3, "wxid_member", "成员", "", "", ""),
                    ],
                )
                conn.executemany(
                    "INSERT INTO chatroom_member(room_id, member_id) VALUES (?, ?)",
                    [(1, 2), (1, 3)],
                )
                blob = b"".join(
                    [
                        _pb_member("wxid_inviter", member_flag=17),
                        _pb_member(
                            "wxid_member",
                            inviter="wxid_inviter",
                            room_name="群内成员名",
                            member_flag=2065,
                        ),
                        _pb_member("wxid_departed", inviter="wxid_inviter"),
                    ]
                )
                conn.execute("INSERT INTO chat_room(id, ext_buffer) VALUES (?, ?)", (1, blob))
                conn.commit()
            finally:
                conn.close()

            members = discover_group_members(FakeContext(path))["room@chatroom"]

        self.assertEqual(2, len(members))
        by_username = {member["username"]: member for member in members}
        self.assertEqual("", by_username["wxid_inviter"]["inviter_username"])
        self.assertEqual(17, by_username["wxid_inviter"]["member_flag"])
        self.assertEqual("wxid_inviter", by_username["wxid_member"]["inviter_username"])
        self.assertEqual("chat_room.ext_buffer", by_username["wxid_member"]["inviter_source"])
        self.assertEqual("群内成员名", by_username["wxid_member"]["room_display_name"])
        self.assertEqual(2065, by_username["wxid_member"]["member_flag"])
        self.assertNotIn("wxid_departed", by_username)

    def test_discovers_group_members_with_one_bulk_join(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "contact.db"
            conn = sqlite3.connect(path)
            try:
                conn.execute(
                    "CREATE TABLE contact (id INTEGER PRIMARY KEY, username TEXT, nick_name TEXT, remark TEXT, small_head_url TEXT, big_head_url TEXT)"
                )
                conn.execute("CREATE TABLE chatroom_member (room_id INTEGER, member_id INTEGER)")
                conn.executemany(
                    "INSERT INTO contact(id, username, nick_name, remark, small_head_url, big_head_url) VALUES (?, ?, ?, ?, ?, ?)",
                    [
                        (1, "room@chatroom", "测试群", "", "https://wx.qlogo.cn/room/132", ""),
                        (2, "wxid_a", "甲", "", "https://wx.qlogo.cn/a/132", ""),
                        (3, "wxid_b", "乙", "备注乙", "", "https://wx.qlogo.cn/b/132"),
                    ],
                )
                conn.executemany(
                    "INSERT INTO chatroom_member(room_id, member_id) VALUES (?, ?)",
                    [(1, 2), (1, 3)],
                )
                conn.commit()
            finally:
                conn.close()

            members = discover_group_members(FakeContext(path))

        self.assertEqual(
            [
                {"username": "wxid_a", "nick_name": "甲", "remark": "", "display_name": "甲", "avatar_url": "https://wx.qlogo.cn/a/132"},
                {"username": "wxid_b", "nick_name": "乙", "remark": "备注乙", "display_name": "备注乙", "avatar_url": "https://wx.qlogo.cn/b/132"},
            ],
            members["room@chatroom"],
        )

    @patch("wechat_cli.launch_ops.source.contact_stamp", return_value=None)
    @patch("wechat_cli.launch_ops.source.discover_group_members")
    @patch("wechat_cli.launch_ops.source.discover_groups")
    def test_local_adapter_preloads_members_once_per_discovery(
        self,
        groups_mock,
        members_mock,
        _stamp_mock,
    ):
        class Context:
            decrypted_dir = "decrypted"
            db_dir = "db"
            msg_db_keys = []
            cache = object()

            def refresh_message_shards(self):
                return False

        group = GroupSource("room@chatroom", "测试群")
        groups_mock.return_value = [group]
        members_mock.return_value = {group.room_id: [{"username": "u1"}]}
        adapter = LocalWeChatSourceAdapter(Context())

        discovered = adapter.discover_groups()
        first = adapter.members(group)
        second = adapter.members(group)

        self.assertEqual([group], discovered)
        self.assertEqual([{"username": "u1"}], first)
        self.assertEqual(first, second)
        members_mock.assert_called_once()

    @patch("wechat_cli.launch_ops.source.contact_stamp")
    @patch("wechat_cli.launch_ops.source.discover_group_members")
    @patch("wechat_cli.launch_ops.source.discover_groups")
    def test_local_adapter_reuses_contact_snapshot_when_contact_db_is_unchanged(
        self,
        groups_mock,
        members_mock,
        stamp_mock,
    ):
        class Context:
            decrypted_dir = "decrypted"
            db_dir = "db"
            msg_db_keys = []
            cache = object()

            def refresh_message_shards(self):
                return False

        stamp = SourceStamp(1, 2, 3, 4)
        stamp_mock.side_effect = [stamp, stamp]
        group = GroupSource("room@chatroom", "测试群")
        groups_mock.return_value = [group]
        members_mock.return_value = {group.room_id: [{"username": "u1"}]}
        adapter = LocalWeChatSourceAdapter(Context())

        first = adapter.discover_groups()
        second = adapter.discover_groups()

        self.assertEqual([group], first)
        self.assertEqual(first, second)
        self.assertEqual([{"username": "u1"}], adapter.members(group))
        self.assertEqual(1, groups_mock.call_count)
        self.assertEqual(1, members_mock.call_count)

    def test_adapter_resolves_avatar_by_username_or_unique_display_name(self):
        ctx = type("Ctx", (), {"cache": object(), "db_dir": "db", "decrypted_dir": "decrypted", "msg_db_keys": []})()
        adapter = LocalWeChatSourceAdapter(ctx)
        adapter._groups_cache = [
            GroupSource("room@chatroom", "测试群", "https://wx.qlogo.cn/room/132")
        ]
        adapter._members_cache = {
            "room@chatroom": [
                {"username": "wxid_a", "display_name": "甲", "avatar_url": "https://wx.qlogo.cn/a/132"},
                {"username": "wxid_b", "display_name": "乙", "avatar_url": "https://wx.qlogo.cn/b/132"},
            ]
        }
        adapter._rebuild_avatar_index()
        adapter._contact_loaded = True

        self.assertEqual("https://wx.qlogo.cn/room/132", adapter.avatar_url("room@chatroom", "测试群"))
        self.assertEqual("https://wx.qlogo.cn/a/132", adapter.avatar_url("wxid_a", "甲"))
        self.assertEqual("https://wx.qlogo.cn/b/132", adapter.avatar_url("", "乙"))
        adapter._members_cache = {
            "room-a@chatroom": [
                {"username": "wxid_a", "display_name": "小米", "avatar_url": "https://wx.qlogo.cn/a/132"},
            ],
            "room-b@chatroom": [
                {"username": "wxid_b", "display_name": "小米", "avatar_url": "https://wx.qlogo.cn/b/132"},
            ],
        }
        adapter._rebuild_avatar_index()
        self.assertEqual("", adapter.avatar_url("", "小米"))
        self.assertEqual(
            "https://wx.qlogo.cn/a/132",
            adapter.avatar_url("", "小米", room_id="room-a@chatroom"),
        )
        self.assertEqual(
            "https://wx.qlogo.cn/b/132",
            adapter.avatar_url("", "小米", room_id="room-b@chatroom"),
        )
        adapter._all_contact_avatar_loaded = True
        self.assertEqual("", adapter.avatar_url("", "不存在"))

    @patch("wechat_cli.launch_ops.source.get_self_username", return_value="wxid_me")
    def test_adapter_falls_back_to_full_contact_table_for_historical_member_avatar(self, _self_mock):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "contact.db"
            conn = sqlite3.connect(path)
            try:
                conn.execute(
                    "CREATE TABLE contact (username TEXT, nick_name TEXT, remark TEXT, small_head_url TEXT, big_head_url TEXT)"
                )
                conn.executemany(
                    "INSERT INTO contact VALUES (?, ?, ?, ?, ?)",
                    [
                        ("wxid_me", "Au Revior", "", "https://wx.qlogo.cn/me/132", ""),
                        ("wxid_old", "历史成员", "", "https://wx.qlogo.cn/old/132", ""),
                        ("wxid_peach", "🍑福小桃", "", "https://wx.qlogo.cn/peach/132", ""),
                        ("wxid_rainbow1", "彩虹", "", "https://wx.qlogo.cn/r1/132", ""),
                        ("wxid_rainbow2", "🌈彩虹", "", "https://wx.qlogo.cn/r2/132", ""),
                        ("wxid_dup1", "重名", "", "https://wx.qlogo.cn/d1/132", ""),
                        ("wxid_dup2", "重名", "", "https://wx.qlogo.cn/d2/132", ""),
                    ],
                )
                conn.commit()
            finally:
                conn.close()
            adapter = LocalWeChatSourceAdapter(FakeContext(path))
            adapter._contact_loaded = True
            adapter._rebuild_avatar_index()

            self.assertEqual("https://wx.qlogo.cn/old/132", adapter.avatar_url("wxid_old", "历史成员"))
            self.assertEqual("https://wx.qlogo.cn/old/132", adapter.avatar_url("", "历史成员"))
            self.assertEqual("https://wx.qlogo.cn/peach/132", adapter.avatar_url("", "福小桃"))
            self.assertEqual("https://wx.qlogo.cn/me/132", adapter.avatar_url("", "你"))
            self.assertEqual("", adapter.avatar_url("", "彩虹🌈"))
            self.assertEqual("", adapter.avatar_url("", "重名"))

    @patch("wechat_cli.launch_ops.source.resolve_message_sources", return_value=[])
    def test_local_adapter_caches_empty_source_resolution(self, resolve_mock):
        ctx = type(
            "Ctx",
            (),
            {"msg_db_keys": [], "cache": object(), "decrypted_dir": "x"},
        )()
        adapter = LocalWeChatSourceAdapter(ctx)
        group = GroupSource("room@chatroom", "测试群")

        self.assertEqual([], adapter.resolve_sources(group))
        self.assertEqual([], adapter.resolve_sources(group))

        resolve_mock.assert_called_once_with(ctx, group)

    def test_bulk_discovers_message_sources_across_shards(self):
        import hashlib

        room_a = GroupSource("room-a@chatroom", "群A")
        room_b = GroupSource("room-b@chatroom", "群B")
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            shard_paths = {}
            for index, room in enumerate((room_a, room_b)):
                path = root / f"message_{index}.db"
                table_name = "Msg_" + hashlib.md5(room.room_id.encode()).hexdigest()
                conn = sqlite3.connect(path)
                try:
                    conn.execute(f"CREATE TABLE [{table_name}] (local_id INTEGER PRIMARY KEY)")
                    conn.commit()
                finally:
                    conn.close()
                shard_paths[f"message/message_{index}.db"] = str(path)

            class Cache:
                def get(self, rel_key):
                    return shard_paths.get(rel_key.replace("\\", "/"))

            ctx = type(
                "Ctx",
                (),
                {
                    "msg_db_keys": list(shard_paths),
                    "cache": Cache(),
                },
            )()

            mapping = discover_message_sources(ctx, [room_a, room_b])

        self.assertEqual(1, len(mapping[room_a.room_id]))
        self.assertEqual(1, len(mapping[room_b.room_id]))
        self.assertEqual(
            "message/message_0.db",
            mapping[room_a.room_id][0].source_shard,
        )
        self.assertEqual(
            "message/message_1.db",
            mapping[room_b.room_id][0].source_shard,
        )

    @patch("wechat_cli.launch_ops.source.contact_stamp", return_value=None)
    @patch("wechat_cli.launch_ops.source.discover_message_sources")
    @patch("wechat_cli.launch_ops.source.discover_group_members", return_value={})
    @patch("wechat_cli.launch_ops.source.discover_groups")
    def test_local_adapter_preloads_all_source_mappings_during_discovery(
        self,
        groups_mock,
        _members_mock,
        source_map_mock,
        _stamp_mock,
    ):
        class Context:
            decrypted_dir = "decrypted"
            db_dir = "db"
            msg_db_keys = []
            cache = object()

            def refresh_message_shards(self):
                return False

        group = GroupSource("room@chatroom", "测试群")
        source = MessageSource(
            room_id=group.room_id,
            source_shard="message/message_0.db",
            db_path="message.db",
            table_name="Msg_" + "a" * 32,
        )
        groups_mock.return_value = [group]
        source_map_mock.return_value = {group.room_id: [source]}
        adapter = LocalWeChatSourceAdapter(Context())

        adapter.discover_groups()
        first = adapter.resolve_sources(group)
        second = adapter.resolve_sources(group)

        self.assertEqual([source], first)
        self.assertEqual(first, second)
        source_map_mock.assert_called_once()

    def test_adapter_reads_table_heads_with_one_refresh_per_shard(self):
        table_a = "Msg_" + "a" * 32
        table_b = "Msg_" + "b" * 32
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "message.db"
            conn = sqlite3.connect(path)
            try:
                for table in (table_a, table_b):
                    conn.execute(
                        f"CREATE TABLE [{table}] (local_id INTEGER PRIMARY KEY AUTOINCREMENT, create_time INTEGER)"
                    )
                conn.executemany(
                    f"INSERT INTO [{table_a}](create_time) VALUES (?)",
                    [(100,), (101,)],
                )
                conn.execute(
                    f"INSERT INTO [{table_b}](create_time) VALUES (?)",
                    (200,),
                )
                conn.commit()
            finally:
                conn.close()

            class Cache:
                def __init__(self):
                    self.calls = []

                def get(self, rel_key):
                    self.calls.append(rel_key)
                    return str(path)

            cache = Cache()
            ctx = type(
                "Ctx",
                (),
                {"cache": cache, "db_dir": str(Path(tmp))},
            )()
            adapter = LocalWeChatSourceAdapter(ctx)
            sources = [
                MessageSource("room-a", "message/message_0.db", str(path), table_a),
                MessageSource("room-b", "message/message_0.db", str(path), table_b),
            ]

            heads = adapter.table_heads(sources)

        self.assertEqual(
            TableHead(local_id=2, create_time=101),
            heads[("message/message_0.db", table_a)],
        )
        self.assertEqual(
            TableHead(local_id=1, create_time=200),
            heads[("message/message_0.db", table_b)],
        )
        self.assertEqual(["message/message_0.db"], cache.calls)

    def test_source_stamp_tracks_db_and_wal_without_decrypting(self):
        with tempfile.TemporaryDirectory() as tmp:
            db_dir = Path(tmp) / "db_storage"
            message_dir = db_dir / "message"
            message_dir.mkdir(parents=True)
            db = message_dir / "message_0.db"
            wal = message_dir / "message_0.db-wal"
            db.write_bytes(b"db")
            wal.write_bytes(b"wal-data")

            stamp = source_stamp(str(db_dir), "message/message_0.db")

        self.assertEqual(2, stamp.db_size)
        self.assertEqual(8, stamp.wal_size)
        self.assertGreater(stamp.db_mtime_ns, 0)
        self.assertGreater(stamp.wal_mtime_ns, 0)

    def test_incremental_reader_preserves_same_second_rows_and_overlap(self):
        table_name = "Msg_" + "a" * 32
        with tempfile.TemporaryDirectory() as tmp:
            db = Path(tmp) / "message.db"
            conn = sqlite3.connect(db)
            try:
                conn.execute(
                    f"""
                    CREATE TABLE [{table_name}] (
                        local_id INTEGER,
                        server_id INTEGER,
                        local_type INTEGER,
                        create_time INTEGER,
                        real_sender_id INTEGER,
                        message_content BLOB,
                        WCDB_CT_message_content INTEGER
                    )
                    """
                )
                conn.executemany(
                    f"INSERT INTO [{table_name}] VALUES (?, ?, ?, ?, ?, ?, ?)",
                    [
                        (1, 101, 1, 100, 1, "one", 0),
                        (2, 102, 1, 100, 1, "two", 0),
                        (3, 103, 1, 101, 1, "three", 0),
                    ],
                )
                conn.commit()
            finally:
                conn.close()

            source = MessageSource(
                room_id="room@chatroom",
                source_shard="message/message_0.db",
                db_path=str(db),
                table_name=table_name,
            )
            rows = read_incremental_rows(
                source,
                start_time=100,
                start_local_id=1,
                overlap_seconds=0,
            )
            overlap = read_incremental_rows(
                source,
                start_time=101,
                start_local_id=3,
                overlap_seconds=2,
            )

        self.assertEqual([2, 3], [row.local_id for row in rows])
        self.assertEqual([1, 2, 3], [row.local_id for row in overlap])

    def test_bounded_reader_returns_only_inclusive_window(self):
        table_name = "Msg_" + "a" * 32
        with tempfile.TemporaryDirectory() as tmp:
            db = Path(tmp) / "message.db"
            conn = sqlite3.connect(db)
            try:
                conn.execute(
                    f"""CREATE TABLE [{table_name}] (
                        local_id INTEGER, server_id INTEGER, local_type INTEGER,
                        create_time INTEGER, real_sender_id INTEGER,
                        message_content BLOB, WCDB_CT_message_content INTEGER
                    )"""
                )
                conn.executemany(
                    f"INSERT INTO [{table_name}] VALUES (?, ?, ?, ?, ?, ?, ?)",
                    [
                        (1, 101, 10000, 99, 0, "before", 0),
                        (2, 102, 10000, 100, 0, "start", 0),
                        (3, 103, 10000, 101, 0, "middle", 0),
                        (4, 104, 10000, 102, 0, "end", 0),
                        (5, 105, 10000, 103, 0, "after", 0),
                    ],
                )
                conn.commit()
            finally:
                conn.close()
            source = MessageSource(
                "room@chatroom", "message/message_0.db", str(db), table_name
            )
            rows = read_rows_between(source, start_time=100, end_time=102)

        self.assertEqual([2, 3, 4], [row.local_id for row in rows])


if __name__ == "__main__":
    unittest.main()
