"""Explicit one-shot outbound text command for the Windows WeChat client."""

from __future__ import annotations

import click

from ..core.contacts import get_contact_full
from ..core.context import AppContext
from ..outbound.models import SendStatus, TargetResolutionError
from ..outbound.resolver import resolve_target_from_contacts
from ..outbound.service import OutboundService
from ..outbound.verifier import ReadbackVerifier
from ..outbound.windows_ui import WindowsWeChatAdapter
from ..output.formatter import output


def _enum_value(value):
    return value.value if value is not None and hasattr(value, "value") else value


def _probe_payload(result):
    return {
        "ok": bool(result.ok),
        "code": _enum_value(result.code),
        "message": result.message,
        "client_version": result.client_version,
        "window_title": result.window_title,
        "strategy": result.strategy,
    }


def _send_payload(result):
    target = None
    if result.target is not None:
        target = {
            "username": result.target.username,
            "display_name": result.target.display_name,
            "kind": result.target.kind,
        }
    return {
        "status": _enum_value(result.status),
        "target": target,
        "code": _enum_value(result.code),
        "message": result.message,
        "attempted_at": result.attempted_at,
        "confirmed_at": result.confirmed_at,
    }


def _emit_probe(result, fmt: str):
    payload = _probe_payload(result)
    if fmt == "json":
        output(payload, "json")
        return
    state = "ready" if result.ok else "blocked"
    version = f" WeChat {result.client_version}" if result.client_version else ""
    strategy = f" [{result.strategy}]" if result.strategy else ""
    output(f"probe {state}:{version}{strategy} {result.message}".strip(), "text")


def _emit_send(result, fmt: str):
    payload = _send_payload(result)
    if fmt == "json":
        output(payload, "json")
        return
    target = result.target
    label = "unknown target"
    if target is not None:
        label = f"{target.display_name} ({target.username})"
    output(f"{_enum_value(result.status)}: {label} - {result.message}", "text")


@click.command("send-text")
@click.option("--to", "target_text", default="", help="精确联系人/群名或稳定 username")
@click.option("--text", "message_text", default="", help="要发送的一条文本消息")
@click.option("--probe", is_flag=True, help="只检查 Windows UI 自动化能力，不发送消息")
@click.option(
    "--format",
    "fmt",
    default="json",
    type=click.Choice(["json", "text"]),
    help="输出格式",
)
@click.pass_context
def send_text(ctx, target_text, message_text, probe, fmt):
    """通过官方 Windows 微信客户端显式发送一条可回读确认的文本消息。"""
    if probe:
        result = WindowsWeChatAdapter().probe()
        _emit_probe(result, fmt)
        if not result.ok:
            ctx.exit(2)
        return

    config_path = None
    if ctx.parent is not None:
        config_path = ctx.parent.params.get("config_path")
    app = AppContext(config_path)
    contacts = get_contact_full(app.cache, app.decrypted_dir)
    try:
        target = resolve_target_from_contacts(target_text, contacts)
    except TargetResolutionError as exc:
        payload = {
            "status": SendStatus.BLOCKED.value,
            "target": None,
            "code": _enum_value(exc.code),
            "message": str(exc),
            "attempted_at": None,
            "confirmed_at": None,
        }
        if fmt == "json":
            output(payload, "json")
        else:
            output(f"blocked: {str(exc)}", "text")
        ctx.exit(2)
        return

    adapter = WindowsWeChatAdapter()
    verifier = ReadbackVerifier(app)
    service = OutboundService(adapter, verifier)
    result = service.send(target, message_text)
    _emit_send(result, fmt)

    if result.status is SendStatus.CONFIRMED:
        return
    if result.status is SendStatus.UNCONFIRMED:
        ctx.exit(3)
    ctx.exit(2)
