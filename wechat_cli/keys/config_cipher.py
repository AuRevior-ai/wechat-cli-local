"""Read the observed Weixin Config.Cipher layout; authenticate every result.

Layout reference: WeChatMsg-4x/wx4/key.py (4.1.11+ observations).
Offsets and the obfuscation mask are compatibility heuristics, not proof of a key.
No process writes, hooks, or secret-bearing diagnostics are used here.
"""

import re
import struct

from .common import scan_memory_for_keys

CONFIG_NAME = b"com.Tencent.WCDB.Config.Cipher"
# Public code-obfuscation constant; this is not an account encryption key.
CONFIG_MASK = bytes.fromhex(
    "d2c7442458020000004889442450488b450048844c2448488944254048584c24"
)
MAX_BLOB_SIZE = 1024
MAX_ADDRESS = 0x800000000000
HEX_LITERAL = re.compile(rb"x'([0-9a-f]{64,192})'")


def name_addresses(base, data):
    addresses = set()
    offset = data.find(CONFIG_NAME)
    while offset >= 0:
        addresses.add(base + offset)
        offset = data.find(CONFIG_NAME, offset + 1)
    return addresses


def _valid_pointer(address):
    return 0x10000 <= address < MAX_ADDRESS


def recover_keys(chunk_factory, read_memory, db_files, print_fn, names=None):
    """Two-pass lookup with exact bounded reads and the shared page verifier.

    chunk_factory must yield (address, bytes) with >= 256 bytes of contiguous
    overlap. Passing names collected during the raw-key scan saves one pass.
    """
    if names is None:
        names = set()
        for base, data in chunk_factory():
            names.update(name_addresses(base, data))
    keys = {}
    salts = {}
    for rel, _, _, salt, _ in db_files:
        salts.setdefault(salt, []).append(rel)
    remaining = set(salts)
    counts = {"names": len(names), "refs": 0, "objects": 0, "blobs": 0, "hex": 0}
    seen_refs = set()
    seen_objects = set()

    def exact(address, size):
        if not _valid_pointer(address) or address + size > MAX_ADDRESS:
            return None
        data = read_memory(address, size)
        return data if data is not None and len(data) == size else None

    if names and remaining:
        patterns = [struct.pack("<QQ", address, len(CONFIG_NAME)) for address in names]
        for base, data in chunk_factory():
            for pattern in patterns:
                offset = data.find(pattern)
                while offset >= 0:
                    ref = base + offset
                    offset = data.find(pattern, offset + 1)
                    if ref in seen_refs:
                        continue
                    seen_refs.add(ref)
                    counts["refs"] += 1
                    node = exact(ref - 0x10, 0x30)
                    if not node or node[0x10:0x20] != pattern:
                        continue
                    config = struct.unpack_from("<Q", node, 0x28)[0]
                    if not _valid_pointer(config) or config in seen_objects:
                        continue
                    seen_objects.add(config)
                    counts["objects"] += 1
                    header = exact(config + 0x88, 0x18)
                    if not header:
                        continue
                    pointer, size = struct.unpack_from("<QQ", header, 8)
                    if not 0 < size <= MAX_BLOB_SIZE:
                        continue
                    blob = exact(pointer, size)
                    if blob is None:
                        continue
                    counts["blobs"] += 1
                    decoded = bytes(
                        value ^ CONFIG_MASK[index % len(CONFIG_MASK)]
                        for index, value in enumerate(blob)
                    ).lower()
                    # Reuse the existing verifier but suppress its secret-bearing
                    # FOUND messages. Only aggregate counts leave this module.
                    counts["hex"] += scan_memory_for_keys(
                        decoded, HEX_LITERAL, db_files, salts, keys, remaining,
                        0, 0, lambda _: None,
                    )
                    if not remaining:
                        break
                if not remaining:
                    break
            if not remaining:
                break
    print_fn(
        "[*] Config.Cipher: "
        + ", ".join(f"{name}={value}" for name, value in counts.items())
        + f", verified={len(keys)}/{len(salts)}"
    )
    return keys
