"""不具合の再現試験（検出専用。修正されるまで xfail(strict)）: スナップショット・RTT・セミホスティング。
台帳: docs/reports/bugs/flash-snapshot-gdb.md"""

import json
import struct
import zipfile

import pytest

from fprobe import rtt
from fprobe.semihosting import SemihostingHost, Op
from fprobe.snapshot import Region, Snapshot, SnapshotError, core_file


def _fsnap(path, manifest):
    with zipfile.ZipFile(path, "w") as z:
        z.writestr("manifest.json", json.dumps(manifest))
    return path


@pytest.mark.parametrize("manifest", [
    {"format": 1, "registers": {"r0": "zz"}, "regions": []},
    {"format": 1, "regions": []},
    {"format": 1, "registers": [], "regions": []},
])
def test_bug_snapshot_load_leaks_raw_exceptions_for_bad_registers(tmp_path, manifest):
    path = _fsnap(tmp_path / "a.fsnap", manifest)
    with pytest.raises(SnapshotError):
        Snapshot.load(path)


def test_bug_snapshot_load_non_object_manifest(tmp_path):
    path = _fsnap(tmp_path / "a.fsnap", [1, 2, 3])
    with pytest.raises(SnapshotError):
        Snapshot.load(path)


@pytest.mark.xfail(strict=True, reason="BUG-SNAP-003: コアファイルの PT_LOAD が p_offset ≡ p_vaddr (mod p_align) を満たさない（4 の倍数でない番地の区画）")
def test_bug_core_file_load_segment_alignment():
    snap = Snapshot({"pc": 1}, [Region(0x20000002, b"abcdef", "ram")])
    data = core_file(snap)
    phnum = struct.unpack_from("<H", data, 44)[0]
    for i in range(phnum):
        p_type, p_offset, p_vaddr, _, _, _, _, p_align = struct.unpack_from("<IIIIIIII", data, 52 + 32 * i)
        if p_type == 1 and p_align > 1:
            assert p_offset % p_align == p_vaddr % p_align


class _Mem:
    """size バイトだけ読めるメモリ。範囲を越える読みは例外（pyOCD のバスエラー相当）。"""

    def __init__(self, size, content=b""):
        self.data = bytearray(size)
        self.data[:len(content)] = content

    def read_memory(self, address, length):
        if address < 0 or address + length > len(self.data):
            raise OSError(f"bus fault 0x{address:X}+{length}")
        return bytes(self.data[address:address + length])

    def write_memory(self, address, data):
        self.data[address:address + len(data)] = data


def test_bug_rtt_open_fails_when_name_is_at_the_end_of_memory():
    size = 0x400
    mem = _Mem(size)
    base = 0
    name_ptr = size - 4                                   # 名前 "ab\0" が最後の 4 バイト
    mem.data[name_ptr:name_ptr + 3] = b"ab\0"
    mem.data[0:16] = b"SEGGER RTT\0\0\0\0\0\0"
    struct.pack_into("<II", mem.data, 16, 1, 0)
    struct.pack_into("<IIIIII", mem.data, 24, name_ptr, 0x100, 0x40, 0, 0, 0)
    block = rtt.Rtt(mem, base, end=size)
    assert block.open().up[0].name == "ab"


class _Target:
    def __init__(self, mem, pc=0x10):
        self.mem, self.regs = mem, {"pc": pc, "r0": Op.SYS_WRITE0, "r1": 0}

    def read_core_register(self, name):
        return self.regs[name]

    def write_core_register(self, name, value):
        self.regs[name] = value

    def read_memory(self, address, length):
        return self.mem.read_memory(address, length)

    def write_memory(self, address, data):
        self.mem.write_memory(address, data)


def test_bug_semihosting_write0_near_the_end_of_memory():
    mem = _Mem(0x100)
    mem.data[0x10:0x12] = struct.pack("<H", 0xBEAB)
    mem.data[0xFC:0x100] = b"hi!\0"                        # 文字列は最後の 4 バイト
    target = _Target(mem)
    target.regs["r1"] = 0xFC
    out = []
    host = SemihostingHost(console=lambda stream, data: out.append(data))
    host.handle(target)
    assert out == [b"hi!"]
