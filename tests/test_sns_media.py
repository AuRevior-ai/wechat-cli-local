import hashlib
import tempfile
import unittest
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch


class SnsMediaCandidateTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name).resolve()
        self.account = self.root / "account"
        self.db_dir = self.account / "db_storage"
        self.db_dir.mkdir(parents=True)
        self.ctx = SimpleNamespace(db_dir=str(self.db_dir))

    def _candidate(self, month="2026-08", shard="ab", name=None):
        name = name or ("a" * 30 + ".mp4")
        path = self.account / "cache" / month / "Sns" / "Video" / shard / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"\x00\x00\x00\x18ftypisomfixture")
        return path

    def test_try_post_month_accepts_only_usable_canonical_int(self):
        from wechat_cli.core.sns_media import _try_post_month

        value = int(datetime(2026, 8, 15, 12, 0, 0).timestamp())
        self.assertEqual(_try_post_month(value), "2026-08")
        for invalid in (None, "1780000000", True, 999999999999999999):
            with self.subTest(invalid=invalid):
                self.assertIsNone(_try_post_month(invalid))

    def test_iter_video_candidates_accepts_only_fixed_family(self):
        from wechat_cli.core.sns_media import _iter_video_candidates

        expected = self._candidate()
        self._candidate(shard="not-hex")
        self._candidate(name="b" * 29 + ".mp4")
        self._candidate(name="b" * 30 + ".jpg")
        self._candidate(name="b" * 30 + ".tmp")
        self._candidate(month="2026-07", name="c" * 30 + ".mp4")

        found = list(_iter_video_candidates(self.ctx, "2026-08"))

        self.assertEqual(found, [expected.resolve()])

    def test_iter_video_candidates_excludes_resolved_path_outside_account_root(self):
        from wechat_cli.core.sns_media import _iter_video_candidates

        candidate = self._candidate()
        outside = self.root / "outside.mp4"
        outside.write_bytes(candidate.read_bytes())
        original_resolve = Path.resolve

        def fake_resolve(path, *args, **kwargs):
            if path == candidate:
                return outside
            return original_resolve(path, *args, **kwargs)

        with patch.object(Path, "resolve", autospec=True, side_effect=fake_resolve):
            found = list(_iter_video_candidates(self.ctx, "2026-08"))

        self.assertEqual(found, [])

    def test_video_root_escape_is_rejected_before_enumeration(self):
        from wechat_cli.core.sns_media import _iter_video_candidates

        video_root = self.account / "cache" / "2026-08" / "Sns" / "Video"
        video_root.mkdir(parents=True)
        outside = self.root / "outside-video-root"
        outside.mkdir()
        original_resolve = Path.resolve
        original_iterdir = Path.iterdir

        def fake_resolve(path, *args, **kwargs):
            if path == video_root:
                return outside
            return original_resolve(path, *args, **kwargs)

        def guarded_iterdir(path):
            if path == video_root:
                raise AssertionError("escaped video root must not be enumerated")
            return original_iterdir(path)

        with patch.object(Path, "resolve", autospec=True, side_effect=fake_resolve), patch.object(
            Path, "iterdir", autospec=True, side_effect=guarded_iterdir
        ):
            found = list(_iter_video_candidates(self.ctx, "2026-08"))

        self.assertEqual(found, [])

    def test_shard_escape_is_rejected_before_shard_enumeration(self):
        from wechat_cli.core.sns_media import _iter_video_candidates

        candidate = self._candidate()
        shard = candidate.parent
        outside = self.root / "outside-shard"
        outside.mkdir()
        original_resolve = Path.resolve
        original_iterdir = Path.iterdir

        def fake_resolve(path, *args, **kwargs):
            if path == shard:
                return outside
            return original_resolve(path, *args, **kwargs)

        def guarded_iterdir(path):
            if path == shard:
                raise AssertionError("escaped shard must not be enumerated")
            return original_iterdir(path)

        with patch.object(Path, "resolve", autospec=True, side_effect=fake_resolve), patch.object(
            Path, "iterdir", autospec=True, side_effect=guarded_iterdir
        ):
            found = list(_iter_video_candidates(self.ctx, "2026-08"))

        self.assertEqual(found, [])


class SnsMediaIdentityTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name).resolve()
        self.account = self.root / "account"
        self.db_dir = self.account / "db_storage"
        self.db_dir.mkdir(parents=True)
        self.ctx = SimpleNamespace(db_dir=str(self.db_dir))
        self.post_id = "123"
        self.media_id = "m_" + "1" * 24
        self.body = b"\x00\x00\x00\x18ftypisomfixture"
        self.create_time = int(datetime(2026, 8, 15, 12, 0, 0).timestamp())

    def _video_root(self):
        root = self.account / "cache" / "2026-08" / "Sns" / "Video" / "ab"
        root.mkdir(parents=True, exist_ok=True)
        return root

    def _source(self, **overrides):
        source = {
            "media_id": self.media_id,
            "type": "video",
            "status": "metadata_only",
            "reason": "remote_deferred",
            "_source_type": 6,
            "_source_url_md5": hashlib.md5(self.body).hexdigest(),
            "create_time": self.create_time,
        }
        source.update(overrides)
        return source

    def test_hash_identity_unique_match_becomes_local_found(self):
        from wechat_cli.core.sns_media import resolve_sns_media

        candidate = self._video_root() / ("a" * 30 + ".mp4")
        candidate.write_bytes(self.body)
        with patch("wechat_cli.core.sns_media.get_sns_media_source", return_value=self._source()):
            resolved = resolve_sns_media(self.ctx, self.post_id, self.media_id)

        self.assertEqual(resolved["status"], "local_found")
        self.assertIsNone(resolved["reason"])
        self.assertEqual(Path(resolved["_local_path"]), candidate.resolve())

    def test_hash_identity_zero_and_many_fail_closed(self):
        from wechat_cli.core.sns_media import resolve_sns_media

        root = self._video_root()
        (root / ("a" * 30 + ".mp4")).write_bytes(b"different")
        with patch("wechat_cli.core.sns_media.get_sns_media_source", return_value=self._source()):
            missing = resolve_sns_media(self.ctx, self.post_id, self.media_id)
        self.assertEqual((missing["status"], missing["reason"], missing["_local_path"]), ("metadata_only", "local_missing", None))

        (root / ("b" * 30 + ".mp4")).write_bytes(self.body)
        (root / ("c" * 30 + ".mp4")).write_bytes(self.body)
        with patch("wechat_cli.core.sns_media.get_sns_media_source", return_value=self._source()):
            ambiguous = resolve_sns_media(self.ctx, self.post_id, self.media_id)
        self.assertEqual((ambiguous["status"], ambiguous["reason"], ambiguous["_local_path"]), ("metadata_only", "local_identity_ambiguous", None))

    def test_missing_md5_or_unusable_time_never_scans(self):
        from wechat_cli.core.sns_media import resolve_sns_media

        cases = [
            self._source(_source_url_md5=""),
            self._source(_source_url_md5="not-md5"),
            self._source(create_time=None),
            self._source(create_time="invalid"),
        ]
        for source in cases:
            with self.subTest(source=source), patch(
                "wechat_cli.core.sns_media.get_sns_media_source", return_value=source
            ), patch("wechat_cli.core.sns_media._iter_video_candidates") as iterator, patch(
                "wechat_cli.core.sns_media._stream_md5"
            ) as stream_md5:
                resolved = resolve_sns_media(self.ctx, self.post_id, self.media_id)
            self.assertEqual(resolved["reason"], "local_identity_unproven")
            iterator.assert_not_called()
            stream_md5.assert_not_called()

    def test_image_and_live_photo_never_scan(self):
        from wechat_cli.core.sns_media import resolve_sns_media

        for media_type in ("image", "live_photo"):
            with self.subTest(media_type=media_type), patch(
                "wechat_cli.core.sns_media.get_sns_media_source",
                return_value=self._source(type=media_type, _source_type=2),
            ), patch("wechat_cli.core.sns_media._iter_video_candidates") as iterator:
                resolved = resolve_sns_media(self.ctx, self.post_id, self.media_id)
            self.assertEqual(resolved["reason"], "local_identity_unproven")
            iterator.assert_not_called()

    def test_identity_proof_file_disappears_is_controlled_local_missing(self):
        from wechat_cli.core.sns import SnsError
        from wechat_cli.core.sns_media import resolve_sns_media

        with patch("wechat_cli.core.sns_media.get_sns_media_source", return_value=self._source()), patch(
            "wechat_cli.core.sns_media._iter_video_candidates", return_value=[Path("fixture.mp4")]
        ), patch("wechat_cli.core.sns_media._stream_md5", side_effect=FileNotFoundError("secret-path")):
            with self.assertRaises(SnsError) as caught:
                resolve_sns_media(self.ctx, self.post_id, self.media_id)
        self.assertEqual(caught.exception.code, "SNS_MEDIA_UNAVAILABLE")
        self.assertEqual(caught.exception.details, {"post_id": self.post_id, "media_id": self.media_id, "reason": "local_missing"})

    def test_identity_proof_permission_or_io_error_is_controlled(self):
        from wechat_cli.core.sns import SnsError
        from wechat_cli.core.sns_media import resolve_sns_media

        for failure in (PermissionError("secret"), OSError("secret")):
            with self.subTest(failure=type(failure).__name__), patch(
                "wechat_cli.core.sns_media.get_sns_media_source", return_value=self._source()
            ), patch("wechat_cli.core.sns_media._iter_video_candidates", side_effect=failure):
                with self.assertRaises(SnsError) as caught:
                    resolve_sns_media(self.ctx, self.post_id, self.media_id)
            self.assertEqual(caught.exception.code, "SNS_MEDIA_UNAVAILABLE")
            self.assertEqual(caught.exception.details, {"post_id": self.post_id, "media_id": self.media_id, "reason": "io_error"})

    def test_unsupported_source_error_propagates(self):
        from wechat_cli.core.sns import SnsError
        from wechat_cli.core.sns_media import resolve_sns_media

        error = SnsError(
            "SNS_MEDIA_UNSUPPORTED",
            "unsupported",
            retryable=False,
            details={"post_id": self.post_id, "media_id": self.media_id, "reason": "unsupported_type"},
        )
        with patch("wechat_cli.core.sns_media.get_sns_media_source", side_effect=error):
            with self.assertRaises(SnsError) as caught:
                resolve_sns_media(self.ctx, self.post_id, self.media_id)
        self.assertIs(caught.exception, error)


class SnsMediaReadTests(unittest.TestCase):
    def setUp(self):
        self.ctx = SimpleNamespace(db_dir=r"C:\fixture\db_storage")
        self.post_id = "123"
        self.media_id = "m_" + "2" * 24
        self.valid_body = b"\x00\x00\x00\x18ftypisomfixture"

    def _resolved(self, **overrides):
        value = {
            "post_id": self.post_id,
            "media_id": self.media_id,
            "type": "video",
            "status": "local_found",
            "reason": None,
            "_local_path": r"C:\fixture\cache\video.mp4",
        }
        value.update(overrides)
        return value

    def _assert_error(self, error, code, reason=None):
        self.assertEqual(error.code, code)
        if reason is None:
            self.assertFalse(error.details)
        else:
            self.assertEqual(
                error.details,
                {"post_id": self.post_id, "media_id": self.media_id, "reason": reason},
            )

    def test_standard_video_component_matrix(self):
        from wechat_cli.core.sns import SnsError
        from wechat_cli.core.sns_media import read_sns_media

        for component in ("still", "video"):
            with self.subTest(component=component), patch(
                "wechat_cli.core.sns_media.resolve_sns_media", return_value=self._resolved()
            ), patch("wechat_cli.core.sns_media.read_media_file_payload") as reader:
                with self.assertRaises(SnsError) as caught:
                    read_sns_media(self.ctx, self.post_id, self.media_id, component=component)
            self._assert_error(caught.exception, "SNS_INVALID_REQUEST")
            reader.assert_not_called()

    def test_live_photo_component_matrix_stays_unproven(self):
        from wechat_cli.core.sns import SnsError
        from wechat_cli.core.sns_media import read_sns_media

        resolved = self._resolved(type="live_photo", status="metadata_only", reason="local_identity_unproven", _local_path=None)
        for component in ("still", "video"):
            with self.subTest(component=component), patch(
                "wechat_cli.core.sns_media.resolve_sns_media", return_value=resolved
            ), patch("wechat_cli.core.sns_media.read_media_file_payload") as reader:
                with self.assertRaises(SnsError) as caught:
                    read_sns_media(self.ctx, self.post_id, self.media_id, component=component)
            self._assert_error(caught.exception, "SNS_MEDIA_UNAVAILABLE", "local_identity_unproven")
            reader.assert_not_called()

        with patch("wechat_cli.core.sns_media.resolve_sns_media", return_value=resolved):
            with self.assertRaises(SnsError) as caught:
                read_sns_media(self.ctx, self.post_id, self.media_id)
        self._assert_error(caught.exception, "SNS_INVALID_REQUEST")

    def test_image_component_matrix_stays_unproven(self):
        from wechat_cli.core.sns import SnsError
        from wechat_cli.core.sns_media import read_sns_media

        resolved = self._resolved(type="image", status="metadata_only", reason="local_identity_unproven", _local_path=None)
        with patch("wechat_cli.core.sns_media.resolve_sns_media", return_value=resolved):
            with self.assertRaises(SnsError) as caught:
                read_sns_media(self.ctx, self.post_id, self.media_id)
        self._assert_error(caught.exception, "SNS_MEDIA_UNAVAILABLE", "local_identity_unproven")

        for component in ("still", "video"):
            with self.subTest(component=component), patch(
                "wechat_cli.core.sns_media.resolve_sns_media", return_value=resolved
            ):
                with self.assertRaises(SnsError) as caught:
                    read_sns_media(self.ctx, self.post_id, self.media_id, component=component)
            self._assert_error(caught.exception, "SNS_INVALID_REQUEST")

    def test_metadata_only_resolver_states_become_controlled_unavailable(self):
        from wechat_cli.core.sns import SnsError
        from wechat_cli.core.sns_media import read_sns_media

        for reason in ("local_missing", "local_identity_ambiguous", "local_identity_unproven"):
            resolved = self._resolved(status="metadata_only", reason=reason, _local_path=None)
            with self.subTest(reason=reason), patch(
                "wechat_cli.core.sns_media.resolve_sns_media", return_value=resolved
            ):
                with self.assertRaises(SnsError) as caught:
                    read_sns_media(self.ctx, self.post_id, self.media_id)
            self._assert_error(caught.exception, "SNS_MEDIA_UNAVAILABLE", reason)

    def test_payload_read_file_disappears_or_io_failure_is_controlled(self):
        from wechat_cli.core.sns import SnsError
        from wechat_cli.core.sns_media import read_sns_media

        cases = [
            (FileNotFoundError("secret"), "local_missing"),
            (PermissionError("secret"), "io_error"),
            (OSError("secret"), "io_error"),
        ]
        for failure, reason in cases:
            with self.subTest(reason=reason), patch(
                "wechat_cli.core.sns_media.resolve_sns_media", return_value=self._resolved()
            ), patch("wechat_cli.core.sns_media.read_media_file_payload", side_effect=failure):
                with self.assertRaises(SnsError) as caught:
                    read_sns_media(self.ctx, self.post_id, self.media_id)
            self._assert_error(caught.exception, "SNS_MEDIA_UNAVAILABLE", reason)

    def test_valid_mp4_payload_becomes_ready(self):
        from wechat_cli.core.sns_media import read_sns_media

        payload = {"body": self.valid_body, "content_type": "video/mp4", "filename": "ignored.mp4"}
        with patch("wechat_cli.core.sns_media.resolve_sns_media", return_value=self._resolved()), patch(
            "wechat_cli.core.sns_media.read_media_file_payload", return_value=payload
        ) as reader:
            result = read_sns_media(self.ctx, self.post_id, self.media_id)
        self.assertEqual(result, {"status": "ready", "type": "video", "body": self.valid_body, "content_type": "video/mp4"})
        reader.assert_called_once_with(r"C:\fixture\cache\video.mp4", db_dir=self.ctx.db_dir)

    def test_invalid_mp4_payload_is_unsupported(self):
        from wechat_cli.core.sns import SnsError
        from wechat_cli.core.sns_media import read_sns_media

        cases = [
            {"body": self.valid_body, "content_type": "application/octet-stream"},
            {"body": b"short", "content_type": "video/mp4"},
            {"body": b"\x00\x00\x00\x18NOPEisomfixture", "content_type": "video/mp4"},
        ]
        for payload in cases:
            with self.subTest(payload=payload), patch(
                "wechat_cli.core.sns_media.resolve_sns_media", return_value=self._resolved()
            ), patch("wechat_cli.core.sns_media.read_media_file_payload", return_value=payload):
                with self.assertRaises(SnsError) as caught:
                    read_sns_media(self.ctx, self.post_id, self.media_id)
            self._assert_error(caught.exception, "SNS_MEDIA_UNSUPPORTED", "invalid_media_payload")


if __name__ == "__main__":
    unittest.main()
