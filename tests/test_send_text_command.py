import json
import unittest
from types import SimpleNamespace
from unittest.mock import Mock, patch

from click.testing import CliRunner

from wechat_cli.main import cli
from wechat_cli.outbound.models import (
    OutboundError,
    ProbeResult,
    ResolvedTarget,
    SendResult,
    SendStatus,
    TargetResolutionError,
)


class SendTextCommandTests(unittest.TestCase):
    def setUp(self):
        self.runner = CliRunner()
        self.target = ResolvedTarget("wxid_alice", "Alice", "private")

    def test_probe_skips_app_context_and_returns_json_success(self):
        probe = ProbeResult(
            ok=True,
            code=None,
            message="ready",
            client_version="4.1.13.12",
            window_title="微信",
            strategy="uia",
        )
        adapter = Mock()
        adapter.probe.return_value = probe

        with patch(
            "wechat_cli.main.AppContext",
            side_effect=AssertionError("root must not load AppContext for send-text"),
        ), patch(
            "wechat_cli.commands.send_text.AppContext",
            side_effect=AssertionError("probe must not load AppContext"),
        ), patch(
            "wechat_cli.commands.send_text.WindowsWeChatAdapter",
            return_value=adapter,
        ):
            result = self.runner.invoke(cli, ["send-text", "--probe", "--format", "json"])

        self.assertEqual(0, result.exit_code, result.output)
        payload = json.loads(result.output)
        self.assertTrue(payload["ok"])
        self.assertEqual("4.1.13.12", payload["client_version"])
        self.assertEqual("uia", payload["strategy"])
        adapter.probe.assert_called_once_with()

    def test_probe_failure_exits_two_without_app_context(self):
        adapter = Mock()
        adapter.probe.return_value = ProbeResult(
            ok=False,
            code=OutboundError.UI_AUTOMATION_UNAVAILABLE,
            message="no UIA",
            client_version="4.1.13.12",
            strategy="uia",
        )

        with patch("wechat_cli.commands.send_text.WindowsWeChatAdapter", return_value=adapter), \
             patch("wechat_cli.commands.send_text.AppContext", side_effect=AssertionError("no AppContext")):
            result = self.runner.invoke(cli, ["send-text", "--probe", "--format", "json"])

        self.assertEqual(2, result.exit_code, result.output)
        payload = json.loads(result.output)
        self.assertFalse(payload["ok"])
        self.assertEqual("UI_AUTOMATION_UNAVAILABLE", payload["code"])

    def test_confirmed_send_resolves_once_and_exits_zero(self):
        app = SimpleNamespace(cache=object(), decrypted_dir="dec")
        service = Mock()
        service.send.return_value = SendResult(
            status=SendStatus.CONFIRMED,
            target=self.target,
            code=None,
            message="confirmed",
            attempted_at=100,
            confirmed_at=101,
        )

        with patch("wechat_cli.commands.send_text.AppContext", return_value=app) as app_context, \
             patch("wechat_cli.commands.send_text.get_contact_full", return_value=[{"username": "wxid_alice"}]), \
             patch("wechat_cli.commands.send_text.resolve_target_from_contacts", return_value=self.target) as resolve_target, \
             patch("wechat_cli.commands.send_text.WindowsWeChatAdapter") as adapter_cls, \
             patch("wechat_cli.commands.send_text.ReadbackVerifier") as verifier_cls, \
             patch("wechat_cli.commands.send_text.OutboundService", return_value=service):
            result = self.runner.invoke(
                cli,
                ["--config", "fixture.json", "send-text", "--to", "Alice", "--text", "hello", "--format", "json"],
            )

        self.assertEqual(0, result.exit_code, result.output)
        payload = json.loads(result.output)
        self.assertEqual("confirmed", payload["status"])
        self.assertEqual("wxid_alice", payload["target"]["username"])
        self.assertEqual(100, payload["attempted_at"])
        self.assertEqual(101, payload["confirmed_at"])
        app_context.assert_called_once_with("fixture.json")
        resolve_target.assert_called_once_with("Alice", [{"username": "wxid_alice"}])
        service.send.assert_called_once_with(self.target, "hello")
        adapter_cls.assert_called_once_with()
        verifier_cls.assert_called_once_with(app)

    def test_target_resolution_failure_exits_two_before_service_construction(self):
        app = SimpleNamespace(cache=object(), decrypted_dir="dec")

        with patch("wechat_cli.commands.send_text.AppContext", return_value=app), \
             patch("wechat_cli.commands.send_text.get_contact_full", return_value=[]), \
             patch(
                 "wechat_cli.commands.send_text.resolve_target_from_contacts",
                 side_effect=TargetResolutionError(OutboundError.TARGET_NOT_FOUND, "missing"),
             ), patch("wechat_cli.commands.send_text.OutboundService") as service_cls:
            result = self.runner.invoke(
                cli,
                ["send-text", "--to", "Nobody", "--text", "hello", "--format", "json"],
            )

        self.assertEqual(2, result.exit_code, result.output)
        payload = json.loads(result.output)
        self.assertEqual("blocked", payload["status"])
        self.assertEqual("TARGET_NOT_FOUND", payload["code"])
        service_cls.assert_not_called()

    def test_unconfirmed_send_uses_distinct_exit_three(self):
        app = SimpleNamespace(cache=object(), decrypted_dir="dec")
        service = Mock()
        service.send.return_value = SendResult(
            status=SendStatus.UNCONFIRMED,
            target=self.target,
            code=OutboundError.SEND_UNCONFIRMED,
            message="may already have been sent",
            attempted_at=100,
        )

        with patch("wechat_cli.commands.send_text.AppContext", return_value=app), \
             patch("wechat_cli.commands.send_text.get_contact_full", return_value=[]), \
             patch("wechat_cli.commands.send_text.resolve_target_from_contacts", return_value=self.target), \
             patch("wechat_cli.commands.send_text.WindowsWeChatAdapter"), \
             patch("wechat_cli.commands.send_text.ReadbackVerifier"), \
             patch("wechat_cli.commands.send_text.OutboundService", return_value=service):
            result = self.runner.invoke(
                cli,
                ["send-text", "--to", "Alice", "--text", "hello", "--format", "json"],
            )

        self.assertEqual(3, result.exit_code, result.output)
        payload = json.loads(result.output)
        self.assertEqual("unconfirmed", payload["status"])
        self.assertEqual("SEND_UNCONFIRMED", payload["code"])

    def test_blocked_and_failed_results_exit_two(self):
        app = SimpleNamespace(cache=object(), decrypted_dir="dec")
        for status, code in (
            (SendStatus.BLOCKED, OutboundError.TARGET_UI_MISMATCH),
            (SendStatus.FAILED, OutboundError.SEND_ACTION_FAILED),
        ):
            with self.subTest(status=status):
                service = Mock()
                service.send.return_value = SendResult(
                    status=status,
                    target=self.target,
                    code=code,
                    message="stopped",
                )
                with patch("wechat_cli.commands.send_text.AppContext", return_value=app), \
                     patch("wechat_cli.commands.send_text.get_contact_full", return_value=[]), \
                     patch("wechat_cli.commands.send_text.resolve_target_from_contacts", return_value=self.target), \
                     patch("wechat_cli.commands.send_text.WindowsWeChatAdapter"), \
                     patch("wechat_cli.commands.send_text.ReadbackVerifier"), \
                     patch("wechat_cli.commands.send_text.OutboundService", return_value=service):
                    result = self.runner.invoke(
                        cli,
                        ["send-text", "--to", "Alice", "--text", "hello", "--format", "json"],
                    )
                self.assertEqual(2, result.exit_code, result.output)

    def test_text_output_does_not_dump_unrelated_chat_content(self):
        app = SimpleNamespace(cache=object(), decrypted_dir="dec")
        service = Mock()
        service.send.return_value = SendResult(
            status=SendStatus.CONFIRMED,
            target=self.target,
            code=None,
            message="confirmed",
            attempted_at=100,
            confirmed_at=101,
        )
        with patch("wechat_cli.commands.send_text.AppContext", return_value=app), \
             patch("wechat_cli.commands.send_text.get_contact_full", return_value=[]), \
             patch("wechat_cli.commands.send_text.resolve_target_from_contacts", return_value=self.target), \
             patch("wechat_cli.commands.send_text.WindowsWeChatAdapter"), \
             patch("wechat_cli.commands.send_text.ReadbackVerifier"), \
             patch("wechat_cli.commands.send_text.OutboundService", return_value=service):
            result = self.runner.invoke(
                cli,
                ["send-text", "--to", "Alice", "--text", "hello", "--format", "text"],
            )

        self.assertEqual(0, result.exit_code, result.output)
        self.assertIn("confirmed", result.output)
        self.assertIn("Alice", result.output)
        self.assertNotIn("message_items", result.output)


if __name__ == "__main__":
    unittest.main()
