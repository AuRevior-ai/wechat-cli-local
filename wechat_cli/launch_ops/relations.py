"""Hybrid invitation relations from durable events and current member metadata."""

from __future__ import annotations

from collections import defaultdict
from typing import Any


def _member_name(member: dict[str, Any]) -> str:
    return str(
        member.get("display_name")
        or member.get("remark")
        or member.get("nick_name")
        or member.get("room_display_name")
        or member.get("username")
        or "未知成员"
    ).strip()


def _name_key(value: object) -> str:
    return str(value or "").strip().casefold()


def _event_relation(event: dict[str, Any]) -> dict[str, Any]:
    relation = dict(event)
    relation["payload"] = dict(event.get("payload") or {})
    relation["relation_source"] = "event"
    relation["time_status"] = "known"
    relation["metadata_conflict"] = False
    return relation


def _metadata_relation(
    room_id: str,
    member: dict[str, Any],
    inviter_username: str,
    inviter_name: str,
) -> dict[str, Any]:
    member_username = str(member.get("username") or "").strip()
    invitee_name = _member_name(member)
    event_id = f"metadata:{room_id}:{member_username}"
    return {
        "event_id": event_id,
        "event_type": "member.inviter_observed",
        "room_id": room_id,
        "event_time": None,
        "source_shard": "__contact__",
        "source_table": "chat_room.ext_buffer",
        "source_identity": event_id,
        "actor_id": inviter_username,
        "subject_id": member_username,
        "payload": {
            "method": "metadata",
            "inviter_name_raw": inviter_name,
            "invitee_name_raw": invitee_name,
            "inviter_username": inviter_username,
            "invitee_username": member_username,
            "inviter_identity_status": "resolved",
            "invitee_identity_status": "resolved",
            "relation_source": "chat_room.ext_buffer",
            "time_status": "unknown",
        },
        "protected": False,
        "created_at": None,
        "relation_source": "chat_room.ext_buffer",
        "time_status": "unknown",
        "metadata_conflict": False,
    }


def build_hybrid_invite_relations(
    store,
    room_ids: list[str],
    *,
    include_metadata: bool,
    start_ts: int | None = None,
    end_ts: int | None = None,
    limit: int = 50_000,
) -> list[dict[str, Any]]:
    """Return real invite events plus uncovered current metadata relations.

    Stored `member.invited` events remain canonical. Current membership metadata is
    projected only when it represents a relation not already covered by history.
    Metadata-only relations intentionally have no event timestamp.
    """
    if not room_ids:
        return []

    events = store.list_events(
        room_ids=list(room_ids),
        event_type="member.invited",
        start_ts=start_ts,
        end_ts=end_ts,
        limit=limit,
    )
    relations = [_event_relation(event) for event in events]
    if not include_metadata:
        return relations

    metadata_by_room_subject: dict[tuple[str, str], dict[str, Any]] = {}
    metadata_by_pair: dict[tuple[str, str, str], list[tuple[str, str]]] = defaultdict(list)
    identity_names: dict[tuple[str, str], str] = {}

    for relation in relations:
        room_id = str(relation.get("room_id") or "")
        payload = relation["payload"]
        actor = str(
            relation.get("actor_id")
            or payload.get("inviter_username")
            or ""
        ).strip()
        subject = str(
            relation.get("subject_id")
            or payload.get("invitee_username")
            or ""
        ).strip()
        inviter_name = str(payload.get("inviter_name_raw") or "").strip()
        invitee_name = str(payload.get("invitee_name_raw") or "").strip()
        if actor and inviter_name:
            identity_names.setdefault((room_id, actor), inviter_name)
        if subject and invitee_name:
            identity_names.setdefault((room_id, subject), invitee_name)

    for room_id in room_ids:
        room_key = str(room_id)
        snapshot = store.latest_membership_snapshot(room_key)
        members = list(snapshot.get("members", [])) if snapshot else []
        members_by_username = {
            str(member.get("username") or "").strip(): member
            for member in members
            if str(member.get("username") or "").strip()
        }
        for username, member in members_by_username.items():
            name = _member_name(member)
            if name:
                identity_names[(room_key, username)] = name

        for username, member in members_by_username.items():
            inviter_username = str(member.get("inviter_username") or "").strip()
            if not inviter_username:
                continue
            inviter_member = members_by_username.get(inviter_username)
            inviter_name = (
                _member_name(inviter_member)
                if inviter_member is not None
                else identity_names.get((room_key, inviter_username), "待确认")
            )
            edge = _metadata_relation(
                room_key,
                member,
                inviter_username,
                inviter_name,
            )
            edge_key = (room_key, username)
            metadata_by_room_subject[edge_key] = edge
            pair_key = (
                room_key,
                _name_key(inviter_name),
                _name_key(edge["payload"]["invitee_name_raw"]),
            )
            metadata_by_pair[pair_key].append(edge_key)

    covered: set[tuple[str, str]] = set()

    # Stable subject identity is authoritative for dedupe. The real event wins on
    # conflict; metadata may only fill a missing inviter identity on the projection.
    for relation in relations:
        room_id = str(relation.get("room_id") or "")
        payload = relation["payload"]
        subject = str(
            relation.get("subject_id")
            or payload.get("invitee_username")
            or ""
        ).strip()
        if not subject:
            continue
        edge_key = (room_id, subject)
        edge = metadata_by_room_subject.get(edge_key)
        if edge is None:
            continue
        covered.add(edge_key)
        metadata_actor = str(edge.get("actor_id") or "").strip()
        event_actor = str(
            relation.get("actor_id")
            or payload.get("inviter_username")
            or ""
        ).strip()
        if event_actor and metadata_actor and event_actor != metadata_actor:
            relation["metadata_conflict"] = True
            continue
        if not event_actor and metadata_actor:
            relation["actor_id"] = metadata_actor
            payload["inviter_username"] = metadata_actor
            existing_name = str(payload.get("inviter_name_raw") or "").strip()
            if not existing_name or existing_name in {"待确认", "待归因"}:
                payload["inviter_name_raw"] = str(
                    edge["payload"].get("inviter_name_raw") or "待确认"
                )
            payload["inviter_identity_status"] = "resolved"
            payload["attribution_source"] = "chat_room.ext_buffer"

    # Legacy CSV/name-only history has no stable subject id. Consume one matching
    # current metadata edge per real name-pair so the hybrid projection does not
    # double-count it, but never assign an arbitrary stable identity to that event.
    for relation in relations:
        payload = relation["payload"]
        stable_subject = str(
            relation.get("subject_id")
            or payload.get("invitee_username")
            or ""
        ).strip()
        if stable_subject:
            continue
        room_id = str(relation.get("room_id") or "")
        pair_key = (
            room_id,
            _name_key(payload.get("inviter_name_raw")),
            _name_key(payload.get("invitee_name_raw")),
        )
        if not pair_key[1] or not pair_key[2]:
            continue
        for edge_key in metadata_by_pair.get(pair_key, []):
            if edge_key in covered:
                continue
            covered.add(edge_key)
            break

    metadata_relations = [
        edge
        for edge_key, edge in metadata_by_room_subject.items()
        if edge_key not in covered
    ]
    metadata_relations.sort(
        key=lambda item: (
            str(item.get("room_id") or ""),
            _name_key(item.get("payload", {}).get("invitee_name_raw")),
            str(item.get("subject_id") or ""),
        )
    )
    return relations + metadata_relations
