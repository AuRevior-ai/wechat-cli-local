"""Localhost web console server.

The server intentionally exposes a narrow JSON API that maps browser form
payloads to the existing click CLI. It never accepts arbitrary commands.
"""

from __future__ import annotations

import ipaddress
import json
import mimetypes
import ntpath
import os
import re
import secrets
import socket
import subprocess
import sys
import time
import webbrowser
from dataclasses import dataclass
from datetime import datetime
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from importlib import resources
from pathlib import Path
from threading import Event, Lock
from typing import Any
from urllib.error import URLError
from urllib.parse import parse_qs, quote, urlparse
from urllib.request import HTTPRedirectHandler, Request, build_opener

from ..core.config import CONFIG_FILE, KEYS_FILE, STATE_DIR, detect_db_dir_candidates
from ..core.context import AppContext
from ..launch_ops.archive import ArchiveMigrator
from ..launch_ops.models import DEFAULT_STORE_PATH
from ..launch_ops.service import LaunchOpsService
from ..launch_ops.source import LocalWeChatSourceAdapter
from ..launch_ops.store import LaunchOpsStore
from ..launch_ops.watcher import LaunchOpsWatcher
from ..core.media import decode_media_bytes as _core_decode_media_bytes
from ..core.media import media_download_filename as _core_media_download_filename
from ..core.media import read_media_file_payload
from ..core.messages import validate_search_scope
from ..core.sns import (
    SnsError,
    build_sns_context,
    sanitize_sns_error_details,
    sns_error_exit_code,
)
from ..core.sns_media import read_sns_media

AVATAR_MAX_BYTES = 2 * 1024 * 1024
AVATAR_ALLOWED_HOSTS = ("qlogo.cn", "qpic.cn", "weixin.qq.com")
AVATAR_CACHE_LIMIT = 512
AVATAR_CACHE_MAX_BYTES = 32 * 1024 * 1024
_AVATAR_CACHE: dict[str, dict[str, Any]] = {}
_AVATAR_CACHE_LOCK = Lock()
_AVATAR_CACHE_INFLIGHT: dict[str, Event] = {}
_AVATAR_CACHE_BYTES = 0
AI_PACKAGE_DIR = os.path.join(STATE_DIR, "ai-packages")
AI_PACKAGE_EXPIRES_SECONDS = 10 * 60
_AI_PACKAGE_DOWNLOADS: dict[str, dict[str, Any]] = {}
_AI_PACKAGE_DOWNLOADS_LOCK = Lock()
_SNS_MEDIA_PATH_PREFIX = "/api/sns/media/"
_SNS_POST_ID_RE = re.compile(r"^(?:0|[1-9][0-9]{0,18}|-[1-9][0-9]{0,18})$")
_SNS_MEDIA_ID_RE = re.compile(r"^m_[0-9a-f]{24}$")
_SQLITE_INT64_MIN = -9223372036854775808
_SQLITE_INT64_MAX = 9223372036854775807
_SNS_MEDIA_QUERY_KEYS = {"variant", "component", "download"}
_SNS_MEDIA_HTTP_STATUS = {
    "SNS_INVALID_REQUEST": HTTPStatus.BAD_REQUEST,
    "SNS_MEDIA_UNAVAILABLE": HTTPStatus.NOT_FOUND,
    "SNS_MEDIA_UNSUPPORTED": HTTPStatus.UNSUPPORTED_MEDIA_TYPE,
    "SNS_DB_NOT_FOUND": HTTPStatus.SERVICE_UNAVAILABLE,
    "SNS_KEY_NOT_FOUND": HTTPStatus.SERVICE_UNAVAILABLE,
    "SNS_SCHEMA_UNSUPPORTED": HTTPStatus.UNPROCESSABLE_ENTITY,
    "SNS_INTERNAL_ERROR": HTTPStatus.INTERNAL_SERVER_ERROR,
}


def _invalid_sns_media_request():
    return SnsError(
        "SNS_INVALID_REQUEST",
        "朋友圈媒体请求参数无效",
        retryable=False,
    )


def _parse_sns_media_path(raw_path):
    if not isinstance(raw_path, str) or not raw_path.startswith(_SNS_MEDIA_PATH_PREFIX):
        raise _invalid_sns_media_request()
    suffix = raw_path[len(_SNS_MEDIA_PATH_PREFIX):]
    parts = suffix.split("/")
    if len(parts) != 2:
        raise _invalid_sns_media_request()
    post_id, media_id = parts
    if not _SNS_POST_ID_RE.fullmatch(post_id):
        raise _invalid_sns_media_request()
    post_value = int(post_id)
    if not (_SQLITE_INT64_MIN <= post_value <= _SQLITE_INT64_MAX):
        raise _invalid_sns_media_request()
    if not _SNS_MEDIA_ID_RE.fullmatch(media_id):
        raise _invalid_sns_media_request()
    return post_id, media_id


def _parse_sns_media_query(raw_query):
    values = parse_qs(raw_query, keep_blank_values=True)
    if any(key not in _SNS_MEDIA_QUERY_KEYS for key in values):
        raise _invalid_sns_media_request()
    if any(len(items) != 1 for items in values.values()):
        raise _invalid_sns_media_request()
    variant = (values.get("variant") or ["preview"])[0]
    component = (values.get("component") or [None])[0]
    download_raw = (values.get("download") or [None])[0]
    if variant not in {"preview", "original"}:
        raise _invalid_sns_media_request()
    if component not in {None, "still", "video"}:
        raise _invalid_sns_media_request()
    if download_raw not in {None, "1"}:
        raise _invalid_sns_media_request()
    return variant, component, download_raw == "1"


def _sns_media_error_payload(error):
    public = {
        "code": error.code,
        "message": error.message,
        "retryable": error.retryable,
    }
    details = sanitize_sns_error_details(error.code, error.details)
    if details:
        public["details"] = details
    return {"ok": False, "error": public}


STATIC_ASSET_NAMES = {
    "app.css", "app.js", "launch.html", "launch.css", "launch.js",
    "au-revior-wechat.jpg", "au-revior-payment.jpg",
}


class _AvatarRedirectHandler(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        _validate_avatar_url(newurl)
        return super().redirect_request(req, fp, code, msg, headers, newurl)


urlopen = build_opener(_AvatarRedirectHandler()).open


def _request_hosts_for_bind(bind_host: str) -> set[str]:
    hosts = {"127.0.0.1", "localhost"}
    if bind_host != "0.0.0.0":
        return hosts
    try:
        _, _, addresses = socket.gethostbyname_ex(socket.gethostname())
    except OSError:
        addresses = []
    for address in addresses:
        if re.fullmatch(r"(?:\d{1,3}\.){3}\d{1,3}", address or ""):
            octets = [int(part) for part in address.split(".")]
            if all(0 <= part <= 255 for part in octets) and address != "0.0.0.0":
                hosts.add(address)
    return hosts


def _is_local_request_source(
    host_header: str,
    origin_header: str,
    server_port: int,
    *,
    require_origin: bool,
    allowed_hosts: set[str] | None = None,
) -> bool:
    host_names = allowed_hosts or {"127.0.0.1", "localhost"}
    expected = {
        f"{host.lower()}:{server_port}"
        for host in host_names
        if host and host != "0.0.0.0"
    }
    if (host_header or "").strip().lower() not in expected:
        return False
    if not require_origin or not (origin_header or "").strip():
        return True
    parsed = urlparse(origin_header.strip())
    return (
        parsed.scheme == "http"
        and not parsed.username
        and not parsed.password
        and (parsed.netloc or "").lower() in expected
        and parsed.path in {"", "/"}
        and not parsed.params
        and not parsed.query
        and not parsed.fragment
    )


@dataclass(frozen=True)
class OptionSpec:
    flag: str
    kind: str = "value"


@dataclass(frozen=True)
class CommandSpec:
    positional: tuple[str, ...] = ()
    optional_positional: tuple[str, ...] = ()
    options: dict[str, OptionSpec] | None = None
    default_format: str | None = None


COMMAND_SPECS: dict[str, CommandSpec] = {
    "init": CommandSpec(
        options={
            "db_dir": OptionSpec("--db-dir"),
            "force": OptionSpec("--force", "bool"),
        },
    ),
    "sessions": CommandSpec(
        options={"limit": OptionSpec("--limit"), "format": OptionSpec("--format")},
        default_format="json",
    ),
    "sns": CommandSpec(
        options={
            "limit": OptionSpec("--limit"),
            "offset": OptionSpec("--offset"),
            "user": OptionSpec("--user"),
            "query": OptionSpec("--query"),
            "start_time": OptionSpec("--start-time"),
            "end_time": OptionSpec("--end-time"),
            "format": OptionSpec("--format"),
        },
        default_format="json",
    ),
    "history": CommandSpec(
        positional=("chat_name",),
        options={
            "limit": OptionSpec("--limit"),
            "offset": OptionSpec("--offset"),
            "start_time": OptionSpec("--start-time"),
            "end_time": OptionSpec("--end-time"),
            "format": OptionSpec("--format"),
            "type": OptionSpec("--type"),
            "media": OptionSpec("--media", "bool"),
        },
        default_format="json",
    ),
    "search": CommandSpec(
        optional_positional=("keyword",),
        options={
            "chat": OptionSpec("--chat", "multi"),
            "start_time": OptionSpec("--start-time"),
            "end_time": OptionSpec("--end-time"),
            "limit": OptionSpec("--limit"),
            "offset": OptionSpec("--offset"),
            "format": OptionSpec("--format"),
            "type": OptionSpec("--type"),
        },
        default_format="json",
    ),
    "contacts": CommandSpec(
        options={
            "query": OptionSpec("--query"),
            "detail": OptionSpec("--detail"),
            "limit": OptionSpec("--limit"),
            "format": OptionSpec("--format"),
        },
        default_format="json",
    ),
    "members": CommandSpec(
        positional=("group_name",),
        options={"format": OptionSpec("--format")},
        default_format="json",
    ),
    "stats": CommandSpec(
        positional=("chat_name",),
        options={
            "start_time": OptionSpec("--start-time"),
            "end_time": OptionSpec("--end-time"),
            "format": OptionSpec("--format"),
        },
        default_format="json",
    ),
    "invite-stats": CommandSpec(
        positional=("group_name",),
        options={
            "start_time": OptionSpec("--start-time"),
            "end_time": OptionSpec("--end-time"),
            "bind_identity": OptionSpec("--bind-identity", "multi"),
            "format": OptionSpec("--format"),
        },
        default_format="json",
    ),
    "export": CommandSpec(
        positional=("chat_name",),
        options={
            "format": OptionSpec("--format"),
            "output_path": OptionSpec("--output"),
            "start_time": OptionSpec("--start-time"),
            "end_time": OptionSpec("--end-time"),
            "limit": OptionSpec("--limit"),
        },
    ),
    "favorites": CommandSpec(
        options={
            "limit": OptionSpec("--limit"),
            "type": OptionSpec("--type"),
            "query": OptionSpec("--query"),
            "format": OptionSpec("--format"),
        },
        default_format="json",
    ),
    "unread": CommandSpec(
        options={"limit": OptionSpec("--limit"), "format": OptionSpec("--format")},
        default_format="json",
    ),
    "new-messages": CommandSpec(
        options={"format": OptionSpec("--format")},
        default_format="json",
    ),
}

WECHAT_V2_DAT_MAGIC = b"\x07\x08V2\x08\x07\x00\x04"


def _is_blank(value: Any) -> bool:
    return value is None or value == "" or value == []


def _stringify(value: Any) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    return str(value)


def build_cli_args(payload: dict[str, Any]) -> list[str]:
    """Build a safe argv list for the existing wechat-cli command group."""
    command = payload.get("command")
    if command not in COMMAND_SPECS:
        raise ValueError(f"Unsupported command: {command}")

    params = payload.get("params") or {}
    if not isinstance(params, dict):
        raise ValueError("params must be an object")

    spec = COMMAND_SPECS[command]
    allowed = set(spec.positional)
    allowed.update(spec.optional_positional)
    allowed.update((spec.options or {}).keys())
    unknown = sorted(set(params) - allowed)
    if unknown:
        raise ValueError(f"Unsupported parameter(s) for {command}: {', '.join(unknown)}")

    args = [command]
    for name in spec.positional:
        value = params.get(name)
        if _is_blank(value):
            raise ValueError(f"Missing required parameter: {name}")
        args.append(_stringify(value))
    for name in spec.optional_positional:
        value = params.get(name)
        if not _is_blank(value):
            args.append(_stringify(value).strip())

    if command == "search":
        validate_search_scope(
            params.get("keyword", ""),
            params.get("start_time", ""),
            params.get("end_time", ""),
        )

    options = spec.options or {}
    normalized = dict(params)
    if spec.default_format and _is_blank(normalized.get("format")):
        normalized["format"] = spec.default_format

    for name, opt in options.items():
        value = normalized.get(name)
        if _is_blank(value):
            continue
        if opt.kind == "bool":
            if bool(value):
                args.append(opt.flag)
        elif opt.kind == "multi":
            if not isinstance(value, list):
                raise ValueError(f"{name} must be a list")
            for item in value:
                if not _is_blank(item):
                    args.extend([opt.flag, _stringify(item)])
        else:
            args.extend([opt.flag, _stringify(value)])

    return args


def _cli_subprocess_argv(args: list[str]) -> list[str]:
    if getattr(sys, "frozen", False):
        return [sys.executable, *args]
    return [sys.executable, "-m", "wechat_cli.main", *args]


def _sns_web_internal_result(args: list[str]) -> dict[str, Any]:
    return {
        "ok": False,
        "returncode": 1,
        "command": ["wechat-cli", *args],
        "stdout": "",
        "stderr": "",
        "data": None,
        "error": {
            "code": "SNS_INTERNAL_ERROR",
            "message": "朋友圈命令返回了无效响应",
            "retryable": False,
        },
    }


def _execute_cli_args(
    args: list[str],
    *,
    response_mode: str = "",
) -> dict[str, Any]:
    env = {
        **os.environ,
        "PYTHONIOENCODING": "utf-8",
        "PYTHONUTF8": "1",
    }
    proc = subprocess.run(
        _cli_subprocess_argv(args),
        cwd=os.getcwd(),
        capture_output=True,
        stdin=subprocess.DEVNULL,
        text=True,
        encoding="utf-8",
        errors="replace",
        env=env,
        timeout=600,
    )
    data = None
    stdout = proc.stdout.strip()
    if stdout:
        try:
            data = json.loads(stdout)
        except json.JSONDecodeError:
            data = None
    if args and args[0] == "sns":
        if (
            proc.returncode == 0
            and isinstance(data, dict)
            and data.get("ok") is True
            and isinstance(data.get("data"), dict)
        ):
            return {
                "ok": True,
                "returncode": 0,
                "command": ["wechat-cli", *args],
                "stdout": "",
                "stderr": "",
                "data": data["data"],
            }

        if (
            isinstance(data, dict)
            and data.get("ok") is False
            and isinstance(data.get("error"), dict)
        ):
            error = data["error"]
            code = error.get("code")
            message = error.get("message")
            retryable = error.get("retryable")
            if not isinstance(code, str):
                return _sns_web_internal_result(args)
            expected_returncode = sns_error_exit_code(code)
            if expected_returncode is None or proc.returncode != expected_returncode:
                return _sns_web_internal_result(args)
            if not isinstance(message, str):
                return _sns_web_internal_result(args)
            if not isinstance(retryable, bool):
                return _sns_web_internal_result(args)
            safe_error = {
                "code": code,
                "message": message,
                "retryable": retryable,
            }
            safe_details = sanitize_sns_error_details(code, error.get("details"))
            if safe_details:
                safe_error["details"] = safe_details
            return {
                "ok": False,
                "returncode": expected_returncode,
                "command": ["wechat-cli", *args],
                "stdout": "",
                "stderr": "",
                "data": None,
                "error": safe_error,
            }
        return _sns_web_internal_result(args)

    if response_mode == "summary" and isinstance(data, dict):
        data = dict(data)
        for duplicate_key in ("messages", "saved_media", "save_dir"):
            data.pop(duplicate_key, None)

    return {
        "ok": proc.returncode == 0,
        "returncode": proc.returncode,
        "command": ["wechat-cli", *args],
        "stdout": "" if response_mode == "summary" and data is not None else proc.stdout,
        "stderr": proc.stderr,
        "data": data,
    }


def run_cli_command(payload: dict[str, Any]) -> dict[str, Any]:
    response_mode = payload.get("response_mode", "")
    if response_mode not in {"", "summary"}:
        raise ValueError(f"Unsupported response mode: {response_mode}")
    if response_mode == "summary" and payload.get("command") != "history":
        raise ValueError("Summary response mode is only available for history")

    args = build_cli_args(payload)
    return _execute_cli_args(args, response_mode=response_mode)


def _safe_ai_package_download_name(chat_name: str) -> str:
    safe = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "_", chat_name).strip(" ._")
    safe = (safe or "微信聊天")[:80]
    return f"{safe}-AI资料包.zip"


def _remove_ai_package_file(path: str) -> None:
    target = Path(path).resolve()
    package_root = Path(AI_PACKAGE_DIR).resolve()
    if target.parent != package_root:
        return
    try:
        target.unlink()
    except FileNotFoundError:
        pass


def _expire_ai_package_downloads(now: float | None = None) -> None:
    now = time.time() if now is None else now
    expired = []
    with _AI_PACKAGE_DOWNLOADS_LOCK:
        for token, entry in list(_AI_PACKAGE_DOWNLOADS.items()):
            if now - entry["created_at"] >= AI_PACKAGE_EXPIRES_SECONDS:
                expired.append(_AI_PACKAGE_DOWNLOADS.pop(token))
    for entry in expired:
        _remove_ai_package_file(entry["path"])
    package_root = Path(AI_PACKAGE_DIR)
    try:
        entries = list(package_root.iterdir())
    except FileNotFoundError:
        return
    for target in entries:
        if target.suffix not in {".zip", ".part"}:
            continue
        try:
            if target.resolve().parent != package_root.resolve():
                continue
            if now - target.stat().st_mtime >= AI_PACKAGE_EXPIRES_SECONDS:
                target.unlink()
        except (FileNotFoundError, OSError):
            continue


def run_ai_package_request(payload: dict[str, Any]) -> dict[str, Any]:
    """Run the CLI with a server-owned output path and issue a one-time URL."""
    allowed = {"chat_name", "start_time", "end_time", "transcribe_voice"}
    unknown = sorted(set(payload) - allowed)
    if unknown:
        raise ValueError(f"不支持的 AI 资料包参数: {', '.join(unknown)}")
    chat_name = payload.get("chat_name")
    if not isinstance(chat_name, str) or not chat_name.strip():
        raise ValueError("请先选择一个聊天")
    start_time = payload.get("start_time") or ""
    end_time = payload.get("end_time") or ""
    for label, value in (("开始日期", start_time), ("结束日期", end_time)):
        if value:
            try:
                datetime.strptime(str(value), "%Y-%m-%d")
            except ValueError as exc:
                raise ValueError(f"{label}必须为 YYYY-MM-DD") from exc
    if start_time and end_time and start_time > end_time:
        raise ValueError("开始日期不能晚于结束日期")
    transcribe = payload.get("transcribe_voice", True)
    if not isinstance(transcribe, bool):
        raise ValueError("语音转文字选项必须为布尔值")

    _expire_ai_package_downloads()
    Path(AI_PACKAGE_DIR).mkdir(parents=True, exist_ok=True)
    token = secrets.token_urlsafe(24)
    target = str((Path(AI_PACKAGE_DIR) / f"{token}.zip").resolve())
    args = ["ai-package", chat_name.strip()]
    if start_time:
        args.extend(["--start-time", str(start_time)])
    if end_time:
        args.extend(["--end-time", str(end_time)])
    args.extend(["--output", target, "--include-copy-data"])
    if not transcribe:
        args.append("--no-transcribe-voice")

    result = _execute_cli_args(args)
    data = result.get("data")
    if not result.get("ok") or not isinstance(data, dict):
        _remove_ai_package_file(target)
        return {
            "ok": False,
            "error": result.get("stderr") or result.get("stdout") or "AI 资料包生成失败",
        }
    if not Path(target).is_file():
        return {"ok": False, "error": "AI 资料包生成后未找到压缩文件"}

    filename = _safe_ai_package_download_name(
        str(data.get("chat") or chat_name)
    )
    with _AI_PACKAGE_DOWNLOADS_LOCK:
        _AI_PACKAGE_DOWNLOADS[token] = {
            "path": target,
            "filename": filename,
            "created_at": time.time(),
        }
    return {
        "ok": True,
        "chat": data.get("chat") or chat_name,
        "username": data.get("username") or "",
        "message_count": int(data.get("message_count") or 0),
        "asset_count": int(data.get("asset_count") or 0),
        "transcription_count": int(data.get("transcription_count") or 0),
        "failures": data.get("failures"),
        "copy_text": str(data.get("copy_text") or ""),
        "key_copy_text": str(data.get("key_copy_text") or ""),
        "download_url": f"/api/ai-package/{token}",
        "filename": filename,
    }


def claim_ai_package_download(token: str) -> dict[str, Any] | None:
    """Claim a prepared download exactly once."""
    if not re.fullmatch(r"[A-Za-z0-9_-]{20,80}", token or ""):
        return None
    _expire_ai_package_downloads()
    with _AI_PACKAGE_DOWNLOADS_LOCK:
        entry = _AI_PACKAGE_DOWNLOADS.pop(token, None)
    if entry is None or not Path(entry["path"]).is_file():
        return None
    return entry


def _account_name_from_db_dir(path: str) -> str:
    normalized = ntpath.normpath(path)
    if ntpath.basename(normalized) == "db_storage":
        return ntpath.basename(ntpath.dirname(normalized))
    return ntpath.basename(normalized)


def db_dir_candidates_payload() -> dict[str, Any]:
    candidates = detect_db_dir_candidates()
    return {
        "ok": True,
        "count": len(candidates),
        "candidates": [
            {
                "path": path,
                "account": _account_name_from_db_dir(path),
                "exists": os.path.isdir(path),
            }
            for path in candidates
        ],
    }


def health_payload() -> dict[str, Any]:
    """Report the local server version without reading account data."""
    from ..version import APP_VERSION, BUILD_ID, PRODUCT

    return {
        "status": "ok",
        "product": PRODUCT,
        "version": APP_VERSION,
        "build_id": BUILD_ID,
        "mode": "local",
    }


def status_payload() -> dict[str, Any]:
    cfg: dict[str, Any] = {}
    if os.path.exists(CONFIG_FILE):
        try:
            with open(CONFIG_FILE, encoding="utf-8") as f:
                cfg = json.load(f)
        except (OSError, json.JSONDecodeError):
            cfg = {}

    keys_count = 0
    if os.path.exists(KEYS_FILE):
        try:
            with open(KEYS_FILE, encoding="utf-8") as f:
                keys = json.load(f)
            keys_count = len([k for k in keys if not str(k).startswith("_")])
        except (OSError, json.JSONDecodeError):
            keys_count = 0

    db_dir = cfg.get("db_dir", "")
    return {
        "initialized": os.path.exists(CONFIG_FILE) and os.path.exists(KEYS_FILE),
        "state_dir": STATE_DIR,
        "config_file": CONFIG_FILE,
        "keys_file": KEYS_FILE,
        "db_dir": db_dir,
        "db_dir_exists": bool(db_dir and os.path.isdir(db_dir)),
        "keys_count": keys_count,
    }


def _validate_avatar_url(url: str) -> str:
    parsed = urlparse(url)
    host = (parsed.hostname or "").lower().rstrip(".")
    allowed = any(
        host == suffix or host.endswith(f".{suffix}")
        for suffix in AVATAR_ALLOWED_HOSTS
    )
    if parsed.scheme != "https" or not allowed:
        raise PermissionError("avatar URL is not an allowed WeChat image URL")
    return url


def _normalize_avatar_url(url: str) -> str:
    """Upgrade legacy HTTP URLs only for already allowlisted WeChat avatar hosts."""
    parsed = urlparse(url)
    host = (parsed.hostname or "").lower().rstrip(".")
    allowed = any(
        host == suffix or host.endswith(f".{suffix}")
        for suffix in AVATAR_ALLOWED_HOSTS
    )
    if parsed.scheme == "http" and allowed:
        url = parsed._replace(scheme="https").geturl()
    return _validate_avatar_url(url)


def avatar_remote_payload(url: str) -> dict[str, Any]:
    """Fetch and validate one WeChat avatar through the localhost server."""
    global _AVATAR_CACHE_BYTES
    url = _normalize_avatar_url(url)
    while True:
        with _AVATAR_CACHE_LOCK:
            cached = _AVATAR_CACHE.get(url)
            if cached is not None:
                return cached
            pending = _AVATAR_CACHE_INFLIGHT.get(url)
            if pending is None:
                pending = Event()
                _AVATAR_CACHE_INFLIGHT[url] = pending
                leader = True
            else:
                leader = False
        if leader:
            break
        pending.wait()

    try:
        request = Request(
            url,
            headers={
                "Accept": "image/*",
                "User-Agent": "wechat-cli-web",
            },
        )
        with urlopen(request, timeout=5) as response:
            _validate_avatar_url(response.geturl())
            declared_type = (response.headers.get("Content-Type") or "").split(";", 1)[0].lower()
            if not declared_type.startswith("image/"):
                raise ValueError("avatar response is not an image")
            declared_size = response.headers.get("Content-Length")
            if declared_size and declared_size.isdigit() and int(declared_size) > AVATAR_MAX_BYTES:
                raise ValueError("avatar response is too large")
            raw = response.read(AVATAR_MAX_BYTES + 1)

        if len(raw) > AVATAR_MAX_BYTES:
            raise ValueError("avatar response is too large")
        detected_type = _image_content_type(raw, "")
        if not detected_type:
            raise ValueError("avatar payload is not a supported image")

        payload = {"body": raw, "content_type": detected_type}
        with _AVATAR_CACHE_LOCK:
            existing = _AVATAR_CACHE.get(url)
            if existing is not None:
                return existing
            while _AVATAR_CACHE and (
                len(_AVATAR_CACHE) >= AVATAR_CACHE_LIMIT
                or _AVATAR_CACHE_BYTES + len(raw) > AVATAR_CACHE_MAX_BYTES
            ):
                removed = _AVATAR_CACHE.pop(next(iter(_AVATAR_CACHE)))
                _AVATAR_CACHE_BYTES -= len(removed["body"])
            _AVATAR_CACHE[url] = payload
            _AVATAR_CACHE_BYTES += len(raw)
        return payload
    finally:
        with _AVATAR_CACHE_LOCK:
            pending = _AVATAR_CACHE_INFLIGHT.pop(url, None)
            if pending is not None:
                pending.set()


def profile_payload() -> dict[str, str]:
    """Return the current account identity shown in the web console."""
    db_dir = str(status_payload().get("db_dir") or "")
    if not db_dir:
        return {"username": "", "display_name": "", "avatar_url": ""}

    account_dir = os.path.basename(os.path.dirname(os.path.normpath(db_dir)))
    match = re.fullmatch(r"(.+?)_[0-9a-fA-F]{4,}", account_dir)
    candidates = [account_dir]
    if match and match.group(1) != account_dir:
        candidates.append(match.group(1))
    if not account_dir:
        return {"username": "", "display_name": "", "avatar_url": ""}

    for candidate in candidates:
        result = run_cli_command({
            "command": "contacts",
            "params": {"detail": candidate},
        })
        data = result.get("data") if result.get("ok") else {}
        if not isinstance(data, dict) or data.get("username") != candidate:
            continue
        return {
            "username": candidate,
            "display_name": str(
                data.get("remark") or data.get("nick_name") or candidate
            ),
            "avatar_url": str(
                data.get("avatar") or data.get("avatar_url") or ""
            ),
        }
    return {
        "username": account_dir,
        "display_name": account_dir,
        "avatar_url": "",
    }


def _static_bytes(name: str) -> bytes:
    root = resources.files("wechat_cli.web.static")
    return root.joinpath(name).read_bytes()


def media_file_payload(path: str) -> dict[str, Any]:
    """Return local media bytes if path is inside the configured WeChat data root."""
    status = status_payload()
    db_dir = status.get("db_dir", "")
    if not db_dir:
        raise PermissionError("wechat db_dir is not configured")
    cfg = {}
    try:
        with open(CONFIG_FILE, encoding="utf-8") as source:
            cfg = json.load(source)
    except (OSError, json.JSONDecodeError):
        pass
    return read_media_file_payload(
        path,
        db_dir=db_dir,
        image_aes_key=cfg.get("image_aes_keys") or cfg.get("image_aes_key"),
        image_xor_key=cfg.get("image_xor_key"),
    )


def _decode_media_bytes(raw: bytes, path: str) -> tuple[bytes, str]:
    return _core_decode_media_bytes(raw, path)


def _media_download_filename(path: str, content_type: str) -> str:
    return _core_media_download_filename(path, content_type)


def _image_content_type(raw: bytes, path: str) -> str:
    if raw.startswith(b"\xff\xd8\xff"):
        return "image/jpeg"
    if raw.startswith(b"\x89PNG\r\n\x1a\n"):
        return "image/png"
    if raw.startswith((b"GIF87a", b"GIF89a")):
        return "image/gif"
    if raw.startswith(b"RIFF") and raw[8:12] == b"WEBP":
        return "image/webp"
    heif_type = _heif_content_type(raw)
    if heif_type:
        return heif_type
    guessed = mimetypes.guess_type(path)[0] or ""
    return guessed if guessed.startswith("image/") else ""


def _heif_content_type(raw: bytes) -> str:
    if len(raw) < 12 or raw[4:8] != b"ftyp":
        return ""
    brand = raw[8:12]
    if brand in {b"avif", b"avis"}:
        return "image/avif"
    if brand in {b"heic", b"heix"}:
        return "image/heic"
    if brand in {b"mif1", b"msf1", b"hevc", b"heim", b"heis"}:
        return "image/heif"
    return ""


def _decode_wechat_dat_image(raw: bytes) -> tuple[bytes, str] | None:
    signatures = [
        (b"\xff\xd8\xff", "image/jpeg"),
        (b"\x89PNG\r\n\x1a\n", "image/png"),
        (b"GIF87a", "image/gif"),
        (b"GIF89a", "image/gif"),
        (b"RIFF", "image/webp"),
    ]
    if not raw:
        return None
    for signature, content_type in signatures:
        key = raw[0] ^ signature[0]
        decoded = bytes(byte ^ key for byte in raw)
        if content_type == "image/webp":
            if decoded.startswith(b"RIFF") and decoded[8:12] == b"WEBP":
                return decoded, content_type
        elif decoded.startswith(signature):
            return decoded, content_type
    return None


def _decode_wechat_v2_dat_image(raw: bytes) -> tuple[bytes, str] | None:
    if not raw.startswith(WECHAT_V2_DAT_MAGIC):
        return None
    candidates = []
    if len(raw) > 31:
        candidates.append(raw[15:-16])
        candidates.append(raw[31:])
    if len(raw) > 15:
        candidates.append(raw[15:])
    for candidate in candidates:
        if not candidate:
            continue
        content_type = _image_content_type(candidate, "")
        if content_type:
            return candidate, content_type
    return None


def _media_placeholder_svg(filename: str) -> bytes:
    safe_name = (
        filename.replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
        .replace('"', "&quot;")
    )
    svg = f"""<svg xmlns="http://www.w3.org/2000/svg" width="640" height="360" viewBox="0 0 640 360">
  <rect width="640" height="360" rx="24" fill="#f3eee5"/>
  <rect x="28" y="28" width="584" height="304" rx="18" fill="#fffaf0" stroke="#d8d2c8" stroke-width="2"/>
  <text x="320" y="150" text-anchor="middle" font-family="Segoe UI, Microsoft YaHei, sans-serif" font-size="30" font-weight="700" fill="#116b5f">图片已定位</text>
  <text x="320" y="196" text-anchor="middle" font-family="Segoe UI, Microsoft YaHei, sans-serif" font-size="20" fill="#6f6a61">现代微信 V2 DAT 暂不能直接解码预览</text>
  <text x="320" y="238" text-anchor="middle" font-family="Consolas, monospace" font-size="18" fill="#b7791f">{safe_name}</text>
</svg>"""
    return svg.encode("utf-8")


def _launch_query_value(
    query: dict[str, list[str]],
    name: str,
    *,
    required: bool = False,
) -> str:
    values = query.get(name, [])
    value = values[0] if values else ""
    if not isinstance(value, str):
        raise ValueError(f"{name} must be a string")
    value = value.strip()
    if required and not value:
        raise ValueError(f"{name} is required")
    return value


def _launch_get_payload(service, path: str, query: dict[str, list[str]]) -> dict[str, Any]:
    if path == "/api/launch/campaigns":
        return {"ok": True, "data": {"campaigns": service.list_campaigns()}}
    if path == "/api/launch/dashboard":
        campaign_id = _launch_query_value(query, "campaign_id", required=True)
        invite_window = _launch_query_value(query, "invite_window") or "campaign"
        return {
            "ok": True,
            "data": service.dashboard(
                campaign_id,
                invite_window=invite_window,
            ),
        }
    if path == "/api/launch/rooms":
        campaign_id = _launch_query_value(query, "campaign_id")
        search = _launch_query_value(query, "query")
        health = _launch_query_value(query, "health")
        rooms = service.list_rooms(
            campaign_id=campaign_id or None,
            query=search,
            health=health,
        )
        return {"ok": True, "data": {"rooms": rooms}}
    if path == "/api/launch/trace":
        campaign_id = _launch_query_value(query, "campaign_id", required=True)
        filters = {
            key: _launch_query_value(query, key)
            for key in ("room_id", "query", "method", "start_ts", "end_ts")
            if _launch_query_value(query, key)
        }
        return {"ok": True, "data": service.trace(campaign_id, filters)}
    if path == "/api/launch/room-members":
        campaign_id = _launch_query_value(query, "campaign_id", required=True)
        room_id = _launch_query_value(query, "room_id", required=True)
        return {
            "ok": True,
            "data": service.room_members(campaign_id, room_id),
        }
    if path == "/api/launch/member-detail":
        campaign_id = _launch_query_value(query, "campaign_id", required=True)
        room_id = _launch_query_value(query, "room_id", required=True)
        member_key = _launch_query_value(query, "member_key", required=True)
        return {
            "ok": True,
            "data": service.member_detail(campaign_id, room_id, member_key),
        }
    if path == "/api/launch/archive/room":
        room_id = _launch_query_value(query, "room_id", required=True)
        return {"ok": True, "data": service.archive_room(room_id)}
    if path == "/api/launch/archive/member":
        room_id = _launch_query_value(query, "room_id", required=True)
        member_key = _launch_query_value(query, "member_key", required=True)
        return {
            "ok": True,
            "data": service.archive_member(room_id, member_key),
        }
    if path == "/api/launch/health":
        return {"ok": True, "data": service.watcher_health()}
    raise ValueError(f"unsupported launch route: {path}")


def _launch_post_payload(service, path: str, payload: dict[str, Any]) -> dict[str, Any]:
    if not isinstance(payload, dict):
        raise ValueError("payload must be an object")
    if path == "/api/launch/campaigns":
        name = payload.get("name")
        if not isinstance(name, str) or not name.strip():
            raise ValueError("name is required")
        return {"ok": True, "data": service.create_campaign(name.strip())}
    if path == "/api/launch/campaigns/end":
        campaign_id = payload.get("campaign_id")
        if not isinstance(campaign_id, str) or not campaign_id.strip():
            raise ValueError("campaign_id is required")
        service.end_campaign(campaign_id.strip())
        return {"ok": True, "data": {"campaign_id": campaign_id.strip()}}
    if path == "/api/launch/campaigns/rooms/add":
        campaign_id = payload.get("campaign_id")
        room_ids = payload.get("room_ids")
        if not isinstance(campaign_id, str) or not campaign_id.strip():
            raise ValueError("campaign_id is required")
        if (
            not isinstance(room_ids, list)
            or not room_ids
            or any(not isinstance(item, str) or not item.strip() for item in room_ids)
        ):
            raise ValueError("room_ids must be a non-empty list of strings")
        rooms = service.add_rooms(
            campaign_id.strip(),
            [item.strip() for item in room_ids],
        )
        return {"ok": True, "data": {"rooms": rooms}}
    if path == "/api/launch/campaigns/rooms/remove":
        campaign_id = payload.get("campaign_id")
        room_id = payload.get("room_id")
        if not isinstance(campaign_id, str) or not campaign_id.strip():
            raise ValueError("campaign_id is required")
        if not isinstance(room_id, str) or not room_id.strip():
            raise ValueError("room_id is required")
        service.remove_room(campaign_id.strip(), room_id.strip())
        return {
            "ok": True,
            "data": {"campaign_id": campaign_id.strip(), "room_id": room_id.strip()},
        }
    if path == "/api/launch/campaigns/invite-backfill":
        campaign_id = payload.get("campaign_id")
        start_ts = payload.get("start_ts")
        end_ts = payload.get("end_ts")
        if not isinstance(campaign_id, str) or not campaign_id.strip():
            raise ValueError("campaign_id is required")
        if isinstance(start_ts, bool) or not isinstance(start_ts, int):
            raise ValueError("start_ts must be an integer")
        if isinstance(end_ts, bool) or not isinstance(end_ts, int):
            raise ValueError("end_ts must be an integer")
        if start_ts < 0 or end_ts < start_ts:
            raise ValueError("invalid invite backfill time window")
        return {
            "ok": True,
            "data": service.backfill_campaign_invites(
                campaign_id.strip(),
                start_ts=start_ts,
                end_ts=end_ts,
            ),
        }
    if path in {
        "/api/launch/archive/start",
        "/api/launch/archive/stop",
        "/api/launch/archive/refresh",
    }:
        room_id = str(payload.get("room_id") or "").strip()
        if not room_id:
            raise ValueError("room_id is required")
        method = {
            "/api/launch/archive/start": service.start_archive,
            "/api/launch/archive/stop": service.stop_archive,
            "/api/launch/archive/refresh": service.refresh_archive,
        }[path]
        return {"ok": True, "data": method(room_id)}
    if path == "/api/launch/archive/manual-confirm":
        room_id = str(payload.get("room_id") or "").strip()
        episode_id = str(payload.get("episode_id") or "").strip()
        reason = str(payload.get("reason") or "").strip()
        if not room_id or not episode_id or not reason:
            raise ValueError("room_id, episode_id and reason are required")
        unresolved = payload.get("unresolved", False)
        if not isinstance(unresolved, bool):
            raise ValueError("unresolved must be a boolean")
        inviter_username = payload.get("inviter_username")
        if inviter_username is not None and not isinstance(inviter_username, str):
            raise ValueError("inviter_username must be a string or null")
        method = payload.get("method")
        if method not in {None, "direct", "qr", "unattributed_qr"}:
            raise ValueError("invalid method")
        event_time = payload.get("event_time")
        if event_time is not None and (isinstance(event_time, bool) or not isinstance(event_time, int)):
            raise ValueError("event_time must be an integer or null")
        return {
            "ok": True,
            "data": service.manual_confirm_archive(
                room_id,
                episode_id,
                inviter_username=(
                    inviter_username.strip() if isinstance(inviter_username, str) else None
                ),
                unresolved=unresolved,
                method=method,
                event_time=event_time,
                reason=reason,
            ),
        }
    if path == "/api/launch/rescan":
        return {"ok": True, "data": service.rescan()}
    raise ValueError(f"unsupported launch route: {path}")


def _launch_error_payload(code: str, message: str, *, retryable: bool) -> dict[str, Any]:
    return {
        "ok": False,
        "error": {
            "code": code,
            "message": message,
            "retryable": retryable,
        },
    }


def _configure_launch_ops(httpd) -> None:
    httpd.launch_ops_service = None
    httpd.launch_ops_watcher = None

    try:
        store = LaunchOpsStore(DEFAULT_STORE_PATH)
    except Exception as exc:
        httpd.launch_ops_error = {
            "code": "launch_ops_unavailable",
            "message": str(exc),
            "retryable": True,
        }
        return

    try:
        ArchiveMigrator(store).migrate_v2(now=int(time.time()))
    except Exception as exc:
        httpd.launch_ops_error = {
            "code": "archive_migration_failed",
            "message": str(exc),
            "retryable": True,
        }
        return

    try:
        ctx = AppContext()
        source = LocalWeChatSourceAdapter(ctx)
        watcher = LaunchOpsWatcher(store, source)
        service = LaunchOpsService(store, source, watcher=watcher)
        service.ensure_default_campaign()
        watcher.start()
    except FileNotFoundError as exc:
        httpd.launch_ops_error = {
            "code": "wechat_data_unavailable",
            "message": str(exc),
            "retryable": True,
        }
        return
    except PermissionError as exc:
        httpd.launch_ops_error = {
            "code": "wechat_data_unreadable",
            "message": str(exc),
            "retryable": True,
        }
        return
    except RuntimeError as exc:
        code = "key_refresh_required" if "密钥" in str(exc) else "launch_ops_unavailable"
        httpd.launch_ops_error = {
            "code": code,
            "message": str(exc),
            "retryable": True,
        }
        return
    except Exception as exc:
        httpd.launch_ops_error = {
            "code": "launch_ops_unavailable",
            "message": str(exc),
            "retryable": True,
        }
        return
    httpd.launch_ops_service = service
    httpd.launch_ops_watcher = watcher
    httpd.launch_ops_error = {}


def _stop_launch_ops(httpd) -> None:
    watcher = getattr(httpd, "launch_ops_watcher", None)
    if watcher is not None:
        watcher.stop()


class WeChatWebHandler(BaseHTTPRequestHandler):
    server_version = "wechat-cli-web/0.1"


    def do_GET(self) -> None:  # noqa: N802
        if not self._validate_request_source(require_origin=False):
            return
        parsed = urlparse(self.path)
        if parsed.path == "/api/health":
            self._send_json(health_payload())
            return
        if parsed.path in ("", "/"):
            self._send_bytes(_static_bytes("index.html"), "text/html; charset=utf-8")
            return
        if parsed.path in {"/launch", "/launch/"}:
            self._send_bytes(_static_bytes("launch.html"), "text/html; charset=utf-8")
            return
        if parsed.path.startswith("/api/launch/"):
            service = getattr(self.server, "launch_ops_service", None)
            if service is None:
                error = getattr(self.server, "launch_ops_error", {}) or {}
                if parsed.path == "/api/launch/health":
                    self._send_json(
                        {
                            "ok": True,
                            "data": {
                                "status": "error",
                                "stale": True,
                                "last_attempt_at": None,
                                "last_success_at": None,
                                "last_event_at": None,
                                "error_code": error.get(
                                    "code", "launch_ops_unavailable"
                                ),
                                "error_message": error.get(
                                    "message", "Launch Ops 尚未就绪"
                                ),
                            },
                        }
                    )
                else:
                    self._send_json(
                        _launch_error_payload(
                            error.get("code", "launch_ops_unavailable"),
                            error.get("message", "Launch Ops 尚未就绪"),
                            retryable=bool(error.get("retryable", True)),
                        ),
                        HTTPStatus.SERVICE_UNAVAILABLE,
                    )
                return
            try:
                self._send_json(
                    _launch_get_payload(service, parsed.path, parse_qs(parsed.query))
                )
            except ValueError as exc:
                not_found = str(exc).startswith("unsupported launch route:")
                self._send_json(
                    _launch_error_payload(
                        "LAUNCH_ROUTE_NOT_FOUND" if not_found else "INVALID_LAUNCH_REQUEST",
                        str(exc),
                        retryable=False,
                    ),
                    HTTPStatus.NOT_FOUND if not_found else HTTPStatus.BAD_REQUEST,
                )
            except Exception as exc:
                self._send_json(
                    _launch_error_payload(
                        "LAUNCH_REQUEST_FAILED",
                        str(exc),
                        retryable=True,
                    ),
                    HTTPStatus.INTERNAL_SERVER_ERROR,
                )
            return
        if parsed.path == "/api/status":
            self._send_json(status_payload())
            return
        if parsed.path == "/api/profile":
            self._send_json(profile_payload())
            return
        if parsed.path == "/api/db-dirs":
            self._send_json(db_dir_candidates_payload())
            return
        if parsed.path.startswith("/api/ai-package/"):
            token = parsed.path.rsplit("/", 1)[-1]
            entry = claim_ai_package_download(token)
            if entry is None:
                self.send_error(HTTPStatus.NOT_FOUND)
                return
            self._send_file_once(
                entry["path"],
                "application/zip",
                entry["filename"],
            )
            return
        if parsed.path == "/api/sns/media" or parsed.path.startswith(_SNS_MEDIA_PATH_PREFIX):
            try:
                post_id, media_id = _parse_sns_media_path(parsed.path)
                variant, component, download = _parse_sns_media_query(parsed.query)
                sns_ctx = build_sns_context()
                payload = read_sns_media(
                    sns_ctx,
                    post_id,
                    media_id,
                    variant=variant,
                    component=component,
                )
                server_filename = f"sns-{post_id}-{media_id}.mp4"
                self._send_bytes(
                    payload["body"],
                    payload["content_type"],
                    server_filename,
                    disposition="attachment" if download else "inline",
                )
            except SnsError as exc:
                status = _SNS_MEDIA_HTTP_STATUS.get(
                    exc.code,
                    HTTPStatus.INTERNAL_SERVER_ERROR,
                )
                if exc.code not in _SNS_MEDIA_HTTP_STATUS:
                    exc = SnsError(
                        "SNS_INTERNAL_ERROR",
                        "朋友圈本地媒体读取发生内部错误",
                        retryable=False,
                    )
                    status = HTTPStatus.INTERNAL_SERVER_ERROR
                self._send_json(_sns_media_error_payload(exc), status)
            except Exception:
                exc = SnsError(
                    "SNS_INTERNAL_ERROR",
                    "朋友圈本地媒体读取发生内部错误",
                    retryable=False,
                )
                self._send_json(
                    _sns_media_error_payload(exc),
                    HTTPStatus.INTERNAL_SERVER_ERROR,
                )
            return
        if parsed.path == "/api/media":
            path = parse_qs(parsed.query).get("path", [""])[0]
            try:
                payload = media_file_payload(path)
                self._send_bytes(payload["body"], payload["content_type"], payload.get("filename"))
            except PermissionError:
                self.send_error(HTTPStatus.FORBIDDEN)
            except FileNotFoundError:
                self.send_error(HTTPStatus.NOT_FOUND)
            return
        if parsed.path == "/api/avatar":
            url = parse_qs(parsed.query).get("url", [""])[0]
            try:
                payload = avatar_remote_payload(url)
                self._send_bytes(
                    payload["body"],
                    payload["content_type"],
                    cache_control="private, max-age=3600",
                )
            except PermissionError:
                self.send_error(HTTPStatus.FORBIDDEN)
            except ValueError:
                self.send_error(HTTPStatus.BAD_REQUEST)
            except (URLError, OSError):
                self.send_error(HTTPStatus.BAD_GATEWAY)
            return
        if parsed.path.startswith("/static/"):
            name = os.path.basename(parsed.path)
            if name not in STATIC_ASSET_NAMES:
                self.send_error(HTTPStatus.NOT_FOUND)
                return
            content_type = mimetypes.guess_type(name)[0] or "application/octet-stream"
            if name.endswith(".css"):
                content_type += "; charset=utf-8"
            if name.endswith(".js"):
                content_type = "application/javascript; charset=utf-8"
            self._send_bytes(_static_bytes(name), content_type)
            return
        self.send_error(HTTPStatus.NOT_FOUND)

    def do_POST(self) -> None:  # noqa: N802
        if not self._validate_request_source(require_origin=True):
            return
        parsed = urlparse(self.path)
        if parsed.path.startswith("/api/launch/"):
            service = getattr(self.server, "launch_ops_service", None)
            if service is None:
                error = getattr(self.server, "launch_ops_error", {}) or {}
                self._discard_rejected_request_body()
                self._send_json(
                    _launch_error_payload(
                        error.get("code", "launch_ops_unavailable"),
                        error.get("message", "Launch Ops 尚未就绪"),
                        retryable=bool(error.get("retryable", True)),
                    ),
                    HTTPStatus.SERVICE_UNAVAILABLE,
                )
                return
            try:
                payload = self._read_json()
                self._send_json(_launch_post_payload(service, parsed.path, payload))
            except ValueError as exc:
                not_found = str(exc).startswith("unsupported launch route:")
                self._send_json(
                    _launch_error_payload(
                        "LAUNCH_ROUTE_NOT_FOUND" if not_found else "INVALID_LAUNCH_REQUEST",
                        str(exc),
                        retryable=False,
                    ),
                    HTTPStatus.NOT_FOUND if not_found else HTTPStatus.BAD_REQUEST,
                )
            except Exception as exc:
                self._send_json(
                    _launch_error_payload(
                        "LAUNCH_REQUEST_FAILED",
                        str(exc),
                        retryable=True,
                    ),
                    HTTPStatus.INTERNAL_SERVER_ERROR,
                )
            return
        if parsed.path not in {"/api/run", "/api/ai-package"}:
            self.send_error(HTTPStatus.NOT_FOUND)
            return
        try:
            payload = self._read_json()
            result = (
                run_ai_package_request(payload)
                if parsed.path == "/api/ai-package"
                else run_cli_command(payload)
            )
            self._send_json(result, HTTPStatus.OK if result["ok"] else HTTPStatus.BAD_REQUEST)
        except ValueError as exc:
            self._send_json({"ok": False, "error": str(exc)}, HTTPStatus.BAD_REQUEST)
        except subprocess.TimeoutExpired:
            self._send_json({"ok": False, "error": "Command timed out"}, HTTPStatus.REQUEST_TIMEOUT)
        except Exception as exc:  # keep local console resilient
            self._send_json({"ok": False, "error": str(exc)}, HTTPStatus.INTERNAL_SERVER_ERROR)

    def _read_json(self) -> dict[str, Any]:
        length = int(self.headers.get("Content-Length", "0") or "0")
        if length > 1024 * 1024:
            raise ValueError("Request body too large")
        raw = self.rfile.read(length)
        try:
            payload = json.loads(raw.decode("utf-8"))
        except json.JSONDecodeError as exc:
            raise ValueError("Invalid JSON body") from exc
        if not isinstance(payload, dict):
            raise ValueError("Request body must be an object")
        return payload

    def _discard_rejected_request_body(self, max_bytes: int = 64 * 1024) -> bool:
        """Drain a small rejected request body so Windows can return HTTP cleanly."""
        if self.command not in {"POST", "PUT", "PATCH"}:
            return True
        if self.headers.get("Transfer-Encoding"):
            self.close_connection = True
            return False
        raw_length = self.headers.get("Content-Length")
        if not raw_length:
            return True
        try:
            length = int(raw_length)
        except (TypeError, ValueError):
            self.close_connection = True
            return False
        if length < 0 or length > max_bytes:
            self.close_connection = True
            return False
        remaining = length
        while remaining:
            chunk = self.rfile.read(min(64 * 1024, remaining))
            if not chunk:
                self.close_connection = True
                return False
            remaining -= len(chunk)
        return True

    def _validate_request_source(self, *, require_origin: bool) -> bool:
        server_port = int(self.server.server_address[1])
        allowed = _is_local_request_source(
            self.headers.get("Host", ""),
            self.headers.get("Origin", ""),
            server_port,
            require_origin=require_origin,
            allowed_hosts=getattr(self.server, "allowed_request_hosts", None),
        )
        if not allowed:
            self._discard_rejected_request_body()
            self._send_json(
                {
                    "ok": False,
                    "error": {
                        "code": "FORBIDDEN_REQUEST_SOURCE",
                        "message": "Forbidden request source.",
                        "retryable": False,
                    },
                },
                HTTPStatus.FORBIDDEN,
            )
        return allowed

    def end_headers(self) -> None:
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header(
            "Content-Security-Policy",
            "default-src 'self'; img-src 'self' data: blob:; "
            "media-src 'self' blob:; "
            "style-src 'self' 'unsafe-inline'; script-src 'self'; "
            "connect-src 'self'; object-src 'none'; base-uri 'none'; "
            "frame-ancestors 'none'",
        )
        super().end_headers()

    def _send_json(self, payload: dict[str, Any], status: HTTPStatus = HTTPStatus.OK) -> None:
        raw = json.dumps(payload, ensure_ascii=False, indent=2).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(raw)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(raw)

    def _send_bytes(
        self,
        raw: bytes,
        content_type: str,
        filename: str | None = None,
        *,
        disposition: str = "inline",
        cache_control: str = "no-store",
    ) -> None:
        if disposition not in {"inline", "attachment"}:
            raise ValueError("unsupported content disposition")
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(raw)))
        self.send_header("Cache-Control", cache_control)
        if filename:
            ascii_name = filename.encode("ascii", "ignore").decode("ascii") or "wechat-media"
            self.send_header(
                "Content-Disposition",
                f"{disposition}; filename=\"{ascii_name}\"; filename*=UTF-8''{quote(filename)}",
            )
        self.end_headers()
        self.wfile.write(raw)

    def _send_file_once(
        self,
        path: str,
        content_type: str,
        filename: str,
    ) -> None:
        target = Path(path)
        try:
            size = target.stat().st_size
            self.send_response(HTTPStatus.OK)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(size))
            self.send_header("Cache-Control", "no-store")
            ascii_name = (
                filename.encode("ascii", "ignore").decode("ascii")
                or "wechat-ai-package.zip"
            )
            self.send_header(
                "Content-Disposition",
                f"attachment; filename=\"{ascii_name}\"; filename*=UTF-8''{quote(filename)}",
            )
            self.end_headers()
            with target.open("rb") as source:
                while True:
                    chunk = source.read(1024 * 1024)
                    if not chunk:
                        break
                    self.wfile.write(chunk)
        finally:
            _remove_ai_package_file(str(target))

    def log_message(self, fmt: str, *args: Any) -> None:
        sys.stderr.write("[wechat-web] " + fmt % args + "\n")


def serve(
    host: str = "127.0.0.1",
    port: int = 8787,
    open_browser: bool = False,
) -> None:
    if host not in {"127.0.0.1", "localhost", "0.0.0.0"}:
        raise ValueError("The web console only supports localhost or explicit LAN binding")
    _expire_ai_package_downloads()
    httpd = ThreadingHTTPServer((host, port), WeChatWebHandler)
    httpd.allowed_request_hosts = _request_hosts_for_bind(host)
    try:
        _configure_launch_ops(httpd)
        display_host = "127.0.0.1" if host == "0.0.0.0" else host
        url = f"http://{display_host}:{httpd.server_address[1]}"
        print(f"WeChat CLI Web is running at {url}", flush=True)
        print("Press Ctrl+C to stop.", flush=True)
        if open_browser:
            webbrowser.open(url)
        try:
            httpd.serve_forever()
        except KeyboardInterrupt:
            print("\nStopping WeChat CLI Web.", flush=True)
    finally:
        _stop_launch_ops(httpd)
        httpd.server_close()
