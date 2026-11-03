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
                ("pl_drops", ctypes.c_uint32), ("seq", ctypes.c_uint8), ("bundle_max", ctypes.c_uint16)]


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
        self.tx_lag, self._lag_left = 0, 0           # TX_DONE の後、次のフレームが見えるまでの TX_HEAD の読みの数
        self.ops = PlOps(RD(self.rd), WR(self.wr), None)

    def rd(self, _ctx, addr):
        if addr == ID:
            return self.ident
        if addr == VERSION:
            return 1
        if addr == RX_STATUS:
            return int(self.busy)
        if addr == TX_HEAD:
            if self._lag_left:
                self._lag_left -= 1
                return 0
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
            self._lag_left = self.tx_lag


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
    f.argtypes = [ctypes.c_char_p, ctypes.c_uint32, ctypes.c_uint32, ctypes.c_uint32, ctypes.c_int]
    mb32 = 32 << 20
    assert f(IOMEM_480M, 0x1E000000, mb32, mb32, 0) == 0                      # 使える
    assert f(IOMEM_480M, 0x1E000000, mb32, 0, 0) == 1                         # 深いバッファの無い回路
    assert f(IOMEM_480M, 0x1F000000, 16 << 20, mb32, 0) < 0                   # 設定が PL の大きさより小さい
    assert f(IOMEM_480M, 0x1E000008, mb32, mb32, 0) < 0                       # 16 の倍数でない
    assert f(b"00000000-1bffffff : System RAM\n", 0x1F000000, mb32, mb32, 0) < 0   # DDR の外へはみ出す
    assert f(IOMEM_FULL, 0x1E000000, mb32, mb32, 0) < 0                       # Linux のメモリと重なる


def put(root, rel, data=b""):
    path = root / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)


def be(*cells):
    return b"".join(c.to_bytes(4, "big") for c in cells)


def reserved_tree(tmp_path, nodes, cells=(1, 1)):
    """/proc/device-tree/reserved-memory の写し。nodes = {名前: (reg のバイト列, 余分な特性のリスト)}"""
    root = tmp_path / "reserved-memory"
    put(root, "#address-cells", be(cells[0]))
    put(root, "#size-cells", be(cells[1]))
    put(root, "ranges")
    put(root, "name", b"reserved-memory\0")
    for name, (reg, extra) in nodes.items():
        put(root, f"{name}/reg", reg)
        put(root, f"{name}/name", name.encode() + b"\0")
        for prop in extra:
            put(root, f"{name}/{prop}")
    return str(root).encode()


MB32 = 32 << 20


def test_a_region_inside_one_reserved_memory_node_is_known_to_linux_as_not_in_use(lib, tmp_path):
    # リングを Linux のデバイスツリーの reserved-memory に置くと、カーネルは使わず（memblock の予約）、/dev/mem の割り当てが書き込み結合になり
    # DDR の読みが速い（実測 約 230 MB/s。Linux の管理外は約 125 MB/s）。Linux が使わないと言えるのは、1 つのノードの reg が領域の全体を含むとき
    f = lib.relay_region_reserved
    f.argtypes = [ctypes.c_char_p, ctypes.c_uint32, ctypes.c_uint32]
    ring = be(0x1E000000, MB32)
    d = reserved_tree(tmp_path, {"fprobe_ring@1e000000": (ring, [])})
    assert f(d, 0x1E000000, MB32) == 1
    assert f(d, 0x1E000000, 1 << 20) == 1                                  # 一部だけ使うのも、含まれていれば使える
    assert f(d, 0x1E100000, MB32) == 0                                     # 端が外にはみ出す
    assert f(d, 0x1D000000, MB32) == 0                                     # かからない
    assert f(d, 0, MB32) == 0 and f(d, 0x1E000000, 0) == 0                 # 0 番地・大きさ 0 は使えない


def test_reserved_memory_that_linux_may_still_touch_does_not_count(lib, tmp_path):
    f = lib.relay_region_reserved
    f.argtypes = [ctypes.c_char_p, ctypes.c_uint32, ctypes.c_uint32]
    ring = be(0x1E000000, MB32)
    for extra in (["no-map"], ["reusable"], ["compatible"]):                # no-map は写像が無い（/dev/mem が最遅）・reusable と共有プールは Linux が使う
        d = reserved_tree(tmp_path / extra[0], {"ring": (ring, extra)})
        assert f(d, 0x1E000000, MB32) == 0, extra
    # 2 つのノードにまたがる領域は認めない（間に使われている所があるかもしれない）
    d = reserved_tree(tmp_path / "two", {"a": (be(0x1E000000, MB32 // 2), []), "b": (be(0x1E000000 + MB32 // 2, MB32 // 2), [])})
    assert f(d, 0x1E000000, MB32) == 0
    # 1 つのノードの reg に複数の組があるとき、その 1 つが含めばよい
    d = reserved_tree(tmp_path / "multi", {"m": (be(0x10000000, 0x100000, 0x1E000000, MB32), [])})
    assert f(d, 0x1E000000, MB32) == 1


def test_reserved_memory_is_read_only_with_one_address_and_one_size_cell(lib, tmp_path):
    f = lib.relay_region_reserved
    f.argtypes = [ctypes.c_char_p, ctypes.c_uint32, ctypes.c_uint32]
    d = reserved_tree(tmp_path / "c2", {"r": (be(0, 0x1E000000, 0, MB32), [])}, cells=(2, 2))
    assert f(d, 0x1E000000, MB32) == 0                                     # 2 セル幅は未対応（読み違えない）
    assert f(str(tmp_path / "nothing").encode(), 0x1E000000, MB32) == 0    # ノードが無い（reserved-memory の無い構成）
    d = reserved_tree(tmp_path / "odd", {"r": (be(0x1E000000, MB32) + b"\0\0", [])})
    assert f(d, 0x1E000000, MB32) == 0                                     # reg の長さが組の倍数でない


def test_a_reserved_region_may_overlap_linux_ram_but_other_checks_still_apply(lib):
    f = lib.relay_deep_region_ok
    f.argtypes = [ctypes.c_char_p, ctypes.c_uint32, ctypes.c_uint32, ctypes.c_uint32, ctypes.c_int]
    assert f(IOMEM_FULL, 0x1E000000, MB32, MB32, 0) < 0                    # 予約が無ければ、Linux のメモリと重なって使えない（今まで通り）
    assert f(IOMEM_FULL, 0x1E000000, MB32, MB32, 1) == 0                   # reserved-memory に入っていれば使える
    assert f(IOMEM_FULL, 0x1E000008, MB32, MB32, 1) < 0                    # 予約済みでも 16 の倍数でなければ使えない
    assert f(IOMEM_FULL, 0x1F000000, 16 << 20, MB32, 1) < 0                # 設定が PL の大きさより小さい


PROC_INTERRUPTS = (b"           CPU0       CPU1       " + b"NEWLINE" + b" 40:   29733143          0 GIC-0  54 Level     eth0NEWLINE"
                   b" 41:         10          0 GIC-0  55 Level     eth0_wakeNEWLINE" b"IPI0:          0          0  CPU wakeup interruptsNEWLINE").replace(b"NEWLINE", b"\n")


def test_the_nic_interrupt_is_found_by_the_exact_name_at_the_end_of_its_line(lib):
    # 割り込みの CPU に載ったスレッドが TCP を送ると遅い（実測 PYNQ: 割り込みの CPU に送信が載ると約 36 MB/s、別の CPU なら約 48 MB/s）。
    # その CPU を /proc/interrupts から知る。名前の一部（eth0_wake）や別の行（IPI）は拾わない
    f = lib.relay_irq_of
    f.argtypes = [ctypes.c_char_p, ctypes.c_char_p]
    assert f(PROC_INTERRUPTS, b"eth0") == 40
    assert f(PROC_INTERRUPTS, b"eth0_wake") == 41
    assert f(PROC_INTERRUPTS, b"eth1") == -1
    assert f(PROC_INTERRUPTS, b"wake") == -1                               # 名前の末尾の一部は別の名前
    assert f(b"", b"eth0") == -1


def test_one_cpu_is_known_only_when_the_affinity_list_names_exactly_one(lib):
    f = lib.relay_single_cpu
    f.argtypes = [ctypes.c_char_p]
    assert [f(x) for x in (b"0\n", b"1\n", b"1", b"12\n")] == [0, 1, 1, 12]
    assert [f(x) for x in (b"0-1\n", b"0,1\n", b"\n", b"", b"x", b"0 1\n", b"-1\n", b"0-0\n")] == [-1] * 8


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
    r = subprocess.run([zig, "cc", "-target", "arm-linux-gnueabihf", "-mcpu=cortex_a9+neon", "-O2", "-Wall", "-Wextra", "-Werror", "-pthread",
                        "-o", str(tmp_path / "fprobe-relay"), str(SRC / "main.c"), str(SRC / "relay.c")],
                       capture_output=True, text=True)
    assert r.returncode == 0, r.stderr


# --- まとめ送り（STREAM などのバイト列のフレームを 1 つの UDP にまとめる）---------------------------------

BUNDLE = 0x40
CH_CONTROL, CH_STREAM = 0, 4


def bundle_request(n):
    return bytes([BUNDLE, 0]) + n.to_bytes(2, "little")


def to_pc_big(lib, pl, st, cap=4096):
    buf = ctypes.create_string_buffer(cap)
    n = lib.relay_to_pc(ctypes.byref(pl.ops), buf, len(buf), ctypes.byref(st))
    return buf.raw[:n]


def inner(ch, payload):
    return bytes([ch]) + len(payload).to_bytes(2, "little") + payload


def test_a_bundle_request_sets_the_limit_and_is_not_passed_to_the_pl(lib):
    pl, st = PlModel(), Stats()
    assert from_pc(lib, pl, bundle_request(1400), st) == 0
    assert st.bundle_max == 1400 and pl.frames == [] and st.to_pl == 0
    assert from_pc(lib, pl, bundle_request(0), st) == 0 and st.bundle_max == 0          # 0 = まとめない
    assert from_pc(lib, pl, bytes([BUNDLE, 0, 1]), st) < 0 and st.dropped_bad == 1      # 2 バイトの大きさが無い


def test_stream_frames_are_bundled_with_consecutive_seq(lib):
    pl, st = PlModel(), Stats()
    from_pc(lib, pl, bundle_request(1000), st)
    pl.tx = [bytes([CH_STREAM]) + bytes([i]) * 100 for i in range(3)]
    expect = bytes([BUNDLE, 0]) + b"".join(inner(CH_STREAM, bytes([i]) * 100) for i in range(3))
    assert to_pc_big(lib, pl, st) == expect
    assert st.seq == 3 and pl.done == 3 and st.from_pl == 3


def test_a_bundle_stops_at_the_size_limit(lib):
    pl, st = PlModel(), Stats()
    from_pc(lib, pl, bundle_request(1000), st)
    pl.tx = [bytes([CH_STREAM]) + bytes([i]) * 400 for i in range(3)]
    first = to_pc_big(lib, pl, st)
    assert first == bytes([BUNDLE, 0]) + inner(CH_STREAM, bytes([0]) * 400) + inner(CH_STREAM, bytes([1]) * 400)
    assert len(first) <= 1000 and pl.done == 2
    assert to_pc_big(lib, pl, st) == bytes([CH_STREAM, 2]) + bytes([2]) * 400          # 1 つだけなら、まとめずそのまま
    assert st.seq == 3 and pl.done == 3


def test_control_and_dap_frames_are_never_bundled_or_held_back(lib):
    pl, st = PlModel(), Stats()
    from_pc(lib, pl, bundle_request(1400), st)
    pl.tx = [bytes([CH_STREAM]) + b"a" * 10, bytes([CH_STREAM]) + b"b" * 10, bytes([CH_CONTROL]) + b"c" * 10]
    assert to_pc_big(lib, pl, st) == bytes([BUNDLE, 0]) + inner(CH_STREAM, b"a" * 10) + inner(CH_STREAM, b"b" * 10)
    assert pl.done == 2 and pl.tx[0][0] == CH_CONTROL                                  # CONTROL は読まずに次へ残す
    assert to_pc_big(lib, pl, st) == bytes([CH_CONTROL, 2]) + b"c" * 10
    pl.tx = [bytes([CH_CONTROL]) + b"x", bytes([CH_STREAM]) + b"y"]
    assert to_pc_big(lib, pl, st) == bytes([CH_CONTROL, 3]) + b"x"                      # 先頭が CONTROL なら、そのまま
    assert to_pc_big(lib, pl, st) == bytes([CH_STREAM, 4]) + b"y"


def test_a_frame_that_does_not_fit_alone_is_sent_plain(lib):
    pl, st = PlModel(), Stats()
    from_pc(lib, pl, bundle_request(100), st)
    pl.tx = [bytes([CH_STREAM]) + bytes(400)]
    assert to_pc_big(lib, pl, st) == bytes([CH_STREAM, 0]) + bytes(400)


def test_the_output_buffer_also_limits_the_bundle(lib):
    pl, st = PlModel(), Stats()
    from_pc(lib, pl, bundle_request(60000), st)
    pl.tx = [bytes([CH_STREAM]) + bytes([i]) * 100 for i in range(4)]
    out = to_pc_big(lib, pl, st, cap=250)                                              # 入れ物は 2 つ分（2 + 2 × 103）まで
    assert out == bytes([BUNDLE, 0]) + inner(CH_STREAM, bytes([0]) * 100) + inner(CH_STREAM, bytes([1]) * 100)
    assert pl.done == 2


def test_without_a_request_nothing_changes(lib):
    pl, st = PlModel(), Stats()
    pl.tx = [bytes([CH_STREAM]) + b"a" * 10, bytes([CH_STREAM]) + b"b" * 10]
    assert to_pc_big(lib, pl, st) == bytes([CH_STREAM, 0]) + b"a" * 10 and pl.done == 1


def test_the_bundle_waits_a_little_for_the_next_frame_the_pl_is_still_preparing(lib):
    pl, st = PlModel(), Stats()
    pl.tx_lag = 100                                   # TX_DONE の直後は 100 回の読みの間、次のフレームが見えない
    from_pc(lib, pl, bundle_request(1000), st)
    pl.tx = [bytes([CH_STREAM]) + bytes([i]) * 50 for i in range(3)]
    expect = bytes([BUNDLE, 0]) + b"".join(inner(CH_STREAM, bytes([i]) * 50) for i in range(3))
    assert to_pc_big(lib, pl, st) == expect and pl.done == 3


def test_the_wait_for_the_next_frame_is_bounded(lib):
    pl, st = PlModel(), Stats()
    pl.tx_lag = 100000                                # いつまでも次が出ない: 待ちを打ち切り、あるものだけを送る
    from_pc(lib, pl, bundle_request(1000), st)
    pl.tx = [bytes([CH_STREAM]) + b"a" * 50, bytes([CH_STREAM]) + b"b" * 50]
    assert to_pc_big(lib, pl, st) == bytes([CH_STREAM, 0]) + b"a" * 50 and pl.done == 1


def test_without_a_bundle_request_the_relay_never_waits_for_the_next_frame(lib):
    pl, st = PlModel(), Stats()
    pl.tx_lag = 100
    pl.tx = [bytes([CH_STREAM]) + b"a" * 50, bytes([CH_STREAM]) + b"b" * 50]
    assert to_pc_big(lib, pl, st) == bytes([CH_STREAM, 0]) + b"a" * 50
    assert pl._lag_left == 100                        # TX_HEAD を余計に読んでいない


# --- 読みと送信を分けるためのリング（1 つのスレッドが書き、もう 1 つが読む）----------------------------------


class Ring(ctypes.Structure):
    pass


@pytest.fixture(scope="module")
def ring_api(lib):
    lib.relay_ring_size.restype = ctypes.c_size_t
    lib.relay_ring_init.argtypes = [ctypes.c_void_p]
    lib.relay_ring_slot.argtypes = [ctypes.c_void_p]
    lib.relay_ring_slot.restype = ctypes.c_void_p
    lib.relay_ring_commit.argtypes = [ctypes.c_void_p, ctypes.c_size_t]
    lib.relay_ring_peek.argtypes = [ctypes.c_void_p, ctypes.POINTER(ctypes.c_size_t)]
    lib.relay_ring_peek.restype = ctypes.c_void_p
    lib.relay_ring_pop.argtypes = [ctypes.c_void_p]
    lib.relay_ring_slots.restype = ctypes.c_uint32
    lib.relay_ring_slot_bytes.restype = ctypes.c_size_t
    return lib


def new_ring(api):
    buf = ctypes.create_string_buffer(api.relay_ring_size())
    api.relay_ring_init(buf)
    return buf


def ring_push(api, ring, data):
    slot = api.relay_ring_slot(ring)
    if not slot:
        return False
    ctypes.memmove(slot, data, len(data))
    api.relay_ring_commit(ring, len(data))
    return True


def ring_pop(api, ring):
    n = ctypes.c_size_t()
    p = api.relay_ring_peek(ring, ctypes.byref(n))
    if not p:
        return None
    data = ctypes.string_at(p, n.value)
    api.relay_ring_pop(ring)
    return data


def test_an_empty_ring_has_nothing_to_read(ring_api):
    ring = new_ring(ring_api)
    assert ring_pop(ring_api, ring) is None


def test_the_ring_returns_items_in_order_and_reports_full(ring_api):
    ring = new_ring(ring_api)
    slots = ring_api.relay_ring_slots()
    for i in range(slots):
        assert ring_push(ring_api, ring, bytes([i]) * (i + 1))
    assert not ring_push(ring_api, ring, b"x")                       # 満杯: 書き込み用の番地を返さない
    assert ring_pop(ring_api, ring) == bytes([0])
    assert ring_push(ring_api, ring, b"new")                         # 1 つ空けば書ける
    for i in range(1, slots):
        assert ring_pop(ring_api, ring) == bytes([i]) * (i + 1)
    assert ring_pop(ring_api, ring) == b"new" and ring_pop(ring_api, ring) is None


class SlcrModel:
    """Zynq の SLCR の模型。ロック中は、ロックの鍵以外のレジスタへの書き込みが無視される（実機と同じ）。"""
    LOCK, UNLOCK, LOCKSTA = 0x004, 0x008, 0x00C

    def __init__(self, locked):
        self.locked, self.regs, self.log = locked, {}, []
        self.ops = PlOps(RD(self.rd), WR(self.wr), None)

    def rd(self, ctx, addr):
        return int(self.locked) if addr == self.LOCKSTA else self.regs.get(addr, 0)

    def wr(self, ctx, addr, value):
        self.log.append((addr, value))
        if addr == self.UNLOCK and value == 0xDF0D:
            self.locked = False
        elif addr == self.LOCK and value == 0x767B:
            self.locked = True
        elif not self.locked:
            self.regs[addr] = value


def slcr_write(lib, model, reg, value):
    lib.relay_slcr_write.argtypes = [ctypes.POINTER(PlOps), ctypes.c_uint32, ctypes.c_uint32]
    lib.relay_slcr_write(ctypes.byref(model.ops), reg, value)


def test_the_relay_service_runs_with_a_higher_priority_than_the_boot_time_python_services():
    # PYNQ の jupyter（起動の約 3 分間と、再起動のとき 1 コア以上）が中継の 2 スレッドと CPU を取り合い、25 MS/s が溢れた。
    # 中継の優先度を -20 に上げると、jupyter などの再起動と CPU 使い切りの中でも 40 秒・2 GB 欠けなし（-10 では溢れた。実測 2026-10-31）
    import pathlib
    unit = (pathlib.Path(__file__).parent.parent / "pynq" / "fprobe-relay" / "fprobe-relay.service").read_text(encoding="utf-8")
    nice = [line for line in unit.splitlines() if line.startswith("Nice=")]
    assert nice == ["Nice=-20"]


def test_the_relay_service_starts_after_the_boot_time_module_loading():
    # 中継は /dev/fprobe_ring（fprobe_ringmap）を起動のときだけ探す。起動時の読み込み（/etc/modules-load.d、
    # fprobe_gw pynq ringmap）の後に起動する（設計 docs/superpowers/specs/2026-11-01-bandwidth-observation-design.md 1.7.9）
    import pathlib
    unit = (pathlib.Path(__file__).parent.parent / "pynq" / "fprobe-relay" / "fprobe-relay.service").read_text(encoding="utf-8")
    after = [w for line in unit.splitlines() if line.startswith("After=") for w in line[len("After="):].split()]
    assert "systemd-modules-load.service" in after and "network.target" in after
    conf = (pathlib.Path(__file__).parent.parent / "pynq" / "fprobe-ringmap" / "fprobe-ringmap.conf").read_text(encoding="utf-8")
    assert [line for line in conf.splitlines() if line and not line.startswith("#")] == ["fprobe_ringmap"]


def test_slcr_write_keeps_an_unlocked_slcr_unlocked(lib):
    # カーネルは起動時に SLCR を解除したままにし、reboot の書き込み（PSS_RST_CTRL）は鍵を書かずに行う。ロックし直すと reboot が止まる
    m = SlcrModel(locked=False)
    slcr_write(lib, m, 0x900, 0xF)
    assert m.regs[0x900] == 0xF and not m.locked
    assert all(addr != SlcrModel.LOCK for addr, _ in m.log)


def test_slcr_write_unlocks_a_locked_slcr_for_the_write_and_locks_it_again(lib):
    m = SlcrModel(locked=True)
    slcr_write(lib, m, 0x900, 0xF)
    assert m.regs[0x900] == 0xF and m.locked
    assert [a for a, _ in m.log] == [SlcrModel.UNLOCK, 0x900, SlcrModel.LOCK]


def sysfs_set(lib, path, value, old_cap=32):
    lib.relay_sysfs_set.argtypes = [ctypes.c_char_p, ctypes.c_char_p, ctypes.c_char_p, ctypes.c_size_t]
    lib.relay_sysfs_set.restype = ctypes.c_int
    old = ctypes.create_string_buffer(old_cap)
    rc = lib.relay_sysfs_set(str(path).encode(), value.encode(), old, old_cap)
    return rc, old.value.decode()


def test_sysfs_set_returns_the_old_value_and_writes_the_new_one(lib, tmp_path):
    f = tmp_path / "napi_defer_hard_irqs"
    f.write_text("0\n")
    assert sysfs_set(lib, f, "8") == (0, "0")                    # 改行は取り除いて返す
    assert f.read_text().strip() == "8"
    assert sysfs_set(lib, f, "0") == (0, "8")                    # 返された値で元に戻せる
    assert f.read_text().strip() == "0"


def test_sysfs_set_on_a_missing_file_fails_and_writes_nothing(lib, tmp_path):
    missing = tmp_path / "no" / "such"
    assert sysfs_set(lib, missing, "8")[0] == -1
    assert not missing.exists()


def test_sysfs_set_does_not_write_when_the_old_value_does_not_fit(lib, tmp_path):
    f = tmp_path / "gro_flush_timeout"
    f.write_text("123456789\n")
    assert sysfs_set(lib, f, "1", old_cap=4)[0] == -1            # 戻せない書き換えはしない
    assert f.read_text().strip() == "123456789"


def ring_peek_many(api, ring, maximum):
    api.relay_ring_peek_many.argtypes = [ctypes.c_void_p, ctypes.POINTER(ctypes.c_void_p),
                                         ctypes.POINTER(ctypes.c_size_t), ctypes.c_int]
    api.relay_ring_peek_many.restype = ctypes.c_int
    base, length = (ctypes.c_void_p * maximum)(), (ctypes.c_size_t * maximum)()
    count = api.relay_ring_peek_many(ring, base, length, maximum)
    return [ctypes.string_at(base[i], length[i]) for i in range(count)]


def test_peek_many_lists_the_ready_slots_in_order_up_to_the_limit_without_taking_them(ring_api):
    ring = new_ring(ring_api)
    assert ring_peek_many(ring_api, ring, 4) == []
    for i in range(6):
        assert ring_push(ring_api, ring, bytes([i]) * (i + 1))
    assert ring_peek_many(ring_api, ring, 4) == [bytes([i]) * (i + 1) for i in range(4)]       # 上限まで
    assert ring_peek_many(ring_api, ring, 10) == [bytes([i]) * (i + 1) for i in range(6)]      # 取っていないので同じ先頭から、あるだけ
    assert ring_pop(ring_api, ring) == bytes([0])
    assert ring_peek_many(ring_api, ring, 2) == [bytes([1]) * 2, bytes([2]) * 3]


def test_a_slot_holds_the_largest_bundle(ring_api):
    ring = new_ring(ring_api)
    n = ring_api.relay_ring_slot_bytes()
    assert n >= 8192
    data = bytes(range(256)) * (n // 256)
    assert ring_push(ring_api, ring, data) and ring_pop(ring_api, ring) == data


def test_a_writer_thread_and_a_reader_thread_lose_and_reorder_nothing(ring_api):
    import threading
    ring = new_ring(ring_api)
    total = 20000
    items = [(i.to_bytes(4, "little") * (1 + i % 37)) for i in range(total)]
    got = []

    def writer():
        for it in items:
            while not ring_push(ring_api, ring, it):
                pass

    t = threading.Thread(target=writer)
    t.start()
    while len(got) < total:
        d = ring_pop(ring_api, ring)
        if d is not None:
            got.append(d)
    t.join()
    assert got == items


def test_the_nic_settings_are_saved_before_tuning_and_loaded_back_after_a_crash(lib, tmp_path):
    # 中継が戻さずに終わっても（SIGKILL など）、次の起動で控えから元の値に戻す（A5）
    lib.relay_nic_save.argtypes = [ctypes.c_char_p, ctypes.c_char_p, ctypes.c_char_p]
    lib.relay_nic_load.argtypes = [ctypes.c_char_p, ctypes.c_char_p, ctypes.c_size_t, ctypes.c_char_p, ctypes.c_size_t]
    saved = tmp_path / "nic-saved"
    defer, flush = ctypes.create_string_buffer(32), ctypes.create_string_buffer(32)
    assert lib.relay_nic_load(str(saved).encode(), defer, 32, flush, 32) == -1          # 控えが無い
    assert lib.relay_nic_save(str(saved).encode(), b"0", b"20000") == 0
    assert saved.read_bytes() == b"0\n20000\n" and not (tmp_path / "nic-saved.tmp").exists()
    assert lib.relay_nic_load(str(saved).encode(), defer, 32, flush, 32) == 0
    assert (defer.value, flush.value) == (b"0", b"20000")
    assert lib.relay_nic_load(str(saved).encode(), defer, 32, flush, 3) == -1            # 入りきらない値は使わない
    saved.write_bytes(b"0\n")
    assert lib.relay_nic_load(str(saved).encode(), defer, 32, flush, 32) == -1           # 欠けた控えは使わない
    assert lib.relay_nic_save(str(tmp_path / "no" / "dir").encode(), b"0", b"1") == -1
