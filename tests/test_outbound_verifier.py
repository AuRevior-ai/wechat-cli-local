import unittest
from types import SimpleNamespace
from unittest.mock import patch

from wechat_cli.outbound.models import ResolvedTarget
from wechat_cli.outbound.verifier import ReadbackVerifier, normalize_text, verify_history_items


class OutboundVerifierPureTests(unittest.TestCase):
    def test_normalize_text_only_normalizes_line_endings(self):
        self.assertEqual("a\nb", normalize_text("a\r\nb"))
        self.assertEqual("a\nb", normalize_text("a\rb"))
        self.assertEqual("a  b", normalize_text("a  b"))

    def test_matches_self_text_in_window(self):
        items = [{
            "chat_username": "wxid_alice",
            "timestamp": 105,
            "is_self": True,
            "type": "text",
            "text": "hello\r\nworld",
        }]
        matched = verify_history_items(
            items,
            username="wxid_alice",
            text="hello\nworld",
            not_before=100,
            not_after=110,
        )
        self.assertEqual(items[0], matched)

    def test_rejects_other_chat_or_other_sender(self):
        items = [
            {"chat_username": "wxid_bob", "timestamp": 105, "is_self": True, "type": "text", "text": "hello"},
            {"chat_username": "wxid_alice", "timestamp": 106, "is_self": False, "type": "text", "text": "hello"},
        ]
        self.assertIsNone(
            verify_history_items(
                items,
                username="wxid_alice",
                text="hello",
                not_before=100,
                not_after=110,
            )
        )

    def test_rejects_outside_window_and_non_text(self):
        items = [
            {"chat_username": "wxid_alice", "timestamp": 99, "is_self": True, "type": "text", "text": "hello"},
            {"chat_username": "wxid_alice", "timestamp": 105, "is_self": True, "type": "image", "text": "hello"},
        ]
        self.assertIsNone(
            verify_history_items(
                items,
                username="wxid_alice",
                text="hello",
                not_before=100,
                not_after=110,
            )
        )

    def test_returns_newest_matching_item(self):
        items = [
            {"chat_username": "wxid_alice", "timestamp": 104, "is_self": True, "type": "text", "text": "hello"},
            {"chat_username": "wxid_alice", "timestamp": 108, "is_self": True, "type": "text", "text": "hello"},
        ]
        matched = verify_history_items(
            items,
            username="wxid_alice",
            text="hello",
            not_before=100,
            not_after=110,
        )
        self.assertEqual(108, matched["timestamp"])


class OutboundReadbackVerifierTests(unittest.TestCase):
    def setUp(self):
        self.app = SimpleNamespace(
            msg_db_keys={"msg_0.db": "key"},
            cache=object(),
            decrypted_dir="decrypted",
            db_dir="db",
            display_name_fn=lambda username, names: names.get(username, username),
        )
        self.target = ResolvedTarget("wxid_alice", "Alice", "private")

    @patch("wechat_cli.outbound.verifier.collect_chat_history_items")
    @patch("wechat_cli.outbound.verifier.get_contact_names")
    @patch("wechat_cli.outbound.verifier.resolve_chat_context")
    def test_find_match_uses_existing_read_path(self, resolve_chat, get_names, collect_items):
        resolve_chat.return_value = {
            "username": "wxid_alice",
            "display_name": "Alice",
            "is_group": False,
            "db_path": "message.db",
            "table_name": "Msg_x",
            "message_tables": [],
        }
        get_names.return_value = {"wxid_alice": "Alice"}
        matching = {
            "chat_username": "wxid_alice",
            "timestamp": 105,
            "is_self": True,
            "type": "text",
            "text": "hello",
        }
        collect_items.return_value = ([matching], [])
        verifier = ReadbackVerifier(self.app, clock=lambda: 110, sleep=lambda _: None)

        result = verifier.find_match(
            self.target,
            "hello",
            not_before=100,
            not_after=110,
        )

        self.assertEqual(matching, result)
        resolve_chat.assert_called_once_with(
            "wxid_alice",
            self.app.msg_db_keys,
            self.app.cache,
            self.app.decrypted_dir,
        )
        collect_items.assert_called_once()
        kwargs = collect_items.call_args.kwargs
        self.assertEqual(100, kwargs["start_ts"])
        self.assertEqual(110, kwargs["end_ts"])
        self.assertEqual(50, kwargs["limit"])
        self.assertEqual(0, kwargs["offset"])
        self.assertEqual("db", kwargs["db_dir"])

    @patch("wechat_cli.outbound.verifier.collect_chat_history_items")
    @patch("wechat_cli.outbound.verifier.get_contact_names")
    @patch("wechat_cli.outbound.verifier.resolve_chat_context")
    def test_find_match_returns_none_when_chat_context_missing(self, resolve_chat, get_names, collect_items):
        resolve_chat.return_value = None
        verifier = ReadbackVerifier(self.app, clock=lambda: 110, sleep=lambda _: None)

        result = verifier.find_match(
            self.target,
            "hello",
            not_before=100,
            not_after=110,
        )

        self.assertIsNone(result)
        get_names.assert_not_called()
        collect_items.assert_not_called()

    def test_wait_for_match_polls_reads_without_sending(self):
        class FakeClock:
            def __init__(self):
                self.now = 100.0
                self.sleep_calls = []

            def time(self):
                return self.now

            def sleep(self, seconds):
                self.sleep_calls.append(seconds)
                self.now += seconds

        clock = FakeClock()
        verifier = ReadbackVerifier(self.app, clock=clock.time, sleep=clock.sleep)
        expected = {"timestamp": 101}
        with patch.object(
            verifier,
            "find_match",
            side_effect=[None, expected],
        ) as find_match:
            result = verifier.wait_for_match(
                self.target,
                "hello",
                not_before=99,
                timeout_seconds=5,
                poll_seconds=1,
            )

        self.assertEqual(expected, result)
        self.assertEqual(2, find_match.call_count)
        self.assertEqual([1], clock.sleep_calls)


if __name__ == "__main__":
    unittest.main()
