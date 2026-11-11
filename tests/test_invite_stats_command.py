import copy
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from click.testing import CliRunner

from wechat_cli.core.invite_stats import (
    format_invite_stats_csv,
    format_invite_stats_text,
)
from wechat_cli.main import cli


SAMPLE = {
    "chat": "测试群",
    "username": "room@chatroom",
    "scope": {
        "first_visible_system_time": "2026-07-23 18:36",
        "last_visible_system_time": "2026-07-28 09:54",
    },
    "summary": {
        "system_message_count": 10,
        "invite_event_count": 3,
        "attributed_event_count": 3,
        "unique_invitee_count": 2,
        "unattributed_count": 0,
        "unresolved_identity_count": 0,
        "unparsed_count": 0,
    },
    "ranking": [{
        "rank": 1,
        "inviter_key": "user:wxid_a",
        "inviter_username": "wxid_a",
        "inviter_name": "甲",
        "historical_names": ["甲"],
        "identity_status": "resolved",
        "identity_source": "member_exact",
        "unique_invitee_count": 2,
        "event_count": 3,
        "direct_count": 2,
        "qr_count": 1,
        "first_invite_time": "2026-07-23 18:40",
        "last_invite_time": "2026-07-24 10:00",
        "invitees": [],
    }],
    "events": [{
        "event_id": "1:0",
        "server_id": 1,
        "rank": 1,
        "timestamp": 1,
        "time": "2026-07-23 18:40",
        "method": "direct",
        "inviter_name_raw": "甲",
        "inviter_username": "wxid_a",
        "inviter_key": "user:wxid_a",
        "inviter_identity_status": "resolved",
        "inviter_identity_source": "member_exact",
        "invitee_name_raw": "乙",
        "invitee_username": "wxid_b",
        "invitee_key": "user:wxid_b",
        "invitee_identity_status": "resolved",
        "invitee_identity_source": "member_exact",
        "raw_text": '"甲"邀请"乙"加入了群聊',
    }],
    "unattributed_events": [],
    "unparsed_messages": [],
    "failures": [],
}


class InviteFormatterTests(unittest.TestCase):
    def test_text_contains_rank_and_stable_identity(self):
        text = format_invite_stats_text(SAMPLE)

        self.assertIn("1. 甲 (wxid_a) — 2 人", text)
        self.assertIn("直接 2 / 二维码 1 / 事件 3", text)

    def test_csv_has_bom_and_relation_columns(self):
        text = format_invite_stats_csv(SAMPLE)

        self.assertTrue(text.startswith("\ufeff"))
        self.assertIn("邀请者排名,邀请者,邀请者账号,邀请者身份状态", text)
        self.assertIn("1,甲,wxid_a", text)

    def test_text_includes_trace_source_and_historical_breakdown(self):
        upgraded = copy.deepcopy(SAMPLE)
        upgraded.update({
            "query_source": "archive",
            "completeness": "archive_complete",
            "excluded_unknown_time_count": 3,
        })
        upgraded["ranking"][0].update({
            "current_invitee_count": 1,
            "departed_invitee_count": 1,
            "unknown_time_count": 3,
            "metadata_only_count": 1,
        })

        text = format_invite_stats_text(upgraded)

        self.assertIn("数据源: archive", text)
        self.assertIn("完整度: archive_complete", text)
        self.assertIn("当前 1 / 已退群 1 / 时间未知 3 / metadata 1", text)
        self.assertIn("时间过滤排除未知时间关系 3", text)

    def test_csv_appends_trace_columns_and_supports_metadata(self):
        upgraded = copy.deepcopy(SAMPLE)
        upgraded["query_source"] = "live"
        upgraded["events"][0].update({
            "method": "metadata",
            "membership_state": "current",
            "evidence_source": "ext_buffer",
            "metadata_conflict": True,
            "time": "时间未知",
        })

        text = format_invite_stats_csv(upgraded)

        self.assertIn("成员状态,查询来源,证据来源,时间状态,冲突", text)
        self.assertIn("元数据", text)
        self.assertIn("current,live,ext_buffer,unknown,yes", text)


class InviteStatsCommandTests(unittest.TestCase):
    def setUp(self):
        self.fake_app = SimpleNamespace(
            msg_db_keys=[],
            cache=object(),
            decrypted_dir="decrypted",
        )
        self.query = None

    def invoke(self, args):
        with patch(
            "wechat_cli.main.AppContext", return_value=self.fake_app
        ), patch(
            "wechat_cli.commands.invite_stats.InviteTraceQueryService"
        ) as service_cls:
            service_cls.return_value.query.return_value = SAMPLE
            result = CliRunner().invoke(cli, args)
            self.query = service_cls.return_value.query
            return result

    def test_command_is_registered(self):
        result = CliRunner().invoke(cli, ["invite-stats", "--help"])

        self.assertEqual(result.exit_code, 0, result.output)
        self.assertIn("--bind-identity", result.output)
        self.assertIn("--output", result.output)

    def test_command_outputs_authoritative_json(self):
        result = self.invoke(["invite-stats", "测试群"])

        self.assertEqual(result.exit_code, 0, result.output)
        self.assertIn('"ranking"', result.output)
        self.assertIn('"wxid_a"', result.output)

    def test_command_delegates_to_query_service(self):
        result = self.invoke(["invite-stats", "测试群"])

        self.assertEqual(result.exit_code, 0, result.output)
        self.query.assert_called_once_with(
            "测试群",
            bindings={},
            start_ts=None,
            end_ts=None,
        )

    def test_command_forwards_repeated_identity_bindings(self):
        result = self.invoke([
            "invite-stats", "测试群",
            "--bind-identity", "旧昵称=wxid_one",
            "--bind-identity", "另一个旧昵称=wxid_two",
        ])

        self.assertEqual(result.exit_code, 0, result.output)
        self.query.assert_called_once_with(
            "测试群",
            bindings={
                "旧昵称": "wxid_one",
                "另一个旧昵称": "wxid_two",
            },
            start_ts=None,
            end_ts=None,
        )

    def test_invalid_time_stops_before_query(self):
        result = self.invoke([
            "invite-stats", "测试群", "--start-time", "not-a-time",
        ])

        self.assertNotEqual(result.exit_code, 0)
        self.query.assert_not_called()

    def test_invalid_binding_stops_before_query(self):
        result = self.invoke([
            "invite-stats", "测试群", "--bind-identity", "bad-binding",
        ])

        self.assertNotEqual(result.exit_code, 0)
        self.query.assert_not_called()

    def test_command_source_does_not_construct_launch_ops_store(self):
        import inspect
        import wechat_cli.commands.invite_stats as module

        source = inspect.getsource(module)
        self.assertNotIn("LaunchOpsStore(", source)

    def test_command_writes_csv_file(self):
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp) / "invite.csv"
            result = self.invoke([
                "invite-stats", "测试群",
                "--format", "csv",
                "--output", str(target),
            ])
            raw = target.read_bytes()

        self.assertEqual(result.exit_code, 0, result.output)
        self.assertTrue(raw.startswith(b"\xef\xbb\xbf"))

    def test_command_reports_unwritable_output_as_click_error(self):
        result = self.invoke([
            "invite-stats", "测试群",
            "--format", "csv",
            "--output", "missing/parent/invite.csv",
        ])

        self.assertNotEqual(result.exit_code, 0)
        self.assertIn("无法写入输出文件", result.output)


if __name__ == "__main__":
    unittest.main()
