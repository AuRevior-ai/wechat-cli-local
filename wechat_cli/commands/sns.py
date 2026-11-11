import json

import click

from ..core.sns import (
    SnsError,
    _validate_sns_query,
    build_sns_context,
    list_sns_posts,
    resolve_sns_username,
    sanitize_sns_error_details,
    sns_error_exit_code,
)


def _format_sns_text(data):
    lines = [f"朋友圈结果：{int(data.get('count', 0))} 条"]
    for post in data.get("posts") or []:
        display_name = str(post.get("display_name") or post.get("username") or "")
        username = str(post.get("username") or "")
        time_text = str(post.get("time") or "时间未知")
        header = f"[{time_text}] {display_name or username}"
        if username and display_name and display_name != username:
            header += f" ({username})"
        lines.extend([header, str(post.get("content") or "(无文字内容)")])

        location = post.get("location") or {}
        for key in ("poiName", "poiAddressName", "poiAddress", "city", "country"):
            if location.get(key):
                lines.append(f"位置：{location[key]}")
                break

        media = post.get("media") or []
        if media:
            labels = []
            for item in media:
                label = f"{item.get('type', 'unsupported')}({item.get('status', 'unsupported')}"
                if item.get("reason"):
                    label += f"/{item['reason']}"
                labels.append(label + ")")
            lines.append("媒体：" + ", ".join(labels))

        likes = post.get("likes") or []
        if likes:
            lines.append(
                "赞："
                + "、".join(
                    str(item.get("display_name") or item.get("username") or "")
                    for item in likes
                )
            )

        for comment in post.get("comments") or []:
            author = str(
                comment.get("display_name") or comment.get("username") or ""
            )
            target = comment.get("reply_to_display_name")
            prefix = f"评论：{author}"
            if target:
                prefix += f" 回复 {target}"
            lines.append(f"{prefix}: {str(comment.get('content') or '')}")

        if post.get("parse_status") == "partial":
            warnings = ",".join(str(item) for item in (post.get("warnings") or []))
            lines.append(f"状态：partial [{warnings}]")
        lines.append("")
    return "\n".join(lines).rstrip()


def _json_sns_error(error):
    public = {
        "code": error.code,
        "message": error.message,
        "retryable": error.retryable,
    }
    details = sanitize_sns_error_details(error.code, error.details)
    if details:
        public["details"] = details
    return {"ok": False, "error": public}


def _parse_sns_cli_int(raw_value):
    try:
        return int(str(raw_value).strip())
    except (TypeError, ValueError) as exc:
        raise SnsError(
            "SNS_INVALID_REQUEST",
            "朋友圈查询参数无效",
            retryable=False,
        ) from exc


def _emit_sns_internal_error(fmt):
    if fmt == "json":
        click.echo(
            json.dumps(
                {
                    "ok": False,
                    "error": {
                        "code": "SNS_INTERNAL_ERROR",
                        "message": "朋友圈读取发生内部错误",
                        "retryable": False,
                    },
                },
                ensure_ascii=False,
            )
        )
    else:
        click.echo(
            "朋友圈读取失败 [SNS_INTERNAL_ERROR]: 朋友圈读取发生内部错误",
            err=True,
        )
    return 1


def run_sns_command(
    config_path=None,
    *,
    fmt="json",
    user=None,
    query=None,
    start_time=None,
    end_time=None,
    limit="20",
    offset="0",
):
    try:
        limit_value = _parse_sns_cli_int(limit)
        offset_value = _parse_sns_cli_int(offset)
        _validate_sns_query(limit_value, offset_value, start_time, end_time)

        sns_ctx = build_sns_context(config_path)
        username = (
            resolve_sns_username(user, sns_ctx.cache, sns_ctx.decrypted_dir)
            if user
            else None
        )
        data = list_sns_posts(
            sns_ctx,
            limit=limit_value,
            offset=offset_value,
            username=username,
            query=query,
            start_time=start_time,
            end_time=end_time,
        )
    except SnsError as exc:
        exit_code = sns_error_exit_code(exc.code)
        if exit_code is None:
            return _emit_sns_internal_error(fmt)
        if fmt == "json":
            click.echo(json.dumps(_json_sns_error(exc), ensure_ascii=False))
        else:
            click.echo(
                f"朋友圈读取失败 [{exc.code}]: {exc.message}",
                err=True,
            )
        return exit_code
    except Exception:
        return _emit_sns_internal_error(fmt)

    if fmt == "json":
        click.echo(json.dumps({"ok": True, "data": data}, ensure_ascii=False))
    else:
        click.echo(_format_sns_text(data))
    return 0


@click.command("sns")
@click.option("--limit", default="20")
@click.option("--offset", default="0")
@click.option("--user", default=None)
@click.option("--query", default=None)
@click.option("--start-time", default=None)
@click.option("--end-time", default=None)
@click.option("--format", "fmt", default="json", type=click.Choice(["json", "text"]))
@click.pass_context
def sns(ctx, limit, offset, user, query, start_time, end_time, fmt):
    config_path = (ctx.obj or {}).get("config_path")
    raise SystemExit(
        run_sns_command(
            config_path=config_path,
            fmt=fmt,
            user=user,
            query=query,
            start_time=start_time,
            end_time=end_time,
            limit=limit,
            offset=offset,
        )
    )
