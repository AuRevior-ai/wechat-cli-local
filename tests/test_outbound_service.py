import unittest

from wechat_cli.outbound.models import (
    OutboundActionError,
    OutboundError,
    ProbeResult,
    ResolvedTarget,
    SendStatus,
)
from wechat_cli.outbound.service import OutboundService


class FakeAdapter:
    def __init__(self, *, probe_result=None, open_error=None, send_error=None):
        self.probe_result = probe_result or ProbeResult(
            ok=True,
            code=None,
            message="ready",
            strategy="uia",
        )
        self.open_error = open_error
        self.send_error = send_error
        self.probe_calls = 0
        self.open_calls = 0
        self.send_calls = 0
        self.opened_targets = []
        self.sent_texts = []

    def probe(self):
        self.probe_calls += 1
        return self.probe_result

    def open_target(self, target):
        self.open_calls += 1
        self.opened_targets.append(target)
        if self.open_error is not None:
            raise self.open_error

    def send_text_once(self, text):
        self.send_calls += 1
        self.sent_texts.append(text)
        if self.send_error is not None:
            raise self.send_error


class FakeVerifier:
    def __init__(self, matched=None, error=None):
        self.matched = matched
        self.error = error
        self.calls = 0
        self.call_args = []

    def wait_for_match(
        self,
        target,
        text,
        *,
        not_before,
        timeout_seconds,
        poll_seconds,
    ):
        self.calls += 1
        self.call_args.append(
            {
                "target": target,
                "text": text,
                "not_before": not_before,
                "timeout_seconds": timeout_seconds,
                "poll_seconds": poll_seconds,
            }
        )
        if self.error is not None:
            raise self.error
        return self.matched


class OutboundServiceTests(unittest.TestCase):
    def setUp(self):
        self.target = ResolvedTarget("wxid_alice", "Alice", "private")

    def _service(self, adapter, verifier, clock=lambda: 100.0):
        return OutboundService(
            adapter,
            verifier,
            clock=clock,
            verification_timeout_seconds=8.0,
            verification_poll_seconds=0.5,
        )

    def test_empty_text_is_blocked_before_ui_interaction(self):
        adapter = FakeAdapter()
        verifier = FakeVerifier()

        result = self._service(adapter, verifier).send(self.target, "   ")

        self.assertEqual(SendStatus.BLOCKED, result.status)
        self.assertEqual(OutboundError.INVALID_TEXT, result.code)
        self.assertEqual(0, adapter.probe_calls)
        self.assertEqual(0, adapter.open_calls)
        self.assertEqual(0, adapter.send_calls)
        self.assertEqual(0, verifier.calls)

    def test_text_over_4096_codepoints_is_blocked_before_ui_interaction(self):
        adapter = FakeAdapter()
        verifier = FakeVerifier()

        result = self._service(adapter, verifier).send(self.target, "x" * 4097)

        self.assertEqual(SendStatus.BLOCKED, result.status)
        self.assertEqual(OutboundError.INVALID_TEXT, result.code)
        self.assertEqual(0, adapter.send_calls)

    def test_failed_probe_blocks_before_open_or_send(self):
        adapter = FakeAdapter(
            probe_result=ProbeResult(
                ok=False,
                code=OutboundError.UI_AUTOMATION_UNAVAILABLE,
                message="no UIA",
                strategy="uia",
            )
        )
        verifier = FakeVerifier()

        result = self._service(adapter, verifier).send(self.target, "hello")

        self.assertEqual(SendStatus.BLOCKED, result.status)
        self.assertEqual(OutboundError.UI_AUTOMATION_UNAVAILABLE, result.code)
        self.assertEqual(1, adapter.probe_calls)
        self.assertEqual(0, adapter.open_calls)
        self.assertEqual(0, adapter.send_calls)

    def test_target_ui_mismatch_blocks_before_send(self):
        adapter = FakeAdapter(
            open_error=OutboundActionError(
                OutboundError.TARGET_UI_MISMATCH,
                "wrong conversation",
            )
        )
        verifier = FakeVerifier()

        result = self._service(adapter, verifier).send(self.target, "hello")

        self.assertEqual(SendStatus.BLOCKED, result.status)
        self.assertEqual(OutboundError.TARGET_UI_MISMATCH, result.code)
        self.assertEqual(1, adapter.open_calls)
        self.assertEqual(0, adapter.send_calls)

    def test_send_exception_returns_failed_and_never_retries(self):
        adapter = FakeAdapter(send_error=RuntimeError("boom"))
        verifier = FakeVerifier()

        result = self._service(adapter, verifier).send(self.target, "hello")

        self.assertEqual(SendStatus.FAILED, result.status)
        self.assertEqual(OutboundError.SEND_ACTION_FAILED, result.code)
        self.assertEqual(1, adapter.send_calls)
        self.assertEqual(0, verifier.calls)

    def test_matching_readback_returns_confirmed(self):
        adapter = FakeAdapter()
        verifier = FakeVerifier(
            matched={
                "chat_username": "wxid_alice",
                "timestamp": 101,
                "is_self": True,
                "type": "text",
                "text": "hello",
            }
        )

        result = self._service(adapter, verifier).send(self.target, "hello")

        self.assertEqual(SendStatus.CONFIRMED, result.status)
        self.assertIsNone(result.code)
        self.assertEqual(100, result.attempted_at)
        self.assertEqual(101, result.confirmed_at)
        self.assertEqual(1, adapter.send_calls)
        self.assertEqual(1, verifier.calls)
        self.assertEqual(98, verifier.call_args[0]["not_before"])
        self.assertEqual(8.0, verifier.call_args[0]["timeout_seconds"])
        self.assertEqual(0.5, verifier.call_args[0]["poll_seconds"])

    def test_missing_readback_returns_unconfirmed_and_never_retries(self):
        adapter = FakeAdapter()
        verifier = FakeVerifier(matched=None)

        result = self._service(adapter, verifier).send(self.target, "hello")

        self.assertEqual(SendStatus.UNCONFIRMED, result.status)
        self.assertEqual(OutboundError.SEND_UNCONFIRMED, result.code)
        self.assertEqual(1, adapter.send_calls)
        self.assertEqual(1, verifier.calls)
        self.assertIn("must not be auto-retried", result.message)

    def test_readback_exception_is_unconfirmed_after_single_send(self):
        adapter = FakeAdapter()
        verifier = FakeVerifier(error=RuntimeError("read path unavailable"))

        result = self._service(adapter, verifier).send(self.target, "hello")

        self.assertEqual(SendStatus.UNCONFIRMED, result.status)
        self.assertEqual(OutboundError.SEND_UNCONFIRMED, result.code)
        self.assertEqual(1, adapter.send_calls)
        self.assertEqual(1, verifier.calls)


if __name__ == "__main__":
    unittest.main()
