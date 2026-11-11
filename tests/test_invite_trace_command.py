import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from click.testing import CliRunner

from wechat_cli.core.messages import parse_time_range
from wechat_cli.main import cli


TRACE_SAMPLE = {
    "chat": "测试群",
    "username": "room@chatroom",
    "query_source": "archive",
    "completeness": "archive_complete",
    "archive_status": {"projection_state": "ready"},
    "scope": {},
    "summary": {},
    "ranking": [],
    "events": [],
    "trace": [
        {
            "episode_index": 1,
            "inviter_name": "甲",
            "inviter_username": "wxid_a",
            "invitee_name": "张三",
            "invitee_username": "wxid_z",
            "membership_state": "departed",
            "method": "direct",
            "event_time": 100,
            "time": "1970-01-01 00:01",
            "resolution_state": "resolved",
            "evidence_source": "system_message",
            "metadata_conflict": False,
        },
        {
            "episode_index": 2,
            "inviter_name": "乙",
            "inviter_username": "wxid_b",
            "invitee_name": "张三",
            "invitee_username": "wxid_z",
            "membership_state": "current",
            "method": "qr",
            "event_time": 200,
            "time": "1970-01-01 00:03",
            "resolution_state": "resolved",
            "evidence_source": "system_message",
            "metadata_conflict": False,
        },
    ],
    "unattributed_events": [],
    "unparsed_messages": [],
    "failures": [],
    "warnings": [],
    "excluded_unknown_time_count": 0,
}


class InviteTraceCommandTests(unittest.TestCase):
    def setUp(self):
        self.fake_app = SimpleNamespace(
            msg_db_keys=[], cache=object(), decrypted_dir="decrypted"
        )
        self.query = None

    def invoke(self, args):
        with patch(
            "wechat_cli.main.AppContext", return_value=self.fake_app
        ), patch(
            "wechat_cli.commands.invite_trace.InviteTraceQueryService"
        ) as service_cls:
            service_cls.return_value.query.return_value = TRACE_SAMPLE
            result = CliRunner().invoke(cli, args)
            self.query = service_cls.return_value.query
            return result

    def test_command_is_registered(self):
        result = CliRunner().invoke(cli, ["invite-trace", "--help"])

        self.assertEqual(result.exit_code, 0, result.output)
        self.assertIn("--bind-identity", result.output)
        self.assertIn("--output", result.output)

    def test_json_outputs_trace_rows(self):
        result = self.invoke(["invite-trace", "测试群"])

        self.assertEqual(result.exit_code, 0, result.output)
        self.assertIn('"trace"', result.output)
        self.assertIn('"departed"', result.output)
        self.assertIn('"current"', result.output)

    def test_text_shows_departed_and_current_rows(self):
        result = self.invoke([
            "invite-trace", "测试群", "--format", "text",
        ])

        self.assertEqual(result.exit_code, 0, result.output)
        self.assertIn("#1 甲 -> 张三 departed", result.output)
        self.assertIn("#2 乙 -> 张三 current", result.output)

    def test_csv_has_bom_and_trace_columns(self):
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp) / "trace.csv"
            result = self.invoke([
                "invite-trace", "测试群",
                "--format", "csv",
                "--output", str(target),
            ])
            raw = target.read_bytes()
            text = raw.decode("utf-8-sig")

        self.assertEqual(result.exit_code, 0, result.output)
        self.assertTrue(raw.startswith(b"\xef\xbb\xbf"))
        self.assertIn(
            "群聊,群聊ID,Episode,邀请人,邀请人账号,被邀请人",
            text,
        )

    def test_output_file_is_written(self):
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp) / "trace.json"
            result = self.invoke([
                "invite-trace", "测试群", "--output", str(target),
            ])
            content = target.read_text(encoding="utf-8")

        self.assertEqual(result.exit_code, 0, result.output)
        self.assertIn('"trace"', content)

    def test_unwritable_output_is_click_error(self):
        result = self.invoke([
            "invite-trace", "测试群",
            "--output", "missing/parent/trace.json",
        ])

        self.assertNotEqual(result.exit_code, 0)
        self.assertIn("无法写入输出文件", result.output)

    def test_bind_identity_and_time_options_are_forwarded(self):
        start_text = "2026-09-01"
        end_text = "2026-09-02"
        start_ts, end_ts = parse_time_range(start_text, end_text)
        result = self.invoke([
            "invite-trace", "测试群",
            "--start-time", start_text,
            "--end-time", end_text,
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
            start_ts=start_ts,
            end_ts=end_ts,
        )


if __name__ == "__main__":
    unittest.main()
