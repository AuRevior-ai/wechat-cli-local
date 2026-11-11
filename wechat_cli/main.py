"""wechat-cli 入口"""

import sys
from functools import wraps

import click

from .core.context import AppContext
from .version import APP_VERSION


def _configure_utf8_stdio(stdout=None, stderr=None):
    stdout = stdout or sys.stdout
    stderr = stderr or sys.stderr
    for stream in (stdout, stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8", errors="replace")


_configure_utf8_stdio()


@click.group()
@click.version_option(version=APP_VERSION, prog_name="wechat-cli")
@click.option("--config", "config_path", default=None, envvar="WECHAT_CLI_CONFIG",
              help="config.json 路径（默认自动查找）")
@click.pass_context
def cli(ctx, config_path):
    """WeChat CLI — 查询微信消息、联系人等数据

    \b
    使用示例:
      wechat-cli init                                # 首次使用：提取密钥
      wechat-cli sessions                            # 最近会话列表
      wechat-cli sessions --limit 10                 # 最近 10 个会话
      wechat-cli history "张三" --limit 20          # 查看张三的最近 20 条消息
      wechat-cli history "AI交流群" --start-time "2026-04-01"  # 指定时间范围
      wechat-cli search "Claude" --chat "AI交流群"   # 在指定群里搜索关键词
      wechat-cli search "你好" --limit 50           # 全局搜索
      wechat-cli contacts --query "李"              # 搜索联系人
      wechat-cli new-messages                       # 获取增量新消息
      wechat-cli invite-stats "群名"                # 群邀请统计与排行
      wechat-cli invite-trace "群名"                # 群邀请历史溯源明细
      wechat-cli ai-package "群名" --output ai.zip  # 生成 AI 聊天资料包
    """
    # SNS manages a separate context. Other queries load theirs after argument parsing.
    if ctx.invoked_subcommand == "sns":
        ctx.obj = {"config_path": config_path}


def _with_app_context(callback):
    """Initialize account data only when a parsed query actually executes."""
    @wraps(callback)
    def execute(*args, **kwargs):
        ctx = click.get_current_context()
        if ctx.obj is None:
            config_path = ctx.find_root().params.get("config_path")
            try:
                ctx.obj = AppContext(config_path)
            except FileNotFoundError as exc:
                click.echo(str(exc), err=True)
                ctx.exit(1)
            except Exception as exc:
                click.echo(f"初始化失败: {exc}", err=True)
                ctx.exit(1)
        return callback(*args, **kwargs)

    return execute


# 注册子命令
from .commands.init import init
from .commands.sessions import sessions
from .commands.history import history
from .commands.search import search
from .commands.contacts import contacts
from .commands.new_messages import new_messages
from .commands.members import members
from .commands.export import export
from .commands.stats import stats
from .commands.unread import unread
from .commands.favorites import favorites
from .commands.web import web
from .commands.media import media
from .commands.invite_stats import invite_stats
from .commands.invite_trace import invite_trace
from .commands.ai_package import ai_package
from .commands.sns import sns
from .commands.send_text import send_text
from .commands.draft_identity_probe import draft_identity_probe

for command in (
    init, sessions, history, search, contacts, new_messages, members, export,
    stats, unread, favorites, web, media, invite_stats, invite_trace,
    ai_package, sns, send_text, draft_identity_probe,
):
    if command not in (init, web, media, sns, send_text):
        command.callback = _with_app_context(command.callback)
    cli.add_command(command)

media.commands["export"].callback = _with_app_context(media.commands["export"].callback)


if __name__ == "__main__":
    cli()
