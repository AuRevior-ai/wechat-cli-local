from __future__ import annotations

from datetime import datetime
import hashlib
import re
import stat
from pathlib import Path

from .media import read_media_file_payload
from .sns import SnsError, get_sns_media_source

_SHARD_RE = re.compile(r"^[0-9a-fA-F]{2}$")
_MP4_RE = re.compile(r"^[0-9a-fA-F]{30}\.mp4$")
_HEX32_RE = re.compile(r"^[0-9a-fA-F]{32}$")


def _account_root(sns_ctx):
    db_dir = Path(sns_ctx.db_dir).resolve()
    return db_dir.parent if db_dir.name.lower() == "db_storage" else db_dir


def _try_post_month(create_time):
    if not isinstance(create_time, int) or isinstance(create_time, bool):
        return None
    try:
        return datetime.fromtimestamp(create_time).strftime("%Y-%m")
    except (OverflowError, OSError, ValueError):
        return None


def _iter_video_candidates(sns_ctx, post_month):
    account_root = _account_root(sns_ctx)
    video_root = account_root / "cache" / post_month / "Sns" / "Video"
    resolved_video_root = video_root.resolve(strict=True)
    try:
        resolved_video_root.relative_to(account_root)
    except ValueError:
        return
    for shard in video_root.iterdir():
        if not _SHARD_RE.fullmatch(shard.name):
            continue
        resolved_shard = shard.resolve(strict=True)
        try:
            resolved_shard.relative_to(account_root)
            resolved_shard.relative_to(resolved_video_root)
        except ValueError:
            continue
        if not stat.S_ISDIR(resolved_shard.stat().st_mode):
            continue
        for candidate in shard.iterdir():
            if not _MP4_RE.fullmatch(candidate.name):
                continue
            resolved = candidate.resolve(strict=True)
            try:
                resolved.relative_to(account_root)
                resolved.relative_to(resolved_video_root)
                resolved.relative_to(resolved_shard)
            except ValueError:
                continue
            if not stat.S_ISREG(resolved.stat().st_mode):
                continue
            yield resolved


def _stream_md5(path):
    digest = hashlib.md5()
    with Path(path).open("rb") as source:
        for block in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _media_result(post_id, media_id, media_type, status, reason, local_path=None):
    return {
        "post_id": str(post_id),
        "media_id": str(media_id),
        "type": media_type,
        "status": status,
        "reason": reason,
        "_local_path": local_path,
    }


def _classify_video_matches(post_id, media_id, matches):
    if not matches:
        return _media_result(
            post_id,
            media_id,
            "video",
            "metadata_only",
            "local_missing",
        )
    if len(matches) != 1:
        return _media_result(
            post_id,
            media_id,
            "video",
            "metadata_only",
            "local_identity_ambiguous",
        )
    return _media_result(
        post_id,
        media_id,
        "video",
        "local_found",
        None,
        str(matches[0]),
    )


def _raise_media_unavailable(post_id, media_id, reason, exc):
    raise SnsError(
        "SNS_MEDIA_UNAVAILABLE",
        "朋友圈本地媒体当前不可用",
        retryable=True,
        details={
            "post_id": str(post_id),
            "media_id": str(media_id),
            "reason": reason,
        },
    ) from exc


def resolve_sns_media(sns_ctx, post_id, media_id):
    source = get_sns_media_source(sns_ctx, post_id, media_id)
    if source["type"] in {"image", "live_photo"}:
        return _media_result(
            post_id,
            media_id,
            source["type"],
            "metadata_only",
            "local_identity_unproven",
        )
    if source["type"] != "video" or source.get("_source_type") != 6:
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

    url_md5 = str(source.get("_source_url_md5") or "")
    if not _HEX32_RE.fullmatch(url_md5):
        return _media_result(
            post_id,
            media_id,
            "video",
            "metadata_only",
            "local_identity_unproven",
        )

    post_month = _try_post_month(source.get("create_time"))
    if post_month is None:
        return _media_result(
            post_id,
            media_id,
            "video",
            "metadata_only",
            "local_identity_unproven",
        )

    try:
        matches = []
        for candidate in _iter_video_candidates(sns_ctx, post_month):
            if _stream_md5(candidate).lower() == url_md5.lower():
                matches.append(candidate)
    except FileNotFoundError as exc:
        _raise_media_unavailable(post_id, media_id, "local_missing", exc)
    except (PermissionError, OSError) as exc:
        _raise_media_unavailable(post_id, media_id, "io_error", exc)
    return _classify_video_matches(post_id, media_id, matches)


def _validate_media_component(resolved, component):
    media_type = resolved["type"]
    if media_type == "live_photo":
        if component not in {"still", "video"}:
            raise SnsError(
                "SNS_INVALID_REQUEST",
                "朋友圈媒体组件参数无效",
                retryable=False,
            )
        return
    if component is not None:
        raise SnsError(
            "SNS_INVALID_REQUEST",
            "朋友圈媒体组件参数无效",
            retryable=False,
        )


def read_sns_media(sns_ctx, post_id, media_id, *, variant="preview", component=None):
    resolved = resolve_sns_media(sns_ctx, post_id, media_id)
    _validate_media_component(resolved, component)

    if resolved["status"] != "local_found":
        raise SnsError(
            "SNS_MEDIA_UNAVAILABLE",
            "朋友圈本地媒体当前不可用",
            retryable=True,
            details={
                "post_id": str(post_id),
                "media_id": str(media_id),
                "reason": resolved["reason"],
            },
        )

    try:
        payload = read_media_file_payload(
            resolved["_local_path"],
            db_dir=sns_ctx.db_dir,
        )
    except FileNotFoundError as exc:
        _raise_media_unavailable(post_id, media_id, "local_missing", exc)
    except (PermissionError, OSError) as exc:
        _raise_media_unavailable(post_id, media_id, "io_error", exc)

    body = payload["body"]
    if (
        payload["content_type"] != "video/mp4"
        or len(body) < 12
        or body[4:8] != b"ftyp"
    ):
        raise SnsError(
            "SNS_MEDIA_UNSUPPORTED",
            "朋友圈本地媒体格式暂不支持",
            retryable=False,
            details={
                "post_id": str(post_id),
                "media_id": str(media_id),
                "reason": "invalid_media_payload",
            },
        )
    return {
        "status": "ready",
        "type": "video",
        "body": body,
        "content_type": "video/mp4",
    }
