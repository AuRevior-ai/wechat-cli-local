import unittest
from unittest.mock import patch

from wechat_cli.outbound.models import OutboundActionError, OutboundError, ResolvedTarget
from wechat_cli.outbound.windows_ui import WindowsWeChatAdapter


class FakeElementInfo:
    def __init__(
        self,
        *,
        process_id=1,
        control_type="Text",
        name="",
        automation_id="",
    ):
        self.process_id = process_id
        self.control_type = control_type
        self.name = name
        self.automation_id = automation_id


class FakeControl:
    def __init__(
        self,
        *,
        process_id=1,
        control_type="Text",
        name="",
        automation_id="",
        visible=True,
        enabled=True,
        descendants=None,
    ):
        self.element_info = FakeElementInfo(
            process_id=process_id,
            control_type=control_type,
            name=name,
            automation_id=automation_id,
        )
        self._visible = visible
        self._enabled = enabled
        self._descendants = list(descendants or [])
        self.focus_calls = 0
        self.set_text_calls = []
        self.click_calls = 0
        self.type_key_calls = []

    def is_visible(self):
        return self._visible

    def is_enabled(self):
        return self._enabled

    def descendants(self):
        return list(self._descendants)

    def window_text(self):
        return self.element_info.name

    def set_focus(self):
        self.focus_calls += 1

    def set_edit_text(self, text):
        self.set_text_calls.append(text)

    def click_input(self):
        self.click_calls += 1

    def type_keys(self, keys):
        self.type_key_calls.append(keys)


class FakeDesktop:
    def __init__(self, windows):
        self._windows = list(windows)

    def windows(self):
        return list(self._windows)


class FakeAutomation:
    def __init__(self, windows):
        self._windows = windows

    def Desktop(self, backend):
        if backend != "uia":
            raise AssertionError(f"unexpected backend: {backend}")
        return FakeDesktop(self._windows)


class WindowsWeChatAdapterTests(unittest.TestCase):
    def test_non_windows_probe_fails_closed(self):
        adapter = WindowsWeChatAdapter()
        with patch("wechat_cli.outbound.windows_ui.sys.platform", "linux"):
            result = adapter.probe()

        self.assertFalse(result.ok)
        self.assertEqual(OutboundError.UI_AUTOMATION_UNAVAILABLE, result.code)

    def test_missing_pywinauto_returns_install_hint(self):
        adapter = WindowsWeChatAdapter()
        with patch("wechat_cli.outbound.windows_ui.sys.platform", "win32"), \
             patch.object(adapter, "_weixin_pids", return_value={1}), \
             patch.object(adapter, "_load_automation", side_effect=ImportError("missing")):
            result = adapter.probe()

        self.assertFalse(result.ok)
        self.assertEqual(OutboundError.UI_AUTOMATION_UNAVAILABLE, result.code)
        self.assertIn("wechat-cli[outbound]", result.message)

    def test_no_weixin_process_reports_not_running(self):
        adapter = WindowsWeChatAdapter()
        with patch("wechat_cli.outbound.windows_ui.sys.platform", "win32"), \
             patch.object(adapter, "_weixin_pids", return_value=set()):
            result = adapter.probe()

        self.assertFalse(result.ok)
        self.assertEqual(OutboundError.WECHAT_NOT_RUNNING, result.code)

    def test_no_visible_enabled_weixin_window_reports_window_not_found(self):
        hidden = FakeControl(process_id=1, visible=False)
        other = FakeControl(process_id=2)
        adapter = WindowsWeChatAdapter()
        with patch("wechat_cli.outbound.windows_ui.sys.platform", "win32"), \
             patch.object(adapter, "_weixin_pids", return_value={1}), \
             patch.object(adapter, "_load_automation", return_value=FakeAutomation([hidden, other])):
            result = adapter.probe()

        self.assertFalse(result.ok)
        self.assertEqual(OutboundError.WECHAT_WINDOW_NOT_FOUND, result.code)

    def test_window_without_semantic_search_and_chat_controls_fails_closed(self):
        unlabeled = FakeControl(control_type="Edit", name="")
        window = FakeControl(process_id=1, name="微信", descendants=[unlabeled])
        adapter = WindowsWeChatAdapter()
        with patch("wechat_cli.outbound.windows_ui.sys.platform", "win32"), \
             patch.object(adapter, "_weixin_pids", return_value={1}), \
             patch.object(adapter, "_load_automation", return_value=FakeAutomation([window])):
            result = adapter.probe()

        self.assertFalse(result.ok)
        self.assertEqual(OutboundError.UI_AUTOMATION_UNAVAILABLE, result.code)
        self.assertEqual("uia", result.strategy)
        self.assertEqual([], unlabeled.set_text_calls)

    def test_positive_probe_identifies_controls_without_typing(self):
        search = FakeControl(control_type="Edit", name="搜索")
        chat = FakeControl(control_type="Edit", name="输入消息")
        window = FakeControl(process_id=1, name="微信", descendants=[search, chat])
        adapter = WindowsWeChatAdapter()
        with patch("wechat_cli.outbound.windows_ui.sys.platform", "win32"), \
             patch.object(adapter, "_weixin_pids", return_value={1}), \
             patch.object(adapter, "_client_version", return_value="4.1.13.12"), \
             patch.object(adapter, "_load_automation", return_value=FakeAutomation([window])):
            result = adapter.probe()

        self.assertTrue(result.ok)
        self.assertIsNone(result.code)
        self.assertEqual("uia", result.strategy)
        self.assertEqual("4.1.13.12", result.client_version)
        self.assertEqual([], search.set_text_calls)
        self.assertEqual([], chat.set_text_calls)

    def test_open_target_refuses_without_successful_probe(self):
        adapter = WindowsWeChatAdapter()
        target = ResolvedTarget("wxid_alice", "Alice", "private")

        with self.assertRaises(OutboundActionError) as cm:
            adapter.open_target(target)

        self.assertEqual(OutboundError.UI_AUTOMATION_UNAVAILABLE, cm.exception.code)

    def test_open_target_requires_unique_exact_result_and_header(self):
        search = FakeControl(control_type="Edit", name="搜索")
        chat = FakeControl(control_type="Edit", name="输入消息")
        result = FakeControl(control_type="ListItem", name="Alice")
        header = FakeControl(control_type="Text", name="Alice")
        window = FakeControl(
            process_id=1,
            name="微信",
            descendants=[search, chat, result, header],
        )
        adapter = WindowsWeChatAdapter()
        adapter._window = window
        adapter._search_control = search
        adapter._chat_input = chat
        adapter._strategy = "uia"
        target = ResolvedTarget("wxid_alice", "Alice", "private")

        adapter.open_target(target)

        self.assertEqual(["Alice"], search.set_text_calls)
        self.assertEqual(1, result.click_calls)
        self.assertIs(adapter._chat_input, chat)

    def test_open_target_blocks_ambiguous_exact_results_before_click(self):
        search = FakeControl(control_type="Edit", name="搜索")
        chat = FakeControl(control_type="Edit", name="输入消息")
        result1 = FakeControl(control_type="ListItem", name="Alice")
        result2 = FakeControl(control_type="DataItem", name="Alice")
        window = FakeControl(
            process_id=1,
            name="微信",
            descendants=[search, chat, result1, result2],
        )
        adapter = WindowsWeChatAdapter()
        adapter._window = window
        adapter._search_control = search
        adapter._chat_input = chat
        adapter._strategy = "uia"

        with self.assertRaises(OutboundActionError) as cm:
            adapter.open_target(ResolvedTarget("wxid_alice", "Alice", "private"))

        self.assertEqual(OutboundError.TARGET_UI_MISMATCH, cm.exception.code)
        self.assertEqual(0, result1.click_calls)
        self.assertEqual(0, result2.click_calls)

    def test_send_text_once_requires_verified_chat_input(self):
        adapter = WindowsWeChatAdapter()

        with self.assertRaises(OutboundActionError) as cm:
            adapter.send_text_once("hello")

        self.assertEqual(OutboundError.CHAT_INPUT_NOT_FOUND, cm.exception.code)

    def test_send_text_once_sets_text_and_presses_enter_once(self):
        chat = FakeControl(control_type="Edit", name="输入消息")
        adapter = WindowsWeChatAdapter()
        adapter._chat_input = chat
        adapter._target_verified = True

        adapter.send_text_once("hello")

        self.assertEqual(["hello"], chat.set_text_calls)
        self.assertEqual(["{ENTER}"], chat.type_key_calls)


if __name__ == "__main__":
    unittest.main()
