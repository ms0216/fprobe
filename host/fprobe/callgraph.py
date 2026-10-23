"""逆アセンブル（fprobe.disasm）からの静的な解析: 関数ごとのスタックの枠（SP の動き）と呼び出しの場所。

関数の中を制御の流れに沿ってたどり（条件分岐は両方、tbb / tbh は表の先、IT の中の条件付きの戻りは続きも）、
各命令の時点の SP の深さ（関数の入口からのバイト数）を数える。枠 = たどった中の最大の深さ。呼び出しの場所には
その時点の深さを付ける（呼び先の深さに足すと、その経路のスタックになる）。

SP を変える命令: push / pop・vpush / vpop・stmdb / ldm sp!・sub / add sp, #即値・str / ldr の [sp, #-n]! / [sp], #n・
mov sp, rX（rX が SP からの既知の値 = フレームポインタからの戻しなら既知の深さ、分からなければ可変長の確保）。
分からない SP の変化（sub sp, rX 等の可変長の確保）は dynamic（上限不明）。

    graph = CallGraph.build(Disassembler.from_elf("app.elf"))
    graph.functions["main"].frame            # 枠のバイト数
    graph.functions["main"].calls            # [CallSite(番地, 呼び先, call / tail / indirect / indirect_tail, 深さ)]
    graph.callees("main") / graph.callers("leaf") / graph.address_taken

仕様: docs/superpowers/specs/2026-10-18-u18-u26-profile-export-stack-design.md
"""

import re
from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass, field

from .disasm import Disassembler, Instruction
from .memview import MemoryReadError

__all__ = ["CallGraph", "CallSite", "FunctionFlow", "analyze_function", "vector_table"]

_CONDITIONS = ("eq", "ne", "cs", "hs", "cc", "lo", "mi", "pl", "vs", "vc", "hi", "ls", "ge", "lt", "gt", "le", "al")
_KNOWN = {"push", "pop", "vpush", "vpop", "stmdb", "stmfd", "ldm", "ldmia", "ldmfd", "sub", "subw", "add", "addw",
          "mov", "str", "strb", "strh", "strd", "ldr", "ldrb", "ldrh", "ldrd", "vstr", "vldr", "vstmdb", "vldmia",
          "stm", "stmia", "bic", "and", "orr", "eor", "mvn", "lsl", "lsr", "asr", "cmp", "cmn", "tst", "teq", "b",
          "bl", "blx", "bx", "cbz", "cbnz", "it", "tbb", "tbh", "nop", "ldrsb", "ldrsh"}
# 最初の運用数を書かない命令（比較・格納・分岐など）: 追っているレジスタの値を消さない
_NO_WRITE = ("str", "vstr", "stm", "vstm", "cmp", "cmn", "tst", "teq", "b", "cb", "it", "nop", "push", "vpush",
             "dmb", "dsb", "isb", "bkpt", "svc", "cps", "msr", "wfi", "wfe", "sev", "vcmp", "pld", "tbb", "tbh")
_IMM = re.compile(r"#(-?(?:0x[0-9a-fA-F]+|\d+))")
_PRE_DEC = re.compile(r"\[sp, #(-(?:0x[0-9a-fA-F]+|\d+))\]!")
_POST_INC = re.compile(r"\[sp\], #((?:0x[0-9a-fA-F]+|\d+))")
_REVISITS = 8                       # 同じ場所をより深い SP で訪ねなおす回数の上限（解析の打ち切り。判定ではない）


@dataclass(frozen=True)
class CallSite:
    address: int
    callee: str | None              # 呼び先の関数（関数ポインタ・ELF の外は None）
    kind: str                       # call / tail（末尾の分岐）/ indirect（blx rX）/ indirect_tail（bx rX 等）
    depth: int                      # その時点の SP の深さ（関数の入口から）
    target: int | None = None       # 分岐の先の番地（分かるとき）


@dataclass
class FunctionFlow:
    name: str
    address: int
    size: int
    frame: int = 0                  # 最大の深さ（バイト）
    calls: list[CallSite] = field(default_factory=list)
    dynamic: bool = False           # 可変長の確保など、深さに上限が無い
    uncertain: bool = False         # たどれない所があった（表の分かれ先が分からない等）。枠は多めに見積もった
    uses_fpu: bool = False          # 浮動小数点の命令がある（例外の枠が大きくなる）
    reasons: list[str] = field(default_factory=list)

    def as_dict(self) -> dict:
        return {"function": self.name, "address": self.address, "size": self.size, "frame": self.frame,
                "dynamic": self.dynamic, "uncertain": self.uncertain, "reasons": list(self.reasons),
                "calls": [{"address": c.address, "callee": c.callee, "kind": c.kind, "depth": c.depth}
                          for c in self.calls]}


# --- 命令の読み ---------------------------------------------------------------------------------

def _op(mnemonic: str) -> str:
    """"push.w" → push、"popne" → pop、"adds" → add、"subseq" → sub。"""
    base = mnemonic.split(".")[0].lower()
    for candidate in (base, base[:-2] if base[-2:] in _CONDITIONS else None):
        if candidate is None:
            continue
        if candidate in _KNOWN:
            return candidate
        if candidate.endswith("s") and candidate[:-1] in _KNOWN:
            return candidate[:-1]
    return base


def _operands(text: str) -> list[str]:
    """括弧 {} [] の中のカンマで分けない。"""
    out, depth, current = [], 0, ""
    for ch in text:
        if ch in "{[":
            depth += 1
        elif ch in "}]":
            depth -= 1
        if ch == "," and depth == 0:
            out.append(current.strip())
            current = ""
        else:
            current += ch
    if current.strip():
        out.append(current.strip())
    return out


def _imm(text: str) -> int | None:
    m = _IMM.fullmatch(text.strip()) or None
    return int(m.group(1), 0) if m else None


def _register_list_bytes(text: str) -> int:
    """"{r4, r5, lr}" / "{d8-d15}" / "{s16, s17}" のバイト数。"""
    inner = text[text.find("{") + 1:text.rfind("}")]
    total = 0
    for part in (p.strip() for p in inner.split(",") if p.strip()):
        first, _, last = part.partition("-")
        unit = 8 if first.startswith("d") else 4
        count = 1
        if last:
            count = int(last.strip()[1:]) - int(first[1:]) + 1
        total += unit * count
    return total


@dataclass
class _State:
    depth: int
    regs: dict[str, int]            # レジスタ → 入口の SP からの深さ（rX = 入口の SP − 深さ）


def _step(insn: Instruction, state: _State, flow: FunctionFlow) -> _State:
    """1 命令の SP・追っているレジスタへの効果。"""
    op = _op(insn.mnemonic)
    ops = _operands(insn.operands)
    depth, regs = state.depth, dict(state.regs)
    text = insn.operands.replace(" ", "")
    if insn.mnemonic.startswith("v"):
        flow.uses_fpu = True
    if op in ("push", "vpush") or (op in ("stmdb", "stmfd", "vstmdb") and text.startswith("sp!")):
        return _State(depth + _register_list_bytes(insn.operands), regs)
    if op in ("pop", "vpop") or (op in ("ldm", "ldmia", "ldmfd", "vldmia") and text.startswith("sp!")):
        return _State(depth - _register_list_bytes(insn.operands), regs)
    if (m := _PRE_DEC.search(insn.operands)) is not None:
        return _State(depth - int(m.group(1), 0), regs)
    if (m := _POST_INC.search(insn.operands)) is not None:
        return _State(depth - int(m.group(1), 0), _forget(regs, ops))
    if op in ("sub", "subw", "add", "addw") and ops:
        sign = 1 if op.startswith("sub") else -1
        dest = ops[0]
        source, value = (ops[0], ops[1]) if len(ops) == 2 else (ops[1], ops[2]) if len(ops) == 3 else (None, None)
        amount = _imm(value) if value is not None else None
        if dest == "sp":
            if source == "sp" and amount is not None:
                return _State(depth + sign * amount, regs)
            if sign > 0:                                           # sub sp, sp, rX: 可変長の確保
                return _dynamic(flow, insn, state)
            return _State(depth, regs)                             # add sp, rX: 戻し（多めに見積もる）
        if amount is not None and source == "sp":
            regs[dest] = depth + sign * amount
            return _State(depth, regs)
        if amount is not None and source in regs:
            regs[dest] = regs[source] + sign * amount
            return _State(depth, regs)
        regs.pop(dest, None)
        return _State(depth, regs)
    if op == "mov" and len(ops) == 2:
        dest, source = ops
        if dest == "sp":
            if source in regs:
                return _State(regs[source], regs)
            return _dynamic(flow, insn, state)
        if source == "sp":
            regs[dest] = depth
        elif source in regs:
            regs[dest] = regs[source]
        else:
            regs.pop(dest, None)
        return _State(depth, regs)
    if ops and ops[0] == "sp" and not op.startswith(_NO_WRITE):
        return _dynamic(flow, insn, state)                         # 分からない SP の書き換え（bic sp 等）
    if ops and not op.startswith(_NO_WRITE):
        regs.pop(ops[0], None)
    return _State(depth, _forget(regs, ops))


def _forget(regs: dict[str, int], ops: list[str]) -> dict[str, int]:
    """書き戻し（[rX, #n]!・ldm rX!）で変わるレジスタを忘れる。"""
    for o in ops:
        if o.endswith("!"):
            base = o.strip("[]!").split(",")[0].strip()
            regs.pop(base, None)
    return regs


def _dynamic(flow: FunctionFlow, insn: Instruction, state: _State) -> _State:
    if not flow.dynamic:
        flow.dynamic = True
        flow.reasons.append(f"0x{insn.address:08X} {insn.mnemonic} {insn.operands}: SP を可変の量だけ動かす"
                            "（alloca・可変長配列など）")
    return _State(state.depth, state.regs)


def _is_return(insn: Instruction) -> bool:
    if insn.flow == "return":
        return True
    return _op(insn.mnemonic) == "ldr" and insn.operands.replace(" ", "").startswith("pc,[sp],")


def _table_targets(code: Sequence[Instruction], index: int) -> list[int] | None:
    """tbb / tbh の表（直後のデータ）の分かれ先。表が読めなければ None。"""
    insn = code[index]
    entries = []
    for item in code[index + 1:]:
        if item.kind != "data" or item.literal_value is None:
            break
        entries.append(item.literal_value)                         # 逆アセンブラは表の幅（1 / 2 バイト）で区切る
    if not entries:
        return None
    return [insn.address + 4 + 2 * e for e in entries]


# --- 関数 ---------------------------------------------------------------------------------------

def analyze_function(code: Sequence[Instruction], name: str, start: int, end: int,
                     resolve: Callable[[int], str | None]) -> FunctionFlow:
    """関数 [start, end) の命令の列を入口からたどる。resolve: 番地 → それを含む関数の名前（関数の外への分岐の先）。"""
    flow = FunctionFlow(name, start, end - start)
    at = {insn.address: n for n, insn in enumerate(code)}
    seen: dict[int, int] = {}                          # 命令の位置 → 訪ねた最大の深さ
    revisits: dict[int, int] = {}
    sites: dict[int, CallSite] = {}
    unknown_jump = False
    work = [(0, _State(0, {}))] if code else []
    while work:
        n, state = work.pop()
        while 0 <= n < len(code):
            if n in seen and state.depth <= seen[n]:
                break
            if n in seen:
                revisits[n] = revisits.get(n, 0) + 1
                if revisits[n] > _REVISITS:
                    if not flow.dynamic:
                        flow.dynamic = True
                        flow.reasons.append(f"0x{code[n].address:08X}: より深い SP で {_REVISITS} 回を超えて訪ねなおしたので"
                                            "解析を打ち切った（深さはここまでの下限）")
                    break
            seen[n] = state.depth
            insn = code[n]
            if insn.kind != "code":
                break
            before = state
            state = _step(insn, state, flow)
            flow.frame = max(flow.frame, before.depth, state.depth)
            if _is_return(insn):
                if insn.conditional:
                    n, state = n + 1, before
                    continue
                break
            if insn.flow == "call":
                callee = resolve(insn.target) if insn.target is not None else None
                kind = "call" if insn.target is not None else "indirect"
                _site(sites, CallSite(insn.address, callee, kind, before.depth, insn.target))
                n += 1
                continue
            if insn.flow == "jump":
                if insn.target is None:
                    if _op(insn.mnemonic) in ("tbb", "tbh"):
                        targets = _table_targets(code, n)
                        if targets is None:
                            unknown_jump = True
                            break
                        work += [(at[t], state) for t in targets if t in at]
                        break
                    _site(sites, CallSite(insn.address, None, "indirect_tail", state.depth))
                    if insn.conditional:
                        n += 1
                        continue
                    break
                if start <= insn.target < end:
                    if insn.target in at:
                        if insn.conditional:
                            work.append((at[insn.target], state))
                            n += 1
                            continue
                        n = at[insn.target]
                        continue
                    unknown_jump = True                    # 命令の境界でない所への分岐
                    break
                _site(sites, CallSite(insn.address, resolve(insn.target), "tail", state.depth, insn.target))
                if insn.conditional:
                    n += 1
                    continue
                break
            n += 1
    if unknown_jump:
        _unreached(code, seen, flow, sites, resolve)
    flow.calls = sorted(sites.values(), key=lambda c: c.address)
    return flow


def _site(sites: dict[int, CallSite], site: CallSite) -> None:
    old = sites.get(site.address)
    if old is None or site.depth > old.depth:
        sites[site.address] = site


def _unreached(code: Sequence[Instruction], seen: dict[int, int], flow: FunctionFlow,
               sites: dict[int, CallSite], resolve: Callable[[int], str | None]) -> None:
    """たどれなかった命令: SP を下げる量を全部足して枠を多めにし、呼び出しは枠の深さで加える。"""
    flow.uncertain = True
    flow.reasons.append("分岐の先が分からない所がある（表の分岐など）: たどれない命令の分を多めに足した")
    extra = 0
    probe = FunctionFlow(flow.name, flow.address, flow.size)
    for n, insn in enumerate(code):
        if n in seen or insn.kind != "code":
            continue
        after = _step(insn, _State(0, {}), probe)
        extra += max(after.depth, 0)
    flow.frame += extra
    if probe.dynamic:
        flow.dynamic = True
        flow.reasons += probe.reasons
    for n, insn in enumerate(code):
        if n in seen or insn.kind != "code" or insn.flow != "call":
            continue
        if insn.target is None:
            _site(sites, CallSite(insn.address, None, "indirect", flow.frame))
        else:
            _site(sites, CallSite(insn.address, resolve(insn.target), "call", flow.frame, insn.target))


# --- 全体 ---------------------------------------------------------------------------------------

def vector_table(image) -> tuple[int, int] | None:
    """ELF のベクタ表の (番地, 大きさ)。名前（g_pfnVectors 等）か、最も低い区画の先頭の変数。"""
    from .elfsize import VECTOR_SYMBOLS
    for name in VECTOR_SYMBOLS:
        try:
            s = image.symbol(name)
        except KeyError:
            continue
        return s.address, s.size or 64
    if image.regions:
        first = image.regions[0].address
        s = image.symbol_at(first, "object")
        if s is not None and s.address == first and s.size:
            return s.address, s.size
    return None


@dataclass
class CallGraph:
    functions: dict[str, FunctionFlow]
    address_taken: set[str]                       # 番地がデータ・リテラルにある関数（関数ポインタの呼び先の候補）
    vectors: dict[int, str] = field(default_factory=dict)    # 例外番号 → 処理の関数（ベクタ表から）
    failed: dict[str, str] = field(default_factory=dict)     # 読めなかった関数 → 理由

    @classmethod
    def build(cls, disassembler: Disassembler, names: Iterable[str] | None = None) -> "CallGraph":
        """ELF の全関数（names で絞れる）を解析する。disassembler は ELF のシンボル付き（Disassembler.from_elf）。"""
        from .profile.coverage import elf_functions
        symbols = disassembler.symbols
        if symbols is None:
            raise ValueError("呼び出しグラフには ELF のシンボルが要る（Disassembler.from_elf）")
        names = list(names) if names is not None else elf_functions(symbols.path)
        starts = {}
        for name in names:
            try:
                starts[name] = symbols.symbol(name).address
            except KeyError:
                continue
        by_start = {address: name for name, address in starts.items()}

        def resolve(address: int) -> str | None:
            if address in by_start:
                return by_start[address]
            where = symbols.describe(address)
            return where.function

        functions, failed, literals = {}, {}, set()
        for name, start in starts.items():
            size = symbols.symbol(name).size
            try:
                listing = disassembler.function(name)
            except (KeyError, MemoryReadError, ValueError) as e:
                failed[name] = str(e)
                continue
            code = listing.instructions
            functions[name] = analyze_function(code, name, start, start + size, resolve)
            literals |= _code_constants(code)
        image = disassembler.image
        table = vector_table(image) if image is not None else None
        vectors = {}
        if table is not None:
            address, size = table
            try:
                raw = image.read_memory(address, size)
            except MemoryReadError:
                raw = b""
            for n in range(1, len(raw) // 4):
                word = int.from_bytes(raw[4 * n:4 * n + 4], "little")
                if word & 1 and (word & ~1) in by_start:
                    vectors[n] = by_start[word & ~1]
        taken = {by_start[v & ~1] for v in literals | _data_words(image, table) if v & 1 and (v & ~1) in by_start}
        return cls(functions, taken, vectors, failed)

    def callees(self, name: str) -> list[str]:
        out = []
        for c in self.functions[name].calls:
            if c.callee is not None and c.callee not in out:
                out.append(c.callee)
        return out

    def callers(self, name: str) -> list[str]:
        return [f.name for f in sorted(self.functions.values(), key=lambda f: f.address)
                if any(c.callee == name for c in f.calls)]

    def uses_fpu(self) -> bool:
        return any(f.uses_fpu for f in self.functions.values())


def _code_constants(code: Sequence[Instruction]) -> set[int]:
    """命令が読む定数（リテラルプールの値、movw / movt の組）。"""
    out = set()
    low: dict[str, int] = {}
    for insn in code:
        if insn.kind != "code":
            continue
        if insn.literal_value is not None:
            out.add(insn.literal_value)
        op = insn.mnemonic.split(".")[0]
        ops = _operands(insn.operands)
        if op == "movw" and len(ops) == 2 and (value := _imm(ops[1])) is not None:
            low[ops[0]] = value
        elif op == "movt" and len(ops) == 2 and ops[0] in low and (value := _imm(ops[1])) is not None:
            out.add(value << 16 | low.pop(ops[0]))
    return out


def _data_words(image, table: tuple[int, int] | None) -> set[int]:
    """データの区画（とコードの中のデータの印 $d の所）の 4 バイトの語。ベクタ表は除く。"""
    if image is None:
        return set()
    out = set()
    code_map = image.code_map
    for region in image.regions:
        for offset in range(-region.address % 4, len(region.data) - 3, 4):
            address = region.address + offset
            if table is not None and table[0] <= address < table[0] + table[1]:
                continue
            if code_map and code_map.kind_at(address) in ("t", "a"):
                continue
            out.add(int.from_bytes(region.data[offset:offset + 4], "little"))
    return out
