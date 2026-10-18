"""止まったターゲットをソースの言葉で調べる窓口: 呼び出し履歴（CFI）・各フレームの引数とローカル変数・C の式。

    insp = Inspector(DebugInfo("app.elf"), access, symbols=Symbols("app.elf"))
    insp.backtrace(variables=True)                  # [{"index", "pc", "function", "file", "line", "cfa", "note", ...}]
    insp.frame_variables(1)                         # {"function", "arguments": [...], "locals": [...]}
    insp.read("head->samples[1]")                   # {"expression", "type", "address", "value"}
    insp.write("sensors[0].gain", 2.5)

巻き戻しの結果は記憶する。ターゲットが動いたら作り直すか invalidate() を呼ぶ。結果はすべて JSON にできる形。
"""

from __future__ import annotations

from .access import TargetAccess, WritableAccess
from .cexpr import ExpressionError, UnknownNameError, Value, evaluate, evaluate_number
from .info import DebugInfo, FrameView, Variable
from .location import EvalContext, MemoryPlace, NotAvailable, read_place
from .typesys import EnumType, strip, type_name
from .unwind import CallFrameTable, FrameState, Unwinder
from .values import DecodeContext, decode, decode_bits, encode, unavailable

__all__ = ["Inspector", "LOCAL_HINT", "check_expression"]

LOCAL_HINT = "（走らせたまま・ELF だけで読めるのは大域・静的の変数。ローカル変数は止まっているときに読む）"


class _NeedsMemory(Exception):
    """式の番地を決めるのにメモリの値が要る（ポインタをたどる式）。"""


def _needs_memory(address: int, length: int) -> bytes:
    raise _NeedsMemory()


def check_expression(info, expression: str) -> dict:
    """C の式を型の情報（DWARF）だけで調べる（ターゲットのメモリ・レジスタは使わない）。式の誤り・名前の無い変数
    （ローカル変数なら止まっているときに読む旨を添える）・型の誤りは ExpressionError。通れば {"expression", "type"
    （ポインタをたどる式は読むまで分からないので None）, "address"?（番地の決まる式）, "live"（走らせたまま読める）,
    "needs_memory"（番地がメモリの値で決まる）}。"""
    try:
        value = evaluate(expression, info, FrameView(None, _needs_memory, _no_register))
    except _NeedsMemory:
        return {"expression": expression, "type": None, "live": True, "needs_memory": True}
    except UnknownNameError as e:
        raise UnknownNameError(f"{e}{LOCAL_HINT}") from None
    out = {"expression": expression, "type": type_name(value.type), "live": True, "needs_memory": False}
    if value.address is not None:
        out["address"] = value.address
    return out


class Inspector:
    def __init__(self, info: DebugInfo, access: TargetAccess, symbols=None, max_frames: int = 64):
        self.info = info
        self.access = access
        self.symbols = symbols                       # fprobe.symbols.Symbols（任意。ファイル:行・番地のシンボル名）
        self.unwinder = Unwinder(CallFrameTable.from_dwarf(info.dwarf), max_frames)
        self._frames: list[FrameState] | None = None

    def invalidate(self) -> None:
        """ターゲットが動いた: 巻き戻しの結果を捨てる。"""
        self._frames = None

    # --- フレーム ----------------------------------------------------------------------------

    def frames(self) -> list[FrameState]:
        if self._frames is None:
            self._frames = self.unwinder.unwind(self.access)
        return self._frames

    def frame(self, index: int) -> FrameState:
        frames = self.frames()
        if not 0 <= index < len(frames):
            raise IndexError(f"フレーム {index} は無い（{len(frames)} 段）")
        return frames[index]

    def view(self, index: int) -> FrameView:
        state = self.frame(index)
        return FrameView(state.lookup_pc, self.access.read_memory, state.registers.read, state.cfa)

    def where(self, state: FrameState) -> dict:
        """関数名・関数の先頭からのバイト数・ファイル:行（呼び出し元は呼び出し命令の行）。"""
        function = self.info.function_at(state.lookup_pc)
        out = {"function": function.name if function else None, "offset": None, "file": None, "line": None}
        if function is not None:
            out["offset"] = state.pc - function.low
        if self.symbols is not None:
            loc = self.symbols.describe(state.lookup_pc)
            out["file"], out["line"] = loc.file, loc.line
            if function is None and loc.function is not None:
                out["function"], out["offset"] = loc.function, state.pc - (loc.address - loc.offset)
        return out

    def backtrace(self, variables: bool = False, max_elements: int = 64) -> list[dict]:
        out = []
        for state in self.frames():
            entry = {"index": state.index, "pc": state.pc, **self.where(state), "cfa": state.cfa, "note": state.note}
            if variables:
                entry.update(self._variables(state.index, max_elements))
            out.append(entry)
        return out

    def frame_variables(self, index: int = 0, max_elements: int = 64) -> dict:
        state = self.frame(index)
        return {"function": self.where(state)["function"], **self._variables(index, max_elements)}

    def _variables(self, index: int, max_elements: int) -> dict:
        state = self.frame(index)
        scope = self.info.scope_at(state.lookup_pc)
        if scope is None:
            return {"arguments": [], "locals": []}
        view = self.view(index)
        return {"arguments": [self.describe(v, view, max_elements) for v in scope.parameters],
                "locals": [self.describe(v, view, max_elements) for v in scope.locals]}

    def describe(self, var: Variable, view: FrameView | None, max_elements: int = 64) -> dict:
        place = var.place(view)
        out = {"name": var.name, "type": type_name(var.type), "value": None}
        if var.decl_line is not None:
            out["line"] = var.decl_line
        if isinstance(place, MemoryPlace):
            out["address"] = place.address
        out["value"] = self.decode(Value(var.type, place), view, max_elements)
        return out

    # --- 式 ----------------------------------------------------------------------------------

    def evaluate(self, expression: str, frame: int = 0) -> Value:
        return evaluate(expression, self.info, self._view_or_none(frame))

    def number(self, expression: str, frame: int = 0):
        """式を数にする（整数・列挙・ポインタ・浮動小数。配列は先頭の番地）: (式の型, 値)。関数の引数に使う。"""
        return evaluate_number(expression, self.info, self._view_or_none(frame))

    def _view_or_none(self, frame: int) -> FrameView | None:
        try:
            return self.view(frame)
        except IndexError:
            if frame == 0:                               # PC が読めない（動作中等）: 大域だけで評価する
                return FrameView(None, self.access.read_memory, _no_register)
            raise

    def read(self, expression: str, frame: int = 0, max_elements: int = 64) -> dict:
        """式を評価して読む: {"expression", "type", "address"（メモリ上なら）, "value"}。"""
        return self._read(expression, self._view_or_none(frame), max_elements)

    def read_global(self, expression: str, max_elements: int = 64) -> dict:
        """フレームを使わずに読む（走っているコア: レジスタは読まず、大域・静的の変数とメモリだけ）。形は read と同じ。"""
        return self._read(expression, FrameView(None, self.access.read_memory, _no_register), max_elements)

    def _read(self, expression: str, view: FrameView | None, max_elements: int) -> dict:
        value = evaluate(expression, self.info, view)
        out = {"expression": expression, "type": type_name(value.type)}
        if value.address is not None:
            out["address"] = value.address
        out["value"] = self.decode(value, view, max_elements)
        return out

    def decode(self, value: Value, view: FrameView | None, max_elements: int = 64) -> object:
        context = DecodeContext(self.access.read_memory, self._describe_address, max_elements)
        size = value.type.size if not value.is_bitfield else (value.bit_offset + value.bit_size + 7) // 8
        if size is None:
            return unavailable(f"大きさの分からない型 {type_name(value.type)}")
        reader = view or FrameView(None, self.access.read_memory, _no_register)
        try:
            data = read_place(value.place, size, _context(reader))
        except NotAvailable as e:
            return unavailable(e.reason)
        if value.is_bitfield:
            return decode_bits(value.type, data, value.bit_offset, value.bit_size)
        return decode(value.type, data, context)

    def write(self, expression: str, new_value: object, frame: int = 0) -> None:
        """式の指す値（メモリ上の基本型・列挙・ポインタ・ビットフィールド）に書く。"""
        if not isinstance(self.access, WritableAccess):
            raise TypeError("この読み手は書き込めない")
        value = self.evaluate(expression, frame)
        if value.address is None:
            raise ExpressionError(f"{expression} はメモリ上に無い（レジスタ・定数には書かない）")
        if value.is_bitfield:
            self._write_bits(value, new_value)
        else:
            self.access.write_memory(value.address, encode(value.type, new_value))
        self.invalidate()

    def _write_bits(self, value: Value, new_value: object) -> None:
        s = strip(value.type)
        if isinstance(s, EnumType) and isinstance(new_value, str):
            number = s.value_of(new_value)
        elif isinstance(new_value, int):
            number = int(new_value)
        else:
            raise TypeError(f"ビットフィールドには整数を書く（{new_value!r}）")
        n = value.bit_size
        lo, hi = (-(1 << (n - 1)), 1 << (n - 1)) if getattr(s, "signed", False) else (0, 1 << n)
        if not lo <= number < hi:
            raise ValueError(f"{number} は {n} ビットのビットフィールドに入らない")
        size = (value.bit_offset + n + 7) // 8
        data = int.from_bytes(self.access.read_memory(value.address, size), "little")
        mask = ((1 << n) - 1) << value.bit_offset
        data = (data & ~mask) | ((number << value.bit_offset) & mask)
        self.access.write_memory(value.address, data.to_bytes(size, "little"))

    def type_info(self, name: str) -> dict:
        """型の名前 → 大きさとメンバー（構造体・共用体）・列挙子。"""
        t = self.info.type_named(name)
        s = strip(t)
        out: dict = {"name": type_name(t), "size": t.size, "kind": type(s).__name__.removesuffix("Type").lower()}
        members = getattr(s, "members", None)
        if members is not None:
            out["members"] = [{"name": m.name, "type": type_name(m.type), "offset": m.offset,
                               **({"bit_size": m.bit_size, "bit_offset": m.bit_offset} if m.bit_size else {})}
                              for m in members]
        enumerators = getattr(s, "enumerators", None)
        if enumerators is not None:
            out["enumerators"] = dict(enumerators)
        return out

    # --- 番地のシンボル名 ---------------------------------------------------------------------

    def _describe_address(self, address: int) -> str | None:
        if self.symbols is None:
            return None
        found = self.symbols.symbol_at(address)
        if found is None:
            return None
        symbol, offset = found
        if symbol.kind == "function" and address & 1:
            offset -= 1                                            # Thumb のビット付きの関数の番地
        return symbol.name if offset == 0 else f"{symbol.name}+{offset}"


def _context(view: FrameView) -> EvalContext:
    return EvalContext(view.read_memory, view.read_register, cfa=view.cfa)


def _no_register(number: int) -> int:
    raise NotAvailable("フレームが無い（レジスタが無い）")
