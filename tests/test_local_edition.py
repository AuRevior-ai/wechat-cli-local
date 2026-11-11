"""Regression checks for the independently runnable local edition."""

import ast
import http.client
import json
import threading
import unittest
from http.server import ThreadingHTTPServer
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch
from urllib.request import urlopen

from click.testing import CliRunner

from wechat_cli.main import cli
from wechat_cli.version import APP_VERSION, PRODUCT
from wechat_cli.web.server import WeChatWebHandler


ROOT = Path(__file__).resolve().parents[1]


class LocalEditionTests(unittest.TestCase):
    def setUp(self):
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), WeChatWebHandler)
        self.server.allowed_request_hosts = {"127.0.0.1", "localhost"}
        self.server.launch_ops_service = SimpleNamespace(
            list_campaigns=lambda: [{"campaign_id": "sample", "name": "示例活动"}],
            create_campaign=lambda name: {"campaign_id": "new", "name": name},
        )
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        host, port = self.server.server_address
        self.base_url = f"http://{host}:{port}"

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=3)

    def test_local_pages_and_health_load_from_package_resources(self):
        for path in ["/", "/launch", "/static/app.js", "/static/launch.js"]:
            with self.subTest(path=path), urlopen(self.base_url + path, timeout=3) as response:
                self.assertEqual(200, response.status)
                self.assertTrue(response.read())
        with urlopen(self.base_url + "/api/health", timeout=3) as response:
            health = json.load(response)
        self.assertEqual("local", health["mode"])
        self.assertEqual(APP_VERSION, health["version"])
        self.assertEqual(PRODUCT, health["product"])

    def test_query_bridge_preserves_command_result(self):
        fixture = {"ok": True, "data": [{"username": "sample", "remark": "示例联系人"}]}
        with patch("wechat_cli.web.server.run_cli_command", return_value=fixture) as run:
            connection = http.client.HTTPConnection(*self.server.server_address, timeout=3)
            connection.request(
                "POST", "/api/run",
                body=json.dumps({"command": "contacts", "params": {"query": "示例"}}),
                headers={"Content-Type": "application/json", "Origin": self.base_url},
            )
            response = connection.getresponse()
            self.assertEqual(200, response.status)
            self.assertEqual(fixture, json.loads(response.read()))
            connection.close()
        run.assert_called_once_with({"command": "contacts", "params": {"query": "示例"}})

    def test_local_campaign_routes_remain_available(self):
        with urlopen(self.base_url + "/api/launch/campaigns", timeout=3) as response:
            self.assertEqual("sample", json.load(response)["data"]["campaigns"][0]["campaign_id"])

    def test_product_management_endpoints_are_absent(self):
        paths = [
            "/api/license", "/api/update-status", "/operator-setup",
            "/operator-admin", "/api/launch/viewer-publications?campaign_id=sample",
        ]
        for path in paths:
            with self.subTest(path=path):
                connection = http.client.HTTPConnection(*self.server.server_address, timeout=3)
                connection.request("GET", path)
                response = connection.getresponse()
                self.assertEqual(404, response.status)
                response.read()
                connection.close()
        for path in ["/api/update/check", "/api/diagnostics/generate", "/api/launch/viewer/publish"]:
            with self.subTest(path=path):
                connection = http.client.HTTPConnection(*self.server.server_address, timeout=3)
                connection.request("POST", path, "{}", {"Content-Type": "application/json"})
                response = connection.getresponse()
                self.assertEqual(404, response.status)
                response.read()
                connection.close()

    def test_all_cli_help_commands_load_without_local_account_data(self):
        runner = CliRunner()
        for command in ["history", "search", "contacts", "web", "invite-trace", "send-text", "media export"]:
            with self.subTest(command=command), patch(
                "wechat_cli.main.AppContext", side_effect=AssertionError("Help must not load account data")
            ):
                result = runner.invoke(cli, [*command.split(), "--help"])
                self.assertEqual(0, result.exit_code, result.output)

    def test_package_has_no_imports_of_removed_product_modules(self):
        removed = {"license", "update", "launcher", "admin", "release", "operator_control", "operator_node"}
        for path in (ROOT / "wechat_cli").rglob("*.py"):
            for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
                if isinstance(node, ast.ImportFrom):
                    self.assertFalse(
                        removed.intersection((node.module or "").split(".")),
                        f"{path.relative_to(ROOT)} imports {node.module}",
                    )


if __name__ == "__main__":
    unittest.main()
