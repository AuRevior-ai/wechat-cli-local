from __future__ import annotations

from contextlib import closing
from dataclasses import dataclass
from enum import Enum
import os
import sqlite3
import time
import uuid

from ..core.contacts import get_contact_names
from ..core.messages import search_all_messages


class DraftProbeCode(str, Enum):
    SESSION_DB_UNAVAILABLE = "SESSION_DB_UNAVAILABLE"
    DRAFT_INPUT_NOT_OBSERVED = "DRAFT_INPUT_NOT_OBSERVED"
    DRAFT_IDENTITY_AMBIGUOUS = "DRAFT_IDENTITY_AMBIGUOUS"
    DRAFT_CLEANUP_FAILED = "DRAFT_CLEANUP_FAILED"
    CLIPBOARD_UNAVAILABLE = "CLIPBOARD_UNAVAILABLE"
    PROBE_INPUT_FAILED = "PROBE_INPUT_FAILED"
    PROBE_MESSAGE_CREATED = "PROBE_MESSAGE_CREATED"


class DraftProbeError(RuntimeError):
    def __init__(self, code: DraftProbeCode, message: str):
        super().__init__(message)
        self.code = code


@dataclass(frozen=True)
class DraftSnapshot:
    drafts: dict[str, str]


@dataclass(frozen=True)
class DraftIdentityObservation:
    username: str
    observed_ms: int


@dataclass(frozen=True)
class DraftProbeResult:
    ok: bool
    code: DraftProbeCode | None
    message: str
    username: str = ""
    display_name: str = ""
    kind: str = ""
    observed_ms: int | None = None
    cleanup_confirmed: bool = False


_RARE_MARKER_POOL = "§¶¤※‰†‡‽◊♢♧♤"


def generate_marker() -> str:
    return f"[[wechat-cli-draft-probe:{uuid.uuid4().hex}]]"


def select_marker(
    baseline: DraftSnapshot,
    *,
    pool: str = _RARE_MARKER_POOL,
    length: int = 4,
) -> str:
    combined = "".join(baseline.drafts.values())
    selected = []
    for ch in str(pool or ""):
        if ch in combined or ch in selected:
            continue
        selected.append(ch)
        if len(selected) == int(length):
            return "".join(selected)
    raise DraftProbeError(
        DraftProbeCode.PROBE_INPUT_FAILED,
        "A safe baseline-absent draft challenge could not be selected.",
    )


class SessionDraftSource:
    def __init__(self, app):
        self.app = app

    def snapshot(self) -> DraftSnapshot:
        path = self.app.cache.get(os.path.join("session", "session.db"))
        if not path:
            raise DraftProbeError(
                DraftProbeCode.SESSION_DB_UNAVAILABLE,
                "Session database is unavailable.",
            )
        try:
            with closing(sqlite3.connect(path)) as conn:
                rows = conn.execute(
                    "SELECT username, draft FROM SessionTable"
                ).fetchall()
        except sqlite3.Error as exc:
            raise DraftProbeError(
                DraftProbeCode.SESSION_DB_UNAVAILABLE,
                "Session database could not be read.",
            ) from exc

        values: dict[str, str] = {}
        for username, value in rows:
            key = str(username or "")
            if not key:
                continue
            if value is None:
                text = ""
            elif isinstance(value, bytes):
                text = value.decode("utf-8", errors="replace")
            else:
                text = str(value)
            values[key] = text
        return DraftSnapshot(values)


def locate_marker(
    baseline: DraftSnapshot,
    current: DraftSnapshot,
    marker: str,
) -> list[str]:
    if not marker:
        return []
    matches = []
    for username, draft in current.drafts.items():
        if marker not in draft:
            continue
        if marker in baseline.drafts.get(username, ""):
            continue
        matches.append(username)
    return sorted(matches)


def cleanup_restored(
    baseline: DraftSnapshot,
    current: DraftSnapshot,
    username: str,
    marker: str,
) -> bool:
    if current.drafts.get(username, "") != baseline.drafts.get(username, ""):
        return False
    if marker and any(marker in draft for draft in current.drafts.values()):
        return False
    return True


def wait_for_identity(
    source,
    baseline: DraftSnapshot,
    marker: str,
    *,
    timeout_seconds: float,
    poll_seconds: float,
    clock=time.monotonic,
    sleep=time.sleep,
) -> DraftIdentityObservation:
    started = float(clock())
    deadline = started + max(0.0, float(timeout_seconds))
    interval = max(0.01, float(poll_seconds))
    while True:
        current = source.snapshot()
        now = float(clock())
        matches = locate_marker(baseline, current, marker)
        if len(matches) > 1:
            raise DraftProbeError(
                DraftProbeCode.DRAFT_IDENTITY_AMBIGUOUS,
                "The temporary marker appeared in more than one session.",
            )
        if len(matches) == 1:
            return DraftIdentityObservation(
                username=matches[0],
                observed_ms=max(0, int(round((now - started) * 1000))),
            )
        if now >= deadline:
            raise DraftProbeError(
                DraftProbeCode.DRAFT_INPUT_NOT_OBSERVED,
                "The temporary marker was not observed in the session database.",
            )
        sleep(min(interval, max(0.0, deadline - now)))


def wait_for_cleanup(
    source,
    baseline: DraftSnapshot,
    username: str,
    marker: str,
    *,
    timeout_seconds: float,
    poll_seconds: float,
    clock=time.monotonic,
    sleep=time.sleep,
) -> bool:
    deadline = float(clock()) + max(0.0, float(timeout_seconds))
    interval = max(0.01, float(poll_seconds))
    while True:
        current = source.snapshot()
        if cleanup_restored(baseline, current, username, marker):
            return True
        now = float(clock())
        if now >= deadline:
            return False
        sleep(min(interval, max(0.0, deadline - now)))


class MessageMarkerGuard:
    def __init__(self, app):
        self.app = app

    def marker_message_exists(
        self,
        marker: str,
        *,
        not_before: int,
        not_after: int,
    ) -> bool:
        names = get_contact_names(self.app.cache, self.app.decrypted_dir)
        entries, _failures = search_all_messages(
            self.app.msg_db_keys,
            self.app.cache,
            names,
            marker,
            self.app.display_name_fn,
            start_ts=not_before,
            end_ts=not_after,
            candidate_limit=5,
        )
        return bool(entries)


class DraftIdentityProbeService:
    def __init__(
        self,
        source,
        driver,
        message_guard,
        *,
        display_names=None,
        marker_factory=select_marker,
        wall_clock=time.time,
        clock=time.monotonic,
        sleep=time.sleep,
        observation_timeout_seconds: float = 15.0,
        cleanup_timeout_seconds: float = 15.0,
        poll_seconds: float = 0.1,
    ):
        self.source = source
        self.driver = driver
        self.message_guard = message_guard
        self.display_names = dict(display_names or {})
        self.marker_factory = marker_factory
        self.wall_clock = wall_clock
        self.clock = clock
        self.sleep = sleep
        self.observation_timeout_seconds = float(observation_timeout_seconds)
        self.cleanup_timeout_seconds = float(cleanup_timeout_seconds)
        self.poll_seconds = float(poll_seconds)

    def _failure(
        self,
        code: DraftProbeCode,
        message: str,
        *,
        observation: DraftIdentityObservation | None = None,
        cleanup_confirmed: bool = False,
    ) -> DraftProbeResult:
        username = observation.username if observation is not None else ""
        return DraftProbeResult(
            ok=False,
            code=code,
            message=message,
            username=username,
            display_name=self.display_names.get(username, username) if username else "",
            kind=("group" if "@chatroom" in username else "private") if username else "",
            observed_ms=observation.observed_ms if observation is not None else None,
            cleanup_confirmed=cleanup_confirmed,
        )

    def run(self) -> DraftProbeResult:
        try:
            baseline = self.source.snapshot()
        except DraftProbeError as exc:
            return self._failure(exc.code, str(exc))
        except Exception:
            return self._failure(
                DraftProbeCode.SESSION_DB_UNAVAILABLE,
                "Session database could not be read.",
            )

        try:
            marker = str(self.marker_factory(baseline))
        except DraftProbeError as exc:
            return self._failure(exc.code, str(exc))
        except Exception:
            return self._failure(
                DraftProbeCode.PROBE_INPUT_FAILED,
                "A safe draft challenge could not be selected.",
            )

        started_wall = int(self.wall_clock())
        observation = None
        primary_error = None
        cleanup_ok = False
        cleanup_failed = False

        try:
            self.driver.insert_challenge(marker)
            observation = wait_for_identity(
                self.source,
                baseline,
                marker,
                timeout_seconds=self.observation_timeout_seconds,
                poll_seconds=self.poll_seconds,
                clock=self.clock,
                sleep=self.sleep,
            )
        except DraftProbeError as exc:
            primary_error = exc
        except Exception:
            primary_error = DraftProbeError(
                DraftProbeCode.PROBE_INPUT_FAILED,
                "The no-send draft challenge could not be completed.",
            )
        finally:
            if int(getattr(self.driver, "inserted_char_count", 0) or 0) > 0:
                try:
                    self.driver.remove_challenge()
                except Exception:
                    cleanup_failed = True

            try:
                if observation is not None:
                    cleanup_ok = wait_for_cleanup(
                        self.source,
                        baseline,
                        observation.username,
                        marker,
                        timeout_seconds=self.cleanup_timeout_seconds,
                        poll_seconds=self.poll_seconds,
                        clock=self.clock,
                        sleep=self.sleep,
                    )
                else:
                    deadline = float(self.clock()) + max(
                        0.0,
                        self.cleanup_timeout_seconds,
                    )
                    interval = max(0.01, self.poll_seconds)
                    while True:
                        current = self.source.snapshot()
                        if current.drafts == baseline.drafts:
                            cleanup_ok = True
                            break
                        now = float(self.clock())
                        if now >= deadline:
                            cleanup_ok = False
                            break
                        self.sleep(min(interval, max(0.0, deadline - now)))
            except Exception:
                cleanup_ok = False
            if not cleanup_ok:
                cleanup_failed = True

        message_created = False
        guard_error = None
        try:
            message_created = self.message_guard.marker_message_exists(
                marker,
                not_before=max(0, started_wall - 2),
                not_after=int(self.wall_clock()) + 2,
            )
        except Exception:
            guard_error = DraftProbeError(
                DraftProbeCode.PROBE_INPUT_FAILED,
                "The message safety check could not be completed.",
            )

        if message_created:
            return self._failure(
                DraftProbeCode.PROBE_MESSAGE_CREATED,
                "The temporary challenge appeared in message history; stop outbound automation.",
                observation=observation,
                cleanup_confirmed=cleanup_ok,
            )
        if cleanup_failed:
            return self._failure(
                DraftProbeCode.DRAFT_CLEANUP_FAILED,
                "The temporary challenge was not proven to be fully removed from drafts.",
                observation=observation,
                cleanup_confirmed=False,
            )
        if guard_error is not None:
            return self._failure(
                guard_error.code,
                str(guard_error),
                observation=observation,
                cleanup_confirmed=cleanup_ok,
            )
        if primary_error is not None:
            return self._failure(
                primary_error.code,
                str(primary_error),
                observation=observation,
                cleanup_confirmed=cleanup_ok,
            )
        if observation is None:
            return self._failure(
                DraftProbeCode.DRAFT_INPUT_NOT_OBSERVED,
                "The temporary marker was not observed in the session database.",
                cleanup_confirmed=cleanup_ok,
            )

        username = observation.username
        return DraftProbeResult(
            ok=True,
            code=None,
            message="Current WeChat compose draft was bound to one stable conversation and restored.",
            username=username,
            display_name=self.display_names.get(username, username),
            kind="group" if "@chatroom" in username else "private",
            observed_ms=observation.observed_ms,
            cleanup_confirmed=True,
        )
