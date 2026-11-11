import ctypes
import sys
import unittest

from wechat_cli.outbound.draft_identity import DraftProbeCode, DraftProbeError
from wechat_cli.outbound import windows_draft_probe
from wechat_cli.outbound.windows_draft_probe import Win32DraftProbeNative, WindowsDraftProbeDriver


class FakeNative:
    def __init__(
        self,
        *,
        set_clipboard_error=None,
        window=None,
        restored_rect=None,
        fail_unicode_at=None,
        caret_owned=True,
    ):
        self.actions = []
        self.set_clipboard_error = set_clipboard_error
        self.window = window if window is not None else (123, (100, 100, 1100, 800))
        self.restored_rect = restored_rect if restored_rect is not None else (100, 100, 1100, 800)
        self.fail_unicode_at = fail_unicode_at
        self.caret_owned = caret_owned
        self.unicode_calls = 0
        self.token = object()

    def find_main_window(self):
        self.actions.append(("find",))
        return self.window

    def activate(self, hwnd):
        self.actions.append(("activate", hwnd))

    def window_rect(self, hwnd):
        self.actions.append(("window_rect", hwnd))
        return self.restored_rect

    def click(self, x, y):
        self.actions.append(("click", x, y))

    def compose_has_caret(self, hwnd):
        self.actions.append(("caret", hwnd))
        return self.caret_owned

    def ctrl_end(self):
        self.actions.append(("ctrl_end",))

    def send_unicode_char(self, ch):
        index = self.unicode_calls
        self.unicode_calls += 1
        self.actions.append(("unicode", ch))
        return self.fail_unicode_at != index

    def backspace(self):
        self.actions.append(("backspace",))
        return True

    def backup_clipboard(self):
        self.actions.append(("backup",))
        return self.token

    def set_clipboard_text(self, text):
        self.actions.append(("clipboard", text))
        if self.set_clipboard_error is not None:
            raise self.set_clipboard_error

    def ctrl_v(self):
        self.actions.append(("ctrl_v",))

    def ctrl_z(self):
        self.actions.append(("ctrl_z",))

    def restore_clipboard(self, token):
        self.actions.append(("restore", token))


@unittest.skipUnless(sys.platform == "win32", "Windows-only Win32 signature contract")
class Win32DraftProbeNativeTests(unittest.TestCase):
    def test_pointer_sized_win32_returns_are_configured(self):
        native = Win32DraftProbeNative()

        self.assertIs(ctypes.c_void_p, native.kernel32.GlobalAlloc.restype)
        self.assertIs(ctypes.c_void_p, native.kernel32.GlobalLock.restype)
        self.assertIs(ctypes.c_void_p, native.kernel32.GlobalFree.restype)
        self.assertIs(ctypes.c_void_p, native.user32.SetClipboardData.restype)
        self.assertIs(ctypes.c_void_p, native.user32.GetForegroundWindow.restype)
        self.assertIs(ctypes.c_void_p, native.kernel32.OpenProcess.restype)

    def test_activate_attaches_to_foreground_and_target_when_direct_switch_is_blocked(self):
        class FakeUser32:
            def __init__(self):
                self.foreground = 999
                self.attached_threads = set()
                self.attach_calls = []

            def ShowWindow(self, hwnd, mode):
                return True

            def BringWindowToTop(self, hwnd):
                return True

            def SetForegroundWindow(self, hwnd):
                if {77, 66} - self.attached_threads:
                    return False
                self.foreground = int(hwnd)
                return True

            def GetForegroundWindow(self):
                return self.foreground

            def GetWindowThreadProcessId(self, hwnd, _pid_ptr):
                if int(hwnd) == 999:
                    return 77
                if int(hwnd) == 123:
                    return 66
                return 0

            def AttachThreadInput(self, first, second, attach):
                thread = int(second)
                self.attach_calls.append((int(first), thread, bool(attach)))
                if attach:
                    self.attached_threads.add(thread)
                else:
                    self.attached_threads.discard(thread)
                return True

        class FakeKernel32:
            def GetCurrentThreadId(self):
                return 88

        native = object.__new__(Win32DraftProbeNative)
        native.user32 = FakeUser32()
        native.kernel32 = FakeKernel32()

        native.activate(123)

        self.assertEqual(123, native.user32.foreground)
        self.assertEqual(
            [
                (88, 77, True),
                (88, 66, True),
                (88, 66, False),
                (88, 77, False),
            ],
            native.user32.attach_calls,
        )


class WindowCandidateTests(unittest.TestCase):
    def test_visible_preferred_tray_placeholder_is_eligible_for_restore(self):
        rank = windows_draft_probe._window_candidate_rank(
            visible=True,
            preferred=True,
            width=159,
            height=27,
        )

        self.assertIsNotNone(rank)

    def test_hidden_localized_main_window_is_eligible_for_restore(self):
        rank = windows_draft_probe._window_candidate_rank(
            visible=False,
            preferred=True,
            allow_hidden=True,
            width=897,
            height=648,
        )

        self.assertIsNotNone(rank)
        self.assertEqual(3, rank[0])

    def test_owned_preferred_tool_window_is_rejected(self):
        self.assertIsNone(
            windows_draft_probe._window_candidate_rank(
                visible=True,
                preferred=True,
                width=825,
                height=734,
                owned=True,
            )
        )

    def test_nonpreferred_tiny_or_hidden_windows_are_rejected(self):
        self.assertIsNone(
            windows_draft_probe._window_candidate_rank(
                visible=True,
                preferred=False,
                width=159,
                height=27,
            )
        )
        self.assertIsNone(
            windows_draft_probe._window_candidate_rank(
                visible=False,
                preferred=True,
                width=1200,
                height=700,
            )
        )


class WindowsDraftProbeDriverTests(unittest.TestCase):
    def test_insert_challenge_moves_to_end_and_types_each_character_once(self):
        native = FakeNative()
        driver = WindowsDraftProbeDriver(native=native, sleep=lambda _s: None)

        driver.insert_challenge("§¶¤※")

        self.assertEqual(4, driver.inserted_char_count)
        self.assertEqual(
            [("ctrl_end",), ("unicode", "§"), ("unicode", "¶"), ("unicode", "¤"), ("unicode", "※")],
            [action for action in native.actions if action[0] in {"ctrl_end", "unicode"}],
        )

    def test_partial_insert_tracks_only_successful_characters(self):
        native = FakeNative(fail_unicode_at=2)
        driver = WindowsDraftProbeDriver(native=native, sleep=lambda _s: None)

        with self.assertRaises(DraftProbeError) as cm:
            driver.insert_challenge("§¶¤※")

        self.assertEqual(DraftProbeCode.PROBE_INPUT_FAILED, cm.exception.code)
        self.assertEqual(2, driver.inserted_char_count)

    def test_remove_challenge_backspaces_exact_inserted_count(self):
        native = FakeNative()
        driver = WindowsDraftProbeDriver(native=native, sleep=lambda _s: None)
        driver.inserted_char_count = 4

        driver.remove_challenge()

        self.assertEqual(4, len([action for action in native.actions if action[0] == "backspace"]))
        self.assertEqual(0, driver.inserted_char_count)

    def test_insert_challenge_requires_compose_caret(self):
        native = FakeNative(caret_owned=False)
        driver = WindowsDraftProbeDriver(native=native, sleep=lambda _s: None)

        with self.assertRaises(DraftProbeError) as cm:
            driver.insert_challenge("§¶¤※")

        self.assertEqual(DraftProbeCode.PROBE_INPUT_FAILED, cm.exception.code)
        self.assertEqual(0, driver.inserted_char_count)

    def test_prepare_and_paste_use_only_safe_actions(self):
        native = FakeNative()
        driver = WindowsDraftProbeDriver(native=native, sleep=lambda _s: None)

        driver.prepare_challenge("[[probe]]")
        driver.paste_challenge()

        self.assertEqual(
            [
                ("backup",),
                ("clipboard", "[[probe]]"),
                ("find",),
                ("activate", 123),
                ("window_rect", 123),
                ("click", 820, 688),
                ("ctrl_v",),
            ],
            native.actions,
        )

    def test_minimized_placeholder_coordinates_are_not_used_after_restore(self):
        native = FakeNative(
            window=(123, (-25600, -25600, -25441, -25573)),
            restored_rect=(200, 150, 1200, 850),
        )
        driver = WindowsDraftProbeDriver(native=native, sleep=lambda _s: None)
        driver.prepare_challenge("[[probe]]")

        driver.paste_challenge()

        self.assertIn(("window_rect", 123), native.actions)
        self.assertIn(("click", 920, 738), native.actions)
        self.assertNotIn(("click", -25486, -25577), native.actions)

    def test_undo_is_exactly_one_ctrl_z(self):
        native = FakeNative()
        driver = WindowsDraftProbeDriver(native=native, sleep=lambda _s: None)
        driver.prepare_challenge("[[probe]]")
        driver.paste_challenge()
        before = len(native.actions)

        driver.undo_challenge()

        self.assertEqual([("ctrl_z",)], native.actions[before:])

    def test_restore_clipboard_uses_original_token_once(self):
        native = FakeNative()
        driver = WindowsDraftProbeDriver(native=native, sleep=lambda _s: None)
        driver.prepare_challenge("[[probe]]")

        driver.restore_clipboard()
        driver.restore_clipboard()

        restores = [action for action in native.actions if action[0] == "restore"]
        self.assertEqual([("restore", native.token)], restores)

    def test_prepare_restores_clipboard_when_marker_install_fails(self):
        native = FakeNative(set_clipboard_error=RuntimeError("clipboard busy"))
        driver = WindowsDraftProbeDriver(native=native, sleep=lambda _s: None)

        with self.assertRaises(DraftProbeError) as cm:
            driver.prepare_challenge("[[probe]]")

        self.assertEqual(DraftProbeCode.CLIPBOARD_UNAVAILABLE, cm.exception.code)
        self.assertEqual(("restore", native.token), native.actions[-1])

    def test_missing_window_is_probe_input_failure(self):
        native = FakeNative(window=None)
        native.window = None
        driver = WindowsDraftProbeDriver(native=native, sleep=lambda _s: None)
        driver.prepare_challenge("[[probe]]")

        with self.assertRaises(DraftProbeError) as cm:
            driver.paste_challenge()

        self.assertEqual(DraftProbeCode.PROBE_INPUT_FAILED, cm.exception.code)
        driver.restore_clipboard()

    def test_public_surface_has_no_generic_or_send_capable_methods(self):
        names = set(dir(WindowsDraftProbeDriver))

        self.assertNotIn("send_keys", names)
        self.assertNotIn("press_enter", names)
        self.assertNotIn("click_send", names)
        self.assertNotIn("send", names)
        self.assertTrue(
            {"prepare_challenge", "paste_challenge", "undo_challenge", "restore_clipboard"}
            <= names
        )


if __name__ == "__main__":
    unittest.main()
