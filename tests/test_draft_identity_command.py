import json
import unittest
from types import SimpleNamespace
from unittest.mock import Mock, patch

from click.testing import CliRunner

from wechat_cli.main import cli
from wechat_cli.outbound.draft_identity import DraftProbeCode, DraftProbeResult


class DraftIdentityCommandTests(unittest.TestCase):
    def setUp(self):
        self.runner = CliRunner()
        self.app = SimpleNamespace(
            cache=object(),
            decrypted_dir="dec",
            msg_db_keys=[],
            display_name_fn=object(),
        )

    def _invoke_with_result(self, probe_result, fmt="json"):
        service = Mock()
        service.run.return_value = probe_result
        with patch("wechat_cli.main.AppContext", return_value=self.app) as app_context, \
             patch("wechat_cli.commands.draft_identity_probe.SessionDraftSource") as source_cls, \
             patch("wechat_cli.commands.draft_identity_probe.WindowsDraftProbeDriver") as driver_cls, \
             patch("wechat_cli.commands.draft_identity_probe.MessageMarkerGuard") as guard_cls, \
             patch("wechat_cli.commands.draft_identity_probe.get_contact_names", return_value={"wxid_a": "Alice"}), \
             patch("wechat_cli.commands.draft_identity_probe.DraftIdentityProbeService", return_value=service) as service_cls:
            result = self.runner.invoke(
                cli,
                ["draft-identity-probe", "--format", fmt],
            )
        return result, app_context, source_cls, driver_cls, guard_cls, service_cls, service

    def test_json_success_shape_and_exit_zero(self):
        probe_result = DraftProbeResult(
            ok=True,
            code=None,
            message="bound and restored",
            username="wxid_a",
            display_name="Alice",
            kind="private",
            observed_ms=123,
            cleanup_confirmed=True,
        )

        result, app_context, source_cls, driver_cls, guard_cls, service_cls, service = self._invoke_with_result(probe_result)

        self.assertEqual(0, result.exit_code, result.output)
        payload = json.loads(result.output)
        self.assertEqual(
            {
                "ok": True,
                "code": None,
                "message": "bound and restored",
                "username": "wxid_a",
                "display_name": "Alice",
                "kind": "private",
                "observed_ms": 123,
                "cleanup_confirmed": True,
            },
            payload,
        )
        app_context.assert_called_once_with(None)
        source_cls.assert_called_once_with(self.app)
        driver_cls.assert_called_once_with()
        guard_cls.assert_called_once_with(self.app)
        service.run.assert_called_once_with()
        self.assertEqual({"wxid_a": "Alice"}, service_cls.call_args.kwargs["display_names"])

    def test_observation_failure_exits_two(self):
        result, *_ = self._invoke_with_result(
            DraftProbeResult(
                ok=False,
                code=DraftProbeCode.DRAFT_INPUT_NOT_OBSERVED,
                message="not observed",
            )
        )

        self.assertEqual(2, result.exit_code, result.output)
        self.assertEqual("DRAFT_INPUT_NOT_OBSERVED", json.loads(result.output)["code"])

    def test_cleanup_failure_exits_three(self):
        result, *_ = self._invoke_with_result(
            DraftProbeResult(
                ok=False,
                code=DraftProbeCode.DRAFT_CLEANUP_FAILED,
                message="cleanup failed",
                username="wxid_a",
                cleanup_confirmed=False,
            )
        )

        self.assertEqual(3, result.exit_code, result.output)

    def test_message_created_failure_exits_three(self):
        result, *_ = self._invoke_with_result(
            DraftProbeResult(
                ok=False,
                code=DraftProbeCode.PROBE_MESSAGE_CREATED,
                message="marker appeared in message history",
                username="wxid_a",
                cleanup_confirmed=True,
            )
        )

        self.assertEqual(3, result.exit_code, result.output)

    def test_text_output_does_not_expose_existing_draft_content(self):
        result, *_ = self._invoke_with_result(
            DraftProbeResult(
                ok=False,
                code=DraftProbeCode.DRAFT_CLEANUP_FAILED,
                message="cleanup failed",
                username="wxid_a",
                display_name="Alice",
            ),
            fmt="text",
        )

        self.assertEqual(3, result.exit_code, result.output)
        self.assertIn("DRAFT_CLEANUP_FAILED", result.output)
        self.assertNotIn("private baseline draft", result.output)


if __name__ == "__main__":
    unittest.main()
