import unittest

from wechat_cli.launch_ops.normalizer import (
    normalize_invite_events,
    normalize_message,
)
from wechat_cli.launch_ops.source import MessageSource, RawMessageRow


class LaunchOpsNormalizerTests(unittest.TestCase):
    def setUp(self):
        self.source = MessageSource(
            room_id="room@chatroom",
            source_shard="message/message_0.db",
            db_path="decrypted.db",
            table_name="Msg_" + "a" * 32,
        )

    def test_server_id_identity_is_deterministic(self):
        row = RawMessageRow(
            local_id=7,
            server_id=123456,
            local_type=1,
            create_time=100,
            real_sender_id=9,
            message_content="你好",
            content_compression=0,
        )

        first = normalize_message(self.source, row)
        second = normalize_message(self.source, row)

        self.assertEqual(first.event.event_id, second.event.event_id)
        self.assertEqual("msg:server:123456", first.event.event_id)
        self.assertEqual("server:123456", first.event.source_identity)
        self.assertEqual("text", first.message_type)
        self.assertEqual("你好", first.text_content)

    def test_fallback_identity_is_stable_and_source_bound(self):
        row = RawMessageRow(
            local_id=8,
            server_id=0,
            local_type=3,
            create_time=101,
            real_sender_id=0,
            message_content="<msg><img /></msg>",
            content_compression=0,
        )

        first = normalize_message(self.source, row)
        second = normalize_message(self.source, row)

        self.assertEqual(first.event.event_id, second.event.event_id)
        self.assertTrue(first.event.event_id.startswith("msg:fallback:"))
        self.assertEqual("image", first.message_type)

    def test_normalizes_supported_message_kinds(self):
        expected = {
            1: "text",
            3: "image",
            34: "voice",
            43: "video",
            47: "sticker",
            48: "location",
            49: "link",
            50: "call",
            10000: "system",
        }
        for local_type, kind in expected.items():
            with self.subTest(local_type=local_type):
                row = RawMessageRow(
                    local_id=local_type,
                    server_id=local_type + 1000,
                    local_type=local_type,
                    create_time=200,
                    real_sender_id=0,
                    message_content="payload",
                    content_compression=0,
                )
                normalized = normalize_message(self.source, row)
                self.assertEqual(kind, normalized.message_type)

    def test_direct_invite_system_message_yields_member_invited_event(self):
        row = RawMessageRow(
            local_id=21,
            server_id=700,
            local_type=10000,
            create_time=300,
            real_sender_id=0,
            message_content='"小陶老师"邀请"张三"加入了群聊',
            content_compression=0,
        )
        parent = normalize_message(self.source, row)

        events = normalize_invite_events(self.source, row, parent)

        self.assertEqual(2, len(events))
        invited, joined = events
        self.assertEqual("member.invited", invited.event_type)
        self.assertEqual("direct", invited.payload["method"])
        self.assertEqual("小陶老师", invited.payload["inviter_name_raw"])
        self.assertEqual("张三", invited.payload["invitee_name_raw"])
        self.assertEqual("invite:server:700:0", invited.event_id)
        self.assertEqual("member.joined", joined.event_type)
        self.assertEqual("join:server:700:0", joined.event_id)
        self.assertEqual("invite_system_notice", joined.payload["provenance"])

    def test_sysmsgtemplate_qr_preserves_stable_inviter_and_invitee_ids(self):
        row = RawMessageRow(
            local_id=23,
            server_id=702,
            local_type=10000,
            create_time=302,
            real_sender_id=0,
            message_content="""<sysmsg type="sysmsgtemplate"><sysmsgtemplate>
            <content_template type="tmpl_type_profile">
              <template><![CDATA["$adder$"通过扫描"$from$"分享的二维码加入群聊]]></template>
              <link_list>
                <link name="adder" type="link_profile"><memberlist><member>
                  <username>wxid_sfsqqt3bgdpg22</username><nickname>诚美</nickname>
                </member></memberlist></link>
                <link name="from" type="link_profile"><memberlist><member>
                  <username>wxid_7e1h7tw8074b12</username><nickname>清柠</nickname>
                </member></memberlist></link>
              </link_list>
            </content_template></sysmsgtemplate></sysmsg>""",
            content_compression=0,
        )
        parent = normalize_message(self.source, row)

        events = normalize_invite_events(self.source, row, parent)

        self.assertEqual(2, len(events))
        invited, joined = events
        self.assertEqual("member.invited", invited.event_type)
        self.assertEqual("wxid_7e1h7tw8074b12", invited.actor_id)
        self.assertEqual("wxid_sfsqqt3bgdpg22", invited.subject_id)
        self.assertEqual("清柠", invited.payload["inviter_name_raw"])
        self.assertEqual("诚美", invited.payload["invitee_name_raw"])
        self.assertEqual("resolved", invited.payload["inviter_identity_status"])
        self.assertEqual("resolved", invited.payload["invitee_identity_status"])
        self.assertEqual("invite:server:702:0", invited.event_id)
        self.assertEqual("join:server:702:0", joined.event_id)

    def test_unattributed_qr_remains_explicitly_unattributed(self):
        row = RawMessageRow(
            local_id=22,
            server_id=701,
            local_type=10000,
            create_time=301,
            real_sender_id=0,
            message_content='"李雨"通过扫描二维码加入群聊',
            content_compression=0,
        )
        parent = normalize_message(self.source, row)

        events = normalize_invite_events(self.source, row, parent)

        self.assertEqual(2, len(events))
        event, joined = events
        self.assertEqual("unattributed_qr", event.payload["method"])
        self.assertEqual("", event.actor_id)
        self.assertEqual("unattributed", event.payload["inviter_identity_status"])
        self.assertEqual("member.joined", joined.event_type)
        self.assertEqual("unattributed_qr", joined.payload["method"])

    def test_non_system_or_unparsed_message_does_not_invent_invite(self):
        text_row = RawMessageRow(1, 1, 1, 1, 0, "hello", 0)
        text_parent = normalize_message(self.source, text_row)
        self.assertEqual([], normalize_invite_events(self.source, text_row, text_parent))

        system_row = RawMessageRow(2, 2, 10000, 2, 0, "群公告已更新", 0)
        system_parent = normalize_message(self.source, system_row)
        self.assertEqual([], normalize_invite_events(self.source, system_row, system_parent))


if __name__ == "__main__":
    unittest.main()
