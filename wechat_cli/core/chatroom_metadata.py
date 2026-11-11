"""Read-only decoding for WeChat chat_room.ext_buffer member metadata."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any


_MAX_VARINT_BYTES = 10


@dataclass(frozen=True)
class ChatroomMetadataParseResult:
    members: dict[str, dict[str, Any]]
    status: str


def _read_varint(data: bytes, offset: int) -> tuple[int, int]:
    value = 0
    shift = 0
    for _ in range(_MAX_VARINT_BYTES):
        if offset >= len(data):
            raise ValueError("truncated varint")
        byte = data[offset]
        offset += 1
        value |= (byte & 0x7F) << shift
        if not byte & 0x80:
            return value, offset
        shift += 7
    raise ValueError("varint too long")


def _read_field(data: bytes, offset: int) -> tuple[int, int, Any, int]:
    key, offset = _read_varint(data, offset)
    field_number = key >> 3
    wire_type = key & 0x07
    if field_number <= 0:
        raise ValueError("invalid field number")
    if wire_type == 0:
        value, offset = _read_varint(data, offset)
        return field_number, wire_type, value, offset
    if wire_type == 2:
        length, offset = _read_varint(data, offset)
        end = offset + length
        if end < offset or end > len(data):
            raise ValueError("truncated length-delimited field")
        return field_number, wire_type, data[offset:end], end
    raise ValueError(f"unsupported wire type: {wire_type}")


def _decode_text(value: bytes) -> str:
    try:
        return value.decode("utf-8").strip()
    except (AttributeError, UnicodeDecodeError):
        return ""


def _parse_member(payload: bytes) -> dict[str, Any] | None:
    username = ""
    room_display_name = ""
    member_flag: int | None = None
    inviter_username = ""
    offset = 0
    while offset < len(payload):
        field, wire_type, value, offset = _read_field(payload, offset)
        if field == 1 and wire_type == 2:
            username = _decode_text(value)
        elif field == 2 and wire_type == 2:
            room_display_name = _decode_text(value)
        elif field == 3 and wire_type == 0:
            member_flag = int(value)
        elif field == 4 and wire_type == 2:
            inviter_username = _decode_text(value)
    if not username:
        return None
    if inviter_username == username:
        inviter_username = ""
    return {
        "username": username,
        "room_display_name": room_display_name,
        "member_flag": member_flag,
        "inviter_username": inviter_username,
    }


def parse_chatroom_ext_buffer_with_status(value: object) -> ChatroomMetadataParseResult:
    """Decode current member metadata while retaining parse completeness."""
    if not isinstance(value, (bytes, bytearray, memoryview)):
        return ChatroomMetadataParseResult({}, "unavailable")
    data = bytes(value)
    if not data:
        return ChatroomMetadataParseResult({}, "unavailable")

    result: dict[str, dict[str, Any]] = {}
    partial = False
    offset = 0
    while offset < len(data):
        try:
            field, wire_type, payload, offset = _read_field(data, offset)
        except ValueError:
            partial = True
            break
        if field != 1 or wire_type != 2:
            continue
        try:
            member = _parse_member(payload)
        except ValueError:
            partial = True
            continue
        if member is not None:
            result[member["username"]] = member
    return ChatroomMetadataParseResult(result, "partial" if partial else "complete")


def parse_chatroom_ext_buffer(value: object) -> dict[str, dict[str, Any]]:
    """Compatibility wrapper returning only decoded current-member metadata."""
    return parse_chatroom_ext_buffer_with_status(value).members
