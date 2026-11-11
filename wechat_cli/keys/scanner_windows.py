"""Windows 密钥提取 — 扫描 Weixin.exe 进程内存"""

import ctypes
import ctypes.wintypes as wt
from concurrent.futures import ThreadPoolExecutor, as_completed
import functools
import hashlib
import os
import re
import subprocess
import sys
import time

from . import config_cipher
from .common import (
    collect_db_files,
    cross_verify_keys,
    save_results,
    scan_memory_for_keys,
    verify_enc_key,
)

print = functools.partial(print, flush=True)

kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
kernel32.OpenProcess.restype = wt.HANDLE
kernel32.OpenProcess.argtypes = [wt.DWORD, wt.BOOL, wt.DWORD]
kernel32.VirtualQueryEx.restype = ctypes.c_size_t
kernel32.VirtualQueryEx.argtypes = [wt.HANDLE, ctypes.c_uint64, ctypes.c_void_p, ctypes.c_size_t]
kernel32.ReadProcessMemory.argtypes = [
    wt.HANDLE, ctypes.c_uint64, ctypes.c_void_p, ctypes.c_size_t,
    ctypes.POINTER(ctypes.c_size_t),
]
kernel32.QueryFullProcessImageNameW.argtypes = [
    wt.HANDLE, wt.DWORD, wt.LPWSTR, ctypes.POINTER(wt.DWORD),
]
kernel32.CloseHandle.argtypes = [wt.HANDLE]
MEM_COMMIT = 0x1000
READABLE = {0x02, 0x04, 0x08, 0x20, 0x40, 0x80}
MEMORY_CHUNK_SIZE = 2 * 1024 * 1024
MEMORY_OVERLAP = 256


def _extract_internal_key_candidates_from_bytes(dll_bytes):
    """Extract 32-byte Weixin 4.1 internal XOR keys from DLL instructions."""
    candidates = []
    seen = set()
    for match in re.finditer(b"\x48\xba", dll_bytes):
        start = match.start()
        pos = start
        key = bytearray()
        for _ in range(4):
            if dll_bytes[pos:pos + 2] != b"\x48\xba":
                break
            key.extend(dll_bytes[pos + 2:pos + 10])
            pos += 10
            limit = min(len(dll_bytes), start + 80)
            next_positions = [
                found
                for needle in (b"\x48\xba", b"\x48\x85")
                if (found := dll_bytes.find(needle, pos, limit)) >= 0
            ]
            if not next_positions:
                break
            pos = min(next_positions)
        if len(key) == 32 and dll_bytes[pos:pos + 3] == b"\x48\x85\xc0":
            value = bytes(key)
            if value not in seen:
                seen.add(value)
                candidates.append(value)
    return candidates


_KEY_STUB_TAIL = b"\x00" * 10 + b"\x20" + b"\x00" * 7 + b"\x2f" + b"\x00" * 7


def _extract_candidate_pointers_from_region(data):
    """Return key-buffer pointers embedded in Weixin 4.1 memory stubs."""
    pointers = []
    start = 0
    while True:
        tail_pos = data.find(_KEY_STUB_TAIL, start)
        if tail_pos < 0:
            break
        start = tail_pos + 1
        if tail_pos < 6:
            continue
        stub_start = tail_pos - 6
        pointers.append(int.from_bytes(data[stub_start:stub_start + 8], "little"))
    return pointers


def _candidate_blocks_from_region(data, pointer_reader):
    values = []
    seen = set()
    for pointer in _extract_candidate_pointers_from_region(data):
        value = pointer_reader(pointer, 32)
        if (
            value
            and _is_potential_key_block(value)
            and value not in seen
        ):
            seen.add(value)
            values.append(value)
    return values


def _decode_passphrase_candidate(memory_block, internal_key):
    if len(memory_block) != 32 or len(internal_key) != 32:
        raise ValueError("memory_block and internal_key must both be 32 bytes")
    return bytes(a ^ b for a, b in zip(memory_block, internal_key))


def _derive_v41_encryption_key(passphrase, salt, iterations=256000):
    return hashlib.pbkdf2_hmac(
        "sha512", passphrase, salt, iterations, dklen=32
    )


def _derive_v41_key_map(
    passphrase,
    db_files,
    iterations=256000,
    max_workers=None,
):
    """Derive and verify the key for every database salt."""
    pages_by_salt = {}
    for _, _, _, salt_hex, page1 in db_files:
        pages_by_salt.setdefault(salt_hex, page1)

    def derive_one(salt_hex, page1):
        enc_key = _derive_v41_encryption_key(
            passphrase, bytes.fromhex(salt_hex), iterations=iterations
        )
        if verify_enc_key(enc_key, page1):
            return salt_hex, enc_key.hex()
        return None

    if not pages_by_salt:
        return {}
    worker_count = max_workers or min(32, max(1, os.cpu_count() or 1))
    with ThreadPoolExecutor(max_workers=worker_count) as executor:
        futures = [
            executor.submit(derive_one, salt_hex, page1)
            for salt_hex, page1 in pages_by_salt.items()
        ]
        return {
            result[0]: result[1]
            for future in as_completed(futures)
            if (result := future.result()) is not None
        }


def _verify_v41_candidate(memory_block, internal_key, db_page1, iterations=256000):
    passphrase = _decode_passphrase_candidate(memory_block, internal_key)
    enc_key = _derive_v41_encryption_key(
        passphrase, db_page1[:16], iterations=iterations
    )
    if verify_enc_key(enc_key, db_page1):
        return passphrase, enc_key
    return None


def _find_verified_v41_candidate(
    memory_blocks,
    internal_keys,
    db_page1,
    iterations=256000,
    max_workers=None,
):
    pairs = [
        (memory_block, internal_key)
        for memory_block in memory_blocks
        for internal_key in internal_keys
    ]
    if not pairs:
        return None
    worker_count = max_workers or min(32, max(1, os.cpu_count() or 1))
    executor = ThreadPoolExecutor(max_workers=worker_count)
    futures = [
        executor.submit(
            _verify_v41_candidate,
            memory_block,
            internal_key,
            db_page1,
            iterations,
        )
        for memory_block, internal_key in pairs
    ]
    try:
        for future in as_completed(futures):
            result = future.result()
            if result:
                for pending in futures:
                    pending.cancel()
                return result
        return None
    finally:
        executor.shutdown(wait=True, cancel_futures=True)


def _is_potential_key_block(value):
    if len(value) != 32 or len(set(value)) < 15:
        return False
    return sum(32 <= byte <= 126 for byte in value) <= 24


def _weixin_dll_paths(executable_path):
    install_dir = os.path.dirname(os.path.abspath(executable_path))
    candidates = [os.path.join(install_dir, "Weixin.dll")]
    try:
        for entry in sorted(os.scandir(install_dir), key=lambda item: item.name):
            if entry.is_dir():
                candidates.append(os.path.join(entry.path, "Weixin.dll"))
    except OSError:
        pass
    return [path for path in candidates if os.path.isfile(path)]


def _find_versioned_weixin_dll(executable_path):
    paths = _weixin_dll_paths(executable_path)
    return paths[0] if paths else None


def _get_loaded_weixin_dll(handle):
    """Prefer the DLL actually mapped by this process, if Windows exposes it."""
    psapi = ctypes.WinDLL("psapi", use_last_error=True)
    enum = psapi.EnumProcessModulesEx
    enum.argtypes = [wt.HANDLE, ctypes.POINTER(wt.HMODULE), wt.DWORD,
                     ctypes.POINTER(wt.DWORD), wt.DWORD]
    get_name = psapi.GetModuleFileNameExW
    get_name.argtypes = [wt.HANDLE, wt.HMODULE, wt.LPWSTR, wt.DWORD]
    modules = (wt.HMODULE * 1024)()
    needed = wt.DWORD()
    try:
        if not enum(handle, modules, ctypes.sizeof(modules), ctypes.byref(needed), 3):
            return None
        if needed.value > ctypes.sizeof(modules):
            modules = (wt.HMODULE * (needed.value // ctypes.sizeof(wt.HMODULE)))()
            if not enum(handle, modules, ctypes.sizeof(modules), ctypes.byref(needed), 3):
                return None
        for module in modules[:min(len(modules), needed.value // ctypes.sizeof(wt.HMODULE))]:
            buffer = ctypes.create_unicode_buffer(32768)
            if get_name(handle, module, buffer, len(buffer)):
                if os.path.basename(buffer.value).lower() == "weixin.dll":
                    return buffer.value
    except (OSError, TypeError, ctypes.ArgumentError):
        return None
    return None


def _load_internal_key_candidates(executable_path, handle=None, print_fn=None):
    loaded = _get_loaded_weixin_dll(handle) if handle is not None else None
    paths = list(dict.fromkeys(([loaded] if loaded else []) + _weixin_dll_paths(executable_path)))
    candidates = []
    for dll_path in paths:
        try:
            with open(dll_path, "rb") as file:
                found = _extract_internal_key_candidates_from_bytes(file.read())
        except OSError as exc:
            if print_fn:
                print_fn(f"[WARN] 无法读取 Weixin.dll (winerror={getattr(exc, 'winerror', None)})")
            continue
        if print_fn:
            print_fn(f"[*] DLL={dll_path}; internal_candidates={len(found)}; loaded={dll_path == loaded}")
        candidates.extend(value for value in found if value not in candidates)
        if dll_path == loaded and found:
            break
    return candidates


def _select_v41_verification_db(db_files):
    message_dbs = []
    for item in db_files:
        match = re.search(
            r"(?:^|[\\/])message_(\d+)\.db$",
            item[0],
            re.IGNORECASE,
        )
        if match:
            message_dbs.append((int(match.group(1)), item))
    if message_dbs:
        return max(message_dbs, key=lambda value: value[0])[1]
    return db_files[0] if db_files else None


class MBI(ctypes.Structure):
    _fields_ = [
        ("BaseAddress", ctypes.c_uint64), ("AllocationBase", ctypes.c_uint64),
        ("AllocationProtect", wt.DWORD), ("_pad1", wt.DWORD),
        ("RegionSize", ctypes.c_uint64), ("State", wt.DWORD),
        ("Protect", wt.DWORD), ("Type", wt.DWORD), ("_pad2", wt.DWORD),
    ]


def _get_pids(print_fn=print):
    """返回所有 Weixin.exe 进程的 (pid, mem_kb) 列表，按内存降序"""
    r = subprocess.run(["tasklist", "/FI", "IMAGENAME eq Weixin.exe", "/FO", "CSV", "/NH"],
                       capture_output=True, text=True)
    pids = []
    for line in r.stdout.strip().split('\n'):
        if not line.strip():
            continue
        p = line.strip('"').split('","')
        if len(p) >= 5:
            pid = int(p[1])
            mem = int(p[4].replace(',', '').replace(' K', '').strip() or '0')
            pids.append((pid, mem))
    if not pids:
        raise RuntimeError("Weixin.exe 未运行")
    pids.sort(key=lambda x: x[1], reverse=True)
    for pid, mem in pids:
        print_fn(f"[+] Weixin.exe PID={pid} ({mem // 1024}MB)")
    return pids


def _get_process_executable_path(handle, query_fn=None):
    query_fn = query_fn or kernel32.QueryFullProcessImageNameW
    buffer = ctypes.create_unicode_buffer(32768)
    size = wt.DWORD(len(buffer))
    if not query_fn(handle, 0, buffer, ctypes.byref(size)):
        return None
    return buffer.value


def _get_file_version(path):
    version = ctypes.WinDLL("version", use_last_error=True)
    version.GetFileVersionInfoSizeW.argtypes = [wt.LPCWSTR, ctypes.POINTER(wt.DWORD)]
    version.GetFileVersionInfoW.argtypes = [wt.LPCWSTR, wt.DWORD, wt.DWORD, ctypes.c_void_p]
    version.VerQueryValueW.argtypes = [ctypes.c_void_p, wt.LPCWSTR,
                                      ctypes.POINTER(ctypes.c_void_p), ctypes.POINTER(wt.UINT)]
    size = version.GetFileVersionInfoSizeW(path, None)
    if not size:
        return "unknown"
    buffer = ctypes.create_string_buffer(size)
    if not version.GetFileVersionInfoW(path, 0, size, buffer):
        return "unknown"
    pointer = ctypes.c_void_p()
    length = wt.UINT()
    if not version.VerQueryValueW(buffer, "\\", ctypes.byref(pointer), ctypes.byref(length)):
        return "unknown"
    if length.value < 16 or not pointer.value:
        return "unknown"
    info = ctypes.cast(pointer, ctypes.POINTER(wt.DWORD))
    return f"{info[2] >> 16}.{info[2] & 0xffff}.{info[3] >> 16}.{info[3] & 0xffff}"


def _read_mem(h, addr, sz):
    buf = ctypes.create_string_buffer(sz)
    n = ctypes.c_size_t(0)
    kernel32.ReadProcessMemory(h, ctypes.c_uint64(addr), buf, sz, ctypes.byref(n))
    # ERROR_PARTIAL_COPY can still return a usable prefix via lpNumberOfBytesRead.
    if n.value:
        return buf.raw[:min(n.value, sz)]
    return None


def _iter_memory_chunks(h, regions, chunk_size=MEMORY_CHUNK_SIZE,
                        overlap=MEMORY_OVERLAP, stats=None):
    """Bound allocations; retry unreadable spans down to 4 KiB, keep overlap.

    Overlap is reset at every unreadable gap. Bytes on opposite sides of a gap
    must never form a fabricated key or object reference.
    """
    if chunk_size <= 0 or overlap < 0:
        raise ValueError("chunk_size must be positive and overlap nonnegative")

    def fragments(base, size):
        data = _read_mem(h, base, size)
        length = len(data) if data else 0
        if stats is not None:
            stats["read_calls"] = stats.get("read_calls", 0) + 1
            stats["read_bytes"] = stats.get("read_bytes", 0) + length
            if length < size:
                stats["short_reads"] = stats.get("short_reads", 0) + 1
        if length:
            yield base, data
            if length < size:
                yield from fragments(base + length, size - length)
        elif size > 4096:
            split = max(4096, (size // 2) // 4096 * 4096)
            yield from fragments(base, split)
            yield from fragments(base + split, size - split)
        elif stats is not None:
            stats["skipped_bytes"] = stats.get("skipped_bytes", 0) + size

    tail = b""
    previous_end = None
    for base, size in regions:
        for offset in range(0, size, chunk_size):
            for address, data in fragments(base + offset, min(chunk_size, size - offset)):
                if address != previous_end:
                    tail = b""
                yield address - len(tail), tail + data
                previous_end = address + len(data)
                tail = (tail + data)[-overlap:] if overlap else b""


def _enum_regions(h):
    regs = []
    addr = 0
    mbi = MBI()
    while addr < 0x7FFFFFFFFFFF:
        if kernel32.VirtualQueryEx(h, ctypes.c_uint64(addr), ctypes.byref(mbi), ctypes.sizeof(mbi)) == 0:
            break
        if (mbi.State == MEM_COMMIT and mbi.Protect & 0xff in READABLE
                and not (mbi.Protect & 0x100) and mbi.RegionSize > 0):
            regs.append((mbi.BaseAddress, mbi.RegionSize))
        nxt = mbi.BaseAddress + mbi.RegionSize
        if nxt <= addr:
            break
        addr = nxt
    return regs


def _collect_v41_memory_blocks(handle, regions=None):
    values = []
    seen = set()
    if regions is None:
        regions = _enum_regions(handle)
    for base, data in _iter_memory_chunks(handle, regions):
        for value in _candidate_blocks_from_region(
            data,
            lambda pointer, block_size: _read_mem(
                handle, pointer, block_size
            ),
        ):
            if value not in seen:
                seen.add(value)
                values.append(value)
    return values


def _recover_config_cipher_key_map(handle, regions, db_files, print_fn=print,
                                    name_addresses=None):
    return config_cipher.recover_keys(
        lambda: _iter_memory_chunks(handle, regions),
        lambda address, size: _read_mem(handle, address, size),
        db_files, print_fn, names=name_addresses,
    )


def _recover_v41_key_map(
    handle,
    executable_path,
    db_files,
    iterations=256000,
    max_workers=None,
    regions=None,
    print_fn=None,
):
    emit = print_fn or (lambda _: None)
    internal_keys = _load_internal_key_candidates(executable_path, handle=handle, print_fn=print_fn)
    if not internal_keys:
        emit("[WARN] 口令恢复: internal_candidates=0，未匹配 DLL 指令特征")
        return {}
    memory_blocks = _collect_v41_memory_blocks(handle, regions=regions)
    target = _select_v41_verification_db(db_files)
    emit(f"[*] 口令恢复: internal_candidates={len(internal_keys)}, memory_candidates={len(memory_blocks)}")
    if not memory_blocks or target is None:
        emit("[WARN] 口令恢复: 无内存候选或可验证数据库")
        return {}
    emit(f"[*] 口令恢复: verifying_pairs={len(memory_blocks) * len(internal_keys)}, target={target[0]}")
    verified = _find_verified_v41_candidate(
        memory_blocks,
        internal_keys,
        target[4],
        iterations=iterations,
        max_workers=max_workers,
    )
    if not verified:
        emit("[WARN] 口令恢复: 候选未通过数据库 HMAC 校验；可能为版本布局或账号不匹配")
        return {}
    passphrase, _ = verified
    return _derive_v41_key_map(
        passphrase,
        db_files,
        iterations=iterations,
        max_workers=max_workers,
    )


def extract_keys(db_dir, output_path, pid=None, print_fn=None):
    """提取 Windows 微信数据库密钥。

    Args:
        db_dir: 微信数据库目录
        output_path: all_keys.json 输出路径
        pid: 可选，指定 PID（默认自动检测所有 Weixin.exe）

    Returns:
        dict: salt_hex -> enc_key_hex 映射
    """
    emit = print_fn or print
    if ctypes.sizeof(ctypes.c_void_p) != 8:
        raise RuntimeError("Windows 微信密钥扫描需要 64 位 Python/应用")
    emit("=" * 60)
    emit(f"[*] 扫描环境: Python={sys.version.split()[0]}, bits=64")
    emit("  提取所有微信数据库密钥")
    emit("=" * 60)

    db_files, salt_to_dbs = collect_db_files(db_dir)

    emit(f"\n找到 {len(db_files)} 个数据库, {len(salt_to_dbs)} 个不同的salt")
    for salt_hex, dbs in sorted(salt_to_dbs.items(), key=lambda x: len(x[1]), reverse=True):
        emit(f"  salt {salt_hex}: {', '.join(dbs)}")

    pids = _get_pids(emit) if pid is None else [(pid, 0)]

    hex_re = re.compile(b"x'([0-9a-fA-F]{64,192})'")
    key_map = {}
    remaining_salts = set(salt_to_dbs.keys())
    all_hex_matches = 0
    t0 = time.time()

    for pid_val, mem_kb in pids:
        h = kernel32.OpenProcess(0x0010 | 0x0400, False, pid_val)
        if not h:
            emit(f"[WARN] 无法打开进程 PID={pid_val}, winerror={ctypes.get_last_error()}；请核对进程权限")
            continue

        try:
            regions = _enum_regions(h)
            total_bytes = sum(s for _, s in regions)
            total_mb = total_bytes / 1024 / 1024
            emit(f"\n[*] 扫描 PID={pid_val} ({total_mb:.0f}MB, {len(regions)} 区域)")

            executable_path = _get_process_executable_path(h)
            if executable_path:
                emit(f"[*] Weixin version={_get_file_version(executable_path)}, executable={executable_path}")
            else:
                emit(f"[WARN] 无法读取 PID={pid_val} 可执行文件路径, winerror={ctypes.get_last_error()}")
            stats = {}
            config_names = set()
            for reg_idx, (base, data) in enumerate(_iter_memory_chunks(h, regions, stats=stats)):
                config_names.update(config_cipher.name_addresses(base, data))

                all_hex_matches += scan_memory_for_keys(
                    data, hex_re, db_files, salt_to_dbs,
                    key_map, remaining_salts, base, pid_val, emit,
                )

                if (reg_idx + 1) % 200 == 0:
                    elapsed = time.time() - t0
                    progress = stats.get("read_bytes", 0) / total_bytes * 100 if total_bytes else 100
                    emit(
                        f"  [{progress:.1f}%] {len(key_map)}/{len(salt_to_dbs)} salts matched, "
                        f"{all_hex_matches} hex patterns, {elapsed:.1f}s"
                    )
                if not remaining_salts:
                    break

            emit(
                f"[*] 内存读取 PID={pid_val}: read_bytes={stats.get('read_bytes', 0)}, "
                f"skipped_bytes={stats.get('skipped_bytes', 0)}, short_reads={stats.get('short_reads', 0)}"
            )
            if remaining_salts:
                config_keys = _recover_config_cipher_key_map(
                    h, regions, db_files, print_fn=emit, name_addresses=config_names,
                )
                key_map.update(config_keys)
                remaining_salts.difference_update(config_keys)

            if remaining_salts:
                if executable_path:
                    emit("\n[*] 未发现完整原始密钥，尝试微信 4.1+ 口令恢复")
                    try:
                        v41_keys = _recover_v41_key_map(
                            h,
                            executable_path,
                            db_files,
                            regions=regions,
                            print_fn=emit,
                        )
                    except Exception as exc:
                        emit(f"[WARN] 微信 4.1+ 恢复失败: {exc}")
                    else:
                        if v41_keys:
                            key_map.update(v41_keys)
                            remaining_salts.difference_update(v41_keys)
                            emit(
                                f"[+] 微信 4.1+ 恢复成功，已验证 "
                                f"{len(v41_keys)} 个数据库 salt"
                            )
        finally:
            kernel32.CloseHandle(h)

        if not remaining_salts:
            emit(f"\n[+] 所有密钥已找到，跳过剩余进程")
            break

    elapsed = time.time() - t0
    emit(f"\n扫描完成: {elapsed:.1f}s, {len(pids)} 个进程, {all_hex_matches} hex模式")

    cross_verify_keys(db_files, salt_to_dbs, key_map, emit)
    if not key_map:
        emit("[WARN] 所有恢复路径均未获得已验证密钥。请确认微信已登录，所选数据目录属于当前账号；保留 version、内存读取和候选数量日志以定位兼容性问题。")
    return save_results(db_files, salt_to_dbs, key_map, output_path, emit)
