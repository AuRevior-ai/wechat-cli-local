from __future__ import annotations

from collections.abc import Iterable, Mapping
import time

from ..core.contacts import get_contact_names
from ..core.messages import collect_chat_history_items, resolve_chat_context


def normalize_text(value: str) -> str:
    return str(value or "").replace("\r\n", "\n").replace("\r", "\n")


def verify_history_items(
    items: Iterable[Mapping[str, object]],
    *,
    username: str,
    text: str,
    not_before: int,
    not_after: int,
):
    expected = normalize_text(text)
    matches = []
    for item in items:
        try:
            timestamp = int(item.get("timestamp", 0))
        except (TypeError, ValueError):
            continue
        if item.get("chat_username") != username:
            continue
        if item.get("is_self") is not True:
            continue
        if item.get("type") != "text":
            continue
        if not (not_before <= timestamp <= not_after):
            continue
        if normalize_text(str(item.get("text", ""))) != expected:
            continue
        matches.append((timestamp, item))
    if not matches:
        return None
    matches.sort(key=lambda pair: pair[0], reverse=True)
    return matches[0][1]


class ReadbackVerifier:
    def __init__(self, app, *, clock=time.time, sleep=time.sleep):
        self.app = app
        self.clock = clock
        self.sleep = sleep

    def find_match(
        self,
        target,
        text: str,
        *,
        not_before: int,
        not_after: int,
    ):
        chat_ctx = resolve_chat_context(
            target.username,
            self.app.msg_db_keys,
            self.app.cache,
            self.app.decrypted_dir,
        )
        if not chat_ctx:
            return None
        names = get_contact_names(self.app.cache, self.app.decrypted_dir)
        items, _failures = collect_chat_history_items(
            chat_ctx,
            names,
            self.app.display_name_fn,
            start_ts=not_before,
            end_ts=not_after,
            limit=50,
            offset=0,
            db_dir=self.app.db_dir,
        )
        return verify_history_items(
            items,
            username=target.username,
            text=text,
            not_before=not_before,
            not_after=not_after,
        )

    def wait_for_match(
        self,
        target,
        text: str,
        *,
        not_before: int,
        timeout_seconds: float,
        poll_seconds: float,
    ):
        deadline = self.clock() + max(0.0, float(timeout_seconds))
        while True:
            now = self.clock()
            matched = self.find_match(
                target,
                text,
                not_before=not_before,
                not_after=int(now),
            )
            if matched is not None:
                return matched
            if now >= deadline:
                return None
            remaining = deadline - now
            self.sleep(min(max(0.0, float(poll_seconds)), remaining))
