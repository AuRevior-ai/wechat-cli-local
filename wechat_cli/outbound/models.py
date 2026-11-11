from __future__ import annotations

from dataclasses import dataclass
from enum import Enum


class SendStatus(str, Enum):
    CONFIRMED = "confirmed"
    UNCONFIRMED = "unconfirmed"
    BLOCKED = "blocked"
    FAILED = "failed"


class OutboundError(str, Enum):
    WECHAT_NOT_RUNNING = "WECHAT_NOT_RUNNING"
    WECHAT_WINDOW_NOT_FOUND = "WECHAT_WINDOW_NOT_FOUND"
    UI_AUTOMATION_UNAVAILABLE = "UI_AUTOMATION_UNAVAILABLE"
    TARGET_NOT_FOUND = "TARGET_NOT_FOUND"
    TARGET_AMBIGUOUS = "TARGET_AMBIGUOUS"
    TARGET_UI_MISMATCH = "TARGET_UI_MISMATCH"
    CHAT_INPUT_NOT_FOUND = "CHAT_INPUT_NOT_FOUND"
    INVALID_TEXT = "INVALID_TEXT"
    SEND_ACTION_FAILED = "SEND_ACTION_FAILED"
    SEND_UNCONFIRMED = "SEND_UNCONFIRMED"


class TargetResolutionError(ValueError):
    def __init__(self, code: OutboundError, message: str):
        super().__init__(message)
        self.code = code


class OutboundActionError(RuntimeError):
    def __init__(self, code: OutboundError, message: str):
        super().__init__(message)
        self.code = code


@dataclass(frozen=True)
class ResolvedTarget:
    username: str
    display_name: str
    kind: str


@dataclass(frozen=True)
class SendRequest:
    target: ResolvedTarget
    text: str


@dataclass(frozen=True)
class SendResult:
    status: SendStatus
    target: ResolvedTarget | None
    code: OutboundError | None
    message: str
    attempted_at: int | None = None
    confirmed_at: int | None = None


@dataclass(frozen=True)
class ProbeResult:
    ok: bool
    code: OutboundError | None
    message: str
    client_version: str = ""
    window_title: str = ""
    strategy: str = ""
