"""Normalize local WeChat rows into durable Launch Ops events."""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from typing import Any

from ..core.invite_stats import normalize_notice_text, parse_invite_notice
from ..core.messages import _message_kind, _parse_message_content, decompress_content
from .models import StandardEvent
from .source import MessageSource, RawMessageRow

_TEXT_CONTENT_LIMIT = 20_000


@dataclass(frozen=True)
class NormalizedMessage:
    event: StandardEvent
    message_id: str
    message_type: str
    sender_id: str
    text_content: str
    payload: dict[str, Any]


def _source_identity(source: MessageSource, row: RawMessageRow) -> str:
    if row.server_id:
        return f"server:{row.server_id}"
    raw = (
        f"{source.source_shard}|{source.table_name}|{row.local_id}|"
        f"{row.create_time}|{row.local_type}"
    )
    return "fallback:" + hashlib.sha256(raw.encode("utf-8")).hexdigest()


def _decoded_content(row: RawMessageRow) -> str:
    value = decompress_content(row.message_content, row.content_compression)
    if value is None:
        return ""
    if not isinstance(value, str):
        value = str(value)
    return value


def normalize_message(
    source: MessageSource,
    row: RawMessageRow,
) -> NormalizedMessage:
    source_identity = _source_identity(source, row)
    event_id = f"msg:{source_identity}"
    message_type = _message_kind(row.local_type)
    decoded = _decoded_content(row)
    sender_from_content, text = _parse_message_content(
        decoded,
        row.local_type,
        True,
    )
    text_content = str(text or "")[:_TEXT_CONTENT_LIMIT]
    sender_id = str(sender_from_content or "")
    if not sender_id and row.real_sender_id:
        sender_id = f"local-id:{row.real_sender_id}"
    payload = {
        "local_id": row.local_id,
        "server_id": row.server_id,
        "local_type": row.local_type,
        "message_type": message_type,
    }
    event = StandardEvent(
        event_id=event_id,
        event_type="message.created",
        room_id=source.room_id,
        event_time=row.create_time,
        source_shard=source.source_shard,
        source_table=source.table_name,
        source_identity=source_identity,
        actor_id=sender_id,
        subject_id="",
        payload=payload,
    )
    return NormalizedMessage(
        event=event,
        message_id=event_id,
        message_type=message_type,
        sender_id=sender_id,
        text_content=text_content,
        payload=payload,
    )


def normalize_invite_events(
    source: MessageSource,
    row: RawMessageRow,
    parent: NormalizedMessage,
) -> list[StandardEvent]:
    if parent.message_type != "system":
        return []
    notice = normalize_notice_text(parent.text_content)
    relations = parse_invite_notice(notice)
    if not relations:
        return []

    events: list[StandardEvent] = []
    for relation_index, relation in enumerate(relations):
        method = relation.get("method", "")
        inviter_username = str(relation.get("inviter_username") or "")
        invitee_username = str(relation.get("invitee_username") or "")
        if method == "unattributed_qr":
            inviter_status = "unattributed"
            inviter_username = ""
        elif inviter_username:
            inviter_status = "resolved"
        else:
            inviter_status = "unresolved"
        invitee_status = "resolved" if invitee_username else "unresolved"
        payload = {
            "method": method,
            "inviter_name_raw": str(relation.get("inviter_name_raw") or ""),
            "invitee_name_raw": str(relation.get("invitee_name_raw") or ""),
            "inviter_username": inviter_username,
            "invitee_username": invitee_username,
            "inviter_identity_status": inviter_status,
            "invitee_identity_status": invitee_status,
            "inviter_is_self": bool(relation.get("inviter_is_self")),
            "invitee_is_self": bool(relation.get("invitee_is_self")),
            "raw_text": notice,
            "parent_event_id": parent.event.event_id,
        }
        invite_source_identity = (
            f"invite:{parent.event.source_identity}:{relation_index}"
        )
        events.append(
            StandardEvent(
                event_id=invite_source_identity,
                event_type="member.invited",
                room_id=source.room_id,
                event_time=row.create_time,
                source_shard=source.source_shard,
                source_table=source.table_name,
                source_identity=invite_source_identity,
                actor_id=inviter_username,
                subject_id=invitee_username,
                payload=payload,
            )
        )
        join_payload = dict(payload)
        join_payload["provenance"] = "invite_system_notice"
        join_source_identity = (
            f"join:{parent.event.source_identity}:{relation_index}"
        )
        events.append(
            StandardEvent(
                event_id=join_source_identity,
                event_type="member.joined",
                room_id=source.room_id,
                event_time=row.create_time,
                source_shard=source.source_shard,
                source_table=source.table_name,
                source_identity=join_source_identity,
                actor_id=inviter_username,
                subject_id=invitee_username,
                payload=join_payload,
            )
        )
    return events
