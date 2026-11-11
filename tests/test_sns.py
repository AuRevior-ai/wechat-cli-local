import json
import os
import sqlite3
import tempfile
import unittest
from contextlib import closing
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from wechat_cli.core.sns import (
    SnsError,
    _readonly_connection,
    _validate_sns_db_predicate,
    build_sns_context,
)


def create_sns_db(path, *, include_pack_info=True, column_overrides=None):
    column_overrides = column_overrides or {}
    tid_decl = column_overrides.get("tid", "tid INTEGER PRIMARY KEY")
    username_decl = column_overrides.get("user_name", "user_name TEXT")
    content_decl = column_overrides.get("content", "content TEXT")
    pack = ", pack_info_buf TEXT" if include_pack_info else ""
    conn = sqlite3.connect(path)
    try:
        conn.execute(
            "CREATE TABLE SnsTimeLine ("
            f"{tid_decl}, {username_decl}, {content_decl}{pack})"
        )
        conn.commit()
    finally:
        conn.close()


def insert_invalid_utf8_text(path, *, tid=1, username="wxid_fixture"):
    conn = sqlite3.connect(path)
    try:
        conn.execute(
            "INSERT INTO SnsTimeLine(tid, user_name, content) "
            "VALUES (?, ?, CAST(X'80FF' AS TEXT))",
            (tid, username),
        )
        conn.commit()
    finally:
        conn.close()


def insert_sns_xml_bytes(path, tid, raw, *, username="wxid_fixture"):
    conn = sqlite3.connect(path)
    try:
        conn.execute(
            "INSERT INTO SnsTimeLine(tid, user_name, content) "
            "VALUES (?, ?, CAST(? AS TEXT))",
            (tid, username, sqlite3.Binary(raw)),
        )
        conn.commit()
    finally:
        conn.close()


def sns_xml(*, username="wxid_fixture", create_time=1780000000, content="hello"):
    return f"""<SnsDataItem>
<TimelineObject>
  <username>{username}</username>
  <createTime>{create_time}</createTime>
  <contentDesc>{content}</contentDesc>
  <location poiName="Fixture Park" latitude="0" longitude="0" />
  <ContentObject><mediaList /></ContentObject>
</TimelineObject>
<LocalExtraInfo><like_user_list/><comment_user_list/></LocalExtraInfo>
</SnsDataItem>"""


class SnsContextTests(unittest.TestCase):
    def _fixture(self):
        tmp = tempfile.TemporaryDirectory()
        root = Path(tmp.name)
        db_dir = root / "account" / "db_storage"
        (db_dir / "sns").mkdir(parents=True)
        (db_dir / "sns" / "sns.db").write_bytes(b"encrypted")
        config = root / "config.json"
        keys = root / "all_keys.json"
        config.write_text(
            json.dumps(
                {
                    "db_dir": str(db_dir),
                    "keys_file": str(keys),
                    "decrypted_dir": str(root / "decrypted"),
                }
            ),
            encoding="utf-8",
        )
        return tmp, root, db_dir, config, keys

    def _configured_fixture(self, label, cache_cls):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        root = Path(tmp.name)
        db_dir = root / f"account-{label}" / "db_storage"
        (db_dir / "sns").mkdir(parents=True)
        (db_dir / "sns" / "sns.db").write_bytes(b"encrypted")
        keys = root / "all_keys.json"
        keys.write_text(
            json.dumps(
                {
                    "sns/sns.db": {
                        "enc_key": "00" * 32,
                        "salt": "11" * 16,
                    }
                }
            ),
            encoding="utf-8",
        )
        decrypted = root / f"sns-{label}.db"
        decrypted.write_bytes(b"sqlite")
        config = root / f"config-{label}.json"
        config.write_text(
            json.dumps(
                {
                    "db_dir": str(db_dir),
                    "keys_file": str(keys),
                    "decrypted_dir": str(root / "decrypted"),
                }
            ),
            encoding="utf-8",
        )
        cache_cls.return_value.get.return_value = str(decrypted)
        return config, db_dir, decrypted

    def test_missing_keys_file_is_sns_key_not_found_without_key_scan(self):
        tmp, root, db_dir, config, keys = self._fixture()
        self.addCleanup(tmp.cleanup)

        with patch("wechat_cli.keys.extract_keys") as extract:
            with self.assertRaises(SnsError) as caught:
                build_sns_context(str(config))

        self.assertEqual(caught.exception.code, "SNS_KEY_NOT_FOUND")
        self.assertEqual(
            caught.exception.details,
            {"reason": "keys_file_missing"},
        )
        extract.assert_not_called()

    def test_unreadable_keys_file_is_sns_key_not_found(self):
        tmp, root, db_dir, config, keys = self._fixture()
        self.addCleanup(tmp.cleanup)
        keys.write_text("[]", encoding="utf-8")

        with self.assertRaises(SnsError) as caught:
            build_sns_context(str(config))

        self.assertEqual(caught.exception.code, "SNS_KEY_NOT_FOUND")
        self.assertEqual(
            caught.exception.details,
            {"reason": "keys_file_unreadable"},
        )

    def test_missing_sns_key_is_sns_key_not_found(self):
        tmp, root, db_dir, config, keys = self._fixture()
        self.addCleanup(tmp.cleanup)
        keys.write_text(json.dumps({"message/message_0.db": {}}), encoding="utf-8")

        with self.assertRaises(SnsError) as caught:
            build_sns_context(str(config))

        self.assertEqual(caught.exception.code, "SNS_KEY_NOT_FOUND")
        self.assertEqual(caught.exception.details, {"reason": "sns_key_missing"})

    def test_missing_sns_source_db_is_sns_db_not_found(self):
        tmp, root, db_dir, config, keys = self._fixture()
        self.addCleanup(tmp.cleanup)
        (db_dir / "sns" / "sns.db").unlink()
        keys.write_text(json.dumps({"sns/sns.db": {}}), encoding="utf-8")

        with self.assertRaises(SnsError) as caught:
            build_sns_context(str(config))

        self.assertEqual(caught.exception.code, "SNS_DB_NOT_FOUND")

    @patch("wechat_cli.core.sns.DBCache")
    def test_explicit_config_path_wins_over_env(self, cache_cls):
        explicit, explicit_db, explicit_dec = self._configured_fixture(
            "explicit", cache_cls
        )
        env_cfg, _, _ = self._configured_fixture("env", cache_cls)
        cache_cls.return_value.get.return_value = str(explicit_dec)

        with patch.dict(
            os.environ,
            {"WECHAT_CLI_CONFIG": str(env_cfg)},
            clear=False,
        ):
            ctx = build_sns_context(str(explicit))

        self.assertEqual(ctx.config_path, os.path.abspath(str(explicit)))
        self.assertEqual(ctx.db_dir, str(explicit_db))

    @patch("wechat_cli.core.sns.DBCache")
    def test_env_config_is_used_when_explicit_path_is_absent(self, cache_cls):
        env_cfg, env_db, _ = self._configured_fixture("env", cache_cls)

        with patch.dict(
            os.environ,
            {"WECHAT_CLI_CONFIG": str(env_cfg)},
            clear=False,
        ):
            ctx = build_sns_context()

        self.assertEqual(ctx.config_path, os.path.abspath(str(env_cfg)))
        self.assertEqual(ctx.db_dir, str(env_db))

    @patch("wechat_cli.core.sns.DBCache")
    def test_default_config_is_used_when_explicit_and_env_are_absent(self, cache_cls):
        default_cfg, default_db, _ = self._configured_fixture("default", cache_cls)

        with patch("wechat_cli.core.sns.CONFIG_FILE", str(default_cfg)), patch.dict(
            os.environ, {}, clear=False
        ):
            os.environ.pop("WECHAT_CLI_CONFIG", None)
            ctx = build_sns_context()

        self.assertEqual(ctx.config_path, os.path.abspath(str(default_cfg)))
        self.assertEqual(ctx.db_dir, str(default_db))

    @patch("wechat_cli.core.sns.DBCache")
    def test_context_only_gets_sns_db_and_never_refreshes_message_shards(
        self, cache_cls
    ):
        tmp, root, db_dir, config, keys = self._fixture()
        self.addCleanup(tmp.cleanup)
        keys.write_text(
            json.dumps(
                {
                    "sns/sns.db": {
                        "enc_key": "00" * 32,
                        "salt": "11" * 16,
                    }
                }
            ),
            encoding="utf-8",
        )
        decrypted = root / "sns-dec.db"
        decrypted.write_bytes(b"sqlite")
        cache_cls.return_value.get.return_value = str(decrypted)

        with patch(
            "wechat_cli.core.context.AppContext",
            side_effect=AssertionError("sns must not construct AppContext"),
        ), patch("wechat_cli.keys.extract_keys") as extract:
            ctx = build_sns_context(str(config))

        self.assertEqual(ctx.sns_db_rel_key, "sns/sns.db")
        self.assertEqual(ctx.sns_db_path, str(decrypted))
        cache_cls.return_value.get.assert_called_once_with("sns/sns.db")
        extract.assert_not_called()


class SnsXmlSafetyTests(unittest.TestCase):
    def test_safe_xml_parses(self):
        from wechat_cli.core.sns import _parse_sns_xml

        root, reason = _parse_sns_xml(sns_xml().encode("utf-8"))
        self.assertEqual(root.tag, "SnsDataItem")
        self.assertIsNone(reason)

    def test_invalid_utf8_is_record_level_decode_failure(self):
        from wechat_cli.core.sns import _parse_sns_xml

        root, reason = _parse_sns_xml(b"\x80\xff")
        self.assertIsNone(root)
        self.assertEqual(reason, "xml_decode_failed")

    def test_doctype_and_entity_are_rejected_before_et_parse(self):
        from wechat_cli.core.sns import _parse_sns_xml

        for raw in (
            b"<!DOCTYPE x><SnsDataItem/>",
            b"<!ENTITY x 'y'><SnsDataItem/>",
        ):
            root, reason = _parse_sns_xml(raw)
            self.assertIsNone(root)
            self.assertEqual(reason, "xml_unsafe_markup")

    def test_oversized_xml_is_rejected(self):
        from wechat_cli.core.sns import _parse_sns_xml

        root, reason = _parse_sns_xml(
            b"<SnsDataItem>" + b"x" * (256 * 1024) + b"</SnsDataItem>"
        )
        self.assertIsNone(root)
        self.assertEqual(reason, "xml_too_large")

    def test_malformed_xml_is_record_level_parse_failure(self):
        from wechat_cli.core.sns import _parse_sns_xml

        root, reason = _parse_sns_xml(b"<SnsDataItem>")
        self.assertIsNone(root)
        self.assertEqual(reason, "xml_parse_failed")

    def test_parser_requires_raw_bytes(self):
        from wechat_cli.core.sns import _parse_sns_xml

        with self.assertRaises(TypeError):
            _parse_sns_xml("<SnsDataItem/>")


class SnsSemanticProbeTests(unittest.TestCase):
    def _new_db(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        path = Path(tmp.name) / "sns.db"
        create_sns_db(path)
        return path

    def assert_schema_unsupported(self, path):
        from wechat_cli.core.sns import get_sns_schema

        with self.assertRaises(SnsError) as caught:
            get_sns_schema(path)
        error = caught.exception
        self.assertEqual(error.code, "SNS_SCHEMA_UNSUPPORTED")
        self.assertFalse(error.retryable)
        self.assertFalse(error.details)
        return error

    def test_semantic_probe_accepts_supported_sns_data_item(self):
        from wechat_cli.core.sns import get_sns_schema

        path = self._new_db()
        insert_sns_xml_bytes(path, 2, sns_xml().encode("utf-8"))

        schema = get_sns_schema(path)

        self.assertEqual(schema["adapter"], "KnownSchemaContract-v1")

    def test_semantic_probe_accepts_empty_timeline_after_db_predicate(self):
        from wechat_cli.core.sns import get_sns_schema

        path = self._new_db()

        schema = get_sns_schema(path)

        self.assertEqual(schema["adapter"], "KnownSchemaContract-v1")

    def test_semantic_probe_ignores_malformed_sample_when_valid_sample_exists(self):
        from wechat_cli.core.sns import get_sns_schema

        path = self._new_db()
        insert_sns_xml_bytes(path, 3, b"<SnsDataItem>")
        insert_sns_xml_bytes(path, 2, sns_xml().encode("utf-8"))

        schema = get_sns_schema(path)

        self.assertEqual(schema["adapter"], "KnownSchemaContract-v1")

    def test_semantic_probe_rejects_when_all_nonempty_samples_are_unparseable(self):
        path = self._new_db()
        insert_sns_xml_bytes(path, 4, b"<SnsDataItem>")
        insert_sns_xml_bytes(path, 3, b"<!DOCTYPE x><SnsDataItem/>")
        insert_sns_xml_bytes(path, 2, b"\x80\xff")

        self.assert_schema_unsupported(path)

    def test_semantic_probe_rejects_successfully_parsed_wrong_root(self):
        path = self._new_db()
        insert_sns_xml_bytes(path, 2, b"<SomeOtherRoot/>")

        self.assert_schema_unsupported(path)

    def test_semantic_probe_rejects_mixed_valid_and_wrong_successful_roots(self):
        path = self._new_db()
        insert_sns_xml_bytes(path, 3, sns_xml().encode("utf-8"))
        insert_sns_xml_bytes(path, 2, b"<SomeOtherRoot/>")

        self.assert_schema_unsupported(path)

    def test_semantic_probe_requires_one_sample_with_timeline_and_local_extra(self):
        path = self._new_db()
        insert_sns_xml_bytes(
            path,
            3,
            b"<SnsDataItem><TimelineObject/></SnsDataItem>",
        )
        insert_sns_xml_bytes(
            path,
            2,
            b"<SnsDataItem><LocalExtraInfo/></SnsDataItem>",
        )

        self.assert_schema_unsupported(path)


class SnsNormalizationTests(unittest.TestCase):
    def test_media_id_is_stable_and_canonical(self):
        from wechat_cli.core.sns import _canonical_media_id

        first = _canonical_media_id("123", 0, "source-1")
        again = _canonical_media_id("123", 0, "source-1")
        other = _canonical_media_id("123", 1, "source-1")

        self.assertEqual(first, again)
        self.assertRegex(first, r"^m_[0-9a-f]{24}$")
        self.assertNotEqual(first, other)

    def test_media_id_remains_stable_when_source_id_is_missing(self):
        from wechat_cli.core.sns import _canonical_media_id

        self.assertEqual(
            _canonical_media_id("123", 2, ""),
            _canonical_media_id("123", 2, None),
        )

    def test_base_post_maps_display_time_content_and_sparse_location(self):
        from datetime import datetime

        from wechat_cli.core.sns import _normalize_post_base

        raw = b"""<SnsDataItem>
<TimelineObject>
  <username>wxid_fixture</username>
  <createTime>1780000000</createTime>
  <contentDesc>hello</contentDesc>
  <location poiName="Fixture Park" poiAddress="Road 1" latitude="0" longitude="0" ignored="secret" />
  <ContentObject><mediaList /></ContentObject>
</TimelineObject>
<LocalExtraInfo><like_user_list/><comment_user_list/></LocalExtraInfo>
</SnsDataItem>"""
        post = _normalize_post_base(
            123,
            "wxid_fixture",
            raw,
            {"wxid_fixture": "Fixture User"},
        )

        self.assertEqual(post["post_id"], "123")
        self.assertEqual(post["username"], "wxid_fixture")
        self.assertEqual(post["display_name"], "Fixture User")
        self.assertEqual(post["create_time"], 1780000000)
        self.assertEqual(
            post["time"],
            datetime.fromtimestamp(1780000000).strftime("%Y-%m-%d %H:%M"),
        )
        self.assertEqual(post["content"], "hello")
        self.assertEqual(
            post["location"],
            {
                "poiName": "Fixture Park",
                "poiAddress": "Road 1",
                "latitude": "0",
                "longitude": "0",
            },
        )
        self.assertNotIn("ignored", post["location"])
        self.assertEqual(post["parse_status"], "ok")
        self.assertEqual(post["warnings"], [])

    def test_base_post_falls_back_to_username_without_display_mapping(self):
        from wechat_cli.core.sns import _normalize_post_base

        post = _normalize_post_base(
            123,
            "wxid_fixture",
            sns_xml().encode("utf-8"),
            {},
        )

        self.assertEqual(post["display_name"], "wxid_fixture")

    def test_base_post_username_mismatch_is_partial(self):
        from wechat_cli.core.sns import _normalize_post_base

        post = _normalize_post_base(
            123,
            "wxid_table",
            sns_xml(username="wxid_xml").encode("utf-8"),
            {},
        )

        self.assertEqual(post["username"], "wxid_table")
        self.assertEqual(post["parse_status"], "partial")
        self.assertIn("username_mismatch", post["warnings"])

    def test_sns_time_invalid_values_are_record_level_partial(self):
        from wechat_cli.core.sns import _normalize_post_base

        for raw_time in (
            "",
            "not-an-int",
            "999999999999999999",
            "-999999999999999999",
        ):
            with self.subTest(raw_time=raw_time):
                post = _normalize_post_base(
                    123,
                    "wxid_fixture",
                    sns_xml(create_time=raw_time).encode("utf-8"),
                    {},
                )
                self.assertIsNone(post["create_time"])
                self.assertEqual(post["time"], "")
                self.assertEqual(post["parse_status"], "partial")
                self.assertIn("missing_create_time", post["warnings"])

    def test_base_post_malformed_xml_is_partial(self):
        from wechat_cli.core.sns import _normalize_post_base

        post = _normalize_post_base(123, "wxid_fixture", b"<SnsDataItem>", {})

        self.assertEqual(post["parse_status"], "partial")
        self.assertEqual(post["warnings"], ["xml_parse_failed"])
        self.assertIsNone(post["create_time"])

    def test_base_post_invalid_utf8_is_partial(self):
        from wechat_cli.core.sns import _normalize_post_base

        post = _normalize_post_base(123, "wxid_fixture", b"\x80\xff", {})

        self.assertEqual(post["parse_status"], "partial")
        self.assertEqual(post["warnings"], ["xml_decode_failed"])

    def test_interactions_normalize_like_and_unique_reply(self):
        from wechat_cli.core.sns import (
            SNS_COMMENT_PUBLIC_KEYS,
            SNS_LIKE_PUBLIC_KEYS,
            _normalize_post_base,
            _normalize_sns_interactions,
        )

        raw = b"""<SnsDataItem>
<TimelineObject><username>wxid_fixture</username><createTime>1780000000</createTime><contentDesc>hello</contentDesc></TimelineObject>
<LocalExtraInfo>
  <like_user_list>
    <user_comment><username>wxid_like</username><nickname>Like User</nickname><create_time>10</create_time></user_comment>
  </like_user_list>
  <comment_user_list>
    <user_comment><comment_id>7</comment_id><username>wxid_target</username><nickname>Target</nickname><content>target</content><create_time>11</create_time><ref_comment_id>0</ref_comment_id></user_comment>
    <user_comment><comment_id>8</comment_id><username>wxid_reply</username><nickname>Reply</nickname><content>reply</content><create_time>12</create_time><ref_comment_id>7</ref_comment_id></user_comment>
  </comment_user_list>
</LocalExtraInfo>
</SnsDataItem>"""
        post = _normalize_post_base(123, "wxid_fixture", raw, {})
        _normalize_sns_interactions(post)

        self.assertEqual(set(post["likes"][0]), SNS_LIKE_PUBLIC_KEYS)
        self.assertEqual(post["likes"][0]["display_name"], "Like User")
        self.assertEqual(set(post["comments"][0]), SNS_COMMENT_PUBLIC_KEYS)
        reply = post["comments"][1]
        self.assertEqual(reply["reply_to_username"], "wxid_target")
        self.assertEqual(reply["reply_to_display_name"], "Target")
        self.assertEqual(post["parse_status"], "ok")

    def test_missing_local_extra_info_is_record_level_partial(self):
        from wechat_cli.core.sns import _normalize_post_base, _normalize_sns_interactions

        raw = b"""<SnsDataItem><TimelineObject><username>wxid_fixture</username><createTime>1780000000</createTime></TimelineObject></SnsDataItem>"""
        post = _normalize_post_base(123, "wxid_fixture", raw, {})
        _normalize_sns_interactions(post)

        self.assertEqual(post["parse_status"], "partial")
        self.assertIn("missing_local_extra_info", post["warnings"])
        self.assertEqual(post["likes"], [])
        self.assertEqual(post["comments"], [])

    def test_missing_reply_target_stays_null_and_warns(self):
        from wechat_cli.core.sns import _normalize_post_base, _normalize_sns_interactions

        raw = b"""<SnsDataItem>
<TimelineObject><username>wxid_fixture</username><createTime>1780000000</createTime></TimelineObject>
<LocalExtraInfo><like_user_list/><comment_user_list>
  <user_comment><comment_id>8</comment_id><username>wxid_reply</username><nickname>Reply</nickname><content>reply</content><ref_comment_id>77</ref_comment_id></user_comment>
</comment_user_list></LocalExtraInfo></SnsDataItem>"""
        post = _normalize_post_base(123, "wxid_fixture", raw, {})
        _normalize_sns_interactions(post)

        comment = post["comments"][0]
        self.assertIsNone(comment["reply_to_username"])
        self.assertIsNone(comment["reply_to_display_name"])
        self.assertIn("reply_target_missing", post["warnings"])

    def test_duplicate_referenced_comment_id_is_ambiguous_not_last_write_wins(self):
        from wechat_cli.core.sns import _normalize_post_base, _normalize_sns_interactions

        raw = b"""<SnsDataItem>
<TimelineObject><username>wxid_fixture</username><createTime>1780000000</createTime></TimelineObject>
<LocalExtraInfo><like_user_list/><comment_user_list>
  <user_comment><comment_id>7</comment_id><username>wxid_alice</username><nickname>Alice</nickname><content>a</content><ref_comment_id>0</ref_comment_id></user_comment>
  <user_comment><comment_id>7</comment_id><username>wxid_bob</username><nickname>Bob</nickname><content>b</content><ref_comment_id>0</ref_comment_id></user_comment>
  <user_comment><comment_id>8</comment_id><username>wxid_reply</username><nickname>Reply</nickname><content>reply</content><ref_comment_id>7</ref_comment_id></user_comment>
</comment_user_list></LocalExtraInfo></SnsDataItem>"""
        post = _normalize_post_base(123, "wxid_fixture", raw, {})
        _normalize_sns_interactions(post)

        self.assertEqual(len(post["comments"]), 3)
        reply = post["comments"][2]
        self.assertIsNone(reply["reply_to_username"])
        self.assertIsNone(reply["reply_to_display_name"])
        self.assertEqual(post["parse_status"], "partial")
        self.assertIn("reply_target_ambiguous", post["warnings"])

    def test_duplicate_unreferenced_comment_ids_are_preserved_without_ambiguity_warning(self):
        from wechat_cli.core.sns import _normalize_post_base, _normalize_sns_interactions

        raw = b"""<SnsDataItem>
<TimelineObject><username>wxid_fixture</username><createTime>1780000000</createTime></TimelineObject>
<LocalExtraInfo><like_user_list/><comment_user_list>
  <user_comment><comment_id>7</comment_id><username>wxid_alice</username><content>a</content><ref_comment_id>0</ref_comment_id></user_comment>
  <user_comment><comment_id>7</comment_id><username>wxid_bob</username><content>b</content><ref_comment_id>0</ref_comment_id></user_comment>
</comment_user_list></LocalExtraInfo></SnsDataItem>"""
        post = _normalize_post_base(123, "wxid_fixture", raw, {})
        _normalize_sns_interactions(post)

        self.assertEqual([x["username"] for x in post["comments"]], ["wxid_alice", "wxid_bob"])
        self.assertNotIn("reply_target_ambiguous", post["warnings"])

    def test_comment_64id_does_not_replace_canonical_reply_reference(self):
        from wechat_cli.core.sns import _normalize_post_base, _normalize_sns_interactions

        raw = b"""<SnsDataItem>
<TimelineObject><username>wxid_fixture</username><createTime>1780000000</createTime></TimelineObject>
<LocalExtraInfo><like_user_list/><comment_user_list>
  <user_comment><comment_id>7</comment_id><comment_64id>7007</comment_64id><username>wxid_target</username><nickname>Target</nickname><content>target</content><ref_comment_id>0</ref_comment_id><ref_comment_64id>0</ref_comment_64id></user_comment>
  <user_comment><comment_id>8</comment_id><comment_64id>8008</comment_64id><username>wxid_reply</username><nickname>Reply</nickname><content>reply</content><ref_comment_id>0</ref_comment_id><ref_comment_64id>7007</ref_comment_64id></user_comment>
</comment_user_list></LocalExtraInfo></SnsDataItem>"""
        post = _normalize_post_base(123, "wxid_fixture", raw, {})
        _normalize_sns_interactions(post)

        reply = post["comments"][1]
        self.assertIsNone(reply["reply_to_username"])
        self.assertIsNone(reply["reply_to_display_name"])
        self.assertNotIn("comment_64id", reply)
        self.assertNotIn("ref_comment_64id", reply)

    def test_media_normalization_freezes_status_matrix_and_private_source_mapping(self):
        from wechat_cli.core.sns import _normalize_post_base, _normalize_sns_media

        raw = b"""<SnsDataItem>
<TimelineObject>
  <username>wxid_fixture</username><createTime>1780000000</createTime>
  <ContentObject><mediaList>
    <media><id>img-1</id><type>2</type><size width="640" height="480"/><url md5="AABBCCDDEEFF00112233445566778899" token="SECRET_TOKEN" key="SECRET_KEY">https://remote/image</url><thumb token="THUMB_TOKEN" key="THUMB_KEY">https://remote/thumb</thumb></media>
    <media><id>vid-1</id><type>6</type><size width="1280" height="720"/><videoDuration>12.5</videoDuration><url md5="00112233445566778899AABBCCDDEEFF">https://remote/video</url></media>
    <media><id>live-1</id><type>2</type><LivePhoto><liveMedia><url md5="FEDCBA98765432100123456789ABCDEF"/></liveMedia></LivePhoto></media>
    <media><id>other-1</id><type>99</type></media>
  </mediaList></ContentObject>
</TimelineObject>
<LocalExtraInfo><like_user_list/><comment_user_list/></LocalExtraInfo>
</SnsDataItem>"""
        post = _normalize_post_base(123, "wxid_fixture", raw, {})
        _normalize_sns_media(post)

        image, video, live, unsupported = post["media"]
        self.assertEqual(
            (image["type"], image["status"], image["reason"]),
            ("image", "metadata_only", "local_identity_unproven"),
        )
        self.assertEqual((image["width"], image["height"]), (640, 480))
        self.assertEqual(
            image["_source_url_md5"],
            "aabbccddeeff00112233445566778899",
        )
        self.assertEqual(
            (video["type"], video["status"], video["reason"]),
            ("video", "metadata_only", "remote_deferred"),
        )
        self.assertEqual(video["duration"], 12.5)
        self.assertEqual(
            (live["type"], live["status"], live["reason"]),
            ("live_photo", "metadata_only", "local_identity_unproven"),
        )
        self.assertEqual(
            live["_components"],
            {
                "image": {
                    "status": "metadata_only",
                    "reason": "local_identity_unproven",
                },
                "video": {
                    "status": "metadata_only",
                    "reason": "local_identity_unproven",
                },
            },
        )
        self.assertEqual(
            (unsupported["type"], unsupported["status"], unsupported["reason"]),
            ("unsupported", "unsupported", "unsupported_type"),
        )
        self.assertIsNone(unsupported["preview_url"])
        self.assertIn("unsupported_media_type", post["warnings"])

        serialized_internal = repr(post["media"])
        for forbidden in (
            "SECRET_TOKEN",
            "SECRET_KEY",
            "THUMB_TOKEN",
            "THUMB_KEY",
            "https://remote/image",
            "https://remote/thumb",
            "https://remote/video",
        ):
            self.assertNotIn(forbidden, serialized_internal)

    def test_invalid_optional_media_dimensions_and_duration_become_none(self):
        from wechat_cli.core.sns import _normalize_post_base, _normalize_sns_media

        raw = b"""<SnsDataItem><TimelineObject><username>wxid_fixture</username><createTime>1780000000</createTime><ContentObject><mediaList><media><id>x</id><type>6</type><size width="bad" height=""/><videoDuration>nan</videoDuration><url md5="00112233445566778899aabbccddeeff"/></media></mediaList></ContentObject></TimelineObject><LocalExtraInfo><like_user_list/><comment_user_list/></LocalExtraInfo></SnsDataItem>"""
        post = _normalize_post_base(123, "wxid_fixture", raw, {})
        _normalize_sns_media(post)

        media = post["media"][0]
        self.assertIsNone(media["width"])
        self.assertIsNone(media["height"])
        self.assertIsNone(media["duration"])

    def test_public_serializers_use_exact_allowlists_and_live_photo_shape(self):
        from wechat_cli.core.sns import (
            SNS_COMMENT_PUBLIC_KEYS,
            SNS_LIKE_PUBLIC_KEYS,
            SNS_LIVE_PHOTO_COMPONENT_PUBLIC_KEYS,
            SNS_LIVE_PHOTO_PUBLIC_KEYS,
            SNS_MEDIA_BASE_PUBLIC_KEYS,
            SNS_POST_PUBLIC_KEYS,
            _public_post,
        )

        internal = {
            "post_id": "123",
            "username": "wxid_fixture",
            "display_name": "Fixture User",
            "create_time": 1780000000,
            "time": "2026-05-27 12:53",
            "content": "hello",
            "location": {"poiName": "Fixture Park"},
            "media": [
                {
                    "media_id": "m_0123456789abcdef01234567",
                    "type": "video",
                    "status": "metadata_only",
                    "reason": "remote_deferred",
                    "preview_url": None,
                    "width": 1280,
                    "height": 720,
                    "duration": 12.5,
                    "_source_url_md5": "secret-md5",
                    "_source_type": 6,
                    "_local_path": "C:/secret/video.mp4",
                },
                {
                    "media_id": "m_89abcdef0123456789abcdef",
                    "type": "live_photo",
                    "status": "metadata_only",
                    "reason": "local_identity_unproven",
                    "preview_url": None,
                    "width": None,
                    "height": None,
                    "duration": None,
                    "_source_type": 2,
                    "_components": {
                        "image": {
                            "status": "metadata_only",
                            "reason": "local_identity_unproven",
                            "private": "secret-image",
                        },
                        "video": {
                            "status": "metadata_only",
                            "reason": "local_identity_unproven",
                            "private": "secret-video",
                        },
                    },
                },
            ],
            "likes": [
                {
                    "username": "wxid_like",
                    "display_name": "Like User",
                    "create_time": 10,
                    "private": "secret-like",
                }
            ],
            "comments": [
                {
                    "comment_id": 7,
                    "username": "wxid_comment",
                    "display_name": "Comment User",
                    "content": "nice",
                    "create_time": 11,
                    "reply_to_username": None,
                    "reply_to_display_name": None,
                    "private": "secret-comment",
                }
            ],
            "parse_status": "ok",
            "warnings": [],
            "_root": "secret-root",
        }

        public = _public_post(internal)

        self.assertEqual(set(public), SNS_POST_PUBLIC_KEYS)
        self.assertEqual(set(public["media"][0]), SNS_MEDIA_BASE_PUBLIC_KEYS)
        live = public["media"][1]
        self.assertEqual(set(live), SNS_LIVE_PHOTO_PUBLIC_KEYS)
        self.assertEqual(set(live["image"]), SNS_LIVE_PHOTO_COMPONENT_PUBLIC_KEYS)
        self.assertEqual(set(live["video"]), SNS_LIVE_PHOTO_COMPONENT_PUBLIC_KEYS)
        self.assertEqual(
            live["image"],
            {"status": "metadata_only", "reason": "local_identity_unproven"},
        )
        self.assertEqual(
            live["video"],
            {"status": "metadata_only", "reason": "local_identity_unproven"},
        )
        self.assertEqual(set(public["likes"][0]), SNS_LIKE_PUBLIC_KEYS)
        self.assertEqual(set(public["comments"][0]), SNS_COMMENT_PUBLIC_KEYS)
        serialized = repr(public)
        for forbidden in (
            "secret-md5",
            "C:/secret/video.mp4",
            "secret-image",
            "secret-video",
            "secret-like",
            "secret-comment",
            "secret-root",
        ):
            self.assertNotIn(forbidden, serialized)

    def test_normalize_sns_row_runs_base_interactions_and_media(self):
        from wechat_cli.core.sns import _normalize_sns_row

        raw = b"""<SnsDataItem><TimelineObject><username>wxid_fixture</username><createTime>1780000000</createTime><ContentObject><mediaList><media><id>x</id><type>6</type><url md5="00112233445566778899aabbccddeeff"/></media></mediaList></ContentObject></TimelineObject><LocalExtraInfo><like_user_list/><comment_user_list/></LocalExtraInfo></SnsDataItem>"""
        post = _normalize_sns_row(
            123,
            "wxid_fixture",
            raw,
            {"wxid_fixture": "Fixture User"},
        )

        self.assertEqual(post["display_name"], "Fixture User")
        self.assertEqual(len(post["media"]), 1)
        self.assertEqual(post["media"][0]["type"], "video")
        self.assertEqual(post["likes"], [])
        self.assertEqual(post["comments"], [])


class SnsSchemaTests(unittest.TestCase):
    def test_supported_schema_allows_optional_pack_info_to_be_absent(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "sns.db"
            create_sns_db(path, include_pack_info=False)
            with closing(_readonly_connection(path)) as conn:
                columns = _validate_sns_db_predicate(conn)

        self.assertEqual(columns["tid"], {"type": "INTEGER", "pk": 1})
        self.assertEqual(columns["user_name"], {"type": "TEXT", "pk": 0})
        self.assertEqual(columns["content"], {"type": "TEXT", "pk": 0})

    def test_missing_timeline_table_is_unsupported(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "sns.db"
            sqlite3.connect(path).close()
            with closing(_readonly_connection(path)) as conn:
                with self.assertRaises(SnsError) as caught:
                    _validate_sns_db_predicate(conn)

        self.assertEqual(caught.exception.code, "SNS_SCHEMA_UNSUPPORTED")
        self.assertEqual(
            caught.exception.details,
            {"missing": ["SnsTimeLine"]},
        )

    def test_tid_wrong_declared_type_is_unsupported(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "sns.db"
            create_sns_db(
                path,
                column_overrides={"tid": "tid TEXT PRIMARY KEY"},
            )
            with closing(_readonly_connection(path)) as conn:
                with self.assertRaises(SnsError) as caught:
                    _validate_sns_db_predicate(conn)

        self.assertEqual(caught.exception.code, "SNS_SCHEMA_UNSUPPORTED")
        self.assertEqual(
            caught.exception.details,
            {
                "failures": [
                    {"column": "tid", "problem": "type", "actual_type": "TEXT"}
                ]
            },
        )

    def test_user_name_wrong_declared_type_is_unsupported(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "sns.db"
            create_sns_db(
                path,
                column_overrides={"user_name": "user_name INTEGER"},
            )
            with closing(_readonly_connection(path)) as conn:
                with self.assertRaises(SnsError) as caught:
                    _validate_sns_db_predicate(conn)

        self.assertEqual(caught.exception.code, "SNS_SCHEMA_UNSUPPORTED")
        self.assertEqual(
            caught.exception.details,
            {
                "failures": [
                    {
                        "column": "user_name",
                        "problem": "type",
                        "actual_type": "INTEGER",
                    }
                ]
            },
        )

    def test_content_wrong_declared_type_is_unsupported(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "sns.db"
            create_sns_db(
                path,
                column_overrides={"content": "content BLOB"},
            )
            with closing(_readonly_connection(path)) as conn:
                with self.assertRaises(SnsError) as caught:
                    _validate_sns_db_predicate(conn)

        self.assertEqual(caught.exception.code, "SNS_SCHEMA_UNSUPPORTED")
        self.assertEqual(
            caught.exception.details,
            {
                "failures": [
                    {
                        "column": "content",
                        "problem": "type",
                        "actual_type": "BLOB",
                    }
                ]
            },
        )

    def test_text_content_is_fetched_as_raw_bytes_even_when_utf8_is_invalid(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "sns.db"
            create_sns_db(path)
            insert_invalid_utf8_text(path)
            with closing(_readonly_connection(path)) as conn:
                row = conn.execute(
                    "SELECT tid, user_name, CAST(content AS BLOB) "
                    "FROM SnsTimeLine WHERE tid = ?",
                    (1,),
                ).fetchone()

        self.assertEqual(row[0], 1)
        self.assertEqual(row[1], "wxid_fixture")
        self.assertIsInstance(row[2], bytes)
        self.assertEqual(row[2], b"\x80\xff")

    def test_readonly_connection_rejects_writes_and_preserves_row_count(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "sns.db"
            create_sns_db(path)
            conn = sqlite3.connect(path)
            try:
                conn.execute(
                    "INSERT INTO SnsTimeLine(tid, user_name, content) VALUES (1, ?, ?)",
                    ("wxid_fixture", "<SnsDataItem/>"),
                )
                conn.commit()
            finally:
                conn.close()

            with closing(_readonly_connection(path)) as readonly:
                before = readonly.execute("SELECT COUNT(*) FROM SnsTimeLine").fetchone()[0]
                with self.assertRaises(sqlite3.OperationalError):
                    readonly.execute(
                        "INSERT INTO SnsTimeLine(tid, user_name, content) VALUES (2, ?, ?)",
                        ("wxid_other", "<SnsDataItem/>"),
                    )
                after = readonly.execute("SELECT COUNT(*) FROM SnsTimeLine").fetchone()[0]

        self.assertEqual(before, 1)
        self.assertEqual(after, 1)


class SnsContactResolverTests(unittest.TestCase):
    def _rows(self):
        return [
            {"username": "wxid_alpha", "nick_name": "Alpha Nick", "remark": "Alpha Remark"},
            {"username": "wxid_beta", "nick_name": "Beta Nick", "remark": ""},
            {"username": "CaseUser", "nick_name": "Gamma", "remark": "Gamma Remark"},
        ]

    def test_direct_wxid_with_nonempty_suffix_bypasses_contacts(self):
        from wechat_cli.core.sns import resolve_sns_username

        with patch("wechat_cli.core.sns.get_contact_full") as get_full:
            self.assertEqual(
                resolve_sns_username("wxid_direct", object(), "unused"),
                "wxid_direct",
            )
        get_full.assert_not_called()

    def test_bare_wxid_prefix_is_not_direct_identity(self):
        from wechat_cli.core.sns import resolve_sns_username

        with patch("wechat_cli.core.sns.get_contact_full", return_value=[]):
            with self.assertRaises(SnsError) as caught:
                resolve_sns_username("wxid_", object(), "unused")
        self.assertEqual(caught.exception.code, "SNS_USER_NOT_FOUND")

    def test_exact_matching_is_case_insensitive_across_all_fields(self):
        from wechat_cli.core.sns import resolve_sns_username

        cases = {
            "WXID_ALPHA": "wxid_alpha",
            "alpha nick": "wxid_alpha",
            "ALPHA REMARK": "wxid_alpha",
            "beta nick": "wxid_beta",
            "gamma remark": "CaseUser",
            "caseuser": "CaseUser",
        }
        for query, expected in cases.items():
            with self.subTest(query=query), patch(
                "wechat_cli.core.sns.get_contact_full", return_value=self._rows()
            ):
                self.assertEqual(
                    resolve_sns_username(query, object(), "unused"), expected
                )

    def test_exact_deduplicates_multiple_matching_fields_by_username(self):
        from wechat_cli.core.sns import resolve_sns_username

        rows = [
            {
                "username": "same",
                "nick_name": "same",
                "remark": "same",
            }
        ]
        with patch("wechat_cli.core.sns.get_contact_full", return_value=rows):
            self.assertEqual(resolve_sns_username("SAME", object(), "unused"), "same")

    def test_exact_ambiguity_is_terminal_and_does_not_enter_fuzzy(self):
        from wechat_cli.core.sns import resolve_sns_username

        rows = [
            {"username": "u1", "nick_name": "Shared", "remark": ""},
            {"username": "u2", "nick_name": "Shared", "remark": ""},
            {"username": "shared-fuzzy-only", "nick_name": "Other", "remark": ""},
        ]
        with patch("wechat_cli.core.sns.get_contact_full", return_value=rows):
            with self.assertRaises(SnsError) as caught:
                resolve_sns_username("shared", object(), "unused")
        self.assertEqual(caught.exception.code, "SNS_USER_AMBIGUOUS")
        self.assertEqual(caught.exception.details["candidate_count"], 2)
        self.assertEqual(
            [item["username"] for item in caught.exception.details["candidates"]],
            ["u1", "u2"],
        )

    def test_fuzzy_runs_only_after_exact_zero_and_is_case_insensitive(self):
        from wechat_cli.core.sns import resolve_sns_username

        with patch("wechat_cli.core.sns.get_contact_full", return_value=self._rows()):
            self.assertEqual(
                resolve_sns_username("pha rem", object(), "unused"),
                "wxid_alpha",
            )

    def test_fuzzy_zero_and_many_are_fail_closed(self):
        from wechat_cli.core.sns import resolve_sns_username

        rows = [
            {"username": "one", "nick_name": "Project Apple", "remark": ""},
            {"username": "two", "nick_name": "Apple Group", "remark": ""},
        ]
        with patch("wechat_cli.core.sns.get_contact_full", return_value=rows):
            with self.assertRaises(SnsError) as ambiguous:
                resolve_sns_username("apple", object(), "unused")
            with self.assertRaises(SnsError) as missing:
                resolve_sns_username("not-present", object(), "unused")
        self.assertEqual(ambiguous.exception.code, "SNS_USER_AMBIGUOUS")
        self.assertEqual(missing.exception.code, "SNS_USER_NOT_FOUND")

    def test_blank_query_is_not_empty_substring_match(self):
        from wechat_cli.core.sns import resolve_sns_username

        with patch("wechat_cli.core.sns.get_contact_full", return_value=self._rows()):
            with self.assertRaises(SnsError) as caught:
                resolve_sns_username("   ", object(), "unused")
        self.assertEqual(caught.exception.code, "SNS_USER_NOT_FOUND")

    def test_ambiguity_details_are_bounded_and_shaped(self):
        from wechat_cli.core.sns import resolve_sns_username

        rows = [
            {
                "username": f"user-{index:02d}",
                "nick_name": f"Target {index:02d}",
                "remark": "",
            }
            for index in range(11)
        ]
        with patch("wechat_cli.core.sns.get_contact_full", return_value=rows):
            with self.assertRaises(SnsError) as caught:
                resolve_sns_username("target", object(), "unused")

        details = caught.exception.details
        self.assertEqual(details["candidate_count"], 11)
        self.assertTrue(details["truncated"])
        self.assertEqual(len(details["candidates"]), 10)
        self.assertEqual(
            set(details["candidates"][0]),
            {"username", "nick_name", "remark", "display_name"},
        )


class SnsQueryValidationTests(unittest.TestCase):
    def test_invalid_pagination_and_time_values_become_sns_invalid_request(self):
        from wechat_cli.core.sns import _validate_sns_query

        invalid_cases = [
            {"limit": 0, "offset": 0, "start_time": None, "end_time": None},
            {"limit": 201, "offset": 0, "start_time": None, "end_time": None},
            {"limit": 20, "offset": -1, "start_time": None, "end_time": None},
            {"limit": 20, "offset": 0, "start_time": "bad", "end_time": None},
            {"limit": 20, "offset": 0, "start_time": None, "end_time": "bad"},
            {
                "limit": 20,
                "offset": 0,
                "start_time": "2026-09-02",
                "end_time": "2026-09-01",
            },
        ]
        for case in invalid_cases:
            with self.subTest(case=case), self.assertRaises(SnsError) as caught:
                _validate_sns_query(**case)
            self.assertEqual(caught.exception.code, "SNS_INVALID_REQUEST")
            self.assertFalse(caught.exception.retryable)
            self.assertEqual(caught.exception.message, "朋友圈查询参数无效")
            self.assertIsNone(caught.exception.details)
            self.assertNotIn("limit", caught.exception.message.lower())
            self.assertNotIn("start_time", caught.exception.message)

    def test_valid_query_returns_parsed_local_time_bounds(self):
        from wechat_cli.core.sns import _validate_sns_query

        start_ts, end_ts = _validate_sns_query(
            20,
            0,
            "2026-09-04",
            "2026-09-04",
        )
        self.assertLessEqual(start_ts, end_ts)
        self.assertEqual(
            datetime.fromtimestamp(end_ts).strftime("%Y-%m-%d %H:%M:%S"),
            "2026-09-04 23:59:59",
        )


class SnsSourceScanAndOrderingTests(unittest.TestCase):
    def _db(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        path = Path(tmp.name) / "sns.db"
        create_sns_db(path)
        return path

    def test_iter_sns_rows_returns_tid_desc_and_raw_bytes(self):
        from wechat_cli.core.sns import _iter_sns_rows

        path = self._db()
        insert_sns_xml_bytes(path, 2, sns_xml(content="two").encode("utf-8"))
        insert_invalid_utf8_text(path, tid=3, username="wxid_bad")
        insert_sns_xml_bytes(path, 1, sns_xml(content="one").encode("utf-8"))
        with closing(_readonly_connection(path)) as conn:
            rows = list(_iter_sns_rows(conn))

        self.assertEqual([row[0] for row in rows], [3, 2, 1])
        self.assertTrue(all(isinstance(row[2], bytes) for row in rows))
        self.assertEqual(rows[0][2], b"\x80\xff")

    def test_iter_sns_rows_username_filter_is_exact(self):
        from wechat_cli.core.sns import _iter_sns_rows

        path = self._db()
        insert_sns_xml_bytes(path, 2, sns_xml(username="wxid_a").encode(), username="wxid_a")
        insert_sns_xml_bytes(path, 1, sns_xml(username="wxid_ab").encode(), username="wxid_ab")
        with closing(_readonly_connection(path)) as conn:
            rows = list(_iter_sns_rows(conn, username="wxid_a"))
        self.assertEqual([(row[0], row[1]) for row in rows], [(2, "wxid_a")])

    def test_ordering_invariant_rejects_increasing_create_time(self):
        from wechat_cli.core.sns import _advance_ordering_invariant

        previous = _advance_ordering_invariant(None, {"parse_status": "ok", "create_time": 100})
        with self.assertRaises(SnsError) as caught:
            _advance_ordering_invariant(previous, {"parse_status": "ok", "create_time": 101})
        self.assertEqual(caught.exception.code, "SNS_SCHEMA_UNSUPPORTED")
        self.assertFalse(caught.exception.retryable)
        self.assertFalse(caught.exception.details)

    def test_ordering_invariant_allows_equal_and_ignores_partial(self):
        from wechat_cli.core.sns import _advance_ordering_invariant

        previous = _advance_ordering_invariant(None, {"parse_status": "ok", "create_time": 100})
        self.assertEqual(
            _advance_ordering_invariant(previous, {"parse_status": "ok", "create_time": 100}),
            100,
        )
        self.assertEqual(
            _advance_ordering_invariant(previous, {"parse_status": "partial", "create_time": 999}),
            100,
        )


class SnsTimelineListTests(unittest.TestCase):
    def _context(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        path = Path(tmp.name) / "sns.db"
        create_sns_db(path)
        return SimpleNamespace(
            sns_db_path=str(path),
            cache=object(),
            decrypted_dir="unused",
        ), path

    def test_list_reuses_one_display_map_and_applies_logical_offset_after_filter(self):
        from wechat_cli.core.sns import list_sns_posts

        ctx, path = self._context()
        insert_sns_xml_bytes(path, 5, sns_xml(username="wxid_a", create_time=500, content="apple five").encode(), username="wxid_a")
        insert_sns_xml_bytes(path, 4, sns_xml(username="wxid_b", create_time=400, content="banana four").encode(), username="wxid_b")
        insert_sns_xml_bytes(path, 3, sns_xml(username="wxid_b", create_time=300, content="APPLE three").encode(), username="wxid_b")
        insert_sns_xml_bytes(path, 2, sns_xml(username="wxid_c", create_time=200, content="apple two").encode(), username="wxid_c")

        names = {"wxid_a": "Alpha", "wxid_b": "Beta", "wxid_c": "Gamma"}
        with patch("wechat_cli.core.sns.get_contact_names", return_value=names) as get_names:
            first = list_sns_posts(ctx, limit=1, offset=0, query="apple")
            second = list_sns_posts(ctx, limit=1, offset=1, query="apple")

        self.assertEqual(first["count"], 1)
        self.assertEqual(first["offset"], 0)
        self.assertEqual(first["limit"], 1)
        self.assertTrue(first["has_more"])
        self.assertEqual(first["posts"][0]["post_id"], "5")
        self.assertEqual(first["posts"][0]["display_name"], "Alpha")
        self.assertEqual(second["posts"][0]["post_id"], "3")
        self.assertEqual(second["posts"][0]["display_name"], "Beta")
        self.assertEqual(get_names.call_count, 2)  # once per list operation, never once per row

    def test_display_lookup_failure_falls_back_to_username(self):
        from wechat_cli.core.sns import list_sns_posts

        ctx, path = self._context()
        insert_sns_xml_bytes(path, 1, sns_xml(username="wxid_a", create_time=100).encode(), username="wxid_a")
        with patch("wechat_cli.core.sns.get_contact_names", side_effect=OSError("fixture")) as get_names:
            page = list_sns_posts(ctx, limit=20, offset=0)
        get_names.assert_called_once()
        self.assertEqual(page["posts"][0]["display_name"], "wxid_a")

    def test_date_only_end_time_includes_end_of_day_and_excludes_next_midnight(self):
        from wechat_cli.core.sns import list_sns_posts

        ctx, path = self._context()
        end_of_day = int(datetime(2026, 9, 4, 23, 59, 59).timestamp())
        next_midnight = int(datetime(2026, 9, 5, 0, 0, 0).timestamp())
        insert_sns_xml_bytes(path, 2, sns_xml(create_time=next_midnight, content="next").encode())
        insert_sns_xml_bytes(path, 1, sns_xml(create_time=end_of_day, content="included").encode())

        with patch("wechat_cli.core.sns.get_contact_names", return_value={}):
            page = list_sns_posts(ctx, limit=20, offset=0, end_time="2026-09-04")

        self.assertEqual([post["post_id"] for post in page["posts"]], ["1"])
        self.assertEqual(page["posts"][0]["content"], "included")

    def test_partial_missing_time_is_only_excluded_when_time_filter_is_active(self):
        from wechat_cli.core.sns import list_sns_posts

        ctx, path = self._context()
        valid_time = int(datetime(2026, 9, 4, 12, 0, 0).timestamp())
        insert_invalid_utf8_text(path, tid=2, username="wxid_bad")
        insert_sns_xml_bytes(
            path,
            1,
            sns_xml(create_time=valid_time, content="valid").encode(),
        )

        with patch("wechat_cli.core.sns.get_contact_names", return_value={}):
            unfiltered = list_sns_posts(ctx, limit=20, offset=0)
            filtered = list_sns_posts(
                ctx,
                limit=20,
                offset=0,
                start_time="2026-09-04",
            )

        self.assertEqual([post["post_id"] for post in unfiltered["posts"]], ["2", "1"])
        self.assertEqual(unfiltered["posts"][0]["parse_status"], "partial")
        self.assertIn("xml_decode_failed", unfiltered["posts"][0]["warnings"])
        self.assertEqual([post["post_id"] for post in filtered["posts"]], ["1"])

    def test_username_filter_is_exact_and_source_order_is_tid_desc(self):
        from wechat_cli.core.sns import list_sns_posts

        ctx, path = self._context()
        insert_sns_xml_bytes(path, 3, sns_xml(username="wxid_a", create_time=300).encode(), username="wxid_a")
        insert_sns_xml_bytes(path, 2, sns_xml(username="wxid_ab", create_time=200).encode(), username="wxid_ab")
        insert_sns_xml_bytes(path, 1, sns_xml(username="wxid_a", create_time=100).encode(), username="wxid_a")

        with patch("wechat_cli.core.sns.get_contact_names", return_value={}):
            page = list_sns_posts(ctx, limit=20, offset=0, username="wxid_a")
        self.assertEqual([post["post_id"] for post in page["posts"]], ["3", "1"])


class SnsSinglePostAndMediaSourceTests(unittest.TestCase):
    def _context(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        path = Path(tmp.name) / "sns.db"
        create_sns_db(path)
        return SimpleNamespace(
            sns_db_path=str(path),
            cache=object(),
            decrypted_dir="unused",
        ), path

    def _video_xml(self, *, source_type=6, source_id="source-video", md5="a" * 32):
        return f"""<SnsDataItem>
<TimelineObject>
  <username>wxid_fixture</username>
  <createTime>1780000000</createTime>
  <contentDesc>video fixture</contentDesc>
  <ContentObject><mediaList><media>
    <id>{source_id}</id>
    <type>{source_type}</type>
    <url md5="{md5}">https://private.invalid/media</url>
  </media></mediaList></ContentObject>
</TimelineObject>
<LocalExtraInfo><like_user_list/><comment_user_list/></LocalExtraInfo>
</SnsDataItem>""".encode("utf-8")

    def test_get_sns_post_existing_and_missing_have_distinct_none_semantics(self):
        from wechat_cli.core import sns as sns_module

        ctx, path = self._context()
        insert_sns_xml_bytes(path, 1, sns_xml(content="fixture").encode())
        with patch("wechat_cli.core.sns.get_contact_names", return_value={"wxid_fixture": "Fixture"}) as get_names, patch(
            "wechat_cli.core.sns._validate_known_schema",
            wraps=sns_module._validate_known_schema,
        ) as validate:
            existing = sns_module.get_sns_post(ctx, 1)
            missing = sns_module.get_sns_post(ctx, 999)

        self.assertEqual(existing["post_id"], "1")
        self.assertEqual(existing["display_name"], "Fixture")
        self.assertIsNone(missing)
        self.assertEqual(get_names.call_count, 2)  # once per operation
        self.assertEqual(validate.call_count, 2)

    def test_get_sns_post_invalid_utf8_is_partial_not_decode_exception(self):
        from wechat_cli.core.sns import get_sns_post

        ctx, path = self._context()
        insert_sns_xml_bytes(path, 2, sns_xml().encode())
        insert_invalid_utf8_text(path, tid=1, username="wxid_bad")
        with patch("wechat_cli.core.sns.get_contact_names", return_value={}):
            post = get_sns_post(ctx, 1)
        self.assertEqual(post["parse_status"], "partial")
        self.assertIn("xml_decode_failed", post["warnings"])

    def test_get_sns_media_source_returns_private_descriptor_without_contact_lookup(self):
        from wechat_cli.core import sns as sns_module

        ctx, path = self._context()
        insert_sns_xml_bytes(path, 1, self._video_xml())
        media_id = sns_module._canonical_media_id("1", 0, "source-video")
        with patch(
            "wechat_cli.core.sns.get_contact_names",
            side_effect=AssertionError("private media lookup must not enrich contacts"),
        ), patch(
            "wechat_cli.core.sns._validate_known_schema",
            wraps=sns_module._validate_known_schema,
        ) as validate:
            source = sns_module.get_sns_media_source(ctx, 1, media_id)

        self.assertEqual(source["media_id"], media_id)
        self.assertEqual(source["type"], "video")
        self.assertEqual(source["_source_url_md5"], "a" * 32)
        self.assertEqual(source["create_time"], 1780000000)
        validate.assert_called_once()

    def test_get_sns_media_source_missing_post_or_media_is_unavailable(self):
        from wechat_cli.core import sns as sns_module

        ctx, path = self._context()
        insert_sns_xml_bytes(path, 1, self._video_xml())
        for post_id, media_id in ((999, "m_" + "0" * 24), (1, "m_" + "0" * 24)):
            with self.subTest(post_id=post_id), self.assertRaises(SnsError) as caught:
                sns_module.get_sns_media_source(ctx, post_id, media_id)
            self.assertEqual(caught.exception.code, "SNS_MEDIA_UNAVAILABLE")
            self.assertEqual(caught.exception.details["reason"], "local_identity_unproven")
            self.assertEqual(caught.exception.details["post_id"], str(post_id))
            self.assertEqual(caught.exception.details["media_id"], media_id)

    def test_get_sns_media_source_unsupported_type_stays_unsupported(self):
        from wechat_cli.core import sns as sns_module

        ctx, path = self._context()
        insert_sns_xml_bytes(path, 1, self._video_xml(source_type=99, source_id="unsupported"))
        media_id = sns_module._canonical_media_id("1", 0, "unsupported")
        with self.assertRaises(SnsError) as caught:
            sns_module.get_sns_media_source(ctx, 1, media_id)
        self.assertEqual(caught.exception.code, "SNS_MEDIA_UNSUPPORTED")
        self.assertFalse(caught.exception.retryable)
        self.assertEqual(caught.exception.details["reason"], "unsupported_type")


if __name__ == "__main__":
    unittest.main()
