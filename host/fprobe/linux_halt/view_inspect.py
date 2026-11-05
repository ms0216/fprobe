"""Linux のプロセスの全コア停止デバッグの見せ方（LinuxDebugView に混ぜ込む部品）: レジスタ・呼び出し履歴・変数・式・
シンボル・逆アセンブル・メモリの表示と、止まっているときの書き込み。

self.d（HaltDebugger）・self.sources などの状態は view.py の LinuxDebugView が持つ。応答は事実だけ。
"""
from __future__ import annotations

import dataclasses

from ..disasm import SYNC_LIMIT, Disassembler, Instruction
from ..dwarf import Inspector
from ..dwarf.inspector import check_expression
from ..memview import MemoryReadError, diff, format_hexdump, hexdump
from ..symbols import Location, is_line_location
from .debugger import REG_NAMES, HaltDebugger, _ThreadAccess
from .mmu import Unmapped

__all__ = ["ViewInspectMixin"]


class ViewInspectMixin:
    """マイコンのデバッガと同じ API の形の、レジスタ・呼び出し履歴・変数・式・メモリ・逆アセンブル・書き込み。"""

    # ------------------------------------------------------------------ レジスタ・呼び出し履歴・変数
    def _regs(self) -> list[int]:
        return self.d.engine.get_regs(self.d.current_tid())

    def _inspector(self, max_frames: int = 64, assume_leaf: bool = False) -> Inspector:
        if self.d.info is None:
            raise RuntimeError("ELF に DWARF（-g）が無い: %s" % self.d.image.path)
        return Inspector(self.d.info, _ThreadAccess(self._regs(), self.d.proc.mem), symbols=self.d.symbols,
                         max_frames=max_frames, assume_leaf=assume_leaf, load_bias=self.d.load_bias)

    def read_registers(self, names: list[str] | None = None) -> dict[str, int]:
        def go():
            regs = dict(zip(REG_NAMES, self._regs()))
            if not names:
                return regs
            alias = {"r13": "sp", "r14": "lr", "r15": "pc"}
            missing = [n for n in names if alias.get(n.lower(), n.lower()) not in regs]
            if missing:
                raise KeyError(f"Linux のユーザのスレッドでは読まないレジスタ: {missing}（読めるのは {REG_NAMES}）")
            return {n: regs[alias.get(n.lower(), n.lower())] for n in names}
        return self.d.use(go, halted=True)

    def _in_library(self, pc: int) -> tuple[str | None, int | None, str | None]:
        """pc が共有ライブラリの中なら (関数名, 先頭からのバイト数, 表示の文字)。関数の名前が引けなくても、どのライブラリの
        中かは分かる（ライブラリ名!ライブラリの中の番地）。ライブラリの外なら (None, None, None)。"""
        lib = self.d.lib_location(pc)
        if lib is not None:
            return lib.function, lib.offset, f"{lib.function}+{lib.offset} ({lib.module})"
        module = self.d.module_of(pc)
        if module is not None:
            return None, None, f"{module[0]}!0x{module[1]:08X}"
        return None, None, None

    def backtrace(self, variables: bool = False, max_frames: int = 64, max_elements: int = 64,
                  assume_leaf: bool = False) -> list[dict]:
        def go():
            insp = self._inspector(max_frames, assume_leaf)
            out = []
            for entry in insp.backtrace(variables, max_elements):
                pc = entry["pc"]
                where = (str(Location(pc, entry["function"], entry["offset"], entry["file"], entry["line"]))
                         if entry["function"] is not None or entry["file"] else f"0x{pc:08X}")
                if entry["function"] is None and not entry["file"]:       # 共有ライブラリの中: 名前（引ければ）とライブラリ名
                    function, offset, text = self._in_library(pc)
                    if text is not None:
                        entry = {**entry, "function": function, "offset": offset}
                        where = text
                out.append({"index": entry["index"], "pc": pc, "where": where, "note": entry["note"], "cfa": entry["cfa"],
                            "function": entry["function"], "offset": entry["offset"], "file": entry["file"],
                            "line": entry["line"],
                            **{k: entry[k] for k in ("stop", "arguments", "locals") if k in entry}})
            return out
        return self.d.use(go, halted=True)

    def frame_variables(self, frame: int = 0, max_elements: int = 64) -> dict:
        return self.d.use(lambda: self._inspector().frame_variables(frame, max_elements), halted=True)

    def evaluate(self, expression: str, frame: int = 0, max_elements: int = 64, live: bool = False) -> dict:
        if live:
            raise RuntimeError("Linux のプロセスは走らせたまま読めない（live）: 止めて読む")
        return self.d.use(lambda: self._inspector().read(expression, frame, max_elements), halted=True)

    def expression_check(self, expression: str) -> dict:
        """ELF の DWARF だけで調べる（番地は実行時 = リンク時 + load bias）。"""
        if self.d.info is None:
            raise RuntimeError("ELF に DWARF（-g）が無い: 式は調べられない")
        return check_expression(self.d.info, expression, load_bias=self.d.load_bias)

    def type_info(self, name: str) -> dict:
        if self.d.info is None:
            raise RuntimeError("ELF に DWARF（-g）が無い: 型は調べられない")
        return Inspector(self.d.info, None, symbols=self.d.symbols, load_bias=self.d.load_bias).type_info(name)

    def symbol_search(self, query: str, kind: str | None, limit: int) -> dict:
        """記号を名前の部分一致で探す（Symbols.search）。番地は実行時の番地（リンク時 + load bias。symbol_info と同じ）。"""
        found = self.d.symbols.search(query, kind, limit)
        bias = self.d.load_bias
        for sym in found["symbols"]:
            sym["address"] = (sym["address"] + bias) & 0xFFFFFFFF
        return found

    def symbol_info(self, location: str) -> dict:
        """場所 ↔ 実行時の番地（リンク時 + load bias）。関数名は前置きの後ではなく関数の先頭（マイコンと同じ）。"""
        symbols, bias = self.d.symbols, self.d.load_bias
        text = location.strip()
        if text.lower().startswith("0x"):
            address = int(text, 16)
        else:
            address = (symbols.resolve(text) + bias) & 0xFFFFFFFF
        loc = symbols.describe((address - bias) & 0xFFFFFFFF)
        out = {"address": address, "where": str(Location(address, loc.function, loc.offset, loc.file, loc.line))
               if loc.function is not None or loc.file else f"0x{address:08X}",
               "function": loc.function, "offset": loc.offset, "file": loc.file, "line": loc.line}
        if not text.lower().startswith("0x") and not is_line_location(text):
            sym = symbols.symbol(text)
            out.update(kind=sym.kind, size=sym.size)
        return out

    def disasm_around(self, address: int | None = None, before: int = 8, after: int = 8, from_elf: bool = False,
                      branch_tables: list[dict] | None = None, sync_limit: int = SYNC_LIMIT,
                      data_ranges: list[dict] | None = None) -> dict:
        """プロセスの仮想メモリ（置いた BKPT は元の命令で）を、ELF の $a / $t / $d で区切って逆アセンブルする。番地は
        実行時（リンク時の番地で解いて load bias を足す）。命令の中の名前（target_name・literal_name）はリンク時の番地で
        引く。from_elf・branch_tables・data_ranges は使わない（渡したら断る）。"""
        if from_elf or branch_tables or data_ranges:
            raise ValueError("Linux のプロセスの逆アセンブルでは from_elf・branch_tables・data_ranges は使わない")

        def go():
            pc = self._regs()[15] if address is None else address
            bias = self.d.load_bias
            memory = _LinkMemory(self.d, bias)
            dis = Disassembler(memory, self.d.symbols)
            listing = dis.around((pc - bias) & 0xFFFFFFFF, before, after, sync_limit)
            # in_flash: Linux のプロセスの番地はマイコンの Flash の領域ではない（target.flash の領域が無い）
            items = [{**_shift(i, bias).as_dict(), "in_flash": False} for i in listing.instructions]
            skipped = listing.sync_skipped
            if skipped is not None:
                skipped = {**skipped, "from": (skipped["from"] + bias) & 0xFFFFFFFF}
            return {"pc": pc, "memory": "target", "sync": listing.sync, "sync_skipped": skipped, "total": len(items),
                    "offset": 0, "pc_index": listing.pc_index, "instructions": items}
        return self.d.use(go, halted=True)

    def memory_view(self, address: int, length: int, unit: int = 8, width: int = 16, compare: bool = False,
                    from_elf: bool = False) -> dict:
        """プロセスの仮想メモリの 16 進表示（マイコンの target.memory.view と同じ形。4096 バイトまで）。全コアが止まって
        いるときだけ読む。置いた BKPT は元の命令で見せる（linux.halt.debug.memory と同じ読み）。compare なら前回の同じ範囲との
        差（変わった単位に印）。番地にシンボル（ELF。リンク時の番地 + load bias）の名前を付ける。"""
        from ..probe_inspect import VIEW_HISTORY, VIEW_LIMIT
        if from_elf:
            raise ValueError("Linux のプロセスのメモリ表示では from_elf は使わない（ELF の中身は debug.elf の道）")
        if not 0 < length <= VIEW_LIMIT:
            raise ValueError(f"length は 1〜{VIEW_LIMIT}")
        if not 0 <= address < 1 << 32:
            raise ValueError(f"番地 0x{address:X} は 32 ビットの番地の空間の外")
        length = min(length, (1 << 32) - address)          # 番地の空間の終わりで止める（折り返して 0 番地を読まない）
        read = self.d.memory(address, length)
        if read["data"] is None:                           # 未マップ: 読めた事実だけを返す（理由は読みの結果のまま）
            raise MemoryReadError(f"0x{address:08X} から {length} バイトは読めない: {read['reason']}")
        data = bytes.fromhex(read["data"])
        key = (address, length)
        stored = self._view_previous.pop(key, None)
        previous = stored if compare else None
        self._view_previous[key] = data                    # 新しいものを後ろへ。古い範囲から忘れる
        while len(self._view_previous) > VIEW_HISTORY:
            del self._view_previous[next(iter(self._view_previous))]
        rows = hexdump(data, address, width=width, unit=unit, previous=previous)
        out = {"memory": "target", "address": address, "length": length, "unit": unit,
               "rows": [{"address": r.address, "values": list(r.values), "text": r.text, "changed": list(r.changed)}
                        for r in rows],
               "dump": format_hexdump(rows, unit), "names": self._names_in(address, length)}
        if compare:
            out["compared"] = previous is not None
            out["changes"] = [{"address": c.address, "before": c.before.hex(), "after": c.after.hex()}
                              for c in (diff(previous, data, address) if previous is not None else [])]
        return out

    def _names_in(self, address: int, length: int) -> list[dict]:
        """範囲の中で始まる ELF のシンボル（実行時の番地 = リンク時の番地 + load bias）。"""
        bias = self.d.load_bias
        found = [{"address": (s.address + bias) & 0xFFFFFFFF, "name": s.name, "kind": "symbol"}
                 for s in self.d.symbols.names()]
        return sorted((n for n in found if address <= n["address"] < address + length), key=lambda n: n["address"])

    # ------------------------------------------------------------------ 書く
    def write_register(self, name: str, value: int) -> None:
        self.d.write_register(self.d.current_tid(), name, value)

    def write_expression(self, expression: str, value, frame: int = 0) -> dict:
        tid = self.d.current_tid()
        self.d.write_expression(tid, expression, value, frame)
        return {**self.evaluate(expression, frame), "via": "halted"}           # 止まっているときだけ書く

    def watch_write(self, expression: str, value) -> dict:
        raise RuntimeError("Linux のプロセスは走らせたまま書けない: 止めて debug.write_expression で書く")

    def watchpoint_set(self, location: str, kind: str = "write", size: int | None = None) -> dict:
        raise RuntimeError("Linux のプロセスのウォッチポイントは画面の道に無い（linux.halt.gdb.serve の gdb の watch）")

    def watchpoint_clear(self, location: str) -> dict:
        raise RuntimeError("Linux のプロセスのウォッチポイントは画面の道に無い")



class _LinkMemory:
    """逆アセンブルの読み手: リンク時の番地 → プロセスの仮想メモリ（実行時 = + load bias）。置いた BKPT は元の命令で。"""

    def __init__(self, debugger: HaltDebugger, bias: int):
        self.d, self.bias = debugger, bias

    def read_memory(self, address: int, length: int) -> bytes:
        va = (address + self.bias) & 0xFFFFFFFF
        try:
            data = self.d.proc.mem.read(va, length)
        except Unmapped as e:
            raise MemoryReadError(str(e)) from None
        return self.d.engine.bps.shadow(self.d.proc.tgid, va, data)


def _shift(i: Instruction, bias: int) -> Instruction:
    """リンク時の番地の命令 → 実行時の番地（番地・分岐の先・PC 相対で読む番地に load bias を足す）。"""
    def add(v):
        return None if v is None else (v + bias) & 0xFFFFFFFF
    return dataclasses.replace(i, address=add(i.address), target=add(i.target), literal=add(i.literal))
