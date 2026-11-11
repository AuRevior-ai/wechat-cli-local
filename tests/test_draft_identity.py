import os
import sqlite3
import tempfile
import unittest
from types import SimpleNamespace
from unittest.mock import patch

from wechat_cli.outbound.draft_identity import (
    DraftProbeCode,
    DraftIdentityProbeService,
    DraftProbeCode,
    DraftProbeError,
    DraftProbeResult,
    DraftSnapshot,
    MessageMarkerGuard,
    SessionDraftSource,
    cleanup_restored,
    generate_marker,
    locate_marker,
    select_marker,
    wait_for_cleanup,
    wait_for_identity,
)


class DraftIdentityPureTests(unittest.TestCase):
    def test_unchanged_snapshot_has_no_identity(self):
        baseline = DraftSnapshot({"wxid_a": "secret draft"})
        current = DraftSnapshot({"wxid_a": "secret draft"})

        self.assertEqual([], locate_marker(baseline, current, "[[probe]]"))

    def test_exactly_one_new_marker_resolves_username(self):
        baseline = DraftSnapshot({"wxid_a": "hello", "wxid_b": ""})
        current = DraftSnapshot({"wxid_a": "hello[[probe]]", "wxid_b": ""})

        self.assertEqual(["wxid_a"], locate_marker(baseline, current, "[[probe]]"))

    def test_multiple_marker_rows_are_ambiguous(self):
        baseline = DraftSnapshot({"wxid_a": "", "wxid_b": ""})
        current = DraftSnapshot({"wxid_a": "[[probe]]", "wxid_b": "x[[probe]]"})

        self.assertEqual(
            ["wxid_a", "wxid_b"],
            locate_marker(baseline, current, "[[probe]]"),
        )

    def test_cleanup_requires_exact_baseline_return(self):
        baseline = DraftSnapshot({"wxid_a": "private text"})

        self.assertTrue(
            cleanup_restored(
                baseline,
                DraftSnapshot({"wxid_a": "private text"}),
                "wxid_a",
                "[[probe]]",
            )
        )
        self.assertFalse(
            cleanup_restored(
                baseline,
                DraftSnapshot({"wxid_a": "private textx"}),
                "wxid_a",
                "[[probe]]",
            )
        )

    def test_cleanup_rejects_marker_anywhere(self):
        baseline = DraftSnapshot({"wxid_a": "", "wxid_b": ""})
        current = DraftSnapshot({"wxid_a": "", "wxid_b": "[[probe]]"})

        self.assertFalse(cleanup_restored(baseline, current, "wxid_a", "[[probe]]"))

    def test_error_code_is_stable_without_exposing_draft_content(self):
        self.assertEqual(
            "DRAFT_CLEANUP_FAILED",
            DraftProbeCode.DRAFT_CLEANUP_FAILED.value,
        )
        self.assertNotIn("private text", DraftProbeCode.DRAFT_CLEANUP_FAILED.value)

    def test_select_marker_returns_four_chars_absent_from_all_baseline_drafts(self):
        baseline = DraftSnapshot({"a": "contains §", "b": "contains ¶"})

        marker = select_marker(baseline)

        self.assertEqual(4, len(marker))
        self.assertTrue(all(ch not in "contains §contains ¶" for ch in marker))

    def test_select_marker_is_deterministic_for_same_baseline(self):
        baseline = DraftSnapshot({"a": ""})

        self.assertEqual(select_marker(baseline), select_marker(baseline))

    def test_select_marker_fails_closed_when_pool_is_exhausted(self):
        baseline = DraftSnapshot({"a": "§¶¤※"})

        with self.assertRaises(DraftProbeError) as cm:
            select_marker(baseline, pool="§¶¤※")

        self.assertEqual(DraftProbeCode.PROBE_INPUT_FAILED, cm.exception.code)


class FakeClock:
    def __init__(self, start=10.0):
        self.value = float(start)

    def __call__(self):
        return self.value

    def sleep(self, seconds):
        self.value += max(0.0, float(seconds))


class SequenceSource:
    def __init__(self, snapshots):
        self.snapshots = list(snapshots)
        self.index = 0

    def snapshot(self):
        value = self.snapshots[min(self.index, len(self.snapshots) - 1)]
        self.index += 1
        return value


class DraftIdentityPollingTests(unittest.TestCase):
    def test_wait_for_identity_returns_unique_username_and_latency(self):
        baseline = DraftSnapshot({"wxid_a": ""})
        source = SequenceSource([
            baseline,
            DraftSnapshot({"wxid_a": "[[probe]]"}),
        ])
        clock = FakeClock()

        observed = wait_for_identity(
            source,
            baseline,
            "[[probe]]",
            timeout_seconds=1,
            poll_seconds=0.1,
            clock=clock,
            sleep=clock.sleep,
        )

        self.assertEqual("wxid_a", observed.username)
        self.assertEqual(100, observed.observed_ms)

    def test_wait_for_identity_reports_ambiguity_without_draft_content(self):
        baseline = DraftSnapshot({"wxid_a": "", "wxid_b": ""})
        source = SequenceSource([
            DraftSnapshot({"wxid_a": "secret[[probe]]", "wxid_b": "[[probe]]"}),
        ])
        clock = FakeClock()

        with self.assertRaises(DraftProbeError) as cm:
            wait_for_identity(
                source,
                baseline,
                "[[probe]]",
                timeout_seconds=1,
                poll_seconds=0,
                clock=clock,
                sleep=clock.sleep,
            )

        self.assertEqual(DraftProbeCode.DRAFT_IDENTITY_AMBIGUOUS, cm.exception.code)
        self.assertNotIn("secret", str(cm.exception))

    def test_wait_for_identity_timeout_is_bounded(self):
        baseline = DraftSnapshot({"wxid_a": ""})
        source = SequenceSource([baseline])
        clock = FakeClock()

        with self.assertRaises(DraftProbeError) as cm:
            wait_for_identity(
                source,
                baseline,
                "[[probe]]",
                timeout_seconds=0.2,
                poll_seconds=0.1,
                clock=clock,
                sleep=clock.sleep,
            )

        self.assertEqual(DraftProbeCode.DRAFT_INPUT_NOT_OBSERVED, cm.exception.code)
        self.assertGreaterEqual(clock.value, 10.2)
        self.assertLessEqual(clock.value, 10.21)

    def test_wait_for_cleanup_requires_exact_restore(self):
        baseline = DraftSnapshot({"wxid_a": "original"})
        source = SequenceSource([
            DraftSnapshot({"wxid_a": "original[[probe]]"}),
            DraftSnapshot({"wxid_a": "original"}),
        ])
        clock = FakeClock()

        self.assertTrue(
            wait_for_cleanup(
                source,
                baseline,
                "wxid_a",
                "[[probe]]",
                timeout_seconds=1,
                poll_seconds=0.1,
                clock=clock,
                sleep=clock.sleep,
            )
        )

    def test_wait_for_cleanup_returns_false_after_timeout(self):
        baseline = DraftSnapshot({"wxid_a": "original"})
        source = SequenceSource([DraftSnapshot({"wxid_a": "changed"})])
        clock = FakeClock()

        self.assertFalse(
            wait_for_cleanup(
                source,
                baseline,
                "wxid_a",
                "[[probe]]",
                timeout_seconds=0.2,
                poll_seconds=0.1,
                clock=clock,
                sleep=clock.sleep,
            )
        )


class MessageMarkerGuardTests(unittest.TestCase):
    def test_guard_returns_only_boolean_for_marker_hits(self):
        app = SimpleNamespace(
            msg_db_keys=["message/message_0.db"],
            cache=object(),
            decrypted_dir="dec",
            display_name_fn=object(),
        )
        with patch(
            "wechat_cli.outbound.draft_identity.get_contact_names",
            return_value={"wxid_a": "Alice"},
        ), patch(
            "wechat_cli.outbound.draft_identity.search_all_messages",
            return_value=([(123, "secret rendered line")], []),
        ) as search:
            found = MessageMarkerGuard(app).marker_message_exists(
                "[[probe]]",
                not_before=100,
                not_after=130,
            )

        self.assertTrue(found)
        self.assertEqual(1, search.call_count)
        self.assertNotIn("secret rendered line", str(found))

    def test_guard_returns_false_when_marker_is_absent(self):
        app = SimpleNamespace(
            msg_db_keys=[],
            cache=object(),
            decrypted_dir="dec",
            display_name_fn=object(),
        )
        with patch(
            "wechat_cli.outbound.draft_identity.get_contact_names",
            return_value={},
        ), patch(
            "wechat_cli.outbound.draft_identity.search_all_messages",
            return_value=([], []),
        ):
            self.assertFalse(
                MessageMarkerGuard(app).marker_message_exists(
                    "[[probe]]",
                    not_before=100,
                    not_after=130,
                )
            )


class FakeProbeDriver:
    def __init__(self, *, insert_error=None, remove_error=None, partial_count=0):
        self.insert_error = insert_error
        self.remove_error = remove_error
        self.partial_count = int(partial_count)
        self.calls = []
        self.inserted_char_count = 0

    def insert_challenge(self, marker):
        self.calls.append(("insert", marker))
        if self.insert_error:
            self.inserted_char_count = self.partial_count
            raise self.insert_error
        self.inserted_char_count = len(marker)

    def remove_challenge(self):
        self.calls.append(("remove", self.inserted_char_count))
        if self.remove_error:
            raise self.remove_error
        self.inserted_char_count = 0

    def prepare_challenge(self, marker):
        raise AssertionError("Revision 2 must not use clipboard preparation")

    def paste_challenge(self):
        raise AssertionError("Revision 2 must not use clipboard paste")

    def undo_challenge(self):
        raise AssertionError("Revision 2 must not use Ctrl+Z cleanup")

    def restore_clipboard(self):
        raise AssertionError("Revision 2 must not restore clipboard")


class FakeMarkerGuard:
    def __init__(self, found=False, error=None):
        self.found = found
        self.error = error
        self.calls = []

    def marker_message_exists(self, marker, *, not_before, not_after):
        self.calls.append((marker, not_before, not_after))
        if self.error:
            raise self.error
        return self.found


class DraftIdentityProbeServiceTests(unittest.TestCase):
    MARKER = "§¶¤※"

    def _service(self, source, driver, guard, *, clock=None):
        clock = clock or FakeClock()
        return DraftIdentityProbeService(
            source,
            driver,
            guard,
            display_names={"wxid_a": "Alice"},
            marker_factory=lambda _baseline: self.MARKER,
            wall_clock=lambda: 1000.0,
            clock=clock,
            sleep=clock.sleep,
            observation_timeout_seconds=0.2,
            cleanup_timeout_seconds=0.2,
            poll_seconds=0.1,
        )

    def test_generate_marker_is_high_entropy_and_prefixed(self):
        first = generate_marker()
        second = generate_marker()
        self.assertTrue(first.startswith("[[wechat-cli-draft-probe:"))
        self.assertTrue(first.endswith("]]"))
        self.assertNotEqual(first, second)

    def test_unique_identity_cleans_up_and_returns_success(self):
        baseline = DraftSnapshot({"wxid_a": "secret draft"})
        source = SequenceSource([
            baseline,
            DraftSnapshot({"wxid_a": "secret draft" + self.MARKER}),
            baseline,
        ])
        driver = FakeProbeDriver()
        guard = FakeMarkerGuard(found=False)

        result = self._service(source, driver, guard).run()

        self.assertTrue(result.ok)
        self.assertIsNone(result.code)
        self.assertEqual("wxid_a", result.username)
        self.assertEqual("Alice", result.display_name)
        self.assertEqual("private", result.kind)
        self.assertTrue(result.cleanup_confirmed)
        self.assertEqual(["insert", "remove"], [call[0] for call in driver.calls])
        self.assertNotIn("secret draft", result.message)

    def test_observation_timeout_still_removes_typed_challenge(self):
        baseline = DraftSnapshot({"wxid_a": "secret draft"})
        source = SequenceSource([baseline])
        driver = FakeProbeDriver()
        guard = FakeMarkerGuard(found=False)

        result = self._service(source, driver, guard).run()

        self.assertFalse(result.ok)
        self.assertEqual(DraftProbeCode.DRAFT_INPUT_NOT_OBSERVED, result.code)
        self.assertTrue(any(call[0] == "remove" for call in driver.calls))
        self.assertNotIn("secret draft", result.message)

    def test_ambiguity_still_removes_typed_challenge(self):
        baseline = DraftSnapshot({"wxid_a": "", "wxid_b": ""})
        source = SequenceSource([
            baseline,
            DraftSnapshot({"wxid_a": self.MARKER, "wxid_b": self.MARKER}),
            baseline,
        ])
        driver = FakeProbeDriver()
        guard = FakeMarkerGuard(found=False)

        result = self._service(source, driver, guard).run()

        self.assertFalse(result.ok)
        self.assertEqual(DraftProbeCode.DRAFT_IDENTITY_AMBIGUOUS, result.code)
        self.assertTrue(any(call[0] == "remove" for call in driver.calls))

    def test_cleanup_mismatch_is_hard_failure(self):
        baseline = DraftSnapshot({"wxid_a": "original"})
        changed = DraftSnapshot({"wxid_a": "changed"})
        source = SequenceSource([
            baseline,
            DraftSnapshot({"wxid_a": "original" + self.MARKER}),
            changed,
            changed,
            changed,
        ])
        driver = FakeProbeDriver()
        guard = FakeMarkerGuard(found=False)

        result = self._service(source, driver, guard).run()

        self.assertFalse(result.ok)
        self.assertEqual(DraftProbeCode.DRAFT_CLEANUP_FAILED, result.code)
        self.assertFalse(result.cleanup_confirmed)

    def test_message_marker_hit_has_highest_failure_priority(self):
        baseline = DraftSnapshot({"wxid_a": ""})
        source = SequenceSource([
            baseline,
            DraftSnapshot({"wxid_a": self.MARKER}),
            baseline,
        ])
        driver = FakeProbeDriver()
        guard = FakeMarkerGuard(found=True)

        result = self._service(source, driver, guard).run()

        self.assertFalse(result.ok)
        self.assertEqual(DraftProbeCode.PROBE_MESSAGE_CREATED, result.code)
        self.assertTrue(result.cleanup_confirmed)

    def test_partial_input_failure_is_fail_closed_and_attempts_exact_remove(self):
        baseline = DraftSnapshot({"wxid_a": ""})
        source = SequenceSource([baseline])
        driver = FakeProbeDriver(
            insert_error=DraftProbeError(
                DraftProbeCode.PROBE_INPUT_FAILED,
                "could not type challenge",
            ),
            partial_count=2,
        )
        guard = FakeMarkerGuard(found=False)

        result = self._service(source, driver, guard).run()

        self.assertFalse(result.ok)
        self.assertEqual(DraftProbeCode.PROBE_INPUT_FAILED, result.code)
        self.assertIn(("remove", 2), driver.calls)

    def test_default_timeouts_are_fifteen_seconds(self):
        service = DraftIdentityProbeService(
            SequenceSource([DraftSnapshot({"wxid_a": ""})]),
            FakeProbeDriver(),
            FakeMarkerGuard(),
            marker_factory=lambda _baseline: self.MARKER,
        )

        self.assertEqual(15.0, service.observation_timeout_seconds)
        self.assertEqual(15.0, service.cleanup_timeout_seconds)

    def test_marker_factory_receives_baseline_snapshot(self):
        baseline = DraftSnapshot({"wxid_a": "baseline"})
        seen = []
        source = SequenceSource([
            baseline,
            DraftSnapshot({"wxid_a": "baseline" + self.MARKER}),
            baseline,
        ])
        driver = FakeProbeDriver()
        guard = FakeMarkerGuard()
        clock = FakeClock()
        service = DraftIdentityProbeService(
            source,
            driver,
            guard,
            marker_factory=lambda snapshot: seen.append(snapshot) or self.MARKER,
            wall_clock=lambda: 1000.0,
            clock=clock,
            sleep=clock.sleep,
            observation_timeout_seconds=0.2,
            cleanup_timeout_seconds=0.2,
            poll_seconds=0.1,
        )

        result = service.run()

        self.assertTrue(result.ok)
        self.assertEqual([baseline], seen)

    def test_clipboard_methods_are_never_called(self):
        baseline = DraftSnapshot({"wxid_a": ""})
        source = SequenceSource([
            baseline,
            DraftSnapshot({"wxid_a": self.MARKER}),
            baseline,
        ])
        driver = FakeProbeDriver()

        result = self._service(source, driver, FakeMarkerGuard()).run()

        self.assertTrue(result.ok)
        self.assertEqual(["insert", "remove"], [call[0] for call in driver.calls])


class SessionDraftSourceTests(unittest.TestCase):
    def test_snapshot_reads_username_and_draft_without_exposing_other_columns(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "session.db")
            conn = sqlite3.connect(path)
            try:
                conn.execute("CREATE TABLE SessionTable (username TEXT, draft)")
                conn.executemany(
                    "INSERT INTO SessionTable(username, draft) VALUES (?, ?)",
                    [
                        ("wxid_a", "secret draft"),
                        ("wxid_b", None),
                        ("room@chatroom", b"bytes-draft"),
                    ],
                )
                conn.commit()
            finally:
                conn.close()

            cache = SimpleNamespace(get=lambda rel: path)
            app = SimpleNamespace(cache=cache)
            snapshot = SessionDraftSource(app).snapshot()

        self.assertEqual(
            {
                "wxid_a": "secret draft",
                "wxid_b": "",
                "room@chatroom": "bytes-draft",
            },
            snapshot.drafts,
        )

    def test_missing_session_database_uses_stable_error_code(self):
        cache = SimpleNamespace(get=lambda rel: None)
        app = SimpleNamespace(cache=cache)

        with self.assertRaises(DraftProbeError) as cm:
            SessionDraftSource(app).snapshot()

        self.assertEqual(DraftProbeCode.SESSION_DB_UNAVAILABLE, cm.exception.code)
        self.assertNotIn("draft", str(cm.exception).casefold())


if __name__ == "__main__":
    unittest.main()
