"""試験の模型: 2 スレッドの競合のアプリ（tests/data/linux_halt/race.c の ARM の PIE）を、模擬のコア（SimCores）と合成のカーネルの
メモリの上に「止まる直前の姿」で置く。実機ではない。

    プロセス 100（comm "race"）: スレッド 100 と 101。ELF を読み込み位置 BIAS（0x00400000）に写す（模擬のページ表）。
    スレッド 101: コア 1 のユーザモード、add_one の race.c:11（counter = after;）。before = 41, after = 42。
    スレッド 100: コア 3 のユーザモード、add_one の race.c:10（after = before + 1;）。before = 41（同じ値を読んだ: 足し損ねの直前）。
    コア 0・2: 対象ではない（別のプロセスのカーネルスタック）。大域変数 counter = 41。

コアの状態（pc・レジスタ）は最初から「当たった後」の位置に置いておき、`hit()` がコア 1 の BKPT の当たり（自分で止まった）を
次の wait_event に積む。"""
from __future__ import annotations

import struct
import time
from pathlib import Path

from fprobe.linux_halt.core_access import CoreState, MOE_HALT
from fprobe.linux_halt.kernel import Kernel
from fprobe.linux_halt.service import HaltSession
from fprobe.symbols import Symbols
from .helpers import map_page
from .test_engine import EXTRA, MM, PGD_VA, SIG, STACK
from .test_kernel import OFF, kernel_with, layout
from .test_service import FakeCores, options

ELF = Path(__file__).resolve().parents[1] / "data" / "linux_halt" / "race_arm"
BIAS = 0x00400000
PID, T_LEADER, T_OTHER = 100, 100, 101
USER = 0x10
SP = {T_OTHER: 0xBEFFE800, T_LEADER: 0xB6FFE800}
IMAGE_PAGES = {0x400000: 0x40000, 0x410000: 0x41000, 0x420000: 0x42000, 0x430000: 0x43000}
STACK_PAGES = {0xBEFFE000: 0x50000, 0xB6FFE000: 0x51000}
AT_PHDR, AT_PHNUM, AT_ENTRY = 3, 5, 9


def _paced(cores) -> None:
    """実機（ArmACores）の wait_event と同じく、当たりが無ければ timeout_s 待ってから返す（待つ上限を実時間で試すため）。"""
    original = cores.wait_event

    def wait_event(timeout_s):
        if not cores.events:
            time.sleep(timeout_s)
            cores.empty_waits = 0              # 待った回の数えは時間の試験では意味がない（上限は時間で決まる）
        return original(timeout_s)
    cores.wait_event = wait_event


class RaceWorld:
    def __init__(self, tmp_path, lock=None, cores_cls=None, counter=41):
        _, m = kernel_with([(PID, [T_LEADER, T_OTHER], "race"), (200, [200], "kworker")])
        k = Kernel(m.read, layout(**EXTRA), max_tasks=100, max_threads=16)
        for t in k.threads(k.find_leader(PID).addr):
            m.w32(t.addr + OFF["mm"], MM)
            m.w32(t.addr + OFF["stack"], STACK[t.pid])
            m.w32(t.addr + OFF["signal"], SIG)
        m.w32(k.find_leader(200).addr + OFF["mm"], 0)
        m.w32(MM + EXTRA["pgd"], PGD_VA)
        m.w32(SIG + EXTRA["live"], 2)
        self.m = m
        self.symbols = Symbols(ELF)
        self.cores = (cores_cls or FakeCores)(m.read)
        _paced(self.cores)
        for va, pa in {**IMAGE_PAGES, **STACK_PAGES}.items():
            map_page(self.cores, va, pa)
        for seg in self.symbols.segments:                       # ELF の中身を読み込み位置に置く
            self.write_user(seg.address + BIAS, seg.data)
        e_entry, phdr = self.symbols.entry, 0x34
        auxv = struct.pack("<6I", AT_PHDR, phdr + BIAS, AT_PHNUM, 9, AT_ENTRY, e_entry + BIAS) + struct.pack("<II", 0, 0)
        m.b[MM + EXTRA["saved_auxv"] - 0xC0000000:MM + EXTRA["saved_auxv"] - 0xC0000000 + len(auxv)] = auxv
        self.write_user(self.addr("counter"), struct.pack("<I", counter))
        # スレッド 101: add_one の race.c:11。sp+0 after, sp+4 before, sp+8 who（DWARF の位置: sp 相対）
        self.write_user(SP[T_OTHER], struct.pack("<III", 42, 41, 2))
        # スレッド 100: add_one の race.c:10。after はまだ書いていない（0）
        self.write_user(SP[T_LEADER], struct.pack("<III", 0, 41, 1))
        for tid in (T_LEADER, T_OTHER):
            worker_sp = SP[tid] + 12                             # 呼び出し元 worker のフレーム（sp+4 i, sp+8 who）と、
            self.write_user(worker_sp, struct.pack("<IIIIII", 0, 7, 1 if tid == T_LEADER else 2, 0,
                                                   0, 0))       # その上の push {r11, lr}（lr = 0: main の先は辿らない）
        self.place(1, T_OTHER, "race.c:11")
        self.place(3, T_LEADER, "race.c:10")
        for core in (0, 2):                                      # 対象ではないコア（kworker のカーネルスタック）
            self.cores.states[core] = CoreState(False, pc=0xC0008000, cpsr=0x13, moe=MOE_HALT, sp_svc=0xC0001F00)
        self.closed = 0
        self.opts = options(tmp_path)
        self.s = HaltSession(self.opts, layout(**EXTRA), self.cores, self._close, (RuntimeError,),
                             {"backend": "fprobe", "n_cores": 4}, lock)

    def _close(self):
        self.closed += 1

    # --- 模型の操作 --------------------------------------------------------------------------------------------
    def addr(self, location: str) -> int:
        """ELF の場所（変数名・関数名・ファイル:行）の実行時の番地。"""
        return self.symbols.resolve(location) + BIAS

    def pa(self, va: int) -> int:
        pages = {**IMAGE_PAGES, **STACK_PAGES}
        return pages[va & ~0xFFF] | (va & 0xFFF)

    def write_user(self, va: int, data: bytes) -> None:
        while data:
            n = min(len(data), 0x1000 - (va & 0xFFF))
            self.cores.mem[self.pa(va):self.pa(va) + n] = data[:n]
            va, data = va + n, data[n:]

    def read_user(self, va: int, n: int) -> bytes:
        return bytes(self.cores.mem[self.pa(va):self.pa(va) + n])

    def unmap(self, va: int) -> None:
        """va のページの写像を消す（模擬のページ表の 2 段目の項を 0 に）。"""
        from .helpers import PGD
        l2 = self.cores._l2[(PGD, va >> 20)]
        self.cores.write_phys(l2 + 4 * ((va >> 12) & 0xFF), b"\0" * 4)

    def place(self, core: int, tid: int, location: str) -> None:
        """コア core がスレッド tid を、ユーザモードの location で走らせている（止まったときに見える姿）。"""
        pc = self.addr(location)
        sp = SP[tid]
        self.cores.states[core] = CoreState(False, pc=pc, cpsr=USER, moe=MOE_HALT, sp_svc=STACK[tid] + 8000)
        regs = [0] * 17
        regs[11], regs[13], regs[14], regs[15], regs[16] = sp + 12 + 16, sp, 0x10224 + BIAS, pc, USER
        self.cores.reg_lists[core] = regs

    def hit(self, core: int = 1) -> None:
        """次の wait_event で、コア core が BKPT で自分で止まる。"""
        self.cores.queue_event(core)
