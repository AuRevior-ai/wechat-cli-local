from __future__ import annotations

import ctypes
from ctypes import wintypes
from dataclasses import dataclass
import os
import sys
import time

from .draft_identity import DraftProbeCode, DraftProbeError


@dataclass(frozen=True)
class _ClipboardToken:
    pointer: int = 0
    ole_initialized: bool = False
    was_empty: bool = False


class _MOUSEINPUT(ctypes.Structure):
    _fields_ = [
        ("dx", wintypes.LONG),
        ("dy", wintypes.LONG),
        ("mouseData", wintypes.DWORD),
        ("dwFlags", wintypes.DWORD),
        ("time", wintypes.DWORD),
        ("dwExtraInfo", ctypes.c_void_p),
    ]


class _KEYBDINPUT(ctypes.Structure):
    _fields_ = [
        ("wVk", wintypes.WORD),
        ("wScan", wintypes.WORD),
        ("dwFlags", wintypes.DWORD),
        ("time", wintypes.DWORD),
        ("dwExtraInfo", ctypes.c_void_p),
    ]


class _HARDWAREINPUT(ctypes.Structure):
    _fields_ = [
        ("uMsg", wintypes.DWORD),
        ("wParamL", wintypes.WORD),
        ("wParamH", wintypes.WORD),
    ]


class _INPUTUNION(ctypes.Union):
    _fields_ = [
        ("mi", _MOUSEINPUT),
        ("ki", _KEYBDINPUT),
        ("hi", _HARDWAREINPUT),
    ]


class _INPUT(ctypes.Structure):
    _anonymous_ = ("u",)
    _fields_ = [("type", wintypes.DWORD), ("u", _INPUTUNION)]


class _GUITHREADINFO(ctypes.Structure):
    _fields_ = [
        ("cbSize", wintypes.DWORD),
        ("flags", wintypes.DWORD),
        ("hwndActive", wintypes.HWND),
        ("hwndFocus", wintypes.HWND),
        ("hwndCapture", wintypes.HWND),
        ("hwndMenuOwner", wintypes.HWND),
        ("hwndMoveSize", wintypes.HWND),
        ("hwndCaret", wintypes.HWND),
        ("rcCaret", wintypes.RECT),
    ]


def _window_candidate_rank(
    *,
    visible: bool,
    preferred: bool,
    width: int,
    height: int,
    allow_hidden: bool = False,
    owned: bool = False,
):
    if owned:
        return None
    width = int(width)
    height = int(height)
    area = max(0, width) * max(0, height)
    if not visible:
        if allow_hidden and width >= 400 and height >= 300:
            return (3, area)
        return None
    if preferred:
        return (2, area)
    if width >= 400 and height >= 300:
        return (1, area)
    return None


class Win32DraftProbeNative:
    _PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
    _GW_OWNER = 4
    _SW_RESTORE = 9
    _CF_UNICODETEXT = 13
    _GMEM_MOVEABLE = 0x0002
    _MOUSEEVENTF_LEFTDOWN = 0x0002
    _MOUSEEVENTF_LEFTUP = 0x0004
    _KEYEVENTF_KEYUP = 0x0002
    _KEYEVENTF_UNICODE = 0x0004
    _INPUT_KEYBOARD = 1
    _VK_BACK = 0x08
    _VK_CONTROL = 0x11
    _VK_END = 0x23
    _VK_V = 0x56
    _VK_Z = 0x5A

    def __init__(self):
        if sys.platform != "win32":
            raise DraftProbeError(
                DraftProbeCode.PROBE_INPUT_FAILED,
                "The current-chat probe is supported only on Windows.",
            )
        self.user32 = ctypes.windll.user32
        self.kernel32 = ctypes.windll.kernel32
        self.ole32 = ctypes.windll.ole32
        self._configure_signatures()

    def _configure_signatures(self) -> None:
        """Declare pointer-sized Win32 signatures explicitly for 64-bit safety."""
        void_p = ctypes.c_void_p

        self.kernel32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
        self.kernel32.OpenProcess.restype = void_p
        self.kernel32.QueryFullProcessImageNameW.argtypes = [
            void_p,
            wintypes.DWORD,
            wintypes.LPWSTR,
            ctypes.POINTER(wintypes.DWORD),
        ]
        self.kernel32.QueryFullProcessImageNameW.restype = wintypes.BOOL
        self.kernel32.CloseHandle.argtypes = [void_p]
        self.kernel32.CloseHandle.restype = wintypes.BOOL
        self.kernel32.GetCurrentThreadId.argtypes = []
        self.kernel32.GetCurrentThreadId.restype = wintypes.DWORD

        self.kernel32.GlobalAlloc.argtypes = [wintypes.UINT, ctypes.c_size_t]
        self.kernel32.GlobalAlloc.restype = void_p
        self.kernel32.GlobalLock.argtypes = [void_p]
        self.kernel32.GlobalLock.restype = void_p
        self.kernel32.GlobalUnlock.argtypes = [void_p]
        self.kernel32.GlobalUnlock.restype = wintypes.BOOL
        self.kernel32.GlobalFree.argtypes = [void_p]
        self.kernel32.GlobalFree.restype = void_p

        self.user32.IsWindowVisible.argtypes = [void_p]
        self.user32.IsWindowVisible.restype = wintypes.BOOL
        self.user32.GetWindowThreadProcessId.argtypes = [
            void_p,
            ctypes.POINTER(wintypes.DWORD),
        ]
        self.user32.GetWindowThreadProcessId.restype = wintypes.DWORD
        self.user32.GetWindowRect.argtypes = [void_p, ctypes.POINTER(wintypes.RECT)]
        self.user32.GetWindowRect.restype = wintypes.BOOL
        self.user32.GetWindowTextW.argtypes = [void_p, wintypes.LPWSTR, ctypes.c_int]
        self.user32.GetWindowTextW.restype = ctypes.c_int
        self.user32.GetWindow.argtypes = [void_p, wintypes.UINT]
        self.user32.GetWindow.restype = void_p
        self.user32.ShowWindow.argtypes = [void_p, ctypes.c_int]
        self.user32.ShowWindow.restype = wintypes.BOOL
        self.user32.BringWindowToTop.argtypes = [void_p]
        self.user32.BringWindowToTop.restype = wintypes.BOOL
        self.user32.SetForegroundWindow.argtypes = [void_p]
        self.user32.SetForegroundWindow.restype = wintypes.BOOL
        self.user32.GetForegroundWindow.argtypes = []
        self.user32.GetForegroundWindow.restype = void_p
        self.user32.AttachThreadInput.argtypes = [
            wintypes.DWORD,
            wintypes.DWORD,
            wintypes.BOOL,
        ]
        self.user32.AttachThreadInput.restype = wintypes.BOOL
        self.user32.GetGUIThreadInfo.argtypes = [
            wintypes.DWORD,
            ctypes.POINTER(_GUITHREADINFO),
        ]
        self.user32.GetGUIThreadInfo.restype = wintypes.BOOL
        self.user32.SendInput.argtypes = [
            wintypes.UINT,
            ctypes.POINTER(_INPUT),
            ctypes.c_int,
        ]
        self.user32.SendInput.restype = wintypes.UINT
        self.user32.OpenClipboard.argtypes = [void_p]
        self.user32.OpenClipboard.restype = wintypes.BOOL
        self.user32.SetClipboardData.argtypes = [wintypes.UINT, void_p]
        self.user32.SetClipboardData.restype = void_p

        self.ole32.OleInitialize.argtypes = [void_p]
        self.ole32.OleInitialize.restype = ctypes.c_long
        self.ole32.OleGetClipboard.argtypes = [ctypes.POINTER(void_p)]
        self.ole32.OleGetClipboard.restype = ctypes.c_long
        self.ole32.OleSetClipboard.argtypes = [void_p]
        self.ole32.OleSetClipboard.restype = ctypes.c_long
        self.ole32.OleUninitialize.argtypes = []
        self.ole32.OleUninitialize.restype = None

    def _process_image_name(self, pid: int) -> str:
        handle = self.kernel32.OpenProcess(
            self._PROCESS_QUERY_LIMITED_INFORMATION,
            False,
            int(pid),
        )
        if not handle:
            return ""
        try:
            buffer = ctypes.create_unicode_buffer(1024)
            size = wintypes.DWORD(len(buffer))
            ok = self.kernel32.QueryFullProcessImageNameW(
                handle,
                0,
                buffer,
                ctypes.byref(size),
            )
            if not ok:
                return ""
            return str(buffer.value or "")
        finally:
            self.kernel32.CloseHandle(handle)

    def find_main_window(self):
        candidates = []
        callback_factory = getattr(ctypes, "WINFUNCTYPE", ctypes.CFUNCTYPE)
        enum_proc_type = callback_factory(
            wintypes.BOOL,
            wintypes.HWND,
            wintypes.LPARAM,
        )

        @enum_proc_type
        def callback(hwnd, _lparam):
            visible = bool(self.user32.IsWindowVisible(hwnd))
            pid = wintypes.DWORD()
            self.user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
            image = self._process_image_name(int(pid.value))
            if os.path.basename(image).casefold() != "weixin.exe":
                return True

            rect = wintypes.RECT()
            if not self.user32.GetWindowRect(hwnd, ctypes.byref(rect)):
                return True
            width = int(rect.right - rect.left)
            height = int(rect.bottom - rect.top)

            title_buffer = ctypes.create_unicode_buffer(512)
            self.user32.GetWindowTextW(hwnd, title_buffer, len(title_buffer))
            title = str(title_buffer.value or "").strip().casefold()
            preferred = title in {"微信", "΢��", "wechat", "weixin"}
            allow_hidden = title in {"微信", "΢��", "wechat"}
            owner = int(self.user32.GetWindow(hwnd, self._GW_OWNER) or 0)
            rank = _window_candidate_rank(
                visible=visible,
                preferred=preferred,
                width=width,
                height=height,
                allow_hidden=allow_hidden,
                owned=bool(owner),
            )
            if rank is None:
                return True
            candidates.append(
                (
                    rank[0],
                    rank[1],
                    int(hwnd),
                    (int(rect.left), int(rect.top), int(rect.right), int(rect.bottom)),
                )
            )
            return True

        self.user32.EnumWindows(callback, 0)
        if not candidates:
            return None
        candidates.sort(key=lambda item: (item[0], item[1]), reverse=True)
        _, _, hwnd, rect = candidates[0]
        return hwnd, rect

    def activate(self, hwnd: int) -> None:
        target = int(hwnd)
        self.user32.ShowWindow(target, self._SW_RESTORE)
        self.user32.BringWindowToTop(target)
        if self.user32.SetForegroundWindow(target):
            if int(self.user32.GetForegroundWindow() or 0) == target:
                return

        foreground = int(self.user32.GetForegroundWindow() or 0)
        foreground_thread = int(
            self.user32.GetWindowThreadProcessId(foreground, None) or 0
        ) if foreground else 0
        target_thread = int(
            self.user32.GetWindowThreadProcessId(target, None) or 0
        )
        current_thread = int(self.kernel32.GetCurrentThreadId() or 0)
        attached_foreground = False
        attached_target = False
        try:
            if (
                foreground_thread
                and current_thread
                and foreground_thread != current_thread
            ):
                attached_foreground = bool(
                    self.user32.AttachThreadInput(
                        current_thread,
                        foreground_thread,
                        True,
                    )
                )
            if (
                target_thread
                and current_thread
                and target_thread != current_thread
                and target_thread != foreground_thread
            ):
                attached_target = bool(
                    self.user32.AttachThreadInput(
                        current_thread,
                        target_thread,
                        True,
                    )
                )
            self.user32.BringWindowToTop(target)
            self.user32.SetForegroundWindow(target)
        finally:
            if attached_target:
                self.user32.AttachThreadInput(
                    current_thread,
                    target_thread,
                    False,
                )
            if attached_foreground:
                self.user32.AttachThreadInput(
                    current_thread,
                    foreground_thread,
                    False,
                )

        if int(self.user32.GetForegroundWindow() or 0) != target:
            raise OSError("WeChat could not be brought to the foreground")

    def window_rect(self, hwnd: int):
        rect = wintypes.RECT()
        if not self.user32.GetWindowRect(int(hwnd), ctypes.byref(rect)):
            return None
        width = int(rect.right - rect.left)
        height = int(rect.bottom - rect.top)
        if width < 400 or height < 300:
            return None
        return (int(rect.left), int(rect.top), int(rect.right), int(rect.bottom))

    def click(self, x: int, y: int) -> None:
        if not self.user32.SetCursorPos(int(x), int(y)):
            raise OSError("Could not position the mouse cursor")
        self.user32.mouse_event(self._MOUSEEVENTF_LEFTDOWN, 0, 0, 0, 0)
        self.user32.mouse_event(self._MOUSEEVENTF_LEFTUP, 0, 0, 0, 0)

    def compose_has_caret(self, hwnd: int) -> bool:
        thread_id = int(self.user32.GetWindowThreadProcessId(int(hwnd), None) or 0)
        if not thread_id:
            return False
        info = _GUITHREADINFO()
        info.cbSize = ctypes.sizeof(_GUITHREADINFO)
        if not self.user32.GetGUIThreadInfo(thread_id, ctypes.byref(info)):
            return False
        return int(info.hwndCaret or 0) == int(hwnd)

    def _send_input_pair(self, down: _KEYBDINPUT, up: _KEYBDINPUT) -> bool:
        inputs = (_INPUT * 2)()
        inputs[0].type = self._INPUT_KEYBOARD
        inputs[0].ki = down
        inputs[1].type = self._INPUT_KEYBOARD
        inputs[1].ki = up
        return int(self.user32.SendInput(2, inputs, ctypes.sizeof(_INPUT))) == 2

    def send_unicode_char(self, ch: str) -> bool:
        value = str(ch or "")
        if len(value) != 1 or ord(value) > 0xFFFF:
            return False
        scan = ord(value)
        return self._send_input_pair(
            _KEYBDINPUT(0, scan, self._KEYEVENTF_UNICODE, 0, None),
            _KEYBDINPUT(0, scan, self._KEYEVENTF_UNICODE | self._KEYEVENTF_KEYUP, 0, None),
        )

    def backspace(self) -> bool:
        return self._send_input_pair(
            _KEYBDINPUT(self._VK_BACK, 0, 0, 0, None),
            _KEYBDINPUT(self._VK_BACK, 0, self._KEYEVENTF_KEYUP, 0, None),
        )

    def ctrl_end(self) -> None:
        self._ctrl_chord(self._VK_END)

    def backup_clipboard(self) -> _ClipboardToken:
        if int(self.user32.CountClipboardFormats()) == 0:
            return _ClipboardToken(was_empty=True)

        hr = int(self.ole32.OleInitialize(None))
        if hr < 0:
            raise OSError("OLE clipboard initialization failed")
        pointer = ctypes.c_void_p()
        get_hr = int(self.ole32.OleGetClipboard(ctypes.byref(pointer)))
        if get_hr < 0 or not pointer.value:
            self.ole32.OleUninitialize()
            raise OSError("The current clipboard could not be preserved")
        return _ClipboardToken(
            pointer=int(pointer.value),
            ole_initialized=True,
            was_empty=False,
        )

    def _open_clipboard(self) -> None:
        for _ in range(20):
            if self.user32.OpenClipboard(None):
                return
            time.sleep(0.01)
        raise OSError("The clipboard is busy")

    def set_clipboard_text(self, text: str) -> None:
        self._open_clipboard()
        memory = None
        try:
            if not self.user32.EmptyClipboard():
                raise OSError("The clipboard could not be cleared")
            buffer = ctypes.create_unicode_buffer(str(text))
            size = ctypes.sizeof(buffer)
            memory = self.kernel32.GlobalAlloc(self._GMEM_MOVEABLE, size)
            if not memory:
                raise OSError("Clipboard memory allocation failed")
            locked = self.kernel32.GlobalLock(memory)
            if not locked:
                raise OSError("Clipboard memory lock failed")
            try:
                ctypes.memmove(locked, ctypes.addressof(buffer), size)
            finally:
                self.kernel32.GlobalUnlock(memory)
            if not self.user32.SetClipboardData(self._CF_UNICODETEXT, memory):
                raise OSError("Clipboard text installation failed")
            memory = None  # clipboard owns the allocation after SetClipboardData succeeds
        finally:
            self.user32.CloseClipboard()
            if memory:
                self.kernel32.GlobalFree(memory)

    def _release_unknown(self, pointer: int) -> None:
        if not pointer:
            return
        interface = ctypes.c_void_p(pointer)
        vtable = ctypes.cast(
            interface,
            ctypes.POINTER(ctypes.POINTER(ctypes.c_void_p)),
        ).contents
        release_type = getattr(ctypes, "WINFUNCTYPE", ctypes.CFUNCTYPE)(
            ctypes.c_ulong,
            ctypes.c_void_p,
        )
        release = release_type(vtable[2])
        release(interface)

    def restore_clipboard(self, token: _ClipboardToken) -> None:
        try:
            if token.was_empty:
                self._open_clipboard()
                try:
                    if not self.user32.EmptyClipboard():
                        raise OSError("Could not restore an empty clipboard")
                finally:
                    self.user32.CloseClipboard()
                return
            if not token.pointer:
                raise OSError("Clipboard backup is unavailable")
            hr = int(self.ole32.OleSetClipboard(ctypes.c_void_p(token.pointer)))
            if hr < 0:
                raise OSError("Original clipboard restoration failed")
        finally:
            if token.pointer:
                self._release_unknown(token.pointer)
            if token.ole_initialized:
                self.ole32.OleUninitialize()

    def _ctrl_chord(self, key_code: int) -> None:
        self.user32.keybd_event(self._VK_CONTROL, 0, 0, 0)
        self.user32.keybd_event(int(key_code), 0, 0, 0)
        self.user32.keybd_event(int(key_code), 0, self._KEYEVENTF_KEYUP, 0)
        self.user32.keybd_event(self._VK_CONTROL, 0, self._KEYEVENTF_KEYUP, 0)

    def ctrl_v(self) -> None:
        self._ctrl_chord(self._VK_V)

    def ctrl_z(self) -> None:
        self._ctrl_chord(self._VK_Z)


class WindowsDraftProbeDriver:
    def __init__(
        self,
        *,
        native=None,
        sleep=time.sleep,
        settle_seconds: float = 0.15,
    ):
        self.native = native if native is not None else Win32DraftProbeNative()
        self.sleep = sleep
        self.settle_seconds = max(0.0, float(settle_seconds))
        self._clipboard_token = None
        self._marker = ""
        self.paste_attempted = False
        self.inserted_char_count = 0

    def _focus_compose(self) -> int:
        window = self.native.find_main_window()
        if not window:
            raise DraftProbeError(
                DraftProbeCode.PROBE_INPUT_FAILED,
                "No usable WeChat main window was found.",
            )
        hwnd, _initial_rect = window
        try:
            self.native.activate(hwnd)
            if self.settle_seconds:
                self.sleep(self.settle_seconds)
            rect = self.native.window_rect(hwnd)
            if not rect:
                raise OSError("WeChat did not restore to usable window bounds")
            left, top, right, bottom = rect
            width = max(1, int(right) - int(left))
            height = max(1, int(bottom) - int(top))
            x = int(round(int(left) + width * 0.72))
            y = int(round(int(top) + height * 0.84))
            self.native.click(x, y)
            if self.settle_seconds:
                self.sleep(self.settle_seconds)
            if not self.native.compose_has_caret(hwnd):
                raise OSError("WeChat compose region did not acquire a caret")
            return int(hwnd)
        except DraftProbeError:
            raise
        except Exception as exc:
            raise DraftProbeError(
                DraftProbeCode.PROBE_INPUT_FAILED,
                "The WeChat compose region could not be focused safely.",
            ) from exc

    def insert_challenge(self, marker: str) -> None:
        value = str(marker or "")
        if not value:
            raise DraftProbeError(
                DraftProbeCode.PROBE_INPUT_FAILED,
                "A non-empty challenge marker is required.",
            )
        self.inserted_char_count = 0
        self._focus_compose()
        try:
            self.native.ctrl_end()
            for ch in value:
                if not self.native.send_unicode_char(ch):
                    raise OSError("Unicode challenge input failed")
                self.inserted_char_count += 1
        except Exception as exc:
            raise DraftProbeError(
                DraftProbeCode.PROBE_INPUT_FAILED,
                "Draft challenge input failed.",
            ) from exc

    def remove_challenge(self) -> None:
        remaining = int(self.inserted_char_count)
        if remaining <= 0:
            return
        self._focus_compose()
        try:
            self.native.ctrl_end()
            while self.inserted_char_count > 0:
                if not self.native.backspace():
                    raise OSError("Backspace challenge cleanup failed")
                self.inserted_char_count -= 1
        except Exception as exc:
            raise DraftProbeError(
                DraftProbeCode.PROBE_INPUT_FAILED,
                "Draft challenge cleanup input failed.",
            ) from exc

    def prepare_challenge(self, marker: str) -> None:
        if not marker:
            raise DraftProbeError(
                DraftProbeCode.PROBE_INPUT_FAILED,
                "A non-empty challenge marker is required.",
            )
        try:
            token = self.native.backup_clipboard()
        except Exception as exc:
            raise DraftProbeError(
                DraftProbeCode.CLIPBOARD_UNAVAILABLE,
                "The current clipboard could not be preserved safely.",
            ) from exc
        self._clipboard_token = token
        self._marker = str(marker)
        self.paste_attempted = False
        try:
            self.native.set_clipboard_text(self._marker)
        except Exception as exc:
            try:
                self.restore_clipboard()
            except Exception:
                pass
            raise DraftProbeError(
                DraftProbeCode.CLIPBOARD_UNAVAILABLE,
                "The temporary clipboard challenge could not be prepared.",
            ) from exc

    def paste_challenge(self) -> None:
        window = self.native.find_main_window()
        if not window:
            raise DraftProbeError(
                DraftProbeCode.PROBE_INPUT_FAILED,
                "No usable WeChat main window was found.",
            )
        hwnd, _initial_rect = window
        try:
            self.native.activate(hwnd)
            if self.settle_seconds:
                self.sleep(self.settle_seconds)
            rect = self.native.window_rect(hwnd)
            if not rect:
                raise OSError("WeChat did not restore to usable window bounds")
            left, top, right, bottom = rect
            width = max(1, int(right) - int(left))
            height = max(1, int(bottom) - int(top))
            x = int(round(int(left) + width * 0.72))
            y = int(round(int(top) + height * 0.84))
            self.native.click(x, y)
            if self.settle_seconds:
                self.sleep(self.settle_seconds)
            self.paste_attempted = True
            self.native.ctrl_v()
        except DraftProbeError:
            raise
        except Exception as exc:
            raise DraftProbeError(
                DraftProbeCode.PROBE_INPUT_FAILED,
                "The no-send draft challenge could not be inserted.",
            ) from exc

    def undo_challenge(self) -> None:
        if not self.paste_attempted:
            return
        try:
            self.native.ctrl_z()
        except Exception as exc:
            raise DraftProbeError(
                DraftProbeCode.PROBE_INPUT_FAILED,
                "The temporary draft challenge could not be undone.",
            ) from exc
        finally:
            self.paste_attempted = False

    def restore_clipboard(self) -> None:
        token = self._clipboard_token
        if token is None:
            return
        self._clipboard_token = None
        try:
            self.native.restore_clipboard(token)
        except Exception as exc:
            raise DraftProbeError(
                DraftProbeCode.CLIPBOARD_UNAVAILABLE,
                "The original clipboard could not be restored.",
            ) from exc
