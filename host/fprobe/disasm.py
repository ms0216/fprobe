"""Thumb / Thumb-2（Cortex-M）の逆アセンブル。ソースとの混在表示・関数単位・PC の前後。

メモリは抽象の読み手（fprobe.memview.MemoryReader）で読む。ELF（ElfImage）でも実機（TargetMemory）でもよい。
ELF があれば、マッピングシンボル（$t / $d）でリテラルプールをデータとして扱い、関数名・変数名・ファイル:行を付ける。

    dis = Disassembler.from_elf("app.elf")                         # 中身も ELF から
    dis = Disassembler.from_elf("app.elf", memory=TargetMemory(t))  # 中身は実機、名前・行・$t/$d は ELF
    dis.function("main")                                           # Listing（命令の列）
    dis.around(pc, before=8, after=8)                              # PC の前後（前は関数の先頭から揃える）
    print(dis.function("main").text(source=lambda file, line: ...))   # ソースとの混在表示

仕様: docs/superpowers/specs/2026-10-17-f2-disasm-svd-design.md
"""

from collections.abc import Callable
from dataclasses import asdict, dataclass
from pathlib import Path

import capstone
from capstone import arm

from .memview import CodeMap, ElfImage, MemoryReader, MemoryReadError
from .symbols import Symbols

__all__ = ["Disassembler", "Instruction", "Listing", "flow_of"]

_SYNC_LIMIT = 64 * 1024          # 確かな点から PC まで、これより遠ければ前を読まない（読む量の上限。PC から読む）
_LITERAL_SIZES = {"ldrb": 1, "ldrsb": 1, "ldrh": 2, "ldrsh": 2, "ldrd": 8}


@dataclass(frozen=True)
class Instruction:
    """1 命令（kind="code"）か、命令の間のデータ（kind="data": リテラルプール・表・解読できないバイト）。"""
    address: int
    data: bytes
    mnemonic: str                        # "push" / "b.w" / ".word"
    operands: str
    kind: str = "code"
    target: int | None = None            # 分岐・呼び出しの先
    target_name: str | None = None       # 先の名前（"tick" / "reset_handler+60"）
    literal: int | None = None           # PC 相対で読む番地（ldr rX, [pc, #n]）・adr の番地
    literal_value: int | None = None     # その番地の値（ldr）。データなら自身の値
    literal_name: str | None = None      # 値を番地とみたときの名前（変数・関数）
    flow: str = ""                       # "call" / "jump" / "return" / ""（流れを変えない）
    conditional: bool = False            # 条件付き（b<cc>・cbz・IT ブロックの中）
    function: str | None = None
    offset: int | None = None
    file: str | None = None
    line: int | None = None
    new_line: bool = False               # 直前の命令とファイル:行が変わった（混在表示でソースの行を挟む所）

    @property
    def size(self) -> int:
        return len(self.data)

    def as_dict(self) -> dict:
        d = asdict(self)
        d["bytes"] = d.pop("data").hex()
        return d

    def comment(self) -> str:
        if self.literal_value is not None and self.kind == "code":
            name = f" <{self.literal_name}>" if self.literal_name else ""
            return f"=0x{self.literal_value:08x}{name}"
        if self.kind == "data" and self.literal_name:
            return f"<{self.literal_name}>"
        if self.target_name:
            return f"<{self.target_name}>"
        if self.literal is not None:
            return f"0x{self.literal:08x}"
        return ""

    def __str__(self) -> str:
        comment = self.comment()
        text = f"{self.address:08x}: {self.data.hex():<8}  {self.mnemonic:<7} {self.operands}".rstrip()
        return f"{text}  ; {comment}" if comment else text


@dataclass(frozen=True)
class Listing:
    instructions: tuple[Instruction, ...]
    sync: str                            # "function" / "mapping" / "start": どこから命令の境界を揃えたか
    pc_index: int | None = None          # around の PC の命令の位置

    def as_dict(self) -> dict:
        return {"sync": self.sync, "pc_index": self.pc_index,
                "instructions": [i.as_dict() for i in self.instructions]}

    def text(self, *, source: Callable[[str, int], str | None] | None = None) -> str:
        """混在表示: 関数の始まりに "名前:"、行が変わる所に "ファイル:行  ソースの行"、その下に命令。"""
        out = []
        for n, i in enumerate(self.instructions):
            if i.function is not None and (i.offset == 0 or n == 0):
                out.append(f"{i.function}:" if i.offset == 0 else f"{i.function}+{i.offset}:")
            if i.new_line:
                code = source(i.file, i.line) if source else None
                out.append(f"  {i.file}:{i.line}" + (f"  {code.strip()}" if code else ""))
            marker = "=> " if n == self.pc_index else "   " if self.pc_index is not None else ""
            out.append(f"{marker}{i}")
        return "\n".join(out)


def flow_of(insn) -> tuple[str, bool]:
    """capstone の命令（detail 付き）の流れ: ("call" / "jump" / "return" / "", 条件付きか)。PC を書く命令
    （pop {pc}・ldr pc・mov pc 等）も分岐とみなす。命令トレースの再構成（fprobe.trace.reconstruct）も使う。"""
    groups = set(insn.groups)
    conditional = (insn.cc not in (arm.ARM_CC_AL, arm.ARM_CC_INVALID)
                   or insn.mnemonic.startswith(("cbz", "cbnz")))
    if capstone.CS_GRP_CALL in groups:
        return "call", conditional
    regs = [o.reg for o in insn.operands if o.type == arm.ARM_OP_REG]
    base = insn.mnemonic.split(".")[0]
    if base.startswith("pop") or base.startswith("ldm"):
        if arm.ARM_REG_PC in regs:
            return "return", conditional
        return "", conditional
    if base.startswith("bx") and regs == [arm.ARM_REG_LR]:
        return "return", conditional
    if capstone.CS_GRP_JUMP in groups:
        return "jump", conditional
    if regs and regs[0] == arm.ARM_REG_PC and base.startswith(("mov", "ldr", "add")):
        return "jump", conditional
    return "", conditional


class Disassembler:
    def __init__(self, memory: MemoryReader, symbols: Symbols | None = None, image: ElfImage | None = None):
        """memory: 中身の読み手。symbols: 関数名・ファイル:行。image: マッピングシンボル（$t/$d）と変数名
        （省略時、symbols があれば同じ ELF から読む）。どちらも無ければ番地だけで読み、リテラルは推定する。"""
        self.memory = memory
        self.symbols = symbols
        self.image = image if image is not None or symbols is None else ElfImage(symbols.path)
        self.code_map: CodeMap | None = self.image.code_map if self.image is not None else None
        self._thumb = capstone.Cs(capstone.CS_ARCH_ARM, capstone.CS_MODE_THUMB | capstone.CS_MODE_MCLASS)
        self._arm = capstone.Cs(capstone.CS_ARCH_ARM, capstone.CS_MODE_ARM)
        for cs in (self._thumb, self._arm):
            cs.detail = True

    @classmethod
    def from_elf(cls, path: str | Path, *, memory: MemoryReader | None = None) -> "Disassembler":
        image = ElfImage(path)
        return cls(memory if memory is not None else image, Symbols(path), image)

    # --- 入口 ---------------------------------------------------------------------------

    def range(self, start: int, end: int) -> Listing:
        """[start, end) を読む。start は命令の境界とみなす。"""
        if end <= start:
            raise ValueError(f"範囲が空: 0x{start:08X}〜0x{end:08X}")
        data = self.memory.read_memory(start, end - start)
        data += self._read_upto(end, 2)                          # 最後の 32 ビット命令が end をまたぐ分
        return Listing(tuple(self._relink(self._decode(start, data, end))), "start")

    def function(self, location: str | int) -> Listing:
        """関数の全体（関数名か、関数の中の番地）。"""
        start, size = self.function_bounds(location)
        if size <= 0:
            raise KeyError(f"{location!r} の大きさが分からない（range で範囲を指定する）")
        data = self.memory.read_memory(start, size)
        return Listing(tuple(self._relink(self._decode(start, data, start + size))), "function")

    def around(self, pc: int, before: int = 8, after: int = 8) -> Listing:
        """PC の前 before 命令・PC・後ろ after 命令。前は確かな境界（関数の先頭・$t）から PC まで読んで揃える。"""
        start, sync = self._sync_start(pc, before)
        tail = self._read_upto(pc, 4 * (after + 1) + 2)
        if not tail:
            raise MemoryReadError(f"0x{pc:08X} が読めない")
        head = self.memory.read_memory(start, pc - start) if start < pc else b""
        # 確かな点から PC の後ろまで続けて読む（IT ブロックの条件は続けて読むときだけ分かる）
        decoded = self._decode(start, head + tail, pc + len(tail))
        at = next((n for n, i in enumerate(decoded) if i.address == pc), None)
        if at is None:                                           # PC がデータの区間の中など: PC から読む
            start, sync, at = pc, "start", 0
            decoded = self._decode(pc, tail, pc + len(tail))
        prior = decoded[:at][-before:] if before > 0 else []
        return Listing(tuple(self._relink(prior + decoded[at:at + after + 1])), sync, len(prior))

    # --- 揃え ---------------------------------------------------------------------------

    def _sync_start(self, pc: int, count: int) -> tuple[int, str]:
        """PC の前を読み始める確かな命令の境界と、その種類（before = 0 でも、IT ブロックの条件のために揃える）。"""
        anchors = []
        function = self._function_at(pc)
        if function is not None:
            anchors.append((function[0], "function"))
        if self.code_map is not None and (start := self.code_map.last_code_start(pc)) is not None:
            anchors.append((start, "mapping"))
        if anchors:
            start, sync = max(anchors)
            if start == pc or (pc - start <= _SYNC_LIMIT and self._decode_to(start, pc) is not None):
                return start, sync
        return pc, "start"                     # 確かな境界が無い: 前の命令の境界を推定しない

    def _decode_to(self, start: int, pc: int, strict: bool = False) -> list[Instruction] | None:
        """start から読んで pc にちょうど着地すれば、その命令の列。strict なら解読できないバイトがあれば失敗。"""
        try:
            data = self.memory.read_memory(start, pc - start)
        except MemoryReadError:
            return None
        decoded = self._decode(start, data, pc)
        if not decoded or decoded[-1].address + decoded[-1].size != pc:
            return None
        if strict and any(i.kind == "data" for i in decoded):
            return None
        return decoded

    def _read_upto(self, address: int, length: int) -> bytes:
        """address から最大 length バイト（読める分だけ。区画の終わりなど）。"""
        low, high, best = 0, length, b""
        while low <= high:
            n = (low + high) // 2
            try:
                best, low = (self.memory.read_memory(address, n) if n else b""), n + 1
            except MemoryReadError:
                high = n - 1
        return best

    # --- 名前 ---------------------------------------------------------------------------

    def function_bounds(self, location: str | int) -> tuple[int, int]:
        """関数（名前か、関数の中の番地）の (先頭の番地, 大きさ)。"""
        if isinstance(location, str):
            if self.symbols is not None:
                address = self.symbols.resolve(location)
            elif self.image is not None:
                address = self.image.symbol(location).address
            else:
                raise KeyError(f"シンボルが無いので {location!r} を引けない")
        else:
            address = location
        found = self._function_at(address)
        if found is None:
            raise KeyError(f"0x{address:08X} を含む関数が無い")
        return found

    def _function_at(self, address: int) -> tuple[int, int] | None:
        """(先頭, 大きさ)。"""
        if self.symbols is not None:
            where = self.symbols.describe(address)
            if where.function is not None:
                return address - where.offset, self.symbols.symbol(where.function).size
        if self.image is not None:
            s = self.image.symbol_at(address, "function")
            if s is not None:
                return s.address, s.size
        return None

    def _name(self, address: int) -> str | None:
        if self.image is not None and (name := self.image.name_of(address)) is not None:
            return name
        if self.symbols is not None:
            where = self.symbols.describe(address)
            if where.function is not None:
                return where.function if where.offset == 0 else f"{where.function}+{where.offset}"
        return None

    def _where(self, address: int) -> tuple[str | None, int | None, str | None, int | None]:
        if self.symbols is not None:
            w = self.symbols.describe(address)
            return w.function, w.offset, w.file, w.line
        if self.image is not None and (s := self.image.symbol_at(address, "function")) is not None:
            return s.name, address - s.address, None, None
        return None, None, None, None

    # --- 解読 ---------------------------------------------------------------------------

    def _kind(self, address: int) -> str:
        if self.code_map is None:
            return "t"
        return self.code_map.kind_at(address) or "t"

    def _decode(self, start: int, data: bytes, end: int) -> list[Instruction]:
        """data（start から）を end まで読む。data は end より長くてよい（end をまたぐ最後の命令の分）。"""
        out: list[Instruction] = []
        literals: dict[int, tuple[int, int]] = {}                 # 推定したデータ: 始まり → (終わり, 単位のバイト)
        stop = start + len(data)
        address = start
        while address < end and address < stop:
            kind = self._kind(address)
            data_end, unit = self._data_end(address, kind, literals)
            if data_end is not None:
                out.append(self._data(address, data[address - start:min(data_end, stop) - start], unit))
                address += out[-1].size
                continue
            # コードの区間をまとめて capstone に渡す（IT ブロックの条件は続けて読むときだけ分かる）
            run_end = stop
            if self.code_map is not None and self.code_map.kind_at(address) is not None:
                run_end = min(stop, self.code_map.region_end(address))
            cs = self._arm if kind == "a" else self._thumb
            progressed = False
            for insn in cs.disasm(data[address - start:run_end - start], address):
                if insn.address >= end or self._overlaps(insn.address, insn.size, literals):
                    break
                out.append(self._instruction(insn, out, literals))
                address = insn.address + insn.size
                progressed = True
            if not progressed and address < end:                  # 解読できない・データに掛かる: 2 バイトのデータ
                out.append(self._data(address, data[address - start:min(address + 2, stop) - start]))
                address += out[-1].size
        return out

    @staticmethod
    def _overlaps(address: int, size: int, literals: dict[int, tuple[int, int]]) -> bool:
        return any(begin < address + size and address < finish for begin, (finish, _) in literals.items())

    def _data_end(self, address: int, kind: str, literals: dict[int, tuple[int, int]]) -> tuple[int | None, int]:
        """address がデータなら (その区間の終わり, 単位のバイト)。コードなら (None, 0)。"""
        if kind == "d":
            return self.code_map.region_end(address), 4
        for begin, (finish, unit) in literals.items():
            if begin <= address < finish:
                return finish, unit
        return None, 0

    def _data(self, address: int, chunk: bytes, unit: int = 4) -> Instruction:
        """データの 1 単位（unit 以下で、揃っていて入るなら .word、次に .short、.byte）。"""
        if unit >= 4 and address % 4 == 0 and len(chunk) >= 4:
            size, mnemonic = 4, ".word"
        elif unit >= 2 and address % 2 == 0 and len(chunk) >= 2:
            size, mnemonic = 2, ".short"
        else:
            size, mnemonic = 1, ".byte"
        raw = chunk[:size]
        value = int.from_bytes(raw, "little")
        name = self.image.name_of(value) if self.image is not None and size == 4 else None
        return Instruction(address, raw, mnemonic, f"0x{value:0{2 * size}x}", "data", literal_value=value,
                           literal_name=name)

    def _instruction(self, insn, previous: list[Instruction], literals: dict[int, tuple[int, int]]) -> Instruction:
        address = insn.address
        target = literal = value = name = None
        flow, conditional = self._flow(insn)
        if flow in ("call", "jump"):
            imms = [o.imm for o in insn.operands if o.type == arm.ARM_OP_IMM]   # bx / blx <reg> には無い
            if imms:
                target = imms[-1] & 0xFFFFFFFF
        for op in insn.operands:
            if op.type == arm.ARM_OP_MEM and op.mem.base == arm.ARM_REG_PC:
                if op.mem.index == 0 and insn.mnemonic.startswith(("ldr", "vldr")):
                    literal = ((address + 4) & ~3) + op.mem.disp
                    size = self._literal_size(insn)
                    literals[literal] = (literal + size, 4)
                    value = self._read_value(literal, min(size, 4))
                elif op.mem.index != 0 and insn.mnemonic in ("tbb", "tbh"):
                    entries = self._table_entries(op.mem.index, previous)
                    if entries:
                        width = 1 if insn.mnemonic == "tbb" else 2
                        begin = address + 4
                        literals[begin] = (begin + (entries * width + 1) // 2 * 2, width)
        if insn.mnemonic.startswith("adr") and insn.operands and insn.operands[-1].type == arm.ARM_OP_IMM:
            literal = ((address + 4) & ~3) + insn.operands[-1].imm
        if value is not None and self.image is not None:
            name = self.image.name_of(value)
        return Instruction(address, bytes(insn.bytes), insn.mnemonic, insn.op_str, "code", target,
                           self._name(target) if target is not None else None, literal, value, name,
                           flow, conditional)

    _flow = staticmethod(flow_of)

    @staticmethod
    def _literal_size(insn) -> int:
        base = insn.mnemonic.split(".")[0]
        if base.startswith("vldr"):
            regs = [o.reg for o in insn.operands if o.type == arm.ARM_OP_REG]
            return 8 if regs and arm.ARM_REG_D0 <= regs[0] <= arm.ARM_REG_D31 else 4
        for prefix, size in _LITERAL_SIZES.items():
            if base.startswith(prefix):
                return size
        return 4

    def _table_entries(self, index_reg: int, previous: list[Instruction]) -> int | None:
        """tbb/tbh の表の項目数: 直前（数命令）の "cmp 添字, #n" から n + 1。"""
        name = self._thumb.reg_name(index_reg)
        for i in reversed(previous[-4:]):
            if i.mnemonic.startswith("cmp"):
                reg, _, imm = i.operands.partition(", #")
                if reg == name and imm:
                    return int(imm, 0) + 1
        return None

    def _read_value(self, address: int, size: int) -> int | None:
        try:
            return int.from_bytes(self.memory.read_memory(address, size), "little")
        except MemoryReadError:
            return None

    def _relink(self, items: list[Instruction]) -> list[Instruction]:
        """ファイル:行・関数を付け、行が変わる所に new_line を立てる（列の中で前の命令と比べる）。"""
        out, previous = [], None
        for i in items:
            function, offset, file, line = self._where(i.address)
            here = (file, line)
            new_line = file is not None and here != previous
            previous = here
            out.append(Instruction(**{**asdict(i), "function": function, "offset": offset, "file": file,
                                      "line": line, "new_line": new_line}))
        return out
