"""invite-stats command — audit and rank group invitations."""

import json

import click

from ..core.invite_stats import (
    format_invite_stats_csv,
    format_invite_stats_text,
    parse_identity_bindings,
)
from ..core.invite_trace import InviteTraceQueryError, InviteTraceQueryService
from ..core.messages import parse_time_range
from ..output.formatter import output


@click.command("invite-stats")
@click.argument("group_name")
@click.option("--start-time", default="", help="起始时间 YYYY-MM-DD [HH:MM[:SS]]")
@click.option("--end-time", default="", help="结束时间 YYYY-MM-DD [HH:MM[:SS]]")
@click.option("--bind-identity", multiple=True, help="历史昵称=稳定账号，可重复")
@click.option(
    "--format", "fmt", default="json",
    type=click.Choice(["json", "text", "csv"]),
    help="输出格式",
)
@click.option("--output", "output_path", default=None, type=click.Path(dir_okay=False))
@click.pass_context
def invite_stats(
    ctx,
    group_name,
    start_time,
    end_time,
    bind_identity,
    fmt,
    output_path,
):
    """统计群聊邀请关系并按唯一拉人数排行。"""
    app = ctx.obj
    try:
        start_ts, end_ts = parse_time_range(start_time, end_time)
        bindings = parse_identity_bindings(bind_identity)
    except ValueError as exc:
        raise click.UsageError(str(exc)) from exc

    service = InviteTraceQueryService(app)
    try:
        result = service.query(
            group_name,
            bindings=bindings,
            start_ts=start_ts,
            end_ts=end_ts,
        )
    except InviteTraceQueryError as exc:
        raise click.ClickException(str(exc)) from exc
    if fmt == "json":
        rendered = json.dumps(result, ensure_ascii=False, indent=2) + "\n"
    elif fmt == "csv":
        rendered = format_invite_stats_csv(result)
    else:
        rendered = format_invite_stats_text(result) + "\n"

    if output_path:
        try:
            with open(
                output_path, "w", encoding="utf-8", newline=""
            ) as file:
                file.write(rendered)
        except OSError as exc:
            raise click.ClickException(
                f"无法写入输出文件 {output_path}: {exc}"
            ) from exc
        click.echo(output_path)
    elif fmt == "json":
        output(result, "json")
    else:
        output(rendered, "text")
