"""群邀请提示解析与统计。"""

from __future__ import annotations

import csv
import io
import re
import sqlite3
import xml.etree.ElementTree as ET
from collections import defaultdict
from contextlib import closing
from datetime import datetime

from .messages import _is_safe_msg_table_name, decompress_content

_SYSTEM_PREFIX_RE = re.compile(r"^\s*\[系统\]\s*")
_QUOTE_RE = re.compile(r'["“](.*?)["”]')
_DIRECT_TO_SELF_RE = re.compile(
    r'^\s*(?P<inviter>.+?)\s*邀请你加入了群聊'
    r'(?:[，,]\s*群聊参与人还有[：:].*)?\s*$'
)
_DIRECT_RE = re.compile(
    r'^\s*["“](?P<inviter>.*?)["”]\s*邀请'
    r'(?P<invitees>.*?)加入了群聊\s*$'
)
_QR_RE = re.compile(
    r'^\s*["“](?P<invitee>.*?)["”]\s*通过扫描'
    r'["“](?P<inviter>.*?)["”]\s*分享的二维码加入群聊\s*$'
)
_UNATTRIBUTED_QR_RE = re.compile(
    r'^\s*(?P<invitee>你|["“].*?["”])通过扫描二维码加入群聊'
)


def normalize_notice_text(text: str) -> str:
    return _SYSTEM_PREFIX_RE.sub("", (text or "").strip())


def normalize_person_name(name: str) -> str:
    return (name or "").strip().strip('"“”').strip()


def _xml_invitee_names(plain: str, usernames: list[str]) -> list[str]:
    invitee_names = [
        normalize_person_name(value)
        for value in _QUOTE_RE.findall(plain)
    ]
    if len(invitee_names) == 1 and len(usernames) > 1:
        batch_names = [
            normalize_person_name(value)
            for value in re.split(r"[、，]", invitee_names[0])
            if normalize_person_name(value)
        ]
        if len(batch_names) == len(usernames):
            return batch_names
    return invitee_names


def _sysmsgtemplate_profile(
    content: ET.Element,
    link_name: str,
) -> dict[str, str] | None:
    for link in content.findall("./link_list/link"):
        if (link.get("name") or "").strip() != link_name:
            continue
        if (link.get("type") or "").strip() != "link_profile":
            return None
        member = link.find("./memberlist/member")
        if member is None:
            return None
        username = (member.findtext("username") or "").strip()
        nickname = normalize_person_name(member.findtext("nickname") or "")
        if not username:
            return None
        return {
            "username": username,
            "name": nickname or username,
        }
    return None


def _parse_sysmsgtemplate_invite_xml(notice: str) -> list[dict] | None:
    xml_start = notice.find("<sysmsg")
    if xml_start < 0:
        return None
    try:
        root = ET.fromstring(notice[xml_start:].strip())
    except ET.ParseError:
        return None
    if root.get("type") != "sysmsgtemplate":
        return None
    content = root.find("./sysmsgtemplate/content_template")
    if content is None or (content.get("type") or "").strip() != "tmpl_type_profile":
        return None
    template = (content.findtext("template") or "").strip()

    if template == '"$adder$"通过扫描"$from$"分享的二维码加入群聊':
        adder = _sysmsgtemplate_profile(content, "adder")
        share_owner = _sysmsgtemplate_profile(content, "from")
        if not adder or not share_owner:
            return None
        return [{
            "method": "qr",
            "inviter_name_raw": share_owner["name"],
            "inviter_username": share_owner["username"],
            "invitee_name_raw": adder["name"],
            "invitee_username": adder["username"],
        }]

    if template == '"$username$"邀请你加入了群聊，群聊参与人还有：$others$':
        inviter = _sysmsgtemplate_profile(content, "username")
        if not inviter:
            return None
        return [{
            "method": "direct",
            "inviter_name_raw": inviter["name"],
            "inviter_username": inviter["username"],
            "invitee_name_raw": "你",
            "invitee_is_self": True,
        }]

    return None


def _parse_self_invite_xml(notice: str) -> list[dict] | None:
    xml_start = notice.find("<sysmsg")
    if xml_start < 0:
        return None
    try:
        root = ET.fromstring(notice[xml_start:].strip())
    except ET.ParseError:
        return None
    if root.get("type") != "delchatroommember":
        return None
    container = root.find("delchatroommember")
    if container is None:
        return None
    scene = (container.findtext("./link/scene") or "").strip()
    if scene not in {"invite", "qrcode"}:
        return None
    plain = (
        container.findtext("plain")
        or container.findtext("text")
        or ""
    )
    invitee_usernames = [
        (node.text or "").strip()
        for node in container.findall("./link/memberlist/username")
    ]
    invitee_names = _xml_invitee_names(plain, invitee_usernames)
    is_direct = scene == "invite" and "你邀请" in plain
    is_self_qr = (
        scene == "qrcode"
        and "通过扫描你分享的二维码加入群聊" in plain
    )
    if not invitee_names or not (is_direct or is_self_qr):
        return None
    return [
        {
            "method": "direct" if is_direct else "qr",
            "inviter_name_raw": "你",
            "inviter_is_self": True,
            "invitee_name_raw": invitee_name,
            "invitee_username": (
                invitee_usernames[index]
                if index < len(invitee_usernames)
                else ""
            ),
        }
        for index, invitee_name in enumerate(invitee_names)
    ]


def parse_invite_notice(text: str) -> list[dict] | None:
    notice = normalize_notice_text(text)
    template_invites = _parse_sysmsgtemplate_invite_xml(notice)
    if template_invites:
        return template_invites
    self_invites = _parse_self_invite_xml(notice)
    if self_invites:
        return self_invites

    qr_match = _QR_RE.fullmatch(notice)
    if qr_match:
        return [{
            "method": "qr",
            "inviter_name_raw": normalize_person_name(
                qr_match.group("inviter")
            ),
            "invitee_name_raw": normalize_person_name(
                qr_match.group("invitee")
            ),
        }]

    direct_to_self_match = _DIRECT_TO_SELF_RE.fullmatch(notice)
    if direct_to_self_match:
        return [{
            "method": "direct",
            "inviter_name_raw": normalize_person_name(
                direct_to_self_match.group("inviter")
            ),
            "invitee_name_raw": "你",
            "invitee_is_self": True,
        }]

    direct_match = _DIRECT_RE.fullmatch(notice)
    if direct_match:
        inviter = normalize_person_name(direct_match.group("inviter"))
        invitees = [
            normalize_person_name(value)
            for value in _QUOTE_RE.findall(
                direct_match.group("invitees")
            )
        ]
        return [
            {
                "method": "direct",
                "inviter_name_raw": inviter,
                "invitee_name_raw": invitee,
            }
            for invitee in invitees
            if invitee
        ] or None

    unattributed_match = _UNATTRIBUTED_QR_RE.match(notice)
    if unattributed_match:
        return [{
            "method": "unattributed_qr",
            "inviter_name_raw": "",
            "invitee_name_raw": normalize_person_name(
                unattributed_match.group("invitee")
            ),
        }]
    return None


def is_invite_like_notice(text: str) -> bool:
    notice = normalize_notice_text(text)
    return "加入" in notice and "群聊" in notice


def parse_identity_bindings(
    values: list[str] | tuple[str, ...],
) -> dict[str, str]:
    bindings: dict[str, str] = {}
    for raw in values:
        if "=" not in raw:
            raise ValueError(f"身份绑定格式无效: {raw}")
        name, username = raw.split("=", 1)
        name = normalize_person_name(name)
        username = username.strip()
        if not name or not username:
            raise ValueError(f"身份绑定格式无效: {raw}")
        if name in bindings and bindings[name] != username:
            raise ValueError(f"历史昵称重复绑定到不同账号: {name}")
        bindings[name] = username
    return bindings


class IdentityResolver:
    def __init__(
        self,
        members: list[dict],
        bindings: dict[str, str],
    ):
        self._bindings = bindings
        self._members_by_username = {
            member["username"]: member
            for member in members
            if member.get("username")
        }
        self._exact: dict[str, set[str]] = {}
        for member in members:
            username = member.get("username", "")
            if not username:
                continue
            for field in ("display_name", "remark", "nick_name"):
                value = normalize_person_name(member.get(field, ""))
                if value:
                    self._exact.setdefault(value, set()).add(username)

    def resolve(self, raw_name: str) -> dict:
        name = normalize_person_name(raw_name)
        if name in self._bindings:
            username = self._bindings[name]
            member = self._members_by_username.get(username, {})
            return {
                "key": f"user:{username}",
                "username": username,
                "name": member.get("display_name") or name,
                "status": "resolved",
                "source": "manual",
            }

        candidates = self._exact.get(name, set())
        if len(candidates) == 1:
            username = next(iter(candidates))
            member = self._members_by_username[username]
            return {
                "key": f"user:{username}",
                "username": username,
                "name": member.get("display_name") or name,
                "status": "resolved",
                "source": "member_exact",
            }

        return {
            "key": f"name:{name}",
            "username": "",
            "name": name,
            "status": "unresolved",
            "source": "ambiguous" if candidates else "unmatched",
        }

    def resolve_username(
        self,
        username: str,
        raw_name: str,
    ) -> dict:
        username = (username or "").strip()
        if not username:
            return self.resolve(raw_name)
        member = self._members_by_username.get(username, {})
        return {
            "key": f"user:{username}",
            "username": username,
            "name": (
                member.get("display_name")
                or normalize_person_name(raw_name)
                or username
            ),
            "status": "resolved",
            "source": "notice_username",
        }


def _system_rows(
    conn,
    table_name,
    start_ts=None,
    end_ts=None,
):
    if not _is_safe_msg_table_name(table_name):
        raise ValueError(f"非法消息表名: {table_name}")
    clauses = ["(local_type & 0xFFFFFFFF) = 10000"]
    params = []
    if start_ts is not None:
        clauses.append("create_time >= ?")
        params.append(start_ts)
    if end_ts is not None:
        clauses.append("create_time <= ?")
        params.append(end_ts)
    return conn.execute(
        f"""SELECT local_id, server_id, create_time, message_content,
                   WCDB_CT_message_content
            FROM [{table_name}]
            WHERE {' AND '.join(clauses)}
            ORDER BY create_time ASC""",
        params,
    ).fetchall()


def _identity_fields(prefix, identity):
    return {
        f"{prefix}_username": identity["username"],
        f"{prefix}_key": identity["key"],
        f"{prefix}_identity_status": identity["status"],
        f"{prefix}_identity_source": identity["source"],
    }


def _format_time(timestamp):
    return datetime.fromtimestamp(timestamp).strftime("%Y-%m-%d %H:%M")


def collect_group_invite_stats(
    chat_ctx,
    members,
    bindings,
    start_ts=None,
    end_ts=None,
):
    if not chat_ctx.get("is_group"):
        raise ValueError("邀请统计只支持群聊")

    resolver = IdentityResolver(members, bindings)
    seen_rows = set()
    events = []
    unattributed = []
    unparsed = []
    failures = []
    system_times = []

    for table in chat_ctx.get("message_tables") or []:
        try:
            with closing(sqlite3.connect(table["db_path"])) as conn:
                rows = _system_rows(
                    conn,
                    table["table_name"],
                    start_ts=start_ts,
                    end_ts=end_ts,
                )
        except Exception as exc:
            failures.append(f'{table.get("db_path", "")}: {exc}')
            continue

        for (
            local_id,
            server_id,
            timestamp,
            content,
            compression,
        ) in rows:
            text = decompress_content(content, compression)
            if text is None:
                text = ""
            notice = normalize_notice_text(text)
            row_key = (
                ("server", int(server_id))
                if server_id
                else ("fallback", int(timestamp), notice)
            )
            if row_key in seen_rows:
                continue
            seen_rows.add(row_key)
            system_times.append(int(timestamp))

            parsed = parse_invite_notice(notice)
            if parsed is None:
                if is_invite_like_notice(notice):
                    unparsed.append({
                        "timestamp": int(timestamp),
                        "time": _format_time(timestamp),
                        "raw_text": notice,
                        "reason": "unknown_template",
                    })
                continue

            for relation_index, relation in enumerate(parsed):
                event = {
                    "event_id": (
                        f"{server_id}:{relation_index}"
                        if server_id
                        else (
                            f"{timestamp}:{local_id}:"
                            f"{relation_index}"
                        )
                    ),
                    "server_id": int(server_id or 0),
                    "timestamp": int(timestamp),
                    "time": _format_time(timestamp),
                    "method": relation["method"],
                    "inviter_name_raw": (
                        relation["inviter_name_raw"]
                    ),
                    "invitee_name_raw": (
                        relation["invitee_name_raw"]
                    ),
                    "raw_text": notice,
                }
                invitee_username = (
                    chat_ctx.get("self_username", "")
                    if relation.get("invitee_is_self")
                    else relation.get("invitee_username", "")
                )
                invitee = resolver.resolve_username(
                    invitee_username,
                    relation["invitee_name_raw"],
                )
                event.update(_identity_fields("invitee", invitee))

                if relation["method"] == "unattributed_qr":
                    event.update({
                        "inviter_username": "",
                        "inviter_key": "",
                        "inviter_identity_status": "unattributed",
                        "inviter_identity_source": "notice",
                    })
                    unattributed.append(event)
                    continue

                inviter_username = (
                    chat_ctx.get("self_username", "")
                    if relation.get("inviter_is_self")
                    else relation.get("inviter_username", "")
                )
                inviter = resolver.resolve_username(
                    inviter_username,
                    relation["inviter_name_raw"],
                )
                event.update(_identity_fields("inviter", inviter))
                events.append(event)

    buckets = defaultdict(list)
    for event in events:
        buckets[event["inviter_key"]].append(event)

    ranking = []
    for inviter_key, inviter_events in buckets.items():
        unique_invitees = {}
        for event in inviter_events:
            unique_invitees.setdefault(
                event["invitee_key"], event
            )
        first_timestamp = min(
            event["timestamp"] for event in inviter_events
        )
        last_timestamp = max(
            event["timestamp"] for event in inviter_events
        )
        sample = inviter_events[0]
        resolved_inviter = resolver.resolve_username(
            sample["inviter_username"],
            sample["inviter_name_raw"],
        )
        ranking.append({
            "rank": 0,
            "inviter_key": inviter_key,
            "inviter_username": sample["inviter_username"],
            "inviter_name": resolved_inviter["name"],
            "historical_names": sorted({
                event["inviter_name_raw"]
                for event in inviter_events
            }),
            "identity_status": (
                sample["inviter_identity_status"]
            ),
            "identity_source": (
                sample["inviter_identity_source"]
            ),
            "unique_invitee_count": len(unique_invitees),
            "event_count": len(inviter_events),
            "direct_count": sum(
                event["method"] == "direct"
                for event in inviter_events
            ),
            "qr_count": sum(
                event["method"] == "qr"
                for event in inviter_events
            ),
            "first_invite_time": _format_time(
                first_timestamp
            ),
            "last_invite_time": _format_time(
                last_timestamp
            ),
            "invitees": [
                {
                    "name": event["invitee_name_raw"],
                    "username": event["invitee_username"],
                    "time": event["time"],
                    "method": event["method"],
                }
                for event in unique_invitees.values()
            ],
        })

    ranking.sort(key=lambda item: (
        -item["unique_invitee_count"],
        item["first_invite_time"],
        item["inviter_name"],
    ))
    for index, item in enumerate(ranking, 1):
        item["rank"] = index

    rank_by_key = {
        item["inviter_key"]: item["rank"]
        for item in ranking
    }
    for event in events:
        event["rank"] = rank_by_key[event["inviter_key"]]

    global_invitees = {
        event["invitee_key"] for event in events
    }
    unresolved_count = sum(
        event["inviter_identity_status"] != "resolved"
        or event["invitee_identity_status"] != "resolved"
        for event in events
    )

    return {
        "chat": chat_ctx.get("display_name", ""),
        "username": chat_ctx.get("username", ""),
        "scope": {
            "start_timestamp": start_ts,
            "end_timestamp": end_ts,
            "first_visible_system_time": (
                _format_time(min(system_times))
                if system_times else None
            ),
            "last_visible_system_time": (
                _format_time(max(system_times))
                if system_times else None
            ),
        },
        "summary": {
            "system_message_count": len(seen_rows),
            "invite_event_count": (
                len(events) + len(unattributed)
            ),
            "attributed_event_count": len(events),
            "unique_invitee_count": len(global_invitees),
            "unattributed_count": len(unattributed),
            "unresolved_identity_count": unresolved_count,
            "unparsed_count": len(unparsed),
        },
        "ranking": ranking,
        "events": sorted(
            events, key=lambda item: item["timestamp"]
        ),
        "unattributed_events": unattributed,
        "unparsed_messages": unparsed,
        "failures": failures,
    }


def format_invite_stats_text(result):
    summary = result.get("summary", {})
    scope = result.get("scope", {})
    lines = [
        f'{result.get("chat", "")} 群邀请统计',
    ]
    if result.get("query_source"):
        lines.append(
            f'数据源: {result.get("query_source", "")}；'
            f'完整度: {result.get("completeness", "unknown")}'
        )
    lines.extend([
        (
            "可见范围: "
            f'{scope.get("first_visible_system_time") or "无"}'
            " ~ "
            f'{scope.get("last_visible_system_time") or "无"}'
        ),
        (
            f'邀请事件 {summary.get("invite_event_count", 0)}；'
            f'已归属 {summary.get("attributed_event_count", 0)}；'
            f'唯一被邀请人 {summary.get("unique_invitee_count", 0)}；'
            f'来源不明 {summary.get("unattributed_count", 0)}；'
            f'身份待确认 {summary.get("unresolved_identity_count", 0)}；'
            f'未解析 {summary.get("unparsed_count", 0)}'
        ),
        "",
        "排行榜:",
    ])
    for item in result.get("ranking", []):
        inviter_username = item.get("inviter_username", "")
        identity = (
            f" ({inviter_username})"
            if inviter_username
            else " (身份待确认)"
        )
        unique_count = item.get(
            "unique_invitee_count",
            item.get("historical_unique_invitee_count", 0),
        )
        lines.append(
            f'{item.get("rank", 0)}. {item.get("inviter_name", "待确认")}'
            f'{identity} — {unique_count} 人'
        )
        lines.append(
            f'   直接 {item.get("direct_count", 0)} / '
            f'二维码 {item.get("qr_count", 0)} / '
            f'事件 {item.get("event_count", 0)}'
        )
        if any(
            key in item
            for key in (
                "current_invitee_count",
                "departed_invitee_count",
                "unknown_time_count",
                "metadata_only_count",
            )
        ):
            lines.append(
                f'   当前 {item.get("current_invitee_count", 0)} / '
                f'已退群 {item.get("departed_invitee_count", 0)} / '
                f'时间未知 {item.get("unknown_time_count", 0)} / '
                f'metadata {item.get("metadata_only_count", 0)}'
            )
    lines.append("")
    lines.append("邀请关系明细:")
    method_names = {
        "direct": "直接邀请",
        "qr": "二维码",
        "metadata": "元数据",
        "unattributed_qr": "来源不明扫码",
        "unknown": "未知",
    }
    for event in result.get("events", []):
        method = method_names.get(event.get("method"), event.get("method") or "未知")
        identity_note = (
            " [身份待确认]"
            if event.get("inviter_identity_status") != "resolved"
            or event.get("invitee_identity_status") != "resolved"
            else ""
        )
        lines.append(
            f'  [{event.get("time", "时间未知")}] '
            f'{event.get("inviter_name_raw", "待确认")}'
            f' -> {event.get("invitee_name_raw", "未知成员")} '
            f'({method}){identity_note}'
        )
    if result.get("unattributed_events"):
        lines.append("")
        lines.append("来源不明:")
        for event in result.get("unattributed_events", []):
            lines.append(
                f'  [{event.get("time", "时间未知")}] '
                f'{event.get("invitee_name_raw", "未知成员")}: '
                f'{event.get("raw_text", "")}'
            )
    if result.get("unparsed_messages"):
        lines.append("")
        lines.append("未解析提示:")
        for item in result.get("unparsed_messages", []):
            lines.append(
                f'  [{item.get("time", "时间未知")}] '
                f'{item.get("raw_text", "")}'
            )
    excluded = int(result.get("excluded_unknown_time_count", 0) or 0)
    if excluded:
        lines.append(f"时间过滤排除未知时间关系 {excluded}")
    if result.get("failures"):
        lines.append("读取失败: " + "；".join(result.get("failures", [])))
    for warning in result.get("warnings", []):
        lines.append(f"警告: {warning}")
    return "\n".join(lines)


def format_invite_stats_csv(result):
    stream = io.StringIO(newline="")
    stream.write("\ufeff")
    writer = csv.writer(stream)
    writer.writerow([
        "邀请者排名", "邀请者", "邀请者账号", "邀请者身份状态",
        "唯一拉人数", "被邀请者", "被邀请者账号",
        "被邀请者身份状态", "入群时间", "邀请方式", "原始提示",
        "成员状态", "查询来源", "证据来源", "时间状态", "冲突",
    ])
    ranking = {
        item.get("inviter_key", ""): item
        for item in result.get("ranking", [])
    }
    all_events = [
        *result.get("events", []),
        *result.get("unattributed_events", []),
    ]
    method_names = {
        "direct": "直接邀请",
        "qr": "二维码",
        "metadata": "元数据",
        "unattributed_qr": "来源不明扫码",
        "unknown": "未知",
    }
    for event in all_events:
        item = ranking.get(event.get("inviter_key", ""), {})
        time_value = event.get("time", "")
        time_status = "unknown" if time_value == "时间未知" else (
            "known" if event.get("timestamp") is not None else ""
        )
        writer.writerow([
            item.get("rank", ""),
            item.get("inviter_name", ""),
            item.get("inviter_username", ""),
            event.get("inviter_identity_status", ""),
            item.get("unique_invitee_count", 0),
            event.get("invitee_name_raw", ""),
            event.get("invitee_username", ""),
            event.get("invitee_identity_status", ""),
            time_value,
            method_names.get(
                event.get("method"), event.get("method") or "未知"
            ),
            event.get("raw_text", ""),
            event.get("membership_state", ""),
            event.get("query_source", result.get("query_source", "")),
            event.get("evidence_source", ""),
            time_status,
            "yes" if event.get("metadata_conflict") else "",
        ])
    return stream.getvalue()
