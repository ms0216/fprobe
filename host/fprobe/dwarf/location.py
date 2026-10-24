"""DWARF の位置式（スタック機械）の評価と、その結果の「場所」（Place）からの読み出し。

位置式の字句解析は pyelftools（DWARFExprParser）に任せ、評価はここで行う。値はアドレスの幅（32 ビット）の
符号なし整数として積む（DWARF の generic type）。

    place = evaluate_location(ops, context)        # MemoryPlace / RegisterPlace / ValuePlace / PiecesPlace / Unavailable
    data = read_place(place, size, context)        # 型の大きさのバイト列（読めなければ NotAvailable）
"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from dataclasses import dataclass, field

from .access import AccessError, register_name

__all__ = ["EvalContext", "MemoryPlace", "NotAvailable", "PiecesPlace", "Place", "RegisterPlace", "Unavailable",
           "ValuePlace", "evaluate_location", "evaluate_value", "read_place", "register_width"]

ADDRESS_BITS = 32
MASK = (1 << ADDRESS_BITS) - 1


class NotAvailable(Exception):
    """値が読めない理由（DWARF に位置が無い・レジスタが復元できない・関数の入口の値 等）。"""

    @property
    def reason(self) -> str:
        return str(self.args[0]) if self.args else "読めない"


@dataclass(frozen=True)
class MemoryPlace:
    address: int


@dataclass(frozen=True)
class RegisterPlace:
    number: int                   # DWARF のレジスタ番号


@dataclass(frozen=True)
class ValuePlace:
    data: bytes                   # 値そのもの（DW_OP_stack_value・implicit_value・DW_AT_const_value）


@dataclass(frozen=True)
class PiecesPlace:
    pieces: tuple[tuple["Place", int], ...]      # (場所, バイト数) の並び（下位から）


@dataclass(frozen=True)
class Unavailable:
    reason: str


Place = MemoryPlace | RegisterPlace | ValuePlace | PiecesPlace | Unavailable


@dataclass
class EvalContext:
    """位置式の評価に要るもの。registers はそのフレームのレジスタ（DWARF 番号 → 値。読めなければ NotAvailable）。"""
    read_memory: Callable[[int, int], bytes]
    read_register: Callable[[int], int]
    frame_base: Callable[[], int] | None = None  # DW_OP_fbreg のとき初めて評価する
    cfa: int | None = None                       # DW_OP_call_frame_cfa
    address_of_index: Callable[[int], int] | None = None   # DW_OP_addrx（.debug_addr）
    base_type: Callable[[int], tuple[int, bool]] | None = None   # DW_OP_convert: CU 内の DIE の位置 → (バイト数, 符号付き)
    extras: dict = field(default_factory=dict)


def register_width(number: int) -> int:
    return 8 if 256 <= number < 288 else 4


def _signed(value: int, bits: int = ADDRESS_BITS) -> int:
    value &= (1 << bits) - 1
    return value - (1 << bits) if value >> (bits - 1) else value


def _memory(context: EvalContext, address: int, size: int) -> bytes:
    try:
        return context.read_memory(address & MASK, size)
    except AccessError as e:
        raise NotAvailable(f"メモリ 0x{address & MASK:08X} が読めない（{e}）") from None


def _register(context: EvalContext, number: int) -> int:
    try:
        return context.read_register(number)
    except AccessError as e:
        raise NotAvailable(f"レジスタ {register_name(number)} が読めない（{e}）") from None


_BINARY = {
    "DW_OP_and": lambda a, b: a & b,
    "DW_OP_or": lambda a, b: a | b,
    "DW_OP_xor": lambda a, b: a ^ b,
    "DW_OP_plus": lambda a, b: a + b,
    "DW_OP_minus": lambda a, b: a - b,
    "DW_OP_mul": lambda a, b: a * b,
    "DW_OP_shl": lambda a, b: a << b if b < ADDRESS_BITS else 0,
    "DW_OP_shr": lambda a, b: a >> b,
    "DW_OP_shra": lambda a, b: _signed(a) >> min(b, ADDRESS_BITS),
    "DW_OP_div": lambda a, b: int(_signed(a) / _signed(b)),       # 0 に向かって丸める（C と同じ）
    "DW_OP_mod": lambda a, b: a % b,
    "DW_OP_eq": lambda a, b: int(_signed(a) == _signed(b)),
    "DW_OP_ne": lambda a, b: int(_signed(a) != _signed(b)),
    "DW_OP_lt": lambda a, b: int(_signed(a) < _signed(b)),
    "DW_OP_gt": lambda a, b: int(_signed(a) > _signed(b)),
    "DW_OP_le": lambda a, b: int(_signed(a) <= _signed(b)),
    "DW_OP_ge": lambda a, b: int(_signed(a) >= _signed(b)),
}
_UNARY = {
    "DW_OP_neg": lambda a: -_signed(a),
    "DW_OP_not": lambda a: ~a,
    "DW_OP_abs": lambda a: abs(_signed(a)),
}


def evaluate_location(ops: Sequence, context: EvalContext, initial: Sequence[int] = ()) -> Place:
    """位置式を評価して場所にする。空の式 = DWARF に位置が無い。読めない途中の値は Unavailable。"""
    if not ops:
        return Unavailable("位置式が空（DWARF に位置が無い）")
    try:
        return _run(ops, context, list(initial))
    except NotAvailable as e:
        return Unavailable(e.reason)


def evaluate_value(ops: Sequence, context: EvalContext, initial: Sequence[int] = ()) -> int:
    """番地を計算する式（CFA の式・フレームの基底）。DW_OP_regN は「そのレジスタの値」として扱う。"""
    place = _run(ops, context, list(initial))
    if isinstance(place, MemoryPlace):
        return place.address
    if isinstance(place, RegisterPlace):
        return _register(context, place.number)
    if isinstance(place, ValuePlace):
        return int.from_bytes(place.data[:4], "little")
    raise NotAvailable(place.reason if isinstance(place, Unavailable) else "番地にならない式")


def _run(ops: Sequence, context: EvalContext, stack: list[int]) -> Place:
    offsets = [op.offset for op in ops]
    pieces: list[tuple[Place, int]] = []
    current: Place | None = None             # 直前の reg / stack_value / implicit_value（piece で区切る）
    i = 0
    while i < len(ops):
        op = ops[i]
        name, args = op.op_name, op.args
        i += 1
        if name.startswith("DW_OP_lit"):
            stack.append(int(name[9:]))
        elif name in ("DW_OP_addr", "DW_OP_const1u", "DW_OP_const2u", "DW_OP_const4u", "DW_OP_const8u",
                      "DW_OP_constu", "DW_OP_const1s", "DW_OP_const2s", "DW_OP_const4s", "DW_OP_const8s",
                      "DW_OP_consts"):
            stack.append(args[0] & MASK)
        elif name in ("DW_OP_addrx", "DW_OP_GNU_addr_index", "DW_OP_constx", "DW_OP_GNU_const_index"):
            if context.address_of_index is None:
                raise NotAvailable(f"{name} を解く .debug_addr が無い")
            stack.append(context.address_of_index(args[0]))
        elif name.startswith("DW_OP_reg") and name != "DW_OP_regx" and not name.startswith("DW_OP_regval"):
            current = RegisterPlace(int(name[9:]))
        elif name == "DW_OP_regx":
            current = RegisterPlace(args[0])
        elif name.startswith("DW_OP_breg") and name != "DW_OP_bregx":
            stack.append((_register(context, int(name[10:])) + args[0]) & MASK)
        elif name == "DW_OP_bregx":
            stack.append((_register(context, args[0]) + args[1]) & MASK)
        elif name == "DW_OP_fbreg":
            if context.frame_base is None:
                raise NotAvailable("フレームの基底（DW_AT_frame_base）が無い")
            stack.append((context.frame_base() + args[0]) & MASK)
        elif name == "DW_OP_call_frame_cfa":
            if context.cfa is None:
                raise NotAvailable("CFA が分からない（CFI が無い）")
            stack.append(context.cfa)
        elif name == "DW_OP_dup":
            stack.append(stack[-1])
        elif name == "DW_OP_drop":
            stack.pop()
        elif name == "DW_OP_over":
            stack.append(stack[-2])
        elif name == "DW_OP_pick":
            stack.append(stack[-1 - args[0]])
        elif name == "DW_OP_swap":
            stack[-1], stack[-2] = stack[-2], stack[-1]
        elif name == "DW_OP_rot":
            stack[-1], stack[-2], stack[-3] = stack[-2], stack[-3], stack[-1]
        elif name == "DW_OP_deref":
            stack.append(int.from_bytes(_memory(context, stack.pop(), 4), "little"))
        elif name == "DW_OP_deref_size":
            stack.append(int.from_bytes(_memory(context, stack.pop(), args[0]), "little"))
        elif name == "DW_OP_plus_uconst":
            stack.append((stack.pop() + args[0]) & MASK)
        elif name in _BINARY:
            b, a = stack.pop(), stack.pop()
            if name in ("DW_OP_div", "DW_OP_mod") and b == 0:
                raise NotAvailable("位置式の中の 0 での割り算")
            stack.append(_BINARY[name](a, b) & MASK)
        elif name in _UNARY:
            stack.append(_UNARY[name](stack.pop()) & MASK)
        elif name in ("DW_OP_skip", "DW_OP_bra"):
            if name == "DW_OP_skip" or stack.pop() != 0:
                end = offsets[i] if i < len(ops) else None
                if end is None:
                    break
                target = end + args[0]
                if target not in offsets:
                    if target > offsets[-1]:
                        break
                    raise NotAvailable("位置式の分岐先が命令の境目にない")
                i = offsets.index(target)
        elif name in ("DW_OP_convert", "DW_OP_GNU_convert"):
            if args[0] != 0:                                # 0 = generic type に戻す
                if context.base_type is None:
                    raise NotAvailable("DW_OP_convert の型が引けない")
                size, signed = context.base_type(args[0])
                bits = size * 8
                value = stack.pop() & ((1 << bits) - 1)
                stack.append((_signed(value, bits) if signed else value) & MASK)
        elif name == "DW_OP_stack_value":
            current = ValuePlace((stack.pop() & MASK).to_bytes(4, "little"))
        elif name == "DW_OP_implicit_value":
            current = ValuePlace(bytes(args[1]) if len(args) > 1 else bytes(args[0]))
        elif name in ("DW_OP_piece", "DW_OP_bit_piece"):
            size_bits = args[0] * 8 if name == "DW_OP_piece" else args[0]
            if size_bits % 8 or (name == "DW_OP_bit_piece" and args[1] % 8):
                raise NotAvailable("バイト境界でない DW_OP_bit_piece は扱わない")
            if current is None:
                current = MemoryPlace(stack.pop()) if stack else Unavailable("位置の無い断片（DWARF に位置が無い）")
            elif name == "DW_OP_bit_piece" and args[1]:
                current = _shifted(current, args[1] // 8)
            pieces.append((current, size_bits // 8))
            current = None
        elif name == "DW_OP_nop":
            pass
        elif name in ("DW_OP_entry_value", "DW_OP_GNU_entry_value"):
            raise NotAvailable("関数の入口の値（entry value）は呼び出し元の情報が要る")
        else:
            raise NotAvailable(f"位置式の {name} は扱わない")
    if pieces:
        if current is not None or stack:
            raise NotAvailable("断片の後に余りがある位置式")
        return PiecesPlace(tuple(pieces))
    if current is not None:
        return current
    if not stack:
        return Unavailable("位置式が場所を残さない")
    return MemoryPlace(stack[-1])


def _shifted(place: Place, offset: int) -> Place:
    if isinstance(place, MemoryPlace):
        return MemoryPlace(place.address + offset)
    if isinstance(place, ValuePlace):
        return ValuePlace(place.data[offset:])
    raise NotAvailable("レジスタの途中からの DW_OP_bit_piece は扱わない")


def read_place(place: Place, size: int, context: EvalContext) -> bytes:
    """場所から size バイトを読む。値が足りなければ 0 で埋める（stack_value の値はアドレスの幅）。"""
    if isinstance(place, MemoryPlace):
        return _memory(context, place.address, size)
    if isinstance(place, RegisterPlace):
        if 256 <= place.number < 288 and size <= 4:          # d*n* の下位 = s*2n*（float を d レジスタに置いたとき）
            return read_place(RegisterPlace(64 + 2 * (place.number - 256)), size, context)
        width = register_width(place.number)
        if size > width:
            raise NotAvailable(f"{size} バイトの値が {register_name(place.number)}（{width} バイト）に入っている")
        return _register(context, place.number).to_bytes(width, "little")[:size]
    if isinstance(place, ValuePlace):
        return (place.data + bytes(max(0, size - len(place.data))))[:size]
    if isinstance(place, PiecesPlace):
        out = bytearray()
        for piece, piece_size in place.pieces:
            out += read_place(piece, piece_size, context)
        if len(out) < size:
            raise NotAvailable("断片が値の大きさに足りない")
        return bytes(out[:size])
    raise NotAvailable(place.reason)
