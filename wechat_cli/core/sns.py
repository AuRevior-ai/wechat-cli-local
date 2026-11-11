from __future__ import annotations

from contextlib import closing, contextmanager
from dataclasses import dataclass
from datetime import datetime
import hashlib
import json
import math
import os
from pathlib import Path
import re
import sqlite3
import xml.etree.ElementTree as ET

from .config import CONFIG_FILE, load_config
from .contacts import get_contact_full, get_contact_names
from .db_cache import DBCache
from .key_utils import get_key_info, strip_key_metadata
from .messages import parse_time_range, validate_pagination

SNS_DB_REL_KEY = "sns/sns.db"

SNS_TIMELINE_SELECT = """
SELECT
    tid,
    user_name,
    CAST(content AS BLOB) AS content_raw
FROM SnsTimeLine
"""

SNS_PROBE_SELECT = """
SELECT CAST(content AS BLOB) AS content_raw
FROM SnsTimeLine
WHERE content IS NOT NULL
  AND length(CAST(content AS BLOB)) > 0
ORDER BY tid DESC
LIMIT 20
"""

SNS_XML_MAX_BYTES = 256 * 1024
_XML_UNSAFE_RE = re.compile(r"<!DOCTYPE|<!ENTITY", re.IGNORECASE)

SNS_POST_PUBLIC_KEYS = {
    "post_id",
    "username",
    "display_name",
    "create_time",
    "time",
    "content",
    "location",
    "media",
    "likes",
    "comments",
    "parse_status",
    "warnings",
}

SNS_LOCATION_KEYS = (
    "poiName",
    "poiAddressName",
    "poiAddress",
    "city",
    "country",
    "latitude",
    "longitude",
)

SNS_LIKE_PUBLIC_KEYS = {"username", "display_name", "create_time"}
SNS_COMMENT_PUBLIC_KEYS = {
    "comment_id",
    "username",
    "display_name",
    "content",
    "create_time",
    "reply_to_username",
    "reply_to_display_name",
}

SNS_MEDIA_BASE_PUBLIC_KEYS = {
    "media_id",
    "type",
    "status",
    "reason",
    "preview_url",
    "width",
    "height",
    "duration",
}
SNS_LIVE_PHOTO_COMPONENT_PUBLIC_KEYS = {"status", "reason"}
SNS_LIVE_PHOTO_PUBLIC_KEYS = SNS_MEDIA_BASE_PUBLIC_KEYS | {"image", "video"}

SNS_ERROR_EXIT_CODES = {
    "SNS_INVALID_REQUEST": 2,
    "SNS_USER_NOT_FOUND": 2,
    "SNS_USER_AMBIGUOUS": 2,
    "SNS_DB_NOT_FOUND": 3,
    "SNS_KEY_NOT_FOUND": 3,
    "SNS_SCHEMA_UNSUPPORTED": 3,
    "SNS_MEDIA_UNAVAILABLE": 3,
    "SNS_MEDIA_UNSUPPORTED": 3,
    "SNS_INTERNAL_ERROR": 1,
}

_SNS_KEY_REASONS = {"keys_file_missing", "keys_file_unreadable", "sns_key_missing"}
_SNS_MEDIA_REASONS = {
    "remote_deferred",
    "local_missing",
    "local_identity_unproven",
    "local_identity_ambiguous",
    "key_unavailable",
    "decode_failed",
    "placeholder_only",
    "unsupported_type",
    "invalid_media_payload",
    "io_error",
}


class SnsError(Exception):
    def __init__(self, code, message, *, retryable=False, details=None):
        super().__init__(message)
        self.code = code
        self.message = message
        self.retryable = bool(retryable)
        self.details = details


def sns_error_exit_code(code):
    return SNS_ERROR_EXIT_CODES.get(code)


def sanitize_sns_error_details(code, details):
    if not isinstance(details, dict):
        return None
    if code == "SNS_SCHEMA_UNSUPPORTED":
        safe = {}
        allowed_missing = {"SnsTimeLine", "tid", "user_name", "content"}
        missing = details.get("missing")
        if isinstance(missing, list):
            safe_missing = [item for item in missing if item in allowed_missing]
            if safe_missing:
                safe["missing"] = safe_missing
        failures = details.get("failures")
        if isinstance(failures, list):
            safe_failures = []
            for item in failures:
                if not isinstance(item, dict):
                    continue
                column = item.get("column")
                problem = item.get("problem")
                if column not in {"tid", "user_name", "content"}:
                    continue
                if problem not in {"missing", "type", "primary_key"}:
                    continue
                row = {"column": column, "problem": problem}
                actual_type = item.get("actual_type")
                if isinstance(actual_type, str) and len(actual_type) <= 64:
                    row["actual_type"] = actual_type
                safe_failures.append(row)
            if safe_failures:
                safe["failures"] = safe_failures
        return safe or None
    if code == "SNS_KEY_NOT_FOUND":
        reason = details.get("reason")
        return {"reason": reason} if reason in _SNS_KEY_REASONS else None
    if code in {"SNS_MEDIA_UNAVAILABLE", "SNS_MEDIA_UNSUPPORTED"}:
        reason = details.get("reason")
        if reason not in _SNS_MEDIA_REASONS:
            return None
        safe = {"reason": reason}
        for key in ("post_id", "media_id"):
            if isinstance(details.get(key), str):
                safe[key] = details[key]
        return safe
    if code == "SNS_USER_AMBIGUOUS":
        raw_candidates = details.get("candidates")
        if not isinstance(raw_candidates, list):
            raw_candidates = []
        candidates = []
        for item in raw_candidates[:10]:
            if not isinstance(item, dict):
                continue
            candidates.append(
                {
                    key: str(item.get(key) or "")
                    for key in ("username", "nick_name", "remark", "display_name")
                }
            )
        candidate_count = details.get("candidate_count")
        if not isinstance(candidate_count, int) or candidate_count < 0:
            candidate_count = len(candidates)
        truncated = details.get("truncated")
        if not isinstance(truncated, bool):
            truncated = False
        return {
            "candidate_count": candidate_count,
            "truncated": truncated,
            "candidates": candidates,
        }
    return None


@dataclass(frozen=True)
class SnsContext:
    config_path: str
    cfg: dict
    db_dir: str
    decrypted_dir: str
    keys_file: str
    all_keys: dict
    cache: DBCache
    sns_db_rel_key: str
    sns_db_path: str


def _child_text(node, path):
    child = node.find(path) if node is not None else None
    return str(child.text or "").strip() if child is not None else ""


def _optional_int_text(value):
    text = str(value or "").strip()
    if not text:
        return None
    try:
        return int(text)
    except (TypeError, ValueError):
        return None


def _normalize_sns_create_time(raw_value):
    value = _optional_int_text(raw_value)
    if value is None:
        return None, ""
    try:
        time_text = datetime.fromtimestamp(value).strftime("%Y-%m-%d %H:%M")
    except (OverflowError, OSError, ValueError):
        return None, ""
    return value, time_text


def _normalize_sns_location(timeline):
    node = timeline.find("location") if timeline is not None else None
    if node is None:
        return None
    location = {}
    for key in SNS_LOCATION_KEYS:
        value = str(node.get(key) or "").strip()
        if value:
            location[key] = value
    return location or None


def _canonical_media_id(post_id, ordinal, source_media_id):
    raw = (
        f"sns-media-v1\0{post_id}\0{ordinal}\0{source_media_id or ''}"
    ).encode("utf-8")
    return "m_" + hashlib.sha256(raw).hexdigest()[:24]


def _parse_sns_xml(raw: bytes | None):
    if raw is None:
        return None, "xml_parse_failed"
    if not isinstance(raw, bytes):
        raise TypeError("SNS XML parser requires raw bytes")
    if len(raw) > SNS_XML_MAX_BYTES:
        return None, "xml_too_large"
    try:
        text = raw.decode("utf-8", errors="strict")
    except UnicodeDecodeError:
        return None, "xml_decode_failed"
    if _XML_UNSAFE_RE.search(text):
        return None, "xml_unsafe_markup"
    try:
        return ET.fromstring(text), None
    except ET.ParseError:
        return None, "xml_parse_failed"


def _normalize_post_base(tid, user_name, content_raw, display_names):
    canonical_username = str(user_name or "")
    post = {
        "post_id": str(int(tid)),
        "username": canonical_username,
        "display_name": display_names.get(canonical_username, canonical_username),
        "create_time": None,
        "time": "",
        "content": "",
        "location": None,
        "media": [],
        "likes": [],
        "comments": [],
        "parse_status": "ok",
        "warnings": [],
        "_root": None,
    }
    root, parse_reason = _parse_sns_xml(content_raw)
    if root is None:
        post["parse_status"] = "partial"
        if parse_reason:
            post["warnings"].append(parse_reason)
        return post

    post["_root"] = root
    timeline = root.find("TimelineObject")
    if timeline is None:
        post["parse_status"] = "partial"
        post["warnings"].append("missing_create_time")
        return post

    xml_username = _child_text(timeline, "username")
    if xml_username and xml_username != canonical_username:
        post["parse_status"] = "partial"
        post["warnings"].append("username_mismatch")

    create_time, time_text = _normalize_sns_create_time(
        _child_text(timeline, "createTime")
    )
    if create_time is None:
        post["parse_status"] = "partial"
        post["warnings"].append("missing_create_time")
    else:
        post["create_time"] = create_time
        post["time"] = time_text

    post["content"] = _child_text(timeline, "contentDesc")
    post["location"] = _normalize_sns_location(timeline)
    return post


def _normalize_sns_interactions(post):
    root = post.get("_root")
    if root is None:
        return
    extra = root.find("LocalExtraInfo")
    if extra is None:
        post["parse_status"] = "partial"
        if "missing_local_extra_info" not in post["warnings"]:
            post["warnings"].append("missing_local_extra_info")
        post["likes"] = []
        post["comments"] = []
        return

    likes = []
    for node in extra.findall("./like_user_list/user_comment"):
        username = _child_text(node, "username")
        nickname = _child_text(node, "nickname")
        likes.append(
            {
                "username": username,
                "display_name": nickname or username,
                "create_time": _optional_int_text(_child_text(node, "create_time")),
            }
        )
    post["likes"] = likes

    comments = []
    for node in extra.findall("./comment_user_list/user_comment"):
        username = _child_text(node, "username")
        nickname = _child_text(node, "nickname")
        comments.append(
            {
                "comment_id": _optional_int_text(_child_text(node, "comment_id")),
                "username": username,
                "display_name": nickname or username,
                "content": _child_text(node, "content"),
                "create_time": _optional_int_text(_child_text(node, "create_time")),
                "reply_to_username": None,
                "reply_to_display_name": None,
                "_ref_comment_id": _optional_int_text(
                    _child_text(node, "ref_comment_id")
                ),
            }
        )

    targets = {}
    for candidate in comments:
        comment_id = candidate["comment_id"]
        if comment_id in {None, 0}:
            continue
        targets.setdefault(comment_id, []).append(candidate)

    for item in comments:
        ref_id = item.pop("_ref_comment_id")
        if ref_id in {None, 0}:
            continue
        matches = targets.get(ref_id, [])
        if len(matches) == 0:
            if "reply_target_missing" not in post["warnings"]:
                post["warnings"].append("reply_target_missing")
            continue
        if len(matches) > 1:
            post["parse_status"] = "partial"
            if "reply_target_ambiguous" not in post["warnings"]:
                post["warnings"].append("reply_target_ambiguous")
            continue
        target = matches[0]
        item["reply_to_username"] = target["username"]
        item["reply_to_display_name"] = target["display_name"]
    post["comments"] = comments


def _initial_sns_media_state(source_type, *, has_live_photo):
    if source_type == 2 and has_live_photo:
        return "live_photo", "metadata_only", "local_identity_unproven"
    if source_type == 2:
        return "image", "metadata_only", "local_identity_unproven"
    if source_type == 6:
        return "video", "metadata_only", "remote_deferred"
    return "unsupported", "unsupported", "unsupported_type"


def _optional_int_attr(node, name):
    if node is None:
        return None
    return _optional_int_text(node.get(name))


def _optional_float_text(value):
    text = str(value or "").strip()
    if not text:
        return None
    try:
        value = float(text)
    except (TypeError, ValueError):
        return None
    return value if math.isfinite(value) else None


def _normalize_one_sns_media(media_node, *, post_id, ordinal):
    source_media_id = _child_text(media_node, "id")
    source_type = _optional_int_text(_child_text(media_node, "type"))
    has_live_photo = media_node.find("./LivePhoto/liveMedia") is not None
    media_type, status, reason = _initial_sns_media_state(
        source_type,
        has_live_photo=has_live_photo,
    )
    size_node = media_node.find("size")
    url_node = media_node.find("url")
    internal = {
        "media_id": _canonical_media_id(post_id, ordinal, source_media_id),
        "type": media_type,
        "status": status,
        "reason": reason,
        "preview_url": None,
        "width": _optional_int_attr(size_node, "width"),
        "height": _optional_int_attr(size_node, "height"),
        "duration": _optional_float_text(_child_text(media_node, "videoDuration")),
        "_source_type": source_type,
        "_source_url_md5": (
            str(url_node.get("md5") or "").strip().lower()
            if url_node is not None
            else ""
        ),
    }
    if media_type == "live_photo":
        internal["_components"] = {
            "image": {
                "status": "metadata_only",
                "reason": "local_identity_unproven",
            },
            "video": {
                "status": "metadata_only",
                "reason": "local_identity_unproven",
            },
        }
    return internal


def _normalize_sns_media(post):
    root = post.get("_root")
    timeline = root.find("TimelineObject") if root is not None else None
    nodes = (
        timeline.findall("./ContentObject/mediaList/media")
        if timeline is not None
        else []
    )
    post["media"] = [
        _normalize_one_sns_media(
            node,
            post_id=post["post_id"],
            ordinal=index,
        )
        for index, node in enumerate(nodes)
    ]
    if any(item["type"] == "unsupported" for item in post["media"]):
        if "unsupported_media_type" not in post["warnings"]:
            post["warnings"].append("unsupported_media_type")


def _public_like(item):
    return {key: item.get(key) for key in SNS_LIKE_PUBLIC_KEYS}


def _public_comment(item):
    return {key: item.get(key) for key in SNS_COMMENT_PUBLIC_KEYS}


def _public_media(item):
    public = {key: item.get(key) for key in SNS_MEDIA_BASE_PUBLIC_KEYS}
    if item.get("type") == "live_photo":
        components = item.get("_components") or {}
        for name in ("image", "video"):
            component = components.get(name) or {}
            public[name] = {
                "status": component.get("status") or "metadata_only",
                "reason": component.get("reason") or "local_identity_unproven",
            }
    return public


def _public_post(post):
    public = {
        "post_id": post.get("post_id"),
        "username": post.get("username"),
        "display_name": post.get("display_name"),
        "create_time": post.get("create_time"),
        "time": post.get("time"),
        "content": post.get("content"),
        "location": post.get("location"),
        "media": [_public_media(item) for item in post.get("media") or []],
        "likes": [_public_like(item) for item in post.get("likes") or []],
        "comments": [
            _public_comment(item) for item in post.get("comments") or []
        ],
        "parse_status": post.get("parse_status"),
        "warnings": list(post.get("warnings") or []),
    }
    return public


def _normalize_sns_row(tid, user_name, content_raw, display_names):
    post = _normalize_post_base(tid, user_name, content_raw, display_names)
    _normalize_sns_interactions(post)
    _normalize_sns_media(post)
    return post


_SNS_AMBIGUOUS_LIMIT = 10


def _sns_contact_candidate(row):
    username = str(row.get("username") or "").strip()
    nick_name = str(row.get("nick_name") or "").strip()
    remark = str(row.get("remark") or "").strip()
    display_name = remark or nick_name or username
    return {
        "username": username,
        "nick_name": nick_name,
        "remark": remark,
        "display_name": display_name,
    }


def _dedupe_sns_candidates(rows):
    by_username = {}
    for row in rows:
        candidate = _sns_contact_candidate(row)
        username = candidate["username"]
        if username and username not in by_username:
            by_username[username] = candidate
    return [
        by_username[key]
        for key in sorted(by_username, key=str.casefold)
    ]


def _sns_candidate_values(candidate):
    return tuple(
        value
        for value in (
            candidate["username"],
            candidate["nick_name"],
            candidate["remark"],
            candidate["display_name"],
        )
        if value
    )


def _raise_sns_user_ambiguous(candidates):
    ordered = sorted(candidates, key=lambda item: item["username"].casefold())
    raise SnsError(
        "SNS_USER_AMBIGUOUS",
        "朋友圈发布者匹配到多个联系人",
        retryable=False,
        details={
            "candidate_count": len(ordered),
            "truncated": len(ordered) > _SNS_AMBIGUOUS_LIMIT,
            "candidates": ordered[:_SNS_AMBIGUOUS_LIMIT],
        },
    )


def _resolve_sns_candidate_set(candidates, *, not_found_message):
    if len(candidates) == 1:
        return candidates[0]["username"]
    if len(candidates) > 1:
        _raise_sns_user_ambiguous(candidates)
    raise SnsError(
        "SNS_USER_NOT_FOUND",
        not_found_message,
        retryable=False,
    )


def resolve_sns_username(query, cache, decrypted_dir):
    raw_query = str(query or "").strip()
    if raw_query.startswith("wxid_") and len(raw_query) > len("wxid_"):
        return raw_query
    if not raw_query:
        raise SnsError(
            "SNS_USER_NOT_FOUND",
            "未找到朋友圈发布者",
            retryable=False,
        )

    target = raw_query.casefold()
    contacts = _dedupe_sns_candidates(get_contact_full(cache, decrypted_dir))

    exact = [
        candidate
        for candidate in contacts
        if any(
            value.casefold() == target
            for value in _sns_candidate_values(candidate)
        )
    ]
    if len(exact) == 1:
        return exact[0]["username"]
    if len(exact) > 1:
        _raise_sns_user_ambiguous(exact)

    fuzzy = [
        candidate
        for candidate in contacts
        if any(
            target in value.casefold()
            for value in _sns_candidate_values(candidate)
        )
    ]
    return _resolve_sns_candidate_set(
        fuzzy,
        not_found_message="未找到朋友圈发布者",
    )


def _validate_sns_query(limit, offset, start_time, end_time):
    try:
        validate_pagination(limit, offset, limit_max=200)
        start_ts, end_ts = parse_time_range(start_time or "", end_time or "")
    except ValueError as exc:
        raise SnsError(
            "SNS_INVALID_REQUEST",
            "朋友圈查询参数无效",
            retryable=False,
        ) from exc
    return start_ts, end_ts


def _iter_sns_rows(conn, username=None):
    sql = SNS_TIMELINE_SELECT
    params = ()
    if username:
        sql += " WHERE user_name = ?"
        params = (username,)
    sql += " ORDER BY tid DESC"
    for tid, user_name, content_raw in conn.execute(sql, params):
        yield tid, user_name, content_raw


def _advance_ordering_invariant(previous_ok_create_time, post):
    if post["parse_status"] != "ok" or post.get("create_time") is None:
        return previous_ok_create_time
    current = int(post["create_time"])
    if previous_ok_create_time is not None and current > previous_ok_create_time:
        raise SnsError(
            "SNS_SCHEMA_UNSUPPORTED",
            "当前微信朋友圈时间线排序语义尚未支持",
            retryable=False,
        )
    return current


def _load_sns_display_names(sns_ctx):
    try:
        names = get_contact_names(sns_ctx.cache, sns_ctx.decrypted_dir)
    except Exception:
        return {}
    return dict(names or {})


def _sns_post_matches_filters(post, *, query, start_ts, end_ts):
    if query:
        needle = str(query).casefold()
        if needle not in str(post.get("content") or "").casefold():
            return False
    if start_ts is not None or end_ts is not None:
        create_time = post.get("create_time")
        if create_time is None:
            return False
        if start_ts is not None and create_time < start_ts:
            return False
        if end_ts is not None and create_time > end_ts:
            return False
    return True


def list_sns_posts(
    sns_ctx,
    *,
    limit=20,
    offset=0,
    username=None,
    query=None,
    start_time=None,
    end_time=None,
):
    start_ts, end_ts = _validate_sns_query(
        limit,
        offset,
        start_time,
        end_time,
    )
    display_names = _load_sns_display_names(sns_ctx)
    collected = []
    skipped = 0
    previous_ok_create_time = None

    with _validated_sns_connection(sns_ctx.sns_db_path) as conn:
        for tid, user_name, content_raw in _iter_sns_rows(conn, username=username):
            post = _normalize_sns_row(
                tid,
                user_name,
                content_raw,
                display_names,
            )
            previous_ok_create_time = _advance_ordering_invariant(
                previous_ok_create_time,
                post,
            )
            if not _sns_post_matches_filters(
                post,
                query=query,
                start_ts=start_ts,
                end_ts=end_ts,
            ):
                continue
            if skipped < offset:
                skipped += 1
                continue
            collected.append(post)
            if len(collected) >= limit + 1:
                break

    page = collected[:limit]
    return {
        "count": len(page),
        "offset": offset,
        "limit": limit,
        "has_more": len(collected) > limit,
        "posts": [_public_post(post) for post in page],
    }


def _select_sns_row_by_post_id(conn, post_id):
    return conn.execute(
        "SELECT tid, user_name, CAST(content AS BLOB) "
        "FROM SnsTimeLine WHERE tid = ?",
        (int(post_id),),
    ).fetchone()


def get_sns_post(sns_ctx, post_id):
    display_names = _load_sns_display_names(sns_ctx)
    with _validated_sns_connection(sns_ctx.sns_db_path) as conn:
        row = _select_sns_row_by_post_id(conn, post_id)
        if row is None:
            return None
        internal = _normalize_sns_row(
            row[0],
            row[1],
            row[2],
            display_names,
        )
        return _public_post(internal)


def _media_identity_unproven(post_id, media_id):
    raise SnsError(
        "SNS_MEDIA_UNAVAILABLE",
        "朋友圈本地媒体当前不可用",
        retryable=True,
        details={
            "post_id": str(post_id),
            "media_id": str(media_id),
            "reason": "local_identity_unproven",
        },
    )


def get_sns_media_source(sns_ctx, post_id, media_id):
    with _validated_sns_connection(sns_ctx.sns_db_path) as conn:
        row = _select_sns_row_by_post_id(conn, post_id)
        if row is None:
            _media_identity_unproven(post_id, media_id)
        internal = _normalize_sns_row(row[0], row[1], row[2], {})

    matched = next(
        (
            item
            for item in internal["media"]
            if item["media_id"] == media_id
        ),
        None,
    )
    if matched is None:
        _media_identity_unproven(post_id, media_id)
    if matched["type"] == "unsupported":
        raise SnsError(
            "SNS_MEDIA_UNSUPPORTED",
            "朋友圈本地媒体类型暂不支持",
            retryable=False,
            details={
                "post_id": str(post_id),
                "media_id": str(media_id),
                "reason": "unsupported_type",
            },
        )
    return {
        **matched,
        "create_time": internal["create_time"],
    }


def _readonly_connection(path):
    conn = sqlite3.connect(f"file:{Path(path).as_posix()}?mode=ro", uri=True)
    conn.execute("PRAGMA query_only=ON")
    return conn


def _validate_sns_db_predicate(conn):
    table = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name='SnsTimeLine'"
    ).fetchone()
    if not table:
        raise SnsError(
            "SNS_SCHEMA_UNSUPPORTED",
            "当前微信朋友圈数据库结构尚未支持",
            retryable=False,
            details={"missing": ["SnsTimeLine"]},
        )

    rows = conn.execute('PRAGMA table_info("SnsTimeLine")').fetchall()
    columns = {
        row[1]: {"type": (row[2] or "").upper(), "pk": int(row[5] or 0)}
        for row in rows
    }
    required = {
        "tid": ("INTEGER", 1),
        "user_name": ("TEXT", 0),
        "content": ("TEXT", 0),
    }
    failures = []
    for name, (declared_type, required_pk) in required.items():
        actual = columns.get(name)
        if actual is None:
            failures.append({"column": name, "problem": "missing"})
            continue
        if actual["type"] != declared_type:
            failures.append(
                {
                    "column": name,
                    "problem": "type",
                    "actual_type": actual["type"],
                }
            )
        if required_pk and actual["pk"] != required_pk:
            failures.append({"column": name, "problem": "primary_key"})
    if failures:
        raise SnsError(
            "SNS_SCHEMA_UNSUPPORTED",
            "当前微信朋友圈数据库结构尚未支持",
            retryable=False,
            details={"failures": failures},
        )
    return columns


def _schema_unsupported(message="当前微信朋友圈数据库结构尚未支持"):
    return SnsError(
        "SNS_SCHEMA_UNSUPPORTED",
        message,
        retryable=False,
    )


def _probe_sns_xml_family(conn):
    rows = conn.execute(SNS_PROBE_SELECT).fetchall()
    if not rows:
        return

    parsed_count = 0
    has_required_semantics = False
    for (content_raw,) in rows:
        root, _parse_reason = _parse_sns_xml(content_raw)
        if root is None:
            continue
        parsed_count += 1
        if root.tag != "SnsDataItem":
            raise _schema_unsupported()
        if (
            root.find("TimelineObject") is not None
            and root.find("LocalExtraInfo") is not None
        ):
            has_required_semantics = True

    if parsed_count == 0 or not has_required_semantics:
        raise _schema_unsupported()


def _validate_known_schema(conn):
    columns = _validate_sns_db_predicate(conn)
    _probe_sns_xml_family(conn)
    return {"adapter": "KnownSchemaContract-v1", "columns": columns}


def get_sns_schema(db_path):
    with closing(_readonly_connection(db_path)) as conn:
        return _validate_known_schema(conn)


@contextmanager
def _validated_sns_connection(db_path):
    conn = _readonly_connection(db_path)
    try:
        _validate_known_schema(conn)
        yield conn
    finally:
        conn.close()


def build_sns_context(config_path=None):
    selected_config = (
        config_path
        or os.environ.get("WECHAT_CLI_CONFIG")
        or CONFIG_FILE
    )
    resolved_config = os.path.abspath(selected_config)
    try:
        cfg = load_config(resolved_config)
        db_dir = cfg["db_dir"]
    except (FileNotFoundError, KeyError) as exc:
        raise SnsError(
            "SNS_DB_NOT_FOUND",
            "无法访问朋友圈数据库",
            retryable=False,
        ) from exc

    decrypted_dir = cfg["decrypted_dir"]
    keys_file = cfg["keys_file"]
    source = Path(db_dir) / "sns" / "sns.db"
    if not source.is_file():
        raise SnsError(
            "SNS_DB_NOT_FOUND",
            "无法访问朋友圈数据库",
            retryable=False,
        )

    if not os.path.exists(keys_file):
        raise SnsError(
            "SNS_KEY_NOT_FOUND",
            "朋友圈数据库密钥不可用",
            retryable=True,
            details={"reason": "keys_file_missing"},
        )

    try:
        raw = json.loads(Path(keys_file).read_text(encoding="utf-8"))
        if not isinstance(raw, dict):
            raise ValueError("keys file must be an object")
    except (OSError, UnicodeError, json.JSONDecodeError, ValueError) as exc:
        raise SnsError(
            "SNS_KEY_NOT_FOUND",
            "朋友圈数据库密钥不可用",
            retryable=True,
            details={"reason": "keys_file_unreadable"},
        ) from exc

    all_keys = strip_key_metadata(raw)
    if not get_key_info(all_keys, SNS_DB_REL_KEY):
        raise SnsError(
            "SNS_KEY_NOT_FOUND",
            "朋友圈数据库密钥不可用",
            retryable=True,
            details={"reason": "sns_key_missing"},
        )

    cache = DBCache(all_keys, db_dir)
    try:
        sns_db_path = cache.get(SNS_DB_REL_KEY)
    except Exception as exc:
        raise SnsError(
            "SNS_INTERNAL_ERROR",
            "朋友圈数据库解密缓存初始化失败",
            retryable=True,
        ) from exc
    if not sns_db_path:
        raise SnsError(
            "SNS_INTERNAL_ERROR",
            "朋友圈数据库解密缓存初始化失败",
            retryable=True,
        )

    return SnsContext(
        config_path=resolved_config,
        cfg=cfg,
        db_dir=db_dir,
        decrypted_dir=decrypted_dir,
        keys_file=keys_file,
        all_keys=all_keys,
        cache=cache,
        sns_db_rel_key=SNS_DB_REL_KEY,
        sns_db_path=sns_db_path,
    )
