import http.client
import json
import os
import tempfile
import threading
import unittest
from http.server import ThreadingHTTPServer
from pathlib import Path
from urllib.error import HTTPError
from urllib.request import urlopen
from unittest.mock import patch

from wechat_cli.core.sns import SnsError
from wechat_cli.web.server import WeChatWebHandler


ROOT = Path(__file__).resolve().parents[1]
INDEX = ROOT / "wechat_cli" / "web" / "static" / "index.html"
APP_JS = ROOT / "wechat_cli" / "web" / "static" / "app.js"
APP_CSS = ROOT / "wechat_cli" / "web" / "static" / "app.css"


class SnsWebMarkupTests(unittest.TestCase):
    def test_sns_navigation_screen_and_form_controls_exist(self):
        html = INDEX.read_text(encoding="utf-8")

        self.assertIn('<button data-target="sns">朋友圈</button>', html)
        self.assertIn('id="sns" class="screen" data-title="朋友圈"', html)
        self.assertIn('class="tool-form sns-form" data-command="sns"', html)
        for param in ("query", "user", "start_time", "end_time"):
            self.assertIn(f'data-param="{param}"', html)
        self.assertIn('id="sns-reset"', html)
        self.assertIn('id="sns-local-error"', html)
        self.assertIn('id="sns-empty"', html)
        self.assertIn('id="sns-timeline"', html)
        self.assertIn('id="sns-load-more"', html)
        self.assertIn('id="sns-loading"', html)

    def test_sns_form_does_not_expose_path_url_token_or_key_inputs(self):
        html = INDEX.read_text(encoding="utf-8")
        start = html.index('<section id="sns"')
        end = html.index("</section>", start)
        sns_html = html[start:end]

        for forbidden in (
            'data-param="path"',
            'data-param="url"',
            'data-param="token"',
            'data-param="key"',
            'data-param="media_id"',
        ):
            self.assertNotIn(forbidden, sns_html)


class SnsWebRenderingContractTests(unittest.TestCase):
    def test_sns_has_dedicated_safe_render_helpers(self):
        js = APP_JS.read_text(encoding="utf-8")

        for helper in (
            "function renderSnsPost(post)",
            "function renderSnsLikes(likes)",
            "function renderSnsComments(comments)",
            "function renderSnsMediaSummary(media)",
            "function renderSnsError(error)",
        ):
            self.assertIn(helper, js)

        self.assertIn("escapeHtml(post.content", js)
        self.assertIn("escapeHtml(post.display_name", js)
        self.assertIn("escapeHtml(comment.content", js)
        self.assertIn("escapeHtml(error.message", js)
        self.assertNotIn("post.raw_xml", js)
        self.assertNotIn("media._source_url_md5", js)
        self.assertNotIn("media._local_path", js)

    def test_partial_records_and_metadata_only_media_have_local_nonfatal_ui(self):
        js = APP_JS.read_text(encoding="utf-8")
        self.assertIn("sns-partial", js)
        self.assertIn("sns-media-summary", js)
        self.assertIn("local_identity_unproven", js)
        self.assertIn("remote_deferred", js)

    def test_sns_styles_follow_existing_tokens_and_loading_feedback(self):
        css = APP_CSS.read_text(encoding="utf-8")
        for selector in (
            ".sns-card",
            ".sns-card-head",
            ".sns-content",
            ".sns-location",
            ".sns-interactions",
            ".sns-media-summary",
            ".sns-partial",
            "#sns-load-more[disabled]",
        ):
            self.assertIn(selector, css)
        self.assertIn("var(--green)", css)
        self.assertIn("var(--line)", css)


class SnsWebPaginationContractTests(unittest.TestCase):
    def test_dedicated_sns_state_and_pure_page_helpers_exist(self):
        js = APP_JS.read_text(encoding="utf-8")
        for declaration in (
            "let snsPosts = [];",
            "let snsOffset = 0;",
            "let snsHasMore = false;",
            "let snsSeenPostIds = new Set();",
            "let snsLoading = false;",
            "let snsFilters = {};",
        ):
            self.assertIn(declaration, js)
        self.assertIn("function mergeSnsPage(current, seenIds, payload)", js)
        self.assertIn("if (!postId || seenIds.has(postId)) continue;", js)
        self.assertIn("seenIds.add(postId);", js)
        self.assertIn("function nextSnsOffset(payload)", js)
        self.assertIn("Number(payload.offset || 0) + Number(payload.count || 0)", js)

    def test_load_more_uses_server_page_count_not_deduplicated_insertions(self):
        js = APP_JS.read_text(encoding="utf-8")
        self.assertIn("async function loadSnsPage({ reset = false } = {})", js)
        self.assertIn('command: "sns"', js)
        self.assertIn("offset: snsOffset", js)
        self.assertIn("snsPosts = mergeSnsPage(snsPosts, snsSeenPostIds, data);", js)
        self.assertIn("snsOffset = nextSnsOffset(data);", js)
        self.assertIn("snsHasMore = Boolean(data.has_more);", js)
        self.assertNotIn("snsOffset += snsPosts.length", js)
        self.assertNotIn("snsOffset += merged", js)

    def test_reset_and_loading_contract_are_explicit(self):
        js = APP_JS.read_text(encoding="utf-8")
        self.assertIn("snsPosts = [];", js)
        self.assertIn("snsSeenPostIds.clear();", js)
        self.assertIn("snsOffset = 0;", js)
        self.assertIn("snsLoadMoreButton.disabled = true;", js)
        self.assertIn("snsLoading = true;", js)
        self.assertIn("snsLoading = false;", js)
        self.assertIn("snsLoadMoreButton.hidden = !snsHasMore", js)

    def test_sns_form_reset_and_load_more_have_dedicated_bindings(self):
        js = APP_JS.read_text(encoding="utf-8")
        self.assertIn('const snsForm = document.querySelector("#sns .sns-form");', js)
        self.assertIn('snsForm?.addEventListener("submit"', js)
        self.assertIn('snsResetButton?.addEventListener("click"', js)
        self.assertIn('snsLoadMoreButton?.addEventListener("click"', js)
        self.assertIn('if (form.classList.contains("sns-form")) return;', js)


class SnsMediaUiContractTests(unittest.TestCase):
    def test_standard_video_has_on_demand_local_check_not_eager_video(self):
        js = APP_JS.read_text(encoding="utf-8")
        self.assertIn("function renderSnsMedia(post)", js)
        self.assertIn("检查本地视频", js)
        self.assertIn("data-sns-resolve-post", js)
        self.assertIn("media.type === \"video\"", js)
        self.assertIn("media.reason === \"remote_deferred\"", js)
        self.assertIn("<video controls", js)

    def test_session_local_media_state_and_endpoint_flow_are_explicit(self):
        js = APP_JS.read_text(encoding="utf-8")
        for needle in (
            "const snsMediaState = new Map();",
            "function snsMediaKey(postId, mediaId)",
            "async function resolveSnsVideo(postId, mediaId)",
            "?variant=preview",
            "URL.createObjectURL(blob)",
            "SNS_MEDIA_UNSUPPORTED",
            "SNS_SCHEMA_UNSUPPORTED",
            "SNS_DB_NOT_FOUND",
            "SNS_KEY_NOT_FOUND",
            "SNS_INTERNAL_ERROR",
            "local_identity_ambiguous",
            "local_identity_unproven",
            "local_missing",
        ):
            self.assertIn(needle, js)

    def test_download_and_object_url_cleanup_contract(self):
        js = APP_JS.read_text(encoding="utf-8")
        self.assertIn("?variant=original&download=1", js)
        self.assertIn("URL.revokeObjectURL", js)
        self.assertIn("function clearSnsMediaState()", js)
        self.assertIn("clearSnsMediaState();", js)
        self.assertNotIn('download="sns-', js)

    def test_image_and_live_photo_stay_metadata_only(self):
        js = APP_JS.read_text(encoding="utf-8")
        self.assertIn('media.type === "image"', js)
        self.assertIn('media.type === "live_photo"', js)
        self.assertIn("snsLivePhotoComponent", js)
        self.assertIn("静态图", js)
        self.assertIn("视频组件", js)


class SnsMediaEndpointTests(unittest.TestCase):
    MEDIA_ID = "m_0123456789abcdef01234567"

    def start_server(self):
        server = ThreadingHTTPServer(("127.0.0.1", 0), WeChatWebHandler)
        server.allowed_request_hosts = {"127.0.0.1", "localhost"}
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()

        def cleanup():
            server.shutdown()
            server.server_close()
            thread.join(timeout=3)

        self.addCleanup(cleanup)
        return server

    def get_json(self, server, path):
        host, port = server.server_address
        try:
            response = urlopen(f"http://{host}:{port}{path}", timeout=4)
            status = response.status
            headers = response.headers
            body = response.read()
            response.close()
        except HTTPError as exc:
            status = exc.code
            headers = exc.headers
            body = exc.read()
        return status, json.loads(body.decode("utf-8")), headers

    @patch("wechat_cli.web.server.read_sns_media", create=True)
    @patch("wechat_cli.web.server.build_sns_context", create=True)
    def test_request_source_gate_precedes_media_resolution(self, build_ctx, read_media):
        server = self.start_server()
        host, port = server.server_address
        conn = http.client.HTTPConnection(host, port, timeout=4)
        conn.request(
            "GET",
            f"/api/sns/media/123/{self.MEDIA_ID}?variant=preview",
            headers={"Host": "malicious.example"},
        )
        response = conn.getresponse()
        payload = json.loads(response.read().decode("utf-8"))
        conn.close()
        self.assertEqual(response.status, 403)
        self.assertEqual(payload["error"]["code"], "FORBIDDEN_REQUEST_SOURCE")
        build_ctx.assert_not_called()
        read_media.assert_not_called()

    @patch("wechat_cli.web.server.read_sns_media", create=True)
    @patch("wechat_cli.web.server.build_sns_context", create=True)
    def test_local_server_reports_missing_media_without_launcher_state(self, build_ctx, read_media):
        build_ctx.return_value = object()
        read_media.side_effect = SnsError(
            "SNS_MEDIA_UNAVAILABLE", "本地媒体不存在", retryable=False
        )
        server = self.start_server()
        status, payload, _headers = self.get_json(
            server, f"/api/sns/media/123/{self.MEDIA_ID}?variant=preview"
        )
        self.assertEqual(status, 404)
        self.assertEqual(payload["error"]["code"], "SNS_MEDIA_UNAVAILABLE")
        build_ctx.assert_called_once_with()
        read_media.assert_called_once()

    @patch("wechat_cli.web.server.read_sns_media", create=True)
    @patch("wechat_cli.web.server.build_sns_context", create=True)
    def test_valid_gate_reaches_media_resolver(self, build_ctx, read_media):
        build_ctx.return_value = object()
        read_media.return_value = {
            "status": "ready",
            "type": "video",
            "body": b"\x00\x00\x00\x18ftypisomfixture",
            "content_type": "video/mp4",
        }
        server = self.start_server()
        host, port = server.server_address
        with urlopen(
            f"http://{host}:{port}/api/sns/media/123/{self.MEDIA_ID}?variant=preview",
            timeout=4,
        ) as response:
            body = response.read()
        self.assertEqual(response.status, 200)
        self.assertEqual(body[4:8], b"ftyp")
        build_ctx.assert_called_once_with()
        read_media.assert_called_once_with(
            build_ctx.return_value,
            "123",
            self.MEDIA_ID,
            variant="preview",
            component=None,
        )

    @patch("wechat_cli.web.server.read_sns_media", create=True)
    @patch("wechat_cli.web.server.build_sns_context", create=True)
    def test_invalid_media_paths_fail_before_context(self, build_ctx, read_media):
        invalid_paths = [
            f"/api/sns/media//{self.MEDIA_ID}",
            f"/api/sns/media/123/{self.MEDIA_ID}/extra",
            f"/api/sns/media/%31%32%33/{self.MEDIA_ID}",
            f"/api/sns/media/123%2F456/{self.MEDIA_ID}",
            f"/api/sns/media/+123/{self.MEDIA_ID}",
            f"/api/sns/media/-0/{self.MEDIA_ID}",
            f"/api/sns/media/-01/{self.MEDIA_ID}",
            f"/api/sns/media/%20123/{self.MEDIA_ID}",
            f"/api/sns/media/000123/{self.MEDIA_ID}",
            f"/api/sns/media/-9223372036854775809/{self.MEDIA_ID}",
            f"/api/sns/media/9223372036854775808/{self.MEDIA_ID}",
            "/api/sns/media/123/m_0123456789ABCDEF01234567",
            "/api/sns/media/123/m_short",
        ]
        server = self.start_server()
        for path in invalid_paths:
            with self.subTest(path=path):
                status, payload, _headers = self.get_json(server, path)
                self.assertEqual(status, 400)
                self.assertEqual(payload["error"]["code"], "SNS_INVALID_REQUEST")
        build_ctx.assert_not_called()
        read_media.assert_not_called()

    @patch("wechat_cli.web.server.read_sns_media", create=True)
    @patch("wechat_cli.web.server.build_sns_context", create=True)
    def test_invalid_or_duplicate_media_queries_fail_before_context(self, build_ctx, read_media):
        queries = [
            "variant=preview&variant=original",
            "component=still&component=video",
            "download=1&download=1",
            "path=a&path=b",
            "download=0",
            "download=true",
            "filename=x",
            "url=x",
            "token=x",
            "key=x",
            "variant=other",
            "component=other",
        ]
        server = self.start_server()
        for query in queries:
            with self.subTest(query=query):
                status, payload, _headers = self.get_json(
                    server, f"/api/sns/media/123/{self.MEDIA_ID}?{query}"
                )
                self.assertEqual(status, 400)
                self.assertEqual(payload["error"]["code"], "SNS_INVALID_REQUEST")
        build_ctx.assert_not_called()
        read_media.assert_not_called()

    @patch("wechat_cli.web.server.read_sns_media", create=True)
    @patch("wechat_cli.web.server.build_sns_context", create=True)
    def test_signed_int64_post_ids_are_transport_valid(self, build_ctx, read_media):
        build_ctx.return_value = object()
        read_media.return_value = {
            "status": "ready",
            "type": "video",
            "body": b"\x00\x00\x00\x18ftypisomfixture",
            "content_type": "video/mp4",
        }
        server = self.start_server()
        for post_id in ("-9223372036854775808", "-1", "0", "1", "9223372036854775807"):
            with self.subTest(post_id=post_id):
                host, port = server.server_address
                with urlopen(
                    f"http://{host}:{port}/api/sns/media/{post_id}/{self.MEDIA_ID}",
                    timeout=4,
                ) as response:
                    response.read()
                self.assertEqual(response.status, 200)
        self.assertEqual(read_media.call_count, 5)

    @patch("wechat_cli.web.server.build_sns_context", create=True, return_value=object())
    def test_http_component_matrix_uses_real_core_validator(self, _build_ctx):
        server = self.start_server()
        cases = [
            ({"type": "video", "status": "local_found", "reason": None, "_local_path": "fixture.mp4"}, "component=still", 400, "SNS_INVALID_REQUEST"),
            ({"type": "video", "status": "local_found", "reason": None, "_local_path": "fixture.mp4"}, "component=video", 400, "SNS_INVALID_REQUEST"),
            ({"type": "live_photo", "status": "metadata_only", "reason": "local_identity_unproven", "_local_path": None}, "component=still", 404, "SNS_MEDIA_UNAVAILABLE"),
            ({"type": "live_photo", "status": "metadata_only", "reason": "local_identity_unproven", "_local_path": None}, "component=video", 404, "SNS_MEDIA_UNAVAILABLE"),
            ({"type": "live_photo", "status": "metadata_only", "reason": "local_identity_unproven", "_local_path": None}, "", 400, "SNS_INVALID_REQUEST"),
            ({"type": "image", "status": "metadata_only", "reason": "local_identity_unproven", "_local_path": None}, "component=still", 400, "SNS_INVALID_REQUEST"),
        ]
        for resolved, query, expected_status, expected_code in cases:
            with self.subTest(type=resolved["type"], query=query), patch(
                "wechat_cli.core.sns_media.resolve_sns_media", return_value={"post_id": "123", "media_id": self.MEDIA_ID, **resolved}
            ), patch("wechat_cli.core.sns_media.read_media_file_payload"):
                suffix = f"?{query}" if query else ""
                status, payload, _headers = self.get_json(
                    server, f"/api/sns/media/123/{self.MEDIA_ID}{suffix}"
                )
            self.assertEqual(status, expected_status)
            self.assertEqual(payload["error"]["code"], expected_code)

    @patch("wechat_cli.web.server.read_sns_media")
    @patch("wechat_cli.core.sns.DBCache")
    def test_direct_media_endpoint_uses_env_selected_account(self, cache_cls, read_media):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            db_dir = root / "account-env" / "db_storage"
            (db_dir / "sns").mkdir(parents=True)
            (db_dir / "sns" / "sns.db").write_bytes(b"encrypted")
            keys = root / "all_keys.json"
            keys.write_text(json.dumps({"sns/sns.db": {"enc_key": "00" * 32, "salt": "11" * 16}}), encoding="utf-8")
            config = root / "env-config.json"
            config.write_text(json.dumps({"db_dir": str(db_dir), "keys_file": str(keys), "decrypted_dir": str(root / "decrypted")}), encoding="utf-8")
            decrypted = root / "sns-dec.db"
            decrypted.write_bytes(b"sqlite")
            cache_cls.return_value.get.return_value = str(decrypted)
            read_media.return_value = {"status": "ready", "type": "video", "body": b"\x00\x00\x00\x18ftypisomfixture", "content_type": "video/mp4"}
            with patch.dict(os.environ, {"WECHAT_CLI_CONFIG": str(config)}, clear=False):
                server = self.start_server()
                host, port = server.server_address
                with urlopen(f"http://{host}:{port}/api/sns/media/123/{self.MEDIA_ID}", timeout=4) as response:
                    response.read()
            self.assertEqual(response.status, 200)
            read_media.assert_called_once()
            ctx = read_media.call_args.args[0]
            self.assertEqual(ctx.config_path, os.path.abspath(str(config)))
            self.assertEqual(ctx.db_dir, str(db_dir))

    def test_controlled_sns_media_errors_have_stable_http_mapping(self):
        cases = [
            ("SNS_INVALID_REQUEST", 400, False, None),
            ("SNS_MEDIA_UNAVAILABLE", 404, True, "local_missing"),
            ("SNS_MEDIA_UNSUPPORTED", 415, False, "unsupported_type"),
            ("SNS_SCHEMA_UNSUPPORTED", 422, False, None),
            ("SNS_DB_NOT_FOUND", 503, False, None),
            ("SNS_KEY_NOT_FOUND", 503, False, None),
            ("SNS_INTERNAL_ERROR", 500, False, None),
        ]
        server = self.start_server()
        for code, expected_status, retryable, reason in cases:
            details = (
                {"post_id": "123", "media_id": self.MEDIA_ID, "reason": reason}
                if reason
                else None
            )
            error = SnsError(code, "safe-message", retryable=retryable, details=details)
            with self.subTest(code=code), patch(
                "wechat_cli.web.server.build_sns_context", create=True, return_value=object()
            ), patch("wechat_cli.web.server.read_sns_media", create=True, side_effect=error):
                status, payload, _headers = self.get_json(
                    server, f"/api/sns/media/123/{self.MEDIA_ID}"
                )
            self.assertEqual(status, expected_status)
            self.assertEqual(payload["error"]["code"], code)
            self.assertEqual(payload["error"]["message"], "safe-message")
            self.assertIs(payload["error"]["retryable"], retryable)

    @patch("wechat_cli.web.server.read_sns_media", create=True)
    @patch("wechat_cli.web.server.build_sns_context", create=True)
    def test_preview_inline_and_download_attachment_use_server_filename(self, build_ctx, read_media):
        build_ctx.return_value = object()
        read_media.return_value = {
            "status": "ready",
            "type": "video",
            "body": b"\x00\x00\x00\x18ftypisomfixture",
            "content_type": "video/mp4",
        }
        server = self.start_server()
        host, port = server.server_address
        with urlopen(
            f"http://{host}:{port}/api/sns/media/123/{self.MEDIA_ID}?variant=preview",
            timeout=4,
        ) as response:
            response.read()
            inline = response.headers.get("Content-Disposition", "")
        with urlopen(
            f"http://{host}:{port}/api/sns/media/123/{self.MEDIA_ID}?variant=original&download=1",
            timeout=4,
        ) as response:
            response.read()
            attachment = response.headers.get("Content-Disposition", "")
        self.assertTrue(inline.startswith("inline;"))
        self.assertTrue(attachment.startswith("attachment;"))
        self.assertIn(f"sns-123-{self.MEDIA_ID}.mp4", attachment)
        self.assertNotIn("fixture", attachment)

    def test_csp_allows_only_same_origin_and_blob_video(self):
        server = self.start_server()
        host, port = server.server_address
        with urlopen(f"http://{host}:{port}/api/health", timeout=4) as response:
            response.read()
            csp = response.headers.get("Content-Security-Policy", "")
        self.assertIn("media-src 'self' blob:;", csp)
        self.assertIn("connect-src 'self';", csp)
        self.assertNotIn("media-src http:", csp)
        self.assertNotIn("media-src https:", csp)
        self.assertNotIn("media-src *", csp)


if __name__ == "__main__":
    unittest.main()
