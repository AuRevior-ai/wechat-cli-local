"""Start the local browser interface."""

import click

from ..web.server import serve


@click.command("web")
@click.option("--host", default="127.0.0.1", type=click.Choice(["127.0.0.1", "localhost", "0.0.0.0"]), help="监听地址；0.0.0.0 仅用于显式局域网访问")
@click.option("--port", default=8787, type=click.IntRange(1, 65535), help="本机端口")
@click.option("--open", "open_browser", is_flag=True, help="启动后自动打开浏览器")
def web(host, port, open_browser):
    """启动本机网页控制台。"""
    serve(host=host, port=port, open_browser=open_browser)
