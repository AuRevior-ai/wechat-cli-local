import json
import unittest
from types import SimpleNamespace
from unittest.mock import patch

from click.testing import CliRunner

from wechat_cli.core.sns import SnsError
from wechat_cli.main import cli


EMPTY_PAGE = {
    "count": 0,
    "offset": 0,
    "limit": 20,
    "has_more": False,
    "posts": [],
}

SAMPLE_PAGE = {
    "count": 2,
    "offset": 0,
    "limit": 20,
    "has_more": False,
    "posts": [
        {
            "post_id": "2",
            "username": "wxid_fixture",
            "display_name": "Fixture User",
            "create_time": 1788515400,
            "time": "2026-09-04 12:30",
            "content": "hello",
            "location": {"poiName": "Fixture Park"},
            "media": [
                {
                    "media_id": "m_" + "a" * 24,
                    "type": "video",
                    "status": "metadata_only",
                    "reason": "remote_deferred",
                    "preview_url": None,
                    "width": 100,
                    "height": 50,
                    "duration": 1.5,
                }
            ],
            "likes": [
                {
                    "username": "wxid_friend_a",
                    "display_name": "Friend A",
                    "create_time": 1788515400,
                }
            ],
            "comments": [
                {
                    "comment_id": 1,
                    "username": "wxid_friend_b",
                    "display_name": "Friend B",
                    "content": "nice",
                    "create_time": 1788515400,
                    "reply_to_username": None,
                    "reply_to_display_name": None,
                }
            ],
            "parse_status": "ok",
            "warnings": [],
        },
        {
            "post_id": "1",
            "username": "wxid_partial",
            "display_name": "wxid_partial",
            "create_time": None,
            "time": "",
            "content": "",
            "location": None,
            "media": [],
            "likes": [],
            "comments": [],
            "parse_status": "partial",
            "warnings": ["missing_create_time"],
        },
    ],
}


class SnsCliSuccessTests(unittest.TestCase):
    def test_default_json_success_envelope_and_arguments(self):
        ctx = SimpleNamespace(cache=object(), decrypted_dir="unused")
        with patch("wechat_cli.commands.sns.build_sns_context", return_value=ctx) as build_ctx, patch(
            "wechat_cli.commands.sns.list_sns_posts", return_value=EMPTY_PAGE
        ) as list_posts:
            result = CliRunner().invoke(cli, ["sns"])

        self.assertEqual(result.exit_code, 0, result.output)
        self.assertEqual(json.loads(result.stdout), {"ok": True, "data": EMPTY_PAGE})
        self.assertEqual(result.stderr, "")
        build_ctx.assert_called_once_with(None)
        list_posts.assert_called_once_with(
            ctx,
            limit=20,
            offset=0,
            username=None,
            query=None,
            start_time=None,
            end_time=None,
        )

    def test_all_options_are_forwarded_and_user_is_resolved(self):
        ctx = SimpleNamespace(cache=object(), decrypted_dir="dec")
        with patch("wechat_cli.commands.sns.build_sns_context", return_value=ctx), patch(
            "wechat_cli.commands.sns.resolve_sns_username", return_value="wxid_fixture"
        ) as resolve, patch(
            "wechat_cli.commands.sns.list_sns_posts", return_value={**EMPTY_PAGE, "offset": 2, "limit": 5}
        ) as list_posts:
            result = CliRunner().invoke(
                cli,
                [
                    "sns",
                    "--limit", "5",
                    "--offset", "2",
                    "--user", "Fixture",
                    "--query", "hello",
                    "--start-time", "2026-08-01",
                    "--end-time", "2026-08-31",
                    "--format", "json",
                ],
            )
        self.assertEqual(result.exit_code, 0, result.output)
        resolve.assert_called_once_with("Fixture", ctx.cache, ctx.decrypted_dir)
        list_posts.assert_called_once_with(
            ctx,
            limit=5,
            offset=2,
            username="wxid_fixture",
            query="hello",
            start_time="2026-08-01",
            end_time="2026-08-31",
        )

    def test_text_success_is_human_readable_and_not_json(self):
        ctx = SimpleNamespace(cache=object(), decrypted_dir="unused")
        with patch("wechat_cli.commands.sns.build_sns_context", return_value=ctx), patch(
            "wechat_cli.commands.sns.list_sns_posts", return_value=SAMPLE_PAGE
        ):
            result = CliRunner().invoke(cli, ["sns", "--format", "text"])

        self.assertEqual(result.exit_code, 0, result.output)
        self.assertEqual(result.stderr, "")
        self.assertIn("朋友圈结果：2 条", result.stdout)
        self.assertIn("[2026-09-04 12:30] Fixture User (wxid_fixture)", result.stdout)
        self.assertIn("位置：Fixture Park", result.stdout)
        self.assertIn("媒体：video(metadata_only/remote_deferred)", result.stdout)
        self.assertIn("赞：Friend A", result.stdout)
        self.assertIn("评论：Friend B: nice", result.stdout)
        self.assertIn("[时间未知] wxid_partial", result.stdout)
        self.assertIn("状态：partial [missing_create_time]", result.stdout)
        self.assertNotIn('{"ok":', result.stdout)
        self.assertNotIn('"error":', result.stdout)


INVALID_REQUEST_CASES = [
    ["--limit", "abc"],
    ["--offset", "abc"],
    ["--limit", "0"],
    ["--limit", "201"],
    ["--offset", "-1"],
    ["--start-time", "not-a-time"],
    ["--end-time", "not-a-time"],
    ["--start-time", "2026-09-02", "--end-time", "2026-09-01"],
]


class SnsCliInvalidRequestTests(unittest.TestCase):
    def test_all_invalid_requests_fail_before_context_in_json_and_text(self):
        runner = CliRunner()
        for args in INVALID_REQUEST_CASES:
            with self.subTest(args=args, fmt="json"), patch(
                "wechat_cli.commands.sns.build_sns_context"
            ) as build_ctx:
                result = runner.invoke(cli, ["sns", *args, "--format", "json"])
                build_ctx.assert_not_called()
                self.assertEqual(result.exit_code, 2, result.output)
                payload = json.loads(result.stdout)
                self.assertEqual(payload["error"]["code"], "SNS_INVALID_REQUEST")
                self.assertFalse(payload["error"]["retryable"])
                self.assertEqual(result.stderr, "")

            with self.subTest(args=args, fmt="text"), patch(
                "wechat_cli.commands.sns.build_sns_context"
            ) as build_ctx:
                result = runner.invoke(cli, ["sns", *args, "--format", "text"])
                build_ctx.assert_not_called()
                self.assertEqual(result.exit_code, 2, result.output)
                self.assertEqual(result.stdout, "")
                self.assertIn("朋友圈读取失败 [SNS_INVALID_REQUEST]", result.stderr)
                self.assertNotIn("Usage:", result.stderr)


class SnsCliErrorWireTests(unittest.TestCase):
    def test_controlled_error_exit_classes_match_json_and_text(self):
        cases = [
            ("SNS_USER_NOT_FOUND", 2),
            ("SNS_USER_AMBIGUOUS", 2),
            ("SNS_DB_NOT_FOUND", 3),
            ("SNS_KEY_NOT_FOUND", 3),
            ("SNS_SCHEMA_UNSUPPORTED", 3),
            ("SNS_MEDIA_UNAVAILABLE", 3),
            ("SNS_MEDIA_UNSUPPORTED", 3),
        ]
        for code, expected_exit in cases:
            error = SnsError(code, "sanitized", retryable=False)
            for fmt in ("json", "text"):
                with self.subTest(code=code, fmt=fmt), patch(
                    "wechat_cli.commands.sns.build_sns_context", side_effect=error
                ):
                    result = CliRunner().invoke(cli, ["sns", "--format", fmt])
                self.assertEqual(result.exit_code, expected_exit, result.output)
                if fmt == "json":
                    payload = json.loads(result.stdout)
                    self.assertEqual(payload["error"]["code"], code)
                    self.assertEqual(result.stderr, "")
                else:
                    self.assertEqual(result.stdout, "")
                    self.assertIn(f"朋友圈读取失败 [{code}]: sanitized", result.stderr)
                    self.assertNotIn('{"ok":', result.stderr)

    def test_unexpected_error_is_generic_and_does_not_leak_exception(self):
        for fmt in ("json", "text"):
            with self.subTest(fmt=fmt), patch(
                "wechat_cli.commands.sns.build_sns_context",
                side_effect=RuntimeError(r"SECRET C:\private\sns.db"),
            ):
                result = CliRunner().invoke(cli, ["sns", "--format", fmt])
            self.assertEqual(result.exit_code, 1, result.output)
            combined = result.stdout + result.stderr
            self.assertIn("SNS_INTERNAL_ERROR", combined)
            self.assertNotIn("SECRET", combined)
            self.assertNotIn("private", combined)
            self.assertNotIn("RuntimeError", combined)

    def test_details_sanitizer_enforces_code_specific_allowlist(self):
        from wechat_cli.core.sns import sanitize_sns_error_details

        self.assertEqual(
            sanitize_sns_error_details(
                "SNS_SCHEMA_UNSUPPORTED",
                {
                    "missing": ["SnsTimeLine", "private"],
                    "probe_reason": "secret",
                    "failures": [
                        {"column": "tid", "problem": "type", "actual_type": "TEXT", "secret": "x"},
                        {"column": "private", "problem": "missing"},
                    ],
                },
            ),
            {
                "missing": ["SnsTimeLine"],
                "failures": [{"column": "tid", "problem": "type", "actual_type": "TEXT"}],
            },
        )
        self.assertEqual(
            sanitize_sns_error_details("SNS_KEY_NOT_FOUND", {"reason": "sns_key_missing", "path": "secret"}),
            {"reason": "sns_key_missing"},
        )
        self.assertEqual(
            sanitize_sns_error_details(
                "SNS_MEDIA_UNAVAILABLE",
                {"post_id": "1", "media_id": "m_" + "a" * 24, "reason": "local_missing", "path": "secret"},
            ),
            {"reason": "local_missing", "post_id": "1", "media_id": "m_" + "a" * 24},
        )
        self.assertIsNone(sanitize_sns_error_details("SNS_INTERNAL_ERROR", {"secret": "x"}))

    def test_ambiguous_details_are_bounded_to_ten_safe_candidates(self):
        from wechat_cli.core.sns import sanitize_sns_error_details

        details = {
            "candidate_count": 11,
            "truncated": True,
            "candidates": [
                {
                    "username": f"u{i}",
                    "nick_name": f"n{i}",
                    "remark": f"r{i}",
                    "display_name": f"d{i}",
                    "private": "secret",
                }
                for i in range(11)
            ],
        }
        safe = sanitize_sns_error_details("SNS_USER_AMBIGUOUS", details)
        self.assertEqual(safe["candidate_count"], 11)
        self.assertTrue(safe["truncated"])
        self.assertEqual(len(safe["candidates"]), 10)
        self.assertEqual(set(safe["candidates"][0]), {"username", "nick_name", "remark", "display_name"})


if __name__ == "__main__":
    unittest.main()
