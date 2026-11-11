import ctypes
import hashlib
import hmac
import os
import re
from pathlib import Path
import struct
import tempfile
import unittest
import sys
from unittest.mock import patch

if sys.platform != "win32":
    raise unittest.SkipTest("Windows process-memory tests")

from wechat_cli.keys import scanner_windows as scanner
from wechat_cli.keys.common import scan_memory_for_keys


def authenticated_db(salt=b"s" * 16, key=bytes(range(32))):
    page = bytearray(4096)
    page[:16] = salt
    mac_key = hashlib.pbkdf2_hmac(
        "sha512", key, bytes(b ^ 0x3A for b in salt), 2, dklen=32
    )
    mac = hmac.new(mac_key, page[16:4032], hashlib.sha512)
    mac.update(struct.pack("<I", 1))
    page[4032:] = mac.digest()
    return ("message/message_0.db", "unused", 4096, salt.hex(), bytes(page))


def config_memory(literal, *, blob_length=None, blob_pointer=None):
    # Independent fixture for the observed Config.Cipher layout, no live data.
    name = b"com.Tencent.WCDB.Config.Cipher"
    mask = bytes.fromhex(
        "d2c7442458020000004889442450488b450048844c2448488944254048584c24"
    )
    blob = bytes(byte ^ mask[i % 32] for i, byte in enumerate(literal))
    base = 0x10000
    memory = bytearray(0x5000)
    memory[100:100 + len(name)] = name
    struct.pack_into("<QQ", memory, 0x1110, base + 100, len(name))
    struct.pack_into("<Q", memory, 0x1128, base + 0x2100)
    struct.pack_into(
        "<QQQ", memory, 0x2188, 0,
        base + 0x3100 if blob_pointer is None else blob_pointer,
        len(blob) if blob_length is None else blob_length,
    )
    memory[0x3100:0x3100 + len(blob)] = blob

    def read(handle, address, size):
        offset = address - base
        if offset < 0 or offset + size > len(memory):
            return None
        return bytes(memory[offset:offset + size])

    return base, memory, read


class WindowsMemoryRegressionTests(unittest.TestCase):
    def test_raw_key_diagnostics_never_include_secret(self):
        db = authenticated_db()
        key_hex = bytes(range(32)).hex()
        logs, keys = [], {}
        scan_memory_for_keys(
            b"x'" + (key_hex + db[3]).encode() + b"'",
            re.compile(rb"x'([0-9a-fA-F]{64,192})'"), [db], {db[3]: [db[0]]},
            keys, {db[3]}, 0, 1, logs.append,
        )
        self.assertEqual(keys, {db[3]: key_hex})
        self.assertNotIn(key_hex, "\n".join(logs))

    def test_raw_hex_salts_are_case_insensitive(self):
        db = authenticated_db(salt=bytes(range(0xa0, 0xb0)))
        key_hex = bytes(range(32)).hex()
        keys = {}
        scan_memory_for_keys(
            b"x'" + (key_hex + db[3]).upper().encode() + b"'",
            re.compile(rb"x'([0-9a-fA-F]{64,192})'"), [db], {db[3]: [db[0]]},
            keys, {db[3]}, 0, 1, lambda _: None,
        )
        self.assertEqual(keys, {db[3]: key_hex})

    def test_prefers_loaded_dll_over_inactive_adjacent_versions(self):
        key = bytes(range(32))
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "Weixin.dll"
            path.write_bytes(b"".join(
                b"\x48\xba" + key[i:i + 8] + b"\x90" for i in range(0, 32, 8)
            ) + b"\x48\x85\xc0")
            with patch.object(scanner, "_get_loaded_weixin_dll", return_value=str(path)):
                found = scanner._load_internal_key_candidates(str(Path(folder) / "Weixin.exe"), handle=1)
        self.assertEqual(found, [key])

    def test_preserves_successful_prefix_of_partial_win32_read(self):
        class Kernel:
            @staticmethod
            def ReadProcessMemory(handle, address, buffer, size, count):
                ctypes.memmove(buffer, b"valid", 5)
                ctypes.cast(count, ctypes.POINTER(ctypes.c_size_t)).contents.value = 5
                return 0

        with patch.object(scanner, "kernel32", Kernel()):
            self.assertEqual(scanner._read_mem(1, 0x1000, 16), b"valid")

    def test_internal_keys_include_dlls_other_than_newest_file(self):
        keys = [bytes(range(32)), bytes(range(32, 64))]
        with tempfile.TemporaryDirectory() as folder:
            for index, key in enumerate(keys):
                path = Path(folder) / f"4.1.{index}" / "Weixin.dll"
                path.parent.mkdir()
                path.write_bytes(b"".join(
                    b"\x48\xba" + key[i:i + 8] + b"\x90"
                    for i in range(0, 32, 8)
                ) + b"\x48\x85\xc0")
                os.utime(path, (index + 1, index + 1))
            found = scanner._load_internal_key_candidates(str(Path(folder) / "Weixin.exe"))
        self.assertEqual(set(found), set(keys))

    def test_enumeration_includes_large_regions_with_readable_modifiers(self):
        class Kernel:
            @staticmethod
            def VirtualQueryEx(handle, address, output, size):
                if address.value:
                    return 0
                mbi = ctypes.cast(output, ctypes.POINTER(scanner.MBI)).contents
                mbi.BaseAddress = 0
                mbi.RegionSize = 600 * 1024 * 1024
                mbi.State = scanner.MEM_COMMIT
                mbi.Protect = 0x04 | 0x200
                return ctypes.sizeof(scanner.MBI)

        with patch.object(scanner, "kernel32", Kernel()):
            self.assertEqual(scanner._enum_regions(1), [(0, 600 * 1024 * 1024)])

    def test_chunks_recover_data_after_an_unreadable_page(self):
        self.assertTrue(hasattr(scanner, "_iter_memory_chunks"))
        data = b"a" * 4096 + b"?" * 4096 + b"b" * 4096

        def read(handle, address, size):
            start = address - 0x10000
            if start < 8192 and start + size > 4096:
                return None
            return data[start:start + size]

        with patch.object(scanner, "_read_mem", side_effect=read):
            chunks = list(scanner._iter_memory_chunks(1, [(0x10000, len(data))]))
        self.assertEqual(chunks, [(0x10000, b"a" * 4096), (0x12000, b"b" * 4096)])

    def test_chunks_keep_overlap_only_for_contiguous_bytes(self):
        self.assertTrue(hasattr(scanner, "_iter_memory_chunks"))
        with patch.object(scanner, "_read_mem", side_effect=lambda h, a, n: bytes([a]) * n):
            chunks = list(scanner._iter_memory_chunks(
                1, [(0, 8), (16, 4)], chunk_size=4, overlap=2
            ))
        self.assertEqual(chunks, [(0, b"\x00" * 4), (2, b"\x00" * 2 + b"\x04" * 4), (16, b"\x10" * 4)])

    def test_guard_pages_are_excluded(self):
        class Kernel:
            @staticmethod
            def VirtualQueryEx(handle, address, output, size):
                if address.value:
                    return 0
                mbi = ctypes.cast(output, ctypes.POINTER(scanner.MBI)).contents
                mbi.RegionSize = 4096
                mbi.State = scanner.MEM_COMMIT
                mbi.Protect = 0x04 | 0x100
                return ctypes.sizeof(scanner.MBI)

        with patch.object(scanner, "kernel32", Kernel()):
            self.assertEqual(scanner._enum_regions(1), [])


class ConfigCipherRegressionTests(unittest.TestCase):
    def recover(self, db, literal, **kwargs):
        self.assertTrue(hasattr(scanner, "_recover_config_cipher_key_map"))
        base, memory, read = config_memory(literal, **kwargs)
        logs = []
        with patch.object(scanner, "_read_mem", side_effect=read):
            result = scanner._recover_config_cipher_key_map(
                1, [(base, len(memory))], [db], print_fn=logs.append
            )
        return result, logs

    def test_recovers_only_hmac_verified_config_key_without_dll(self):
        db = authenticated_db()
        key_hex = bytes(range(32)).hex()
        result, logs = self.recover(db, b"X'" + (key_hex + db[3]).upper().encode() + b"'")
        self.assertEqual(result, {db[3]: key_hex})
        self.assertNotIn(key_hex, "\n".join(logs))
        self.assertTrue(any("Config.Cipher" in line for line in logs))

    def test_rejects_config_blob_that_does_not_authenticate_database(self):
        db = authenticated_db()
        result, logs = self.recover(db, b"x'" + ("ab" * 32 + db[3]).encode() + b"'")
        self.assertEqual(result, {})

    def test_rejects_oversized_blob_before_read(self):
        result, _ = self.recover(authenticated_db(), b"x''", blob_length=2 ** 40)
        self.assertEqual(result, {})

    def test_rejects_invalid_pointer(self):
        result, _ = self.recover(authenticated_db(), b"x''", blob_pointer=0xFFFF000000000000)
        self.assertEqual(result, {})

    def test_extract_keys_recovers_config_keys_before_legacy_fallback(self):
        db = authenticated_db()
        key_hex = bytes(range(32)).hex()
        base, memory, read = config_memory(b"x'" + (key_hex + db[3]).encode() + b"'")
        logs = []

        class Kernel:
            @staticmethod
            def OpenProcess(*args):
                return 1

            @staticmethod
            def CloseHandle(handle):
                return 1

        with tempfile.TemporaryDirectory() as folder, \
                patch.object(scanner, "kernel32", Kernel()), \
                patch.object(scanner, "collect_db_files", return_value=([db], {db[3]: [db[0]]})), \
                patch.object(scanner, "_get_pids", return_value=[(123, 100)]), \
                patch.object(scanner, "_get_process_executable_path", return_value="Weixin.exe"), \
                patch.object(scanner, "_get_file_version", return_value="4.1.test"), \
                patch.object(scanner, "_enum_regions", return_value=[(base, len(memory))]), \
                patch.object(scanner, "_read_mem", side_effect=read), \
                patch.object(scanner, "_recover_v41_key_map", side_effect=AssertionError("legacy not needed")):
            result = scanner.extract_keys("unused", str(Path(folder) / "keys.json"), print_fn=logs.append)
        self.assertEqual(result, {db[3]: key_hex})
        self.assertNotIn(key_hex, "\n".join(logs))
        self.assertTrue(any("read_bytes=" in line for line in logs))

    def test_name_marker_can_cross_a_read_chunk_boundary(self):
        from wechat_cli.keys import config_cipher

        marker = config_cipher.CONFIG_NAME
        memory = b" " * 20 + marker + b" " * 60
        names = set()
        with patch.object(scanner, "_read_mem", side_effect=lambda h, a, n: memory[a:a + n]):
            for base, data in scanner._iter_memory_chunks(1, [(0, len(memory))], chunk_size=32):
                names.update(config_cipher.name_addresses(base, data))
        self.assertEqual(names, {20})

    def test_empty_regions_remain_empty_instead_of_reenumerating(self):
        with patch.object(scanner, "_enum_regions", side_effect=AssertionError("unexpected rescan")):
            self.assertEqual(scanner._collect_v41_memory_blocks(1, regions=[]), [])


if __name__ == "__main__":
    unittest.main()
