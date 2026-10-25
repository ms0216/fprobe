"""PYNQ-Z2 の PS の中継（host/pynq/fprobe-relay/relay.c）: zig cc で共有ライブラリにし、PL を Python の模型にして試す。

模型は zynq_frame_link のレジスタ表（gateware/rtl/hostlink/zynq_frame_link.vhd）どおり。
"""
import ctypes
import shutil
import subprocess
from pathlib import Path

import pytest

SRC = Path(__file__).resolve().parents[1] / "pynq" / "fprobe-relay"
ID, VERSION, RX_STATUS, RX_COMMIT, TX_HEAD, TX_DONE, DROPS, RX_BUF, TX_BUF = (
    0x000, 0x004, 0x008, 0x00C, 0x010, 0x014, 0x018, 0x400, 0x800)


class Stats(ctypes.Structure):
    _fields_ = [("to_pl", ctypes.c_uint32), ("from_pl", ctypes.c_uint32), ("dropped_bad", ctypes.c_uint32),
                ("pl_drops", ctypes.c_uint32), ("seq", ctypes.c_uint8)]


class Peer(ctypes.Structure):
    _fields_ = [("addr", ctypes.c_uint32), ("port", ctypes.c_uint16), ("known", ctypes.c_int)]


RD = ctypes.CFUNCTYPE(ctypes.c_uint32, ctypes.c_void_p, ctypes.c_uint32)
WR = ctypes.CFUNCTYPE(None, ctypes.c_void_p, ctypes.c_uint32, ctypes.c_uint32)


class PlOps(ctypes.Structure):
    _fields_ = [("rd", RD), ("wr", WR), ("ctx", ctypes.c_void_p)]


@pytest.fixture(scope="module")
def lib(tmp_path_factory):
    zig = shutil.which("zig")
    assert zig, "zig が要る（試験の前提）"
    out = tmp_path_factory.mktemp("relay") / "relay.dll"
    subprocess.run([zig, "cc", "-shared", "-O2", "-Wall", "-Werror", "-o", str(out), str(SRC / "relay.c")],
                   check=True)
    lib = ctypes.CDLL(str(out))
    lib.relay_check_design.argtypes = [ctypes.POINTER(PlOps)]
    lib.relay_from_pc.argtypes = [ctypes.POINTER(PlOps), ctypes.c_char_p, ctypes.c_size_t, ctypes.POINTER(Stats)]
    lib.relay_to_pc.argtypes = [ctypes.POINTER(PlOps), ctypes.c_char_p, ctypes.c_size_t, ctypes.POINTER(Stats)]
    lib.relay_to_pc.restype = ctypes.c_size_t
    lib.relay_peer_update.argtypes = [ctypes.POINTER(Peer), ctypes.c_uint32, ctypes.c_uint16]
    return lib


class PlModel:
    def __init__(self, ident=0x46504C4B):
        self.ident, self.busy, self.rx = ident, False, bytearray(1024)
        self.frames, self.tx, self.done = [], [], 0
        self.ops = PlOps(RD(self.rd), WR(self.wr), None)

    def rd(self, _ctx, addr):
        if addr == ID:
            return self.ident
        if addr == VERSION:
            return 1
        if addr == RX_STATUS:
            return int(self.busy)
        if addr == TX_HEAD:
            return len(self.tx[0]) if self.tx else 0
        if TX_BUF <= addr < TX_BUF + 1024 and self.tx:
            return int.from_bytes(self.tx[0][addr - TX_BUF:addr - TX_BUF + 4].ljust(4, b"\0"), "little")
        return 0

    def wr(self, _ctx, addr, value):
        if RX_BUF <= addr < RX_BUF + 1024:
            self.rx[addr - RX_BUF:addr - RX_BUF + 4] = value.to_bytes(4, "little")
        elif addr == RX_COMMIT:
            self.frames.append(bytes(self.rx[:value]))
        elif addr == TX_DONE:
            self.tx.pop(0)
            self.done += 1


def from_pc(lib, pl, dgram, st):
    return lib.relay_from_pc(ctypes.byref(pl.ops), dgram, len(dgram), ctypes.byref(st))


def to_pc(lib, pl, st):
    buf = ctypes.create_string_buffer(600)
    n = lib.relay_to_pc(ctypes.byref(pl.ops), buf, len(buf), ctypes.byref(st))
    return buf.raw[:n]


def test_datagram_becomes_a_frame(lib):
    pl, st = PlModel(), Stats()
    assert from_pc(lib, pl, bytes([1, 0x77]) + bytes(range(9)), st) == 0
    assert pl.frames == [bytes([1]) + bytes(range(9))]          # seq は捨てる
    assert from_pc(lib, pl, bytes([0, 0]), st) == 0             # 空の CONTROL（宛先の結び付け）も渡す
    assert pl.frames[-1] == b"\x00" and st.to_pl == 2


def test_frame_becomes_a_datagram_with_seq(lib):
    pl, st = PlModel(), Stats()
    pl.tx = [bytes([1]) + bytes(range(5)), bytes([3]) + bytes(512)]
    assert to_pc(lib, pl, st) == bytes([1, 0]) + bytes(range(5))
    assert to_pc(lib, pl, st) == bytes([3, 1]) + bytes(512)
    assert to_pc(lib, pl, st) == b"" and pl.done == 2


def test_full_rx_makes_the_relay_wait(lib):
    pl, st = PlModel(), Stats()
    pl.busy = True
    assert from_pc(lib, pl, b"\x01\x00abc", st) == 1 and pl.frames == []
    pl.busy = False
    assert from_pc(lib, pl, b"\x01\x00abc", st) == 0 and pl.frames == [b"\x01abc"]


def test_bad_datagrams_are_dropped_and_counted(lib):
    pl, st = PlModel(), Stats()
    for bad in (b"", b"\x01", bytes(2 + 513)):
        assert from_pc(lib, pl, bad, st) < 0
    assert from_pc(lib, pl, bytes(2 + 512), st) == 0            # ちょうど最大は渡す
    assert st.dropped_bad == 3 and len(pl.frames) == 1


def test_relay_refuses_without_the_design(lib):
    assert lib.relay_check_design(ctypes.byref(PlModel().ops)) == 0
    assert lib.relay_check_design(ctypes.byref(PlModel(ident=0).ops)) < 0


def test_peer_follows_the_last_sender(lib):
    peer = Peer()
    lib.relay_peer_update(ctypes.byref(peer), 0x0101A8C0, 5000)
    lib.relay_peer_update(ctypes.byref(peer), 0x0201A8C0, 6000)
    assert (peer.addr, peer.port, peer.known) == (0x0201A8C0, 6000, 1)


def test_only_well_formed_datagrams_move_the_peer(lib):
    # main.c は relay_dgram_ok が真のときだけ送り主を宛先にする（形の悪いものを送った相手に乗っ取られない）
    lib.relay_dgram_ok.argtypes = [ctypes.c_size_t]
    assert [lib.relay_dgram_ok(n) for n in (0, 1, 2, 514, 515, 2048)] == [0, 0, 1, 1, 0, 0]


IOMEM_FULL = b"00000000-1fffffff : System RAM\n  00008000-00efffff : Kernel code\nfffc0000-fffcffff : fffc0000.sram\n"
IOMEM_480M = b"00000000-1dffffff : System RAM\n  00008000-00efffff : Kernel code\nfffc0000-fffcffff : fffc0000.sram\n"


def test_the_deep_buffer_region_must_be_outside_linux_ram(lib):
    # PL が DDR に書く領域は Linux の使っているメモリ（/proc/iomem の System RAM）と重なってはならない
    lib.relay_region_free.argtypes = [ctypes.c_char_p, ctypes.c_uint32, ctypes.c_uint32]
    assert lib.relay_region_free(IOMEM_FULL, 0x1E000000, 32 << 20) == 0      # mem=480M の前: 重なる
    assert lib.relay_region_free(IOMEM_480M, 0x1E000000, 32 << 20) == 1      # mem=480M の後: 空いている
    assert lib.relay_region_free(IOMEM_480M, 0x1DF00000, 32 << 20) == 0      # 端が重なる
    assert lib.relay_region_free(IOMEM_480M, 0, 32 << 20) == 0               # 0 は使えない


def test_the_deep_buffer_is_checked_against_what_the_pl_actually_uses(lib):
    # 設定の大きさでなく、PL の MEM_BYTES（実際に使う大きさ）の範囲で確かめる。16 の倍数・DDR（512 MB）の中
    f = lib.relay_deep_region_ok
    f.argtypes = [ctypes.c_char_p, ctypes.c_uint32, ctypes.c_uint32, ctypes.c_uint32]
    mb32 = 32 << 20
    assert f(IOMEM_480M, 0x1E000000, mb32, mb32) == 0                      # 使える
    assert f(IOMEM_480M, 0x1E000000, mb32, 0) == 1                         # 深いバッファの無い回路
    assert f(IOMEM_480M, 0x1F000000, 16 << 20, mb32) < 0                   # 設定が PL の大きさより小さい
    assert f(IOMEM_480M, 0x1E000008, mb32, mb32) < 0                       # 16 の倍数でない
    assert f(b"00000000-1bffffff : System RAM\n", 0x1F000000, mb32, mb32) < 0   # DDR の外へはみ出す
    assert f(IOMEM_FULL, 0x1E000000, mb32, mb32) < 0                       # Linux のメモリと重なる


def test_time_in_ns_does_not_overflow_a_32_bit_long(lib):
    # ARM の long は 32 ビット。秒 × 10^9 を long で計算すると 2.1 秒で溢れ、経過時間の比べが狂う（待たずに CPU を使い切る）
    lib.relay_ns.argtypes = [ctypes.c_int64, ctypes.c_int64]
    lib.relay_ns.restype = ctypes.c_int64
    assert lib.relay_ns(10, 5) == 10_000_000_005
    assert lib.relay_ns(100_000, 999_999_999) == 100_000_999_999_999


def test_payload_size_limits_are_exact(lib):
    pl, st = PlModel(), Stats()
    assert from_pc(lib, pl, bytes([1, 0]) + bytes(512), st) == 0 and len(pl.frames[-1]) == 513
    assert from_pc(lib, pl, bytes([1, 0]) + bytes(513), st) < 0 and st.dropped_bad == 1
    assert from_pc(lib, pl, b"\x01", st) < 0 and st.dropped_bad == 2         # ch だけで seq が無い
    pl.tx = [bytes(514)]                                                       # PL が上限を超える長さを出した
    assert to_pc(lib, pl, st) == b"" and pl.done == 1 and st.dropped_bad == 3  # 読まずに捨てて先へ進む
    pl.tx = [bytes(513)]
    small = ctypes.create_string_buffer(513)                                   # 出力の入れ物が足りない
    assert lib.relay_to_pc(ctypes.byref(pl.ops), small, len(small), ctypes.byref(st)) == 0 and pl.done == 2


def test_a_system_ram_line_after_nested_lines_is_still_seen(lib):
    lib.relay_region_free.argtypes = [ctypes.c_char_p, ctypes.c_uint32, ctypes.c_uint32]
    iomem = b"00000000-0fffffff : System RAM\n  00008000-00efffff : Kernel code\n10000000-1fffffff : System RAM\n"
    assert lib.relay_region_free(iomem, 0x1E000000, 32 << 20) == 0


def test_main_compiles_for_the_target_arm_linux(tmp_path):
    # PYNQ の上で make する前に、同じ警告の厳しさ（-Wall -Wextra -Werror）で ARM 向けに通るか確かめる
    zig = shutil.which("zig")
    assert zig
    r = subprocess.run([zig, "cc", "-target", "arm-linux-gnueabihf", "-O2", "-Wall", "-Wextra", "-Werror",
                        "-o", str(tmp_path / "fprobe-relay"), str(SRC / "main.c"), str(SRC / "relay.c")],
                       capture_output=True, text=True)
    assert r.returncode == 0, r.stderr
