"""No-send current-chat identity challenge using SessionTable.draft."""

from __future__ import annotations

import click

from ..core.contacts import get_contact_names
from ..outbound.draft_identity import (
    DraftIdentityProbeService,
    DraftProbeCode,
    MessageMarkerGuard,
    SessionDraftSource,
)
from ..outbound.windows_draft_probe import WindowsDraftProbeDriver
from ..output.formatter import output


def _code_value(code):
    return code.value if code is not None and hasattr(code, "value") else code


def _payload(result):
    return {
        "ok": bool(result.ok),
        "code": _code_value(result.code),
        "message": result.message,
        "username": result.username,
        "display_name": result.display_name,
        "kind": result.kind,
        "observed_ms": result.observed_ms,
        "cleanup_confirmed": bool(result.cleanup_confirmed),
    }


def _emit(result, fmt: str) -> None:
    if fmt == "json":
        output(_payload(result), "json")
        return
    code = _code_value(result.code) or "OK"
    identity = ""
    if result.username:
        label = result.display_name or result.username
        identity = f" {label} ({result.username})"
    latency = f" observed={result.observed_ms}ms" if result.observed_ms is not None else ""
    cleanup = " cleanup=confirmed" if result.cleanup_confirmed else " cleanup=unconfirmed"
    output(f"{code}:{identity}{latency}{cleanup} - {result.message}".strip(), "text")


@click.command("draft-identity-probe")
@click.option(
    "--format",
    "fmt",
    default="json",
    type=click.Choice(["json", "text"]),
    help="输出格式",
)
@click.pass_context
def draft_identity_probe(ctx, fmt):
    """临时写入并撤销当前聊天草稿，以只读数据库确认稳定会话身份；绝不发送。"""
    app = ctx.obj
    names = get_contact_names(app.cache, app.decrypted_dir)
    service = DraftIdentityProbeService(
        SessionDraftSource(app),
        WindowsDraftProbeDriver(),
        MessageMarkerGuard(app),
        display_names=names,
    )
    result = service.run()
    _emit(result, fmt)

    if result.ok:
        return
    if result.code in {
        DraftProbeCode.DRAFT_CLEANUP_FAILED,
        DraftProbeCode.PROBE_MESSAGE_CREATED,
    }:
        ctx.exit(3)
    ctx.exit(2)
