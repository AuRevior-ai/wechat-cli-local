import unittest

from wechat_cli.core.chatroom_metadata import (
    parse_chatroom_ext_buffer,
    parse_chatroom_ext_buffer_with_status,
)


def _varint(value: int) -> bytes:
    out = bytearray()
    value = int(value)
    while True:
        byte = value & 0x7F
        value >>= 7
        if value:
            out.append(byte | 0x80)
        else:
            out.append(byte)
            return bytes(out)


def _field_varint(field: int, value: int) -> bytes:
    return _varint((field << 3) | 0) + _varint(value)


def _field_bytes(field: int, value: bytes | str) -> bytes:
    raw = value.encode("utf-8") if isinstance(value, str) else bytes(value)
    return _varint((field << 3) | 2) + _varint(len(raw)) + raw


def _member(
    username: str,
    *,
    room_display_name: str = "",
    member_flag: int | None = None,
    inviter_username: str = "",
    extra: bytes = b"",
) -> bytes:
    payload = _field_bytes(1, username)
    if room_display_name:
        payload += _field_bytes(2, room_display_name)
    if member_flag is not None:
        payload += _field_varint(3, member_flag)
    if inviter_username:
        payload += _field_bytes(4, inviter_username)
    payload += extra
    return _field_bytes(1, payload)


class ChatroomMetadataTests(unittest.TestCase):
    def test_parses_member_inviter_metadata_and_ignores_unknown_fields(self):
        blob = b"".join(
            [
                _member("wxid_owner", member_flag=1, extra=_field_varint(9, 77)),
                _member(
                    "wxid_member",
                    room_display_name="群昵称",
                    member_flag=2065,
                    inviter_username="wxid_inviter",
                    extra=_field_bytes(10, "future-field"),
                ),
            ]
        )

        records = parse_chatroom_ext_buffer(blob)

        self.assertEqual(
            {
                "wxid_owner": {
                    "username": "wxid_owner",
                    "room_display_name": "",
                    "member_flag": 1,
                    "inviter_username": "",
                },
                "wxid_member": {
                    "username": "wxid_member",
                    "room_display_name": "群昵称",
                    "member_flag": 2065,
                    "inviter_username": "wxid_inviter",
                },
            },
            records,
        )

    def test_missing_inviter_keeps_member_without_relation(self):
        records = parse_chatroom_ext_buffer(_member("wxid_owner", member_flag=1))

        self.assertEqual("", records["wxid_owner"]["inviter_username"])

    def test_self_inviter_is_ignored_fail_closed(self):
        records = parse_chatroom_ext_buffer(
            _member("wxid_member", inviter_username="wxid_member")
        )

        self.assertEqual("", records["wxid_member"]["inviter_username"])

    def test_truncated_member_submessage_fails_closed(self):
        good = _member("wxid_good", inviter_username="wxid_inviter")
        malformed_payload = _field_bytes(1, "wxid_bad") + bytes([(4 << 3) | 2, 10]) + b"xx"
        malformed = _varint((1 << 3) | 2) + _varint(len(malformed_payload)) + malformed_payload

        records = parse_chatroom_ext_buffer(good + malformed)

        self.assertEqual({"wxid_good"}, set(records))
        self.assertEqual("wxid_inviter", records["wxid_good"]["inviter_username"])

    def test_truncated_top_level_field_returns_safe_empty_or_prior_records(self):
        good = _member("wxid_good", inviter_username="wxid_inviter")
        truncated = bytes([(1 << 3) | 2, 20]) + b"abc"

        records = parse_chatroom_ext_buffer(good + truncated)

        self.assertEqual({"wxid_good"}, set(records))

    def test_non_bytes_and_empty_payload_are_safe(self):
        self.assertEqual({}, parse_chatroom_ext_buffer(None))
        self.assertEqual({}, parse_chatroom_ext_buffer(b""))
        self.assertEqual({}, parse_chatroom_ext_buffer("not-bytes"))

    def test_ext_buffer_diagnostics_report_partial_without_discarding_valid_records(self):
        blob = _member("wxid_a") + b"\x0a\x05bad"
        result = parse_chatroom_ext_buffer_with_status(blob)
        self.assertEqual(result.status, "partial")
        self.assertIn("wxid_a", result.members)


if __name__ == "__main__":
    unittest.main()
