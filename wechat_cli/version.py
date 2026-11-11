"""Shared version metadata for the CLI and local web interface."""

from __future__ import annotations

import os

PRODUCT = "wechat-cli-web"
APP_VERSION = "0.5.0"
BUILD_ID = os.environ.get("WECHAT_CLI_BUILD_ID", "dev")
