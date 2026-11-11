"""Read-only adapters from local WeChat databases into Launch Ops."""

from __future__ import annotations

import hashlib
import json
import os
import sqlite3
from dataclasses import dataclass

from ..core.chatroom_metadata import (
    parse_chatroom_ext_buffer,
    parse_chatroom_ext_buffer_with_status,
)
from ..core.contacts import get_self_username
from ..core.messages import _is_safe_msg_table_name, resolve_chat_context


def _avatar_name_key(value: str) -> str:
    """Ignore decorative glyphs while keeping the full alphanumeric name body exact."""
    return "".join(
        char.casefold()
        for char in str(value or "").strip()
        if char.isalnum()
    )


def _canonical_members(members: tuple[dict, ...] | list[dict]) -> str:
    normalized = [dict(item) for item in members]
    normalized.sort(key=lambda item: str(item.get("username") or ""))
    return json.dumps(
        normalized,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )


def _membership_semantic_hash(
    members: tuple[dict, ...] | list[dict],
    membership_status: str,
    inviter_metadata_status: str,
) -> str:
    payload = "\0".join(
        (
            _canonical_members(members),
            str(membership_status),
            str(inviter_metadata_status),
        )
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _contact_source_state_key(stamp: SourceStamp | None) -> str:
    if stamp is None:
        return "contact-unavailable"
    payload = "\0".join(
        str(value)
        for value in (
            stamp.db_mtime_ns,
            stamp.wal_mtime_ns,
            stamp.db_size,
            stamp.wal_size,
        )
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


@dataclass(frozen=True)
class GroupSource:
    room_id: str
    display_name: str
    avatar_url: str = ""


@dataclass(frozen=True)
class MessageSource:
    room_id: str
    source_shard: str
    db_path: str
    table_name: str


@dataclass(frozen=True)
class SourceStamp:
    db_mtime_ns: int
    wal_mtime_ns: int
    db_size: int
    wal_size: int


@dataclass(frozen=True)
class MembershipSourceRead:
    members: tuple[dict, ...]
    membership_status: str
    inviter_metadata_status: str
    diagnostic_code: str
    source_stamp: SourceStamp | None


@dataclass(frozen=True)
class MembershipObservation:
    room_id: str
    observed_at: int
    source_state_key: str
    semantic_hash: str
    membership_status: str
    inviter_metadata_status: str
    members: tuple[dict, ...]
    diagnostic_code: str = ""


@dataclass(frozen=True)
class TableHead:
    local_id: int
    create_time: int


@dataclass(frozen=True)
class RawMessageRow:
    local_id: int
    server_id: int
    local_type: int
    create_time: int
    real_sender_id: int
    message_content: object
    content_compression: int | None


class LocalWeChatSourceAdapter:
    """Bound read-only source operations for one AppContext."""

    def __init__(self, ctx):
        self.ctx = ctx
        self._source_cache: dict[str, list[MessageSource]] = {}
        self._source_cache_loaded = False
        self._members_cache: dict[str, list[dict]] = {}
        self._groups_cache: list[GroupSource] = []
        self._contact_stamp: SourceStamp | None = None
        self._contact_loaded = False
        self._avatar_by_username: dict[str, str] = {}
        self._avatar_by_name: dict[str, str] = {}
        self._avatar_by_compact_name: dict[str, str] = {}
        self._avatar_by_room_name: dict[tuple[str, str], str] = {}
        self._avatar_by_room_compact_name: dict[tuple[str, str], str] = {}
        self._all_contact_avatar_loaded = False
        self._all_avatar_by_username: dict[str, str] = {}
        self._all_avatar_by_name: dict[str, str] = {}
        self._all_avatar_by_compact_name: dict[str, str] = {}

    def discover_groups(self) -> list[GroupSource]:
        refresh = getattr(self.ctx, "refresh_message_shards", None)
        shards_changed = bool(callable(refresh) and refresh())
        if shards_changed:
            self._source_cache.clear()
            self._source_cache_loaded = False

        stamp = contact_stamp(self.ctx)
        contact_changed = not self._contact_loaded or stamp != self._contact_stamp
        if contact_changed:
            groups = discover_groups(self.ctx)
            self._members_cache = discover_group_members(self.ctx)
            self._groups_cache = list(groups)
            self._rebuild_avatar_index()
            self._all_contact_avatar_loaded = False
            self._all_avatar_by_username = {}
            self._all_avatar_by_name = {}
            self._all_avatar_by_compact_name = {}
            self._contact_stamp = stamp
            self._contact_loaded = True
            self._source_cache_loaded = False
        else:
            groups = list(self._groups_cache)

        if not self._source_cache_loaded:
            self._source_cache = discover_message_sources(self.ctx, groups)
            self._source_cache_loaded = True
        return list(groups)

    def resolve_sources(self, group: GroupSource) -> list[MessageSource]:
        if self._source_cache_loaded or group.room_id in self._source_cache:
            return list(self._source_cache.get(group.room_id, []))
        resolved = resolve_message_sources(self.ctx, group)
        self._source_cache[group.room_id] = list(resolved)
        return resolved

    def stamp(self, source: MessageSource) -> SourceStamp:
        return source_stamp(self.ctx.db_dir, source.source_shard)

    def refresh(self, source: MessageSource) -> MessageSource:
        path = self.ctx.cache.get(source.source_shard)
        if not path:
            raise FileNotFoundError(source.source_shard)
        return MessageSource(
            room_id=source.room_id,
            source_shard=source.source_shard,
            db_path=str(path),
            table_name=source.table_name,
        )

    def table_heads(
        self,
        sources: list[MessageSource],
    ) -> dict[tuple[str, str], TableHead]:
        by_shard: dict[str, list[MessageSource]] = {}
        for source in sources:
            by_shard.setdefault(source.source_shard, []).append(source)

        result: dict[tuple[str, str], TableHead] = {}
        for shard, shard_sources in by_shard.items():
            path = self.ctx.cache.get(shard)
            if not path:
                raise FileNotFoundError(shard)
            conn = sqlite3.connect(path)
            try:
                for source in shard_sources:
                    if not _is_safe_msg_table_name(source.table_name):
                        raise ValueError(f"非法消息表名: {source.table_name}")
                    row = conn.execute(
                        f"SELECT local_id, create_time FROM [{source.table_name}] "
                        "ORDER BY local_id DESC LIMIT 1"
                    ).fetchone()
                    result[(shard, source.table_name)] = TableHead(
                        local_id=int(row[0] or 0) if row else 0,
                        create_time=int(row[1] or 0) if row else 0,
                    )
            finally:
                conn.close()
        return result

    def read_rows(
        self,
        source: MessageSource,
        *,
        start_time: int,
        start_local_id: int,
        overlap_seconds: int,
    ) -> list[RawMessageRow]:
        return read_incremental_rows(
            source,
            start_time=start_time,
            start_local_id=start_local_id,
            overlap_seconds=overlap_seconds,
        )

    def read_rows_between(
        self,
        source: MessageSource,
        *,
        start_time: int,
        end_time: int,
    ) -> list[RawMessageRow]:
        return read_rows_between(
            source,
            start_time=start_time,
            end_time=end_time,
        )

    def _read_membership_source(
        self, group: GroupSource, *, force: bool
    ) -> MembershipSourceRead:
        if force:
            self._members_cache.pop(group.room_id, None)
            self._contact_loaded = False
            self._contact_stamp = None
            self._all_contact_avatar_loaded = False

        stamp = contact_stamp(self.ctx)
        path = _contact_db_path(self.ctx)
        if not path:
            return MembershipSourceRead(
                members=(),
                membership_status="failed",
                inviter_metadata_status="unavailable",
                diagnostic_code="contact_read_failed",
                source_stamp=stamp,
            )

        conn = sqlite3.connect(path)
        try:
            room_row = conn.execute(
                "SELECT id FROM contact WHERE username = ? LIMIT 1",
                (group.room_id,),
            ).fetchone()
            if room_row is None:
                return MembershipSourceRead(
                    members=(),
                    membership_status="failed",
                    inviter_metadata_status="unavailable",
                    diagnostic_code="contact_room_missing",
                    source_stamp=stamp,
                )
            room_db_id = int(room_row[0])
            rows = conn.execute(
                """
                SELECT member.username, member.nick_name, member.remark,
                       member.small_head_url, member.big_head_url
                FROM chatroom_member AS cm
                JOIN contact AS member ON member.id = cm.member_id
                WHERE cm.room_id = ?
                ORDER BY member.username ASC
                """,
                (room_db_id,),
            ).fetchall()
        except sqlite3.Error:
            return MembershipSourceRead(
                members=(),
                membership_status="failed",
                inviter_metadata_status="unavailable",
                diagnostic_code="contact_read_failed",
                source_stamp=stamp,
            )

        metadata_result = None
        try:
            metadata_row = conn.execute(
                "SELECT ext_buffer FROM chat_room WHERE id = ?",
                (room_db_id,),
            ).fetchone()
            if metadata_row is not None:
                metadata_result = parse_chatroom_ext_buffer_with_status(metadata_row[0])
        except sqlite3.Error:
            metadata_result = None
        finally:
            conn.close()

        if metadata_result is None:
            metadata_by_username: dict[str, dict] = {}
            inviter_status = "unavailable"
            diagnostic_code = "ext_buffer_unavailable"
        else:
            metadata_by_username = metadata_result.members
            inviter_status = metadata_result.status
            diagnostic_code = (
                ""
                if inviter_status == "complete"
                else f"ext_buffer_{inviter_status}"
            )

        members: list[dict] = []
        missing_metadata = False
        for username, nick_name, remark, small_head_url, big_head_url in rows:
            member_id = str(username or "").strip()
            if not member_id:
                continue
            nick = str(nick_name or "")
            member_remark = str(remark or "")
            item = {
                "username": member_id,
                "nick_name": nick,
                "remark": member_remark,
                "display_name": member_remark or nick or member_id,
                "avatar_url": str(small_head_url or big_head_url or "").strip(),
            }
            metadata = metadata_by_username.get(member_id)
            if metadata is None:
                if inviter_status == "complete":
                    missing_metadata = True
                item["inviter_username"] = ""
            else:
                inviter_username = str(metadata.get("inviter_username") or "").strip()
                item.update(
                    {
                        "inviter_username": inviter_username,
                        "inviter_source": (
                            "chat_room.ext_buffer" if inviter_username else ""
                        ),
                        "member_flag": metadata.get("member_flag"),
                        "room_display_name": str(
                            metadata.get("room_display_name") or ""
                        ).strip(),
                    }
                )
            members.append(item)

        if missing_metadata:
            inviter_status = "partial"
            diagnostic_code = "ext_buffer_partial"

        self._members_cache[group.room_id] = list(members)
        self._contact_stamp = stamp
        self._contact_loaded = True
        return MembershipSourceRead(
            members=tuple(members),
            membership_status="complete",
            inviter_metadata_status=inviter_status,
            diagnostic_code=diagnostic_code,
            source_stamp=stamp,
        )

    def membership_observation(
        self,
        group: GroupSource,
        *,
        observed_at: int,
        force: bool = False,
    ) -> MembershipObservation:
        raw = self._read_membership_source(group, force=force)
        semantic_hash = _membership_semantic_hash(
            raw.members,
            raw.membership_status,
            raw.inviter_metadata_status,
        )
        return MembershipObservation(
            room_id=group.room_id,
            observed_at=int(observed_at),
            source_state_key=_contact_source_state_key(raw.source_stamp),
            semantic_hash=semantic_hash,
            membership_status=raw.membership_status,
            inviter_metadata_status=raw.inviter_metadata_status,
            members=raw.members,
            diagnostic_code=raw.diagnostic_code,
        )

    def members(self, group: GroupSource) -> list[dict]:
        return list(self._members_cache.get(group.room_id, []))

    def _rebuild_avatar_index(self) -> None:
        by_username: dict[str, str] = {}
        name_candidates: dict[str, set[str]] = {}
        compact_name_candidates: dict[str, set[str]] = {}
        room_name_candidates: dict[tuple[str, str], set[str]] = {}
        room_compact_name_candidates: dict[tuple[str, str], set[str]] = {}

        def remember(username: str, names: list[str], avatar_url: str, room_id: str = "") -> None:
            avatar = str(avatar_url or "").strip()
            if not avatar:
                return
            user_key = str(username or "").strip()
            if user_key:
                by_username[user_key] = avatar
            for raw_name in names:
                name = str(raw_name or "").strip().casefold()
                if not name:
                    continue
                name_candidates.setdefault(name, set()).add(avatar)
                compact_name = _avatar_name_key(raw_name)
                if compact_name:
                    compact_name_candidates.setdefault(compact_name, set()).add(avatar)
                if room_id:
                    room_name_candidates.setdefault((room_id, name), set()).add(avatar)
                    if compact_name:
                        room_compact_name_candidates.setdefault(
                            (room_id, compact_name), set()
                        ).add(avatar)

        for group in self._groups_cache:
            remember(group.room_id, [group.display_name], group.avatar_url)
        for room_id, members in self._members_cache.items():
            for member in members:
                remember(
                    str(member.get("username") or ""),
                    [
                        str(member.get("display_name") or ""),
                        str(member.get("remark") or ""),
                        str(member.get("nick_name") or ""),
                    ],
                    str(member.get("avatar_url") or ""),
                    room_id=room_id,
                )
        self._avatar_by_username = by_username
        self._avatar_by_name = {
            name: next(iter(urls))
            for name, urls in name_candidates.items()
            if len(urls) == 1
        }
        self._avatar_by_compact_name = {
            name: next(iter(urls))
            for name, urls in compact_name_candidates.items()
            if len(urls) == 1
        }
        self._avatar_by_room_name = {
            key: next(iter(urls))
            for key, urls in room_name_candidates.items()
            if len(urls) == 1
        }
        self._avatar_by_room_compact_name = {
            key: next(iter(urls))
            for key, urls in room_compact_name_candidates.items()
            if len(urls) == 1
        }

    def _load_all_contact_avatar_index(self) -> None:
        if self._all_contact_avatar_loaded:
            return
        by_username: dict[str, str] = {}
        name_candidates: dict[str, set[str]] = {}
        compact_name_candidates: dict[str, set[str]] = {}
        try:
            path = _contact_db_path(self.ctx)
        except (AttributeError, OSError):
            path = None
        if path:
            conn = sqlite3.connect(path)
            try:
                rows = conn.execute(
                    "SELECT username, nick_name, remark, small_head_url, big_head_url FROM contact"
                ).fetchall()
            except sqlite3.Error:
                rows = []
            finally:
                conn.close()
            for username, nick_name, remark, small_head_url, big_head_url in rows:
                avatar = str(small_head_url or big_head_url or "").strip()
                if not avatar:
                    continue
                user_key = str(username or "").strip()
                if user_key:
                    by_username[user_key] = avatar
                for raw_name in (remark, nick_name):
                    name = str(raw_name or "").strip().casefold()
                    if not name:
                        continue
                    name_candidates.setdefault(name, set()).add(avatar)
                    compact_name = _avatar_name_key(raw_name)
                    if compact_name:
                        compact_name_candidates.setdefault(compact_name, set()).add(avatar)
        self._all_avatar_by_username = by_username
        self._all_avatar_by_name = {
            name: next(iter(urls))
            for name, urls in name_candidates.items()
            if len(urls) == 1
        }
        self._all_avatar_by_compact_name = {
            name: next(iter(urls))
            for name, urls in compact_name_candidates.items()
            if len(urls) == 1
        }
        try:
            self_username = get_self_username(
                getattr(self.ctx, "db_dir", ""),
                getattr(self.ctx, "cache", None),
                getattr(self.ctx, "decrypted_dir", ""),
            )
        except Exception:
            self_username = ""
        self_avatar = by_username.get(str(self_username or "").strip(), "")
        if self_avatar:
            self._all_avatar_by_name["你"] = self_avatar
            self._all_avatar_by_name["我"] = self_avatar
        self._all_contact_avatar_loaded = True

    def avatar_url(
        self,
        username: str = "",
        display_name: str = "",
        *,
        room_id: str = "",
    ) -> str:
        if not self._contact_loaded:
            self.discover_groups()
        user_key = str(username or "").strip()
        if user_key and user_key in self._avatar_by_username:
            return self._avatar_by_username[user_key]
        name_key = str(display_name or "").strip().casefold()
        compact_name_key = _avatar_name_key(display_name)
        room_key = str(room_id or "").strip()
        if room_key and name_key:
            room_avatar = self._avatar_by_room_name.get((room_key, name_key), "")
            if room_avatar:
                return room_avatar
        if room_key and compact_name_key:
            room_avatar = self._avatar_by_room_compact_name.get(
                (room_key, compact_name_key), ""
            )
            if room_avatar:
                return room_avatar
        if name_key and name_key in self._avatar_by_name:
            return self._avatar_by_name[name_key]
        if compact_name_key and compact_name_key in self._avatar_by_compact_name:
            return self._avatar_by_compact_name[compact_name_key]
        self._load_all_contact_avatar_index()
        if user_key and user_key in self._all_avatar_by_username:
            return self._all_avatar_by_username[user_key]
        if name_key and name_key in self._all_avatar_by_name:
            return self._all_avatar_by_name[name_key]
        if compact_name_key:
            return self._all_avatar_by_compact_name.get(compact_name_key, "")
        return ""


def _contact_db_path(ctx) -> str | None:
    pre_decrypted = os.path.join(ctx.decrypted_dir, "contact", "contact.db")
    if os.path.exists(pre_decrypted):
        return pre_decrypted
    return ctx.cache.get(os.path.join("contact", "contact.db"))


def contact_stamp(ctx) -> SourceStamp | None:
    raw_path = os.path.join(ctx.db_dir, "contact", "contact.db")
    if os.path.exists(raw_path):
        return source_stamp(ctx.db_dir, "contact/contact.db")
    path = _contact_db_path(ctx)
    if not path or not os.path.exists(path):
        return None
    stat = os.stat(path)
    return SourceStamp(
        db_mtime_ns=int(stat.st_mtime_ns),
        wal_mtime_ns=0,
        db_size=int(stat.st_size),
        wal_size=0,
    )


def discover_groups(ctx) -> list[GroupSource]:
    """Return actual chatroom contacts from the configured local WeChat account."""
    path = _contact_db_path(ctx)
    if not path:
        return []
    conn = sqlite3.connect(path)
    try:
        rows = conn.execute(
            """
            SELECT username, nick_name, remark, small_head_url, big_head_url
            FROM contact
            WHERE username LIKE '%@chatroom%'
            """
        ).fetchall()
    finally:
        conn.close()
    groups = []
    for username, nick_name, remark, small_head_url, big_head_url in rows:
        room_id = str(username or "").strip()
        if not room_id or "@chatroom" not in room_id:
            continue
        display_name = str(remark or nick_name or room_id).strip() or room_id
        groups.append(
            GroupSource(
                room_id=room_id,
                display_name=display_name,
                avatar_url=str(small_head_url or big_head_url or "").strip(),
            )
        )
    groups.sort(key=lambda item: (item.display_name.casefold(), item.room_id))
    return groups


def discover_group_members(ctx) -> dict[str, list[dict]]:
    """Bulk-load chatroom members from contact.db for one watcher scan."""
    path = _contact_db_path(ctx)
    if not path:
        return {}
    conn = sqlite3.connect(path)
    try:
        rows = conn.execute(
            """
            SELECT room.id, room.username, member.username, member.nick_name, member.remark,
                   member.small_head_url, member.big_head_url
            FROM chatroom_member AS cm
            JOIN contact AS room ON room.id = cm.room_id
            JOIN contact AS member ON member.id = cm.member_id
            WHERE room.username LIKE '%@chatroom%'
            ORDER BY room.username ASC, member.username ASC
            """
        ).fetchall()
        metadata_by_room_id: dict[int, dict[str, dict]] = {}
        try:
            for room_id, ext_buffer in conn.execute(
                "SELECT id, ext_buffer FROM chat_room"
            ).fetchall():
                metadata_by_room_id[int(room_id)] = parse_chatroom_ext_buffer(ext_buffer)
        except sqlite3.Error:
            metadata_by_room_id = {}
    except sqlite3.Error:
        return {}
    finally:
        conn.close()

    result: dict[str, list[dict]] = {}
    for room_db_id, room_username, username, nick_name, remark, small_head_url, big_head_url in rows:
        room_id = str(room_username or "").strip()
        member_id = str(username or "").strip()
        if not room_id or not member_id:
            continue
        nick = str(nick_name or "")
        member_remark = str(remark or "")
        display_name = member_remark or nick or member_id
        item = {
            "username": member_id,
            "nick_name": nick,
            "remark": member_remark,
            "display_name": display_name,
            "avatar_url": str(small_head_url or big_head_url or "").strip(),
        }
        metadata = metadata_by_room_id.get(int(room_db_id), {}).get(member_id)
        if metadata is not None:
            inviter_username = str(metadata.get("inviter_username") or "").strip()
            item.update(
                {
                    "inviter_username": inviter_username,
                    "inviter_source": (
                        "chat_room.ext_buffer" if inviter_username else ""
                    ),
                    "member_flag": metadata.get("member_flag"),
                    "room_display_name": str(
                        metadata.get("room_display_name") or ""
                    ).strip(),
                }
            )
        result.setdefault(room_id, []).append(item)
    return result


def discover_message_sources(
    ctx,
    groups: list[GroupSource],
) -> dict[str, list[MessageSource]]:
    """Bulk-map all discovered chatrooms to message tables shard by shard."""
    mapping: dict[str, list[MessageSource]] = {
        group.room_id: [] for group in groups
    }
    table_to_room = {
        f"Msg_{hashlib.md5(group.room_id.encode()).hexdigest()}": group.room_id
        for group in groups
    }
    if not table_to_room:
        return mapping

    for rel_key in getattr(ctx, "msg_db_keys", []) or []:
        path = ctx.cache.get(rel_key)
        if not path:
            continue
        conn = sqlite3.connect(path)
        try:
            rows = conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name LIKE 'Msg_%'"
            ).fetchall()
        finally:
            conn.close()
        normalized_shard = str(rel_key).replace("\\", "/")
        for (table_name,) in rows:
            table_name = str(table_name or "")
            room_id = table_to_room.get(table_name)
            if not room_id or not _is_safe_msg_table_name(table_name):
                continue
            mapping[room_id].append(
                MessageSource(
                    room_id=room_id,
                    source_shard=normalized_shard,
                    db_path=str(path),
                    table_name=table_name,
                )
            )

    for sources in mapping.values():
        sources.sort(key=lambda item: (item.source_shard, item.table_name))
    return mapping


def resolve_message_sources(ctx, room: GroupSource) -> list[MessageSource]:
    chat_ctx = resolve_chat_context(
        room.room_id,
        ctx.msg_db_keys,
        ctx.cache,
        ctx.decrypted_dir,
    )
    if not chat_ctx:
        return []
    result = []
    for table in chat_ctx.get("message_tables") or []:
        rel_key = table.get("rel_key")
        db_path = table.get("db_path")
        table_name = table.get("table_name")
        if not rel_key or not db_path or not table_name:
            continue
        result.append(
            MessageSource(
                room_id=room.room_id,
                source_shard=str(rel_key).replace("\\", "/"),
                db_path=str(db_path),
                table_name=str(table_name),
            )
        )
    return result


def source_stamp(db_dir: str, source_shard: str) -> SourceStamp:
    rel_path = source_shard.replace("\\", "/").replace("/", os.sep)
    db_path = os.path.join(db_dir, rel_path)
    wal_path = db_path + "-wal"
    db_stat = os.stat(db_path)
    if os.path.exists(wal_path):
        wal_stat = os.stat(wal_path)
        wal_mtime_ns = int(wal_stat.st_mtime_ns)
        wal_size = int(wal_stat.st_size)
    else:
        wal_mtime_ns = 0
        wal_size = 0
    return SourceStamp(
        db_mtime_ns=int(db_stat.st_mtime_ns),
        wal_mtime_ns=wal_mtime_ns,
        db_size=int(db_stat.st_size),
        wal_size=wal_size,
    )


def read_rows_between(
    source: MessageSource,
    *,
    start_time: int,
    end_time: int,
) -> list[RawMessageRow]:
    start = int(start_time)
    end = int(end_time)
    if start < 0 or end < start:
        raise ValueError("invalid bounded message time window")
    if not _is_safe_msg_table_name(source.table_name):
        raise ValueError(f"非法消息表名: {source.table_name}")
    conn = sqlite3.connect(source.db_path)
    try:
        rows = conn.execute(
            f"""
            SELECT local_id, server_id, local_type, create_time, real_sender_id,
                   message_content, WCDB_CT_message_content
            FROM [{source.table_name}]
            WHERE create_time >= ? AND create_time <= ?
            ORDER BY create_time ASC, local_id ASC
            """,
            (start, end),
        ).fetchall()
    finally:
        conn.close()
    return [
        RawMessageRow(
            local_id=int(row[0] or 0),
            server_id=int(row[1] or 0),
            local_type=int(row[2] or 0),
            create_time=int(row[3] or 0),
            real_sender_id=int(row[4] or 0),
            message_content=row[5],
            content_compression=(None if row[6] is None else int(row[6])),
        )
        for row in rows
    ]


def read_incremental_rows(
    source: MessageSource,
    *,
    start_time: int,
    start_local_id: int = 0,
    overlap_seconds: int = 0,
) -> list[RawMessageRow]:
    if not _is_safe_msg_table_name(source.table_name):
        raise ValueError(f"非法消息表名: {source.table_name}")
    threshold = max(0, int(start_time) - max(0, int(overlap_seconds)))
    conn = sqlite3.connect(source.db_path)
    try:
        rows = conn.execute(
            f"""
            SELECT local_id, server_id, local_type, create_time, real_sender_id,
                   message_content, WCDB_CT_message_content
            FROM [{source.table_name}]
            WHERE create_time >= ?
            ORDER BY create_time ASC, local_id ASC
            """,
            (threshold,),
        ).fetchall()
    finally:
        conn.close()

    result = []
    for row in rows:
        raw = RawMessageRow(
            local_id=int(row[0] or 0),
            server_id=int(row[1] or 0),
            local_type=int(row[2] or 0),
            create_time=int(row[3] or 0),
            real_sender_id=int(row[4] or 0),
            message_content=row[5],
            content_compression=(None if row[6] is None else int(row[6])),
        )
        if overlap_seconds <= 0:
            if raw.create_time < start_time:
                continue
            if raw.create_time == start_time and raw.local_id <= start_local_id:
                continue
        result.append(raw)
    return result
