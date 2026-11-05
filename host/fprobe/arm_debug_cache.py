"""ArmDebugCore のキャッシュ操作（I キャッシュ・分岐予測の無効化、書いた範囲の D キャッシュ掃き出し）の部品（U32）。

ArmDebugCore（fprobe.arm_debug）に mixin される。
"""

from .arm_debug_defs import (
    CP15, DSB, MODE_MASK, MODE_SYS, MODE_USR, _from_dtr
)


class _CacheMixin:
    """キャッシュの操作。"""

    def invalidate_instruction_caches(self) -> None:
        """このコアだけの I キャッシュと分岐予測を無効にする（書いたコードを、このコアの命令フェッチから見えるようにする）。
        1 つの列（無効にする操作は何度しても同じ）。"""
        self._require_halted()
        self._dirty.add("r0")
        ops = [("put", 0, _from_dtr(0)), *([("exec", DSB)] if self.v7 else []), ("exec", CP15["iciallu"]),
               ("exec", CP15["bpiall"]), *([("exec", DSB)] if self.v7 else [])]
        cpsr = self._cache["cpsr"]
        if cpsr & MODE_MASK == MODE_USR:
            ops = self._mode_steps((cpsr & ~MODE_MASK) | MODE_SYS) + ops + self._mode_steps(cpsr)
        self._run(ops)

    def line_size(self) -> int:
        if self._line is None:
            self._set_line(self._read_cp15("ctr"))
        return self._line

    def _set_line(self, ctr: int) -> None:
        self._line = 4 << ((ctr >> 16) & 0xF) if ctr >> 29 == 0b100 else 1 << (((ctr >> 12) & 3) + 3)

    def _sync_caches(self, address: int, length: int) -> None:
        """書いた範囲: D キャッシュを PoU まで掃き出し、I キャッシュ・分岐予測を無効にする（ARMv7 で MP 拡張が
        あれば内側の共有域の全コアに）。1 つの列（各行の番地を r0 に入れ直して掃き出す。掃き出し・無効化は何度しても同じ）。"""
        self._dirty.add("r0")
        if self.v7 and self._line is None and self._mp is None:   # 初めて: CTR と MPIDR を 1 つの列で（r0 を毎回入れ直す）
            ctr, mpidr = self._run(self._cp15_steps("ctr") + self._cp15_steps("mpidr"))
            self._set_line(ctr)
            self._mp = bool(mpidr >> 31)
        line = self.line_size()
        if self.v7 and self._mp is None:
            self._mp = bool(self._read_cp15("mpidr") >> 31)
        clean = CP15["dccmvau"] if self.v7 else CP15["v6_clean_dline"]
        steps = []
        for at in range(address & ~(line - 1), address + length, line):
            steps += [("put", at, _from_dtr(0)), ("exec", clean)]
        steps.append(("put", 0, _from_dtr(0)))
        if self.v7:
            steps += [("exec", DSB), ("exec", CP15["iciallis" if self._mp else "iciallu"]),
                      ("exec", CP15["bpiallis" if self._mp else "bpiall"]), ("exec", DSB)]
        else:
            steps += [("exec", CP15["v6_dsb"]), ("exec", CP15["iciallu"]), ("exec", CP15["bpiall"])]
        self._run(steps)
