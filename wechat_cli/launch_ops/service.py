"""API-facing Launch Ops use cases and projections."""

from __future__ import annotations

import time
from collections import Counter
from datetime import datetime
from typing import Any

from .archive import read_current_archive_projection_snapshot
from .models import DEFAULT_CAMPAIGN_GROUP_PATTERN, DEFAULT_CAMPAIGN_NAME
from .relations import build_hybrid_invite_relations
from .source import GroupSource


class LaunchOpsService:
    def __init__(self, store, source_adapter, *, watcher=None):
        self.store = store
        self.source = source_adapter
        self.watcher = watcher

    def ensure_default_campaign(self, *, now: int | None = None) -> dict[str, Any]:
        existing = self.store.list_campaigns()
        if existing:
            return existing[0]
        ts = int(time.time() if now is None else now)
        groups = self.source.discover_groups()
        for group in groups:
            self.store.upsert_room(
                group.room_id,
                group.display_name,
                health_status="unknown",
                now=ts,
            )
        campaign = self.store.create_campaign(DEFAULT_CAMPAIGN_NAME, now=ts)
        matched = []
        for group in groups:
            match = DEFAULT_CAMPAIGN_GROUP_PATTERN.fullmatch(group.display_name)
            if match:
                matched.append((int(match.group("number")), group))
        for _, group in sorted(matched, key=lambda pair: pair[0]):
            self.store.add_room_to_campaign(
                campaign["campaign_id"], group.room_id, now=ts
            )
        self.store.set_meta("default_campaign_bootstrap_v1", "complete")
        return campaign

    def list_campaigns(self) -> list[dict[str, Any]]:
        return self.store.list_campaigns()

    def create_campaign(self, name: str, *, now: int | None = None) -> dict[str, Any]:
        return self.store.create_campaign(name, now=now)


    def end_campaign(self, campaign_id: str, *, now: int | None = None) -> None:
        self._require_campaign(campaign_id)
        self.store.end_campaign(campaign_id, now=now)

    def add_rooms(
        self,
        campaign_id: str,
        room_ids: list[str],
        *,
        now: int | None = None,
    ) -> list[dict[str, Any]]:
        campaign = self._require_campaign(campaign_id)
        if campaign.get("ended_at") is not None:
            raise ValueError("campaign has ended")
        available = {room["room_id"] for room in self.store.list_rooms()}
        result = []
        for room_id in room_ids:
            if room_id not in available:
                raise ValueError(f"room not found: {room_id}")
            result.append(
                self.store.add_room_to_campaign(campaign_id, room_id, now=now)
            )
        return result

    def remove_room(
        self,
        campaign_id: str,
        room_id: str,
        *,
        now: int | None = None,
    ) -> None:
        campaign = self._require_campaign(campaign_id)
        if campaign.get("ended_at") is not None:
            raise ValueError("campaign has ended")
        self.store.remove_room_from_campaign(campaign_id, room_id, now=now)

    def _current_relations_for_room(
        self, room_id: str, *, now: int | None = None
    ) -> dict[str, Any]:
        status, archive_relations = read_current_archive_projection_snapshot(
            self.store, room_id
        )
        if status["use_archive_current_projection"]:
            return {
                "source": "archive",
                "relations": archive_relations,
                "archive_status": status,
            }
        ts = int(time.time() if now is None else now)
        return {
            "source": "hybrid",
            "relations": build_hybrid_invite_relations(
                self.store,
                [room_id],
                include_metadata=True,
                start_ts=None,
                end_ts=ts,
            ),
            "archive_status": status,
        }

    def archive_room(self, room_id: str) -> dict[str, Any]:
        rooms = self.store.list_rooms()
        room = next((item for item in rooms if item["room_id"] == room_id), None)
        if room is None:
            raise ValueError("room not found")
        current = self._current_relations_for_room(room_id)
        status = current["archive_status"]
        return {
            "room_id": room_id,
            "display_name": str(room.get("display_name") or room_id),
            "archiving_enabled": bool(status["archiving_enabled"]),
            "archive_projection_state": status["projection_state"],
            "archive_current_source": current["source"],
            "archive_last_refresh_at": status["last_refresh_at"],
            "archive_last_success_at": status["last_success_at"],
            "archive_refresh_error_code": status["last_error_code"],
            "archive_refresh_error_message": status["last_error_message"],
            "current_relation_count": len(current["relations"]),
        }

    def archive_member(self, room_id: str, member_key: str) -> dict[str, Any]:
        person = self.store.get_trace_person_by_username(member_key)
        if person is None:
            raise ValueError("archive member not found")
        episodes = self.store.list_trace_membership_episodes(room_id, person["person_id"])
        return {
            "room_id": room_id,
            "member_key": member_key,
            "display_name": str(
                person.get("current_remark")
                or person.get("current_nick_name")
                or member_key
            ),
            "episodes": [
                {
                    "episode_id": episode["episode_id"],
                    "episode_index": episode["episode_index"],
                    "first_observed_at": episode["first_observed_at"],
                    "last_observed_at": episode["last_observed_at"],
                    "status": episode["status"],
                    "active_relation": self.store.get_active_trace_relation(
                        episode["episode_id"]
                    ),
                }
                for episode in episodes
            ],
        }

    def start_archive(self, room_id: str) -> dict[str, Any]:
        room = next(
            (item for item in self.store.list_rooms() if item["room_id"] == room_id),
            None,
        )
        if room is None:
            raise ValueError("room not found")
        self.store.ensure_trace_managed_room(
            room_id, source="manual", now=int(time.time())
        )
        return self.archive_room(room_id)

    def stop_archive(self, room_id: str) -> dict[str, Any]:
        self.store.set_trace_managed_room_enabled(
            room_id, False, source="manual", now=int(time.time())
        )
        return self.archive_room(room_id)

    def refresh_archive(self, room_id: str) -> dict[str, Any]:
        if self.watcher is None:
            raise RuntimeError("Launch Ops watcher 尚未启动")
        self.watcher.force_refresh_room(room_id, now=int(time.time()))
        return self.archive_room(room_id)

    def manual_confirm_archive(
        self,
        room_id: str,
        episode_id: str,
        *,
        inviter_username: str | None,
        unresolved: bool,
        method: str | None,
        event_time: int | None,
        reason: str,
    ) -> dict[str, Any]:
        if self.watcher is None:
            raise RuntimeError("Launch Ops watcher 尚未启动")
        managed = self.store.get_trace_managed_room(room_id)
        if managed is None or not bool(managed["archiving_enabled"]):
            raise ValueError("archive is disabled; start archive before manual confirmation")
        episode = self.store.get_trace_membership_episode(episode_id)
        if episode is None or episode["room_id"] != room_id:
            raise ValueError("membership episode not found for room")
        now = int(time.time())
        inviter_person_id = None
        if not unresolved:
            cleaned = str(inviter_username or "").strip()
            if not cleaned:
                raise ValueError("inviter_username is required unless unresolved")
            inviter_person_id = self.store.ensure_trace_person(
                cleaned, now=now
            )["person_id"]
        self.store.add_trace_manual_confirmation(
            episode_id,
            inviter_person_id=inviter_person_id,
            unresolved=bool(unresolved),
            method=method,
            event_time=event_time,
            reason=reason,
            operator_id="local-author",
            now=now,
        )
        self.watcher._drain_trace_reconcile_queue(now=now, room_id=room_id)
        status = self.store.get_trace_room_projection_status(room_id)
        if status["projection_state"] != "ready":
            raise RuntimeError("archive room did not return to ready after manual confirmation")
        return {
            "room_id": room_id,
            "episode_id": episode_id,
            "reconciled": True,
            "projection_state": "ready",
            "active_relation": self.store.get_active_trace_relation(episode_id),
        }

    def list_rooms(
        self,
        *,
        campaign_id: str | None = None,
        query: str = "",
        health: str = "",
        now: int | None = None,
    ) -> list[dict[str, Any]]:
        if campaign_id:
            self._require_campaign(campaign_id)
        rooms = self.store.list_rooms(
            campaign_id=campaign_id,
            query=query,
            health=health,
        )
        if not rooms:
            return []
        ts = int(time.time() if now is None else now)
        room_ids = [room["room_id"] for room in rooms]
        today_start = self._local_day_start(ts)
        joined = self.store.list_events(
            room_ids=room_ids,
            event_type="member.joined",
            start_ts=today_start,
            end_ts=ts,
            limit=50_000,
        )
        invites = self.store.list_events(
            room_ids=room_ids,
            event_type="member.invited",
            limit=50_000,
        )
        joined_counts = Counter(event["room_id"] for event in joined)
        invite_counts = Counter(event["room_id"] for event in invites)
        result = []
        for room in rooms:
            item = dict(room)
            item["avatar_url"] = self._avatar_url(
                room.get("room_id") or "",
                room.get("display_name") or "",
            )
            item["today_joined"] = joined_counts.get(room["room_id"], 0)
            item["invite_event_count"] = invite_counts.get(room["room_id"], 0)
            item["today_left"] = {"available": False, "value": None}
            archive_status = self.store.get_trace_room_projection_status(room["room_id"])
            item["archiving_enabled"] = archive_status["archiving_enabled"]
            item["archive_projection_state"] = archive_status["projection_state"]
            item["archive_current_source"] = (
                "archive" if archive_status["use_archive_current_projection"] else "hybrid"
            )
            item["archive_last_refresh_at"] = archive_status["last_refresh_at"]
            item["archive_last_success_at"] = archive_status["last_success_at"]
            item["archive_refresh_error_code"] = archive_status["last_error_code"]
            item["archive_refresh_error_message"] = archive_status["last_error_message"]
            result.append(item)
        return result

    def room_members(self, campaign_id: str, room_id: str) -> dict[str, Any]:
        self._require_campaign(campaign_id)
        rooms = self.store.list_rooms(campaign_id=campaign_id)
        room = next((item for item in rooms if item["room_id"] == room_id), None)
        if room is None:
            raise ValueError("room not in campaign")
        room = dict(room)
        room["avatar_url"] = self._avatar_url(room_id, room.get("display_name") or room_id)
        group = GroupSource(
            room_id=room_id,
            display_name=room["display_name"],
            avatar_url=room["avatar_url"],
        )
        return {"room": room, "members": self.source.members(group)}

    def dashboard(
        self,
        campaign_id: str,
        *,
        now: int | None = None,
        invite_window: str = "campaign",
    ) -> dict[str, Any]:
        self._require_campaign(campaign_id)
        if invite_window not in {"campaign", "7d", "today"}:
            raise ValueError("invalid invite_window")
        ts = int(time.time() if now is None else now)
        day_start = self._local_day_start(ts)
        rooms = self.store.list_rooms(campaign_id=campaign_id)
        room_ids = [room["room_id"] for room in rooms]
        if invite_window == "today":
            invite_start = day_start
        elif invite_window == "7d":
            invite_start = ts - 7 * 24 * 60 * 60
        else:
            invite_start = None
        if invite_window == "campaign":
            invite_events = []
            for room_id in room_ids:
                invite_events.extend(
                    self._current_relations_for_room(room_id, now=ts)["relations"]
                )
        else:
            invite_events = build_hybrid_invite_relations(
                self.store,
                room_ids,
                include_metadata=False,
                start_ts=invite_start,
                end_ts=ts,
            )
        joined_events = self.store.list_events(
            room_ids=room_ids,
            event_type="member.joined",
            start_ts=day_start,
            end_ts=ts,
        )

        ranking: Counter[str] = Counter()
        ranking_avatar_candidates: dict[str, set[str]] = {}
        hourly: Counter[int] = Counter()
        for event in invite_events:
            payload = event["payload"]
            method = payload.get("method", "")
            inviter_name = (
                payload.get("inviter_name_raw")
                or event.get("actor_id")
                or ""
            )
            if method != "unattributed_qr" and inviter_name:
                inviter_name = str(inviter_name)
                ranking[inviter_name] += 1
                avatar_url = self._avatar_url(
                    str(event.get("actor_id") or payload.get("inviter_username") or ""),
                    inviter_name,
                    room_id=str(event.get("room_id") or ""),
                )
                if avatar_url:
                    ranking_avatar_candidates.setdefault(inviter_name, set()).add(avatar_url)
        for event in joined_events:
            hourly[datetime.fromtimestamp(event["event_time"]).hour] += 1

        invite_ranking = [
            {
                "name": name,
                "count": count,
                "avatar_url": (
                    next(iter(ranking_avatar_candidates.get(name, set())))
                    if len(ranking_avatar_candidates.get(name, set())) == 1
                    else ""
                ),
            }
            for name, count in sorted(
                ranking.items(),
                key=lambda item: (-item[1], item[0]),
            )
        ]
        hourly_joined = [
            {"hour": hour, "count": hourly.get(hour, 0)}
            for hour in range(24)
        ]
        return {
            "campaign_id": campaign_id,
            "room_count": len(rooms),
            "current_members": sum(int(room.get("member_count") or 0) for room in rooms),
            "today_joined": len(joined_events),
            "today_left": {"available": False, "value": None},
            "hourly_joined": hourly_joined,
            "invite_ranking": invite_ranking,
            "invite_window": invite_window,
            "watcher": self.watcher_health(),
        }

    def trace(
        self,
        campaign_id: str,
        filters: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        self._require_campaign(campaign_id)
        filters = dict(filters or {})
        room_ids = [
            room["room_id"]
            for room in self.store.list_rooms(campaign_id=campaign_id)
        ]
        selected_room = str(filters.get("room_id") or "")
        if selected_room:
            if selected_room not in room_ids:
                return {"items": []}
            room_ids = [selected_room]
        start_ts = self._optional_int(filters.get("start_ts"))
        end_ts = self._optional_int(filters.get("end_ts"))
        if start_ts is None and end_ts is None:
            events = []
            for room_id in room_ids:
                events.extend(
                    self._current_relations_for_room(room_id)["relations"]
                )
        else:
            events = build_hybrid_invite_relations(
                self.store,
                room_ids,
                include_metadata=False,
                start_ts=start_ts,
                end_ts=end_ts,
            )
        method_filter = str(filters.get("method") or "")
        query = str(filters.get("query") or "").strip().casefold()
        inviter_counts: Counter[str] = Counter(
            event.get("actor_id") or event["payload"].get("inviter_name_raw") or ""
            for event in events
            if (event.get("actor_id") or event["payload"].get("inviter_name_raw"))
        )
        items = []
        room_names = {
            room["room_id"]: room["display_name"]
            for room in self.store.list_rooms(campaign_id=campaign_id)
        }
        for event in events:
            payload = event["payload"]
            method = str(payload.get("method") or "")
            if method_filter and method != method_filter:
                continue
            invitee_name = str(
                payload.get("invitee_name_raw")
                or event.get("subject_id")
                or "未知成员"
            )
            inviter_key = str(
                event.get("actor_id")
                or payload.get("inviter_name_raw")
                or ""
            )
            inviter_name = (
                "待归因"
                if method == "unattributed_qr"
                else str(payload.get("inviter_name_raw") or inviter_key or "待确认")
            )
            haystack = " ".join((invitee_name, inviter_name)).casefold()
            if query and query not in haystack:
                continue
            room_name = room_names.get(event["room_id"], event["room_id"])
            member_key = str(event.get("subject_id") or invitee_name)
            items.append(
                {
                    "event_id": event["event_id"],
                    "room_id": event["room_id"],
                    "room_name": room_name,
                    "room_avatar_url": self._avatar_url(
                        event["room_id"], room_name, room_id=event["room_id"]
                    ),
                    "event_time": event.get("event_time"),
                    "episode_id": event.get("episode_id"),
                    "member_key": member_key,
                    "member_name": invitee_name,
                    "member_avatar_url": self._avatar_url(
                        member_key, invitee_name, room_id=event["room_id"]
                    ),
                    "inviter_key": inviter_key,
                    "inviter_name": inviter_name,
                    "inviter_avatar_url": self._avatar_url(
                        inviter_key, inviter_name, room_id=event["room_id"]
                    ),
                    "method": method,
                    "invite_count": inviter_counts.get(
                        str(event.get("subject_id") or invitee_name), 0
                    ),
                    "identity_status": payload.get(
                        "invitee_identity_status", "unresolved"
                    ),
                    "status": (
                        "当前在群"
                        if event.get("relation_source") in {"chat_room.ext_buffer", "trace_archive"}
                        else "状态待确认"
                    ),
                }
            )
        return {"items": items}

    def member_detail(
        self,
        campaign_id: str,
        room_id: str,
        member_key: str,
    ) -> dict[str, Any]:
        self._require_campaign(campaign_id)
        campaign_room_ids = {
            room["room_id"]
            for room in self.store.list_rooms(campaign_id=campaign_id)
        }
        if room_id not in campaign_room_ids:
            raise ValueError("room not in campaign")
        events = self._current_relations_for_room(room_id)["relations"]
        invite_items = []
        for event in events:
            payload = event["payload"]
            keys = {
                str(event.get("actor_id") or ""),
                str(event.get("subject_id") or ""),
                str(payload.get("inviter_name_raw") or ""),
                str(payload.get("invitee_name_raw") or ""),
            }
            if member_key in keys:
                item = dict(event)
                item["inviter_avatar_url"] = self._avatar_url(
                    str(event.get("actor_id") or payload.get("inviter_username") or ""),
                    str(payload.get("inviter_name_raw") or ""),
                    room_id=room_id,
                )
                item["invitee_avatar_url"] = self._avatar_url(
                    str(event.get("subject_id") or payload.get("invitee_username") or ""),
                    str(payload.get("invitee_name_raw") or ""),
                    room_id=room_id,
                )
                invite_items.append(item)
        messages = self.store.list_messages(
            room_id=room_id,
            sender_id=member_key,
            limit=200,
        )
        media_types = {"image", "sticker", "voice", "video", "file", "forwarded"}
        media = [
            item for item in messages if item.get("message_type") in media_types
        ]
        room = next(
            (
                item
                for item in self.store.list_rooms(campaign_id=campaign_id)
                if item["room_id"] == room_id
            ),
            {"room_id": room_id, "display_name": room_id},
        )
        member_name = member_key
        for event in invite_items:
            payload = event.get("payload") or {}
            if str(event.get("subject_id") or "") == member_key:
                member_name = str(payload.get("invitee_name_raw") or member_key)
                break
            if str(event.get("actor_id") or "") == member_key:
                member_name = str(payload.get("inviter_name_raw") or member_key)
                break
        return {
            "campaign_id": campaign_id,
            "room_id": room_id,
            "member_key": member_key,
            "member_avatar_url": self._avatar_url(
                member_key, member_name, room_id=room_id
            ),
            "room_avatar_url": self._avatar_url(
                room_id,
                str(room.get("display_name") or room_id),
            ),
            "sections": {
                "invite": {"items": invite_items},
                "messages": {"items": messages},
                "media": {"items": media},
                "ai": {"available": False, "label": "即将开放"},
            },
        }

    def watcher_health(self) -> dict[str, Any]:
        if self.watcher is None:
            return {
                "status": "unavailable",
                "stale": True,
                "last_attempt_at": None,
                "last_success_at": None,
                "last_event_at": None,
                "error_code": "watcher_not_started",
                "error_message": "Launch Ops watcher 尚未启动",
            }
        health = self.watcher.health()
        return {
            "status": health.status,
            "stale": health.stale,
            "last_attempt_at": health.last_attempt_at,
            "last_success_at": health.last_success_at,
            "last_event_at": health.last_event_at,
            "error_code": health.error_code,
            "error_message": health.error_message,
        }

    def rescan(self) -> dict[str, Any]:
        if self.watcher is None:
            raise RuntimeError("Launch Ops watcher 尚未启动")
        result = self.watcher.scan_once()
        return {
            "scanned_sources": result.scanned_sources,
            "skipped_sources": result.skipped_sources,
            "inserted_events": result.inserted_events,
            "inserted_messages": result.inserted_messages,
            "discovered_rooms": result.discovered_rooms,
            "last_event_at": result.last_event_at,
        }

    def backfill_campaign_invites(
        self,
        campaign_id: str,
        *,
        start_ts: int,
        end_ts: int,
    ) -> dict[str, Any]:
        self._require_campaign(campaign_id)
        if self.watcher is None:
            raise RuntimeError("Launch Ops watcher 尚未启动")
        start = int(start_ts)
        end = int(end_ts)
        if start < 0 or end < start:
            raise ValueError("invalid invite backfill time window")
        room_ids = [
            room["room_id"]
            for room in self.store.list_campaign_rooms(campaign_id)
        ]
        result = self.watcher.backfill_invites(
            room_ids,
            start_time=start,
            end_time=end,
        )
        return {
            "campaign_id": campaign_id,
            "start_ts": start,
            "end_ts": end,
            **result,
        }

    def _require_campaign(self, campaign_id: str) -> dict[str, Any]:
        campaign = self.store.get_campaign(campaign_id)
        if campaign is None:
            raise ValueError("campaign not found")
        return campaign

    def _avatar_url(
        self,
        username: str = "",
        display_name: str = "",
        *,
        room_id: str = "",
    ) -> str:
        resolver = getattr(self.source, "avatar_url", None)
        if not callable(resolver):
            return ""
        return str(resolver(username, display_name, room_id=room_id) or "")

    @staticmethod
    def _local_day_start(timestamp: int) -> int:
        dt = datetime.fromtimestamp(timestamp)
        return int(dt.replace(hour=0, minute=0, second=0, microsecond=0).timestamp())

    @staticmethod
    def _optional_int(value: Any) -> int | None:
        if value in (None, ""):
            return None
        return int(value)
