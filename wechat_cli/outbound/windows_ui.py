from __future__ import annotations

import csv
import io
import subprocess
import sys
import time
from types import SimpleNamespace

from .models import OutboundActionError, OutboundError, ProbeResult

_SEARCH_TOKENS = ("search", "搜索", "查找")
_CHAT_INPUT_TOKENS = ("input", "message", "消息", "输入", "编辑")
_RESULT_CONTROL_TYPES = {"ListItem", "DataItem", "TreeItem"}
_HEADER_CONTROL_TYPES = {"Text", "Group", "Pane"}
_EDIT_CONTROL_TYPES = {"Edit", "Document"}


def _safe_bool_call(control, name: str, default: bool = False) -> bool:
    try:
        return bool(getattr(control, name)())
    except Exception:
        return default


def _control_type(control) -> str:
    try:
        return str(control.element_info.control_type or "")
    except Exception:
        return ""


def _control_text(control) -> str:
    values = []
    try:
        values.append(control.window_text())
    except Exception:
        pass
    try:
        values.append(control.element_info.name)
    except Exception:
        pass
    try:
        values.append(control.element_info.automation_id)
    except Exception:
        pass
    return " ".join(str(value or "") for value in values).strip()


def _exact_visible_controls(controls, display_name: str, allowed_types: set[str]):
    expected = str(display_name or "").strip().casefold()
    if not expected:
        return []
    result = []
    for control in controls:
        if _control_type(control) not in allowed_types:
            continue
        if not _safe_bool_call(control, "is_visible"):
            continue
        if not _safe_bool_call(control, "is_enabled"):
            continue
        try:
            text = str(control.window_text() or "").strip()
        except Exception:
            text = ""
        if text.casefold() == expected:
            result.append(control)
    return result


class WindowsWeChatAdapter:
    def __init__(self, *, sleep=time.sleep, settle_seconds: float = 0.15):
        self._sleep = sleep
        self._settle_seconds = max(0.0, float(settle_seconds))
        self._window = None
        self._search_control = None
        self._chat_input = None
        self._strategy = ""
        self._target_verified = False

    def _load_automation(self):
        from pywinauto import Desktop

        return SimpleNamespace(Desktop=Desktop)

    def _weixin_pids(self) -> set[int]:
        if sys.platform != "win32":
            return set()
        try:
            completed = subprocess.run(
                [
                    "tasklist",
                    "/FI",
                    "IMAGENAME eq Weixin.exe",
                    "/FO",
                    "CSV",
                    "/NH",
                ],
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=5,
                check=False,
            )
        except (OSError, subprocess.SubprocessError):
            return set()
        if completed.returncode != 0:
            return set()
        pids: set[int] = set()
        for row in csv.reader(io.StringIO(completed.stdout or "")):
            if len(row) < 2:
                continue
            if str(row[0]).strip().casefold() != "weixin.exe":
                continue
            try:
                pids.add(int(str(row[1]).replace(",", "").strip()))
            except ValueError:
                continue
        return pids

    def _client_version(self) -> str:
        if sys.platform != "win32":
            return ""
        command = (
            "$p=Get-Process Weixin -ErrorAction SilentlyContinue | Select-Object -First 1; "
            "if($p -and $p.Path){(Get-Item $p.Path).VersionInfo.FileVersion}"
        )
        try:
            completed = subprocess.run(
                ["powershell", "-NoProfile", "-Command", command],
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=5,
                check=False,
            )
        except (OSError, subprocess.SubprocessError):
            return ""
        if completed.returncode != 0:
            return ""
        return str(completed.stdout or "").strip().splitlines()[0].strip() if completed.stdout.strip() else ""

    def _descendants(self, window):
        try:
            return list(window.descendants())
        except Exception:
            return []

    def _semantic_control(self, controls, tokens):
        matches = []
        for control in controls:
            if _control_type(control) not in _EDIT_CONTROL_TYPES:
                continue
            if not _safe_bool_call(control, "is_visible"):
                continue
            if not _safe_bool_call(control, "is_enabled"):
                continue
            semantic = _control_text(control).casefold()
            if semantic and any(token.casefold() in semantic for token in tokens):
                matches.append(control)
        if len(matches) != 1:
            return None
        return matches[0]

    def probe(self) -> ProbeResult:
        self._window = None
        self._search_control = None
        self._chat_input = None
        self._strategy = ""
        self._target_verified = False

        if sys.platform != "win32":
            return ProbeResult(
                ok=False,
                code=OutboundError.UI_AUTOMATION_UNAVAILABLE,
                message="Outbound UI automation is supported only on Windows.",
            )

        pids = self._weixin_pids()
        version = self._client_version()
        if not pids:
            return ProbeResult(
                ok=False,
                code=OutboundError.WECHAT_NOT_RUNNING,
                message="Weixin.exe is not running.",
                client_version=version,
            )

        try:
            automation = self._load_automation()
        except ImportError:
            return ProbeResult(
                ok=False,
                code=OutboundError.UI_AUTOMATION_UNAVAILABLE,
                message=(
                    "pywinauto is not installed. Install the optional outbound dependency "
                    "with: pip install -e \".[outbound]\" (package extra: wechat-cli[outbound])."
                ),
                client_version=version,
            )
        except Exception as exc:
            return ProbeResult(
                ok=False,
                code=OutboundError.UI_AUTOMATION_UNAVAILABLE,
                message=f"Windows UI Automation could not be initialized: {exc}",
                client_version=version,
            )

        try:
            windows = list(automation.Desktop(backend="uia").windows())
        except Exception as exc:
            return ProbeResult(
                ok=False,
                code=OutboundError.UI_AUTOMATION_UNAVAILABLE,
                message=f"Windows UI Automation could not enumerate windows: {exc}",
                client_version=version,
                strategy="uia",
            )

        candidates = []
        for window in windows:
            try:
                pid = int(window.element_info.process_id)
            except Exception:
                continue
            if pid not in pids:
                continue
            if not _safe_bool_call(window, "is_visible"):
                continue
            if not _safe_bool_call(window, "is_enabled"):
                continue
            candidates.append(window)

        if not candidates:
            return ProbeResult(
                ok=False,
                code=OutboundError.WECHAT_WINDOW_NOT_FOUND,
                message="No visible, enabled top-level WeChat window was found.",
                client_version=version,
                strategy="uia",
            )

        for window in candidates:
            controls = self._descendants(window)
            search = self._semantic_control(controls, _SEARCH_TOKENS)
            chat_input = self._semantic_control(controls, _CHAT_INPUT_TOKENS)
            if search is None or chat_input is None or search is chat_input:
                continue
            self._window = window
            self._search_control = search
            self._chat_input = chat_input
            self._strategy = "uia"
            try:
                title = str(window.window_text() or "")
            except Exception:
                title = ""
            return ProbeResult(
                ok=True,
                code=None,
                message="A verifiable WeChat UI Automation path is available.",
                client_version=version,
                window_title=title,
                strategy="uia",
            )

        return ProbeResult(
            ok=False,
            code=OutboundError.UI_AUTOMATION_UNAVAILABLE,
            message=(
                "WeChat is running, but its accessible UI tree does not expose uniquely "
                "identifiable search and chat-input controls. Refusing coordinate fallback."
            ),
            client_version=version,
            strategy="uia",
        )

    def open_target(self, target) -> None:
        if self._strategy != "uia" or self._window is None or self._search_control is None:
            raise OutboundActionError(
                OutboundError.UI_AUTOMATION_UNAVAILABLE,
                "A successful UI Automation probe is required before opening a target.",
            )

        self._target_verified = False
        display_name = str(target.display_name or "").strip()
        try:
            self._search_control.set_focus()
            self._search_control.set_edit_text(display_name)
        except Exception as exc:
            raise OutboundActionError(
                OutboundError.TARGET_UI_MISMATCH,
                f"Could not search for the resolved target in WeChat: {exc}",
            ) from exc

        if self._settle_seconds:
            self._sleep(self._settle_seconds)
        controls = self._descendants(self._window)
        results = _exact_visible_controls(
            controls,
            display_name,
            _RESULT_CONTROL_TYPES,
        )
        if len(results) != 1:
            raise OutboundActionError(
                OutboundError.TARGET_UI_MISMATCH,
                f"Expected one exact visible WeChat result for {display_name!r}, found {len(results)}.",
            )

        try:
            results[0].click_input()
        except Exception as exc:
            raise OutboundActionError(
                OutboundError.TARGET_UI_MISMATCH,
                f"Could not open the exact WeChat result: {exc}",
            ) from exc

        if self._settle_seconds:
            self._sleep(self._settle_seconds)
        controls = self._descendants(self._window)
        headers = _exact_visible_controls(
            controls,
            display_name,
            _HEADER_CONTROL_TYPES,
        )
        if len(headers) != 1:
            raise OutboundActionError(
                OutboundError.TARGET_UI_MISMATCH,
                f"Opened conversation could not be uniquely verified as {display_name!r}.",
            )

        chat_input = self._semantic_control(controls, _CHAT_INPUT_TOKENS)
        if chat_input is None:
            raise OutboundActionError(
                OutboundError.CHAT_INPUT_NOT_FOUND,
                "The verified conversation does not expose a unique chat input control.",
            )
        self._chat_input = chat_input
        self._target_verified = True

    def send_text_once(self, text: str) -> None:
        if not self._target_verified or self._chat_input is None:
            raise OutboundActionError(
                OutboundError.CHAT_INPUT_NOT_FOUND,
                "A verified chat input is required before sending.",
            )
        try:
            self._chat_input.set_focus()
            self._chat_input.set_edit_text(text)
            self._chat_input.type_keys("{ENTER}")
        except Exception as exc:
            raise OutboundActionError(
                OutboundError.SEND_ACTION_FAILED,
                f"WeChat UI send action failed: {exc}",
            ) from exc
        finally:
            self._target_verified = False
