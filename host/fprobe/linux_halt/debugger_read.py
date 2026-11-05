"""`HaltDebugger` の部品: 止めたまま調べる・書く（`threads`・`read`・`memory`・`write_register`・`write_expression`）。

スレッドのレジスタ・DWARF の変数と式・プロセスのメモリを事実の dict で返す。
"""
from __future__ import annotations

from ..dwarf import AccessError
from ..dwarf.location import NotAvailable
from .debugger_common import MASK, REG_NAMES
from .engine import EngineError
from .mmu import Unmapped


class DebuggerReadMixin:
    """読み書き（`HaltDebugger` の状態を使う）。"""

    def threads(self, variables: bool, expressions: list[str], max_elements: int) -> dict:
        def read():
            rows = []
            on_core = {s.tid: (n, s) for n, s in sorted(self.engine.core.items()) if s.halted}
            for t in self.proc.threads:
                core = on_core.get(t.pid)
                row = {"tid": t.pid, "comm": t.comm, "on_core": core[0] if core else None,
                       "mode": ("user" if core[1].user else "kernel") if core else None,
                       "registers_from": "core" if self.engine.live(t.pid) is not None else "pt_regs"}
                try:
                    regs = self.engine.get_regs(t.pid)
                except (KeyError, EngineError, OSError) as e:
                    row.update(registers=None, pc=None, function=None, file=None, line=None,
                               error="%s: %s" % (type(e).__name__, e))
                    rows.append(row)
                    continue
                row["registers"] = dict(zip(REG_NAMES, regs))
                row["pc"] = regs[15]
                row.update(self._where(regs[15]))
                if variables or expressions:
                    insp = self._inspector(regs)
                    if variables:
                        if insp is None:
                            row.update(arguments=None, locals=None, variables_error="the ELF has no DWARF")
                        else:
                            try:
                                fv = insp.frame_variables(0, max_elements)
                                row.update(arguments=fv["arguments"], locals=fv["locals"])
                            except (IndexError, NotAvailable, AccessError) as e:
                                row.update(arguments=None, locals=None,
                                           variables_error="%s: %s" % (type(e).__name__, e))
                    row["expressions"] = [self._evaluate(insp, x, 0, max_elements) for x in expressions]
                rows.append(row)
            out = {"pid": self.proc.tgid, "threads": rows, "stop": (self.last_stop or {}).get("stop")
                   or self.attach_stop, "load_bias": self.load_bias}
            self.last_threads = out
            return out
        return self._call(read, halted=True)

    def read(self, expressions: list[str], tid: int, frame: int, max_elements: int) -> dict:
        def go():
            self._require_tid(tid)
            regs = self.engine.get_regs(tid)
            insp = self._inspector(regs)
            where = None
            if insp is not None:
                try:
                    state = insp.frame(frame)
                    where = {"index": frame, "pc": state.pc, **insp.where(state)}
                except IndexError as e:
                    raise EngineError("frame %d of tid %d: %s" % (frame, tid, e)) from None
            return {"tid": tid, "frame": where,
                    "values": [self._evaluate(insp, x, frame, max_elements) for x in expressions]}
        return self._call(go, halted=True)

    def memory(self, address: int, length: int) -> dict:
        def go():
            try:
                data = self.proc.mem.read(address, length)
            except Unmapped as e:
                return {"address": address, "length": length, "data": None, "reason": str(e)}
            data = self.engine.bps.shadow(self.proc.tgid, address, data)    # 置いた BKPT ではなく元の命令を見せる
            return {"address": address, "length": length, "data": data.hex(), "reason": None}
        return self._call(go, halted="auto")                 # 走行中なら、全コアを短く止めて読み、また走らせる

    def write_register(self, tid: int, name: str, value: int) -> None:
        """スレッド tid のレジスタ（r0〜r12・sp・lr・pc・cpsr）に書く（コアにいればコア、いなければ pt_regs）。"""
        def go():
            self._require_tid(tid)
            key = {"r13": "sp", "r14": "lr", "r15": "pc"}.get(name.lower(), name.lower())
            if key not in REG_NAMES:
                raise EngineError("register %s is not one of %s" % (name, REG_NAMES))
            regs = list(self.engine.get_regs(tid))
            regs[REG_NAMES.index(key)] = value & MASK
            self.engine.set_regs(tid, regs)
        self._call(go, halted=True)

    def write_expression(self, tid: int, expression: str, value, frame: int) -> None:
        """スレッド tid のフレーム frame で、C の式の指す値（メモリ上）に書く（プロセスの仮想メモリ）。"""
        def go():
            self._require_tid(tid)
            insp = self._inspector(self.engine.get_regs(tid))
            if insp is None:
                raise EngineError("the ELF has no DWARF (no types and variables)")
            insp.write(expression, value, frame)
        self._call(go, halted=True)
