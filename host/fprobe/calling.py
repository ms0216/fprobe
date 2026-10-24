"""ターゲットの関数の呼び出し（debug.call）と、AAPCS（Arm の関数呼び出しの規約）の引数の置き場所・戻り値の読み方。

止まったコアで、関数を引数付きで実行して戻り値を得る（GDB の print f(1, 2)、winIDEA testIDEA の関数単位の試験の土台）。

    caller = FunctionCaller(target, runner, info, symbols)
    caller.call(signature_of(info, symbols, "square"), [7])
    # → {"returned": True, "return": {"type": "int", "value": {"kind": "int", "value": 49}, "raw": 49}, ...}

手順:
1. レジスタ（コア・特殊・FPU）を保存する。
2. いまの sp の下に、文字列・バイト列の引数とメモリで返る戻り値の置き場所、スタックに渡す引数を置く（そこの元の中身は
   保存しておく）。
3. AAPCS で引数をレジスタ（r0〜r3、ハード浮動小数なら s0〜s15 / d0〜d7）とスタックに置き、LR = 踏み台、PC = 関数。
   踏み台はリセットの入口（VTOR のベクタ表の 2 語目）に置くブレークポイント。関数が戻るとそこで止まる。スタックに
   命令を書かないので、MPU の実行禁止（XN）でも動く。
4. 踏み台に着くまで走らせる（利用者のブレークポイント・ウォッチポイントは止まらずに続ける。BKPT 命令（セミホスティング
   以外）・故障・時間切れなら打ち切る）。
5. 戻り値を読み、保存したレジスタとメモリを書き戻す。打ち切ったときは、その場所と故障の解析を添えて書き戻す。
   故障の例外に入ったまま（IPSR が変わった）は書き戻しても例外が有効のまま: restored = False（リセットを勧める）。

AAPCS（Procedure Call Standard for the Arm Architecture, AAPCS32）:
- 4 バイト以下の整数・列挙・ポインタは r0〜r3、続きはスタック（4 バイトの枠）。8 バイトの整数は偶数のレジスタの組
  （r0:r1 / r2:r3）、入らなければスタック（8 バイト境界）。
- ハード浮動小数（ELF の Tag_ABI_VFP_args）なら float / double は s0〜s15 / d0〜d7（空いた s の後埋めあり）。可変長の
  引数は常に基本の規約（float は double に格上げしてコアのレジスタ）。
- 戻り値: 4 バイト以下は r0、8 バイトは r0:r1、ハード浮動小数の float / double / 同じ浮動小数の型だけの構造体（HFA）は
  s0〜 / d0〜、それより大きい構造体は呼び手が用意したメモリ（その番地を隠れた最初の引数 r0 で渡す）。
未対応: 構造体・共用体の値渡しの引数（ポインタで渡す）。
"""

import time
from collections.abc import Callable
from dataclasses import dataclass, field

from . import fault
from .dwarf.typesys import ArrayType, BaseType, CType, EnumType, PointerType, StructType, VoidType, strip, type_name
from .dwarf.values import DecodeContext, decode, encode, unavailable
from .stepping import FAULTS, Halt, Runner

__all__ = ["CallError", "FunctionCaller", "Placement", "Signature", "allocate", "hard_float", "read_return_value",
           "signature_of"]

VTOR = 0xE000ED08
CFSR, HFSR = 0xE000ED28, 0xE000ED2C
XPSR_T = 1 << 24
XPSR_ICI_IT = 0x0600FC00                # EPSR の IT / ICI（関数の先頭の命令が条件付きにならないよう消す）
SCRATCH_LIMIT = 64 << 10                # 引数・戻り値の置き場所に使う sp の下の大きさの上限
CORE_REGISTERS = [f"r{i}" for i in range(13)] + ["lr", "pc", "xpsr", "control", "msp", "psp", "primask", "basepri",
                                                 "faultmask"]
FPU_REGISTERS = [f"s{i}" for i in range(32)] + ["fpscr"]


class CallError(RuntimeError):
    """呼び出しを始められない（停止していない・引数が型に合わない・置き場所が無い等）。"""


# --- ABI ------------------------------------------------------------------------------------------

def hard_float(elf) -> bool:
    """浮動小数の引数・戻り値を VFP のレジスタで渡すか（ELF の属性 Tag_ABI_VFP_args、無ければ e_flags の
    EF_ARM_ABI_FLOAT_HARD）。"""
    section = elf.get_section_by_name(".ARM.attributes")
    if section is not None:
        try:
            for sub in section.iter_subsections():
                for subsub in sub.iter_subsubsections():
                    for attr in subsub.iter_attributes():
                        if attr.tag == "TAG_ABI_VFP_ARGS":
                            return attr.value == 1
        except Exception:                                      # 読めない属性: e_flags で判断する
            pass
    return bool(elf["e_flags"] & 0x400)


def _homogeneous_float(t: CType) -> tuple[int, int] | None:
    """同じ浮動小数の型だけの集まり（HFA。4 個まで）なら (要素の大きさ 4 / 8, 個数)。"""
    sizes: list[int] = []

    def walk(x: CType) -> bool:
        s = strip(x)
        if isinstance(s, BaseType) and s.encoding == "float":
            sizes.append(s.size)
            return True
        if isinstance(s, StructType) and s.size:
            return bool(s.members) and all(m.bit_size is None and walk(m.type) for m in s.members)
        if isinstance(s, ArrayType) and s.length:
            return all(walk(s.element) for _ in range(s.length))
        return False

    s = strip(t)
    if not isinstance(s, (StructType, ArrayType)) or not walk(t) or not 1 <= len(sizes) <= 4 or len(set(sizes)) != 1:
        return None
    return sizes[0], len(sizes)


def _returns_in_memory(t: CType, hard: bool) -> bool:
    s = strip(t)
    if not isinstance(s, (StructType, ArrayType)):
        return False
    return not (hard and _homogeneous_float(t)) and (s.size or 0) > 4


@dataclass
class Placement:
    """引数の置き場所: コアのレジスタ（番号 → 語）、VFP の s レジスタ（番号 → 語）、スタック（sp からのバイト列）。"""
    core: dict[int, int] = field(default_factory=dict)
    vfp: dict[int, int] = field(default_factory=dict)
    stack: bytearray = field(default_factory=bytearray)
    _ncrn: int = 0
    _vfp_open: bool = True

    def to_core(self, data: bytes, align8: bool = False) -> None:
        data = data + bytes(-len(data) % 4)
        words = [int.from_bytes(data[i:i + 4], "little") for i in range(0, len(data), 4)]
        if align8 and self._ncrn % 2:
            self._ncrn += 1
        if self._ncrn + len(words) <= 4:
            for w in words:
                self.core[self._ncrn] = w
                self._ncrn += 1
            return
        self._ncrn = 4                                     # 入らなければ以後のコアの引数はすべてスタック
        self.to_stack(data, 8 if align8 else 4)

    def to_vfp(self, data: bytes, element: int) -> None:
        """浮動小数（element = 4: s、8: d）。空いた s を後埋めする。入らなければ以後の VFP の引数はすべてスタック。"""
        n = len(data) // 4
        step = element // 4
        if self._vfp_open:
            for first in range(0, 16 - n + 1, step):
                if all(first + k not in self.vfp for k in range(n)):
                    for k in range(n):
                        self.vfp[first + k] = int.from_bytes(data[4 * k:4 * k + 4], "little")
                    return
            self._vfp_open = False
        self.to_stack(data, element)

    def to_stack(self, data: bytes, align: int) -> None:
        self.stack.extend(bytes(-len(self.stack) % align))
        self.stack.extend(data + bytes(-len(data) % 4))


def _word_bytes(t: CType, value: object) -> bytes:
    """引数の値を型の大きさのバイト列に（4 バイト未満は 4 バイトへ符号・ゼロ拡張）。"""
    data = encode(t, value)
    s = strip(t)
    if len(data) < 4:
        signed = isinstance(s, (BaseType, EnumType)) and s.signed
        data = int.from_bytes(data, "little", signed=signed).to_bytes(4, "little", signed=signed)
    return data


def allocate(parameters: list[CType], values: list, hard: bool, variadic: list | None = None,
             hidden_result: int | None = None) -> Placement:
    """引数（型と値）を AAPCS で置く。variadic: 可変長の部分の値（int か float。型は値から）。hidden_result: 戻り値の
    置き場所の番地（r0 で渡す）。"""
    p = Placement()
    if hidden_result is not None:
        p.to_core(hidden_result.to_bytes(4, "little"))
    for t, value in zip(parameters, values):
        s = strip(t)
        if isinstance(s, (StructType, ArrayType)) or s.size is None:
            raise CallError(f"{type_name(t)} の値渡しは未対応（ポインタで渡す）")
        data = _word_bytes(t, value)
        if isinstance(s, BaseType) and s.encoding == "float" and hard:
            p.to_vfp(data, s.size)
        else:
            p.to_core(data, align8=len(data) == 8)
    for value in variadic or []:
        if isinstance(value, float):
            p.to_core(encode(BaseType("double", 8, "float"), value), align8=True)
        elif isinstance(value, bool) or not isinstance(value, int):
            raise CallError(f"可変長の引数は整数か浮動小数（{value!r}）")
        elif -(1 << 31) <= value < 1 << 32:
            p.to_core((value & 0xFFFFFFFF).to_bytes(4, "little"))
        elif -(1 << 63) <= value < 1 << 64:
            p.to_core((value & (1 << 64) - 1).to_bytes(8, "little"), align8=True)
        else:
            raise CallError(f"{value} は 64 ビットに入らない")
    return p


def read_return_value(target, info, t: CType, hard: bool, memory: int | None = None) -> dict:
    """戻り値を読む（関数から戻った直後に）。memory: メモリで返る戻り値の置き場所（呼び手が渡した番地）。"""
    s = strip(t)
    name = type_name(t)
    if isinstance(s, VoidType):
        return {"type": name, "value": None}
    size = t.size
    if size is None:
        return {"type": name, "value": unavailable("大きさの分からない型")}
    hfa = _homogeneous_float(t) if hard else None
    if hard and isinstance(s, BaseType) and s.encoding == "float":
        data = _vfp_bytes(target, size // 4)
    elif hfa is not None:
        data = _vfp_bytes(target, size // 4)
    elif _returns_in_memory(t, hard):
        if memory is None:
            return {"type": name, "value": unavailable("メモリで返る戻り値（置き場所の番地が分からない）")}
        data = bytes(target.read_memory_block8(memory, size))
    else:
        r0, r1 = target.read_core_registers_raw(["r0", "r1"])
        data = (r0 | r1 << 32).to_bytes(8, "little")[:size]
    context = DecodeContext(read_memory=lambda a, n: bytes(target.read_memory_block8(a, n)))
    out = {"type": name, "value": decode(t, data, context)}
    if isinstance(s, PointerType) or isinstance(s, BaseType) and s.encoding != "float":
        out["raw"] = int.from_bytes(data, "little")
    return out


def _vfp_bytes(target, count: int) -> bytes:
    words = target.read_core_registers_raw([f"s{i}" for i in range(count)])
    return b"".join(w.to_bytes(4, "little") for w in words)


# --- 関数の型 ----------------------------------------------------------------------------------------

@dataclass(frozen=True)
class Signature:
    """呼ぶ関数: 名前・番地・引数（名前, 型）・戻り値の型（DWARF が無ければ None）・可変長か。"""
    name: str
    address: int
    parameters: tuple[tuple[str, CType], ...] = ()
    result: CType | None = None
    variadic: bool = True

    @property
    def known(self) -> bool:
        return self.result is not None


def signature_of(info, symbols, location: str | int) -> Signature:
    """場所（関数名・"0x..."・番地）の関数の型を DWARF から。DWARF が無い関数は型の分からない Signature（引数は
    整数だけ、戻り値は r0・r1 のまま）。"""
    if symbols is None and not isinstance(location, int):
        raise CallError("ELF が読み込まれていない: 関数は番地で指定する")
    address = location if isinstance(location, int) else symbols.resolve(location)
    if symbols is not None:
        function = symbols.function_at(address)
        if function is not None and function.address != address:
            raise CallError(f"0x{address:08X} は {function.name} の先頭ではない")
    name = location if isinstance(location, str) else f"0x{address:08X}"
    dwarf_function = info.function_at(address) if info is not None else None
    if dwarf_function is None or dwarf_function.low != address:
        return Signature(name, address)
    scope = info.scope_at(address)
    parameters = tuple((v.name, v.type) for v in scope.parameters)
    variadic = any(c.tag == "DW_TAG_unspecified_parameters" for c in dwarf_function.die.iter_children())
    return Signature(dwarf_function.name, address, parameters, info.types.type_of(dwarf_function.die), variadic)


# --- 呼び出し ------------------------------------------------------------------------------------------

@dataclass(frozen=True)
class _Blob:
    data: bytes
    text: bool                              # {"string": ...}（結果を文字列で返す）


class FunctionCaller:
    """止まったコアで関数を呼ぶ。runner は fprobe.stepping.Runner（ロック・時間切れ・ブレークポイントの扱い）。
    evaluate: 文字列の引数（C の式）を (型, 数) にする（いまのフレームで）。"""

    def __init__(self, target, runner: Runner, info=None, symbols=None,
                 evaluate: Callable[[str], tuple[CType, int | float]] | None = None):
        self.target = target
        self.runner = runner
        self.info = info
        self.symbols = symbols
        self.evaluate = evaluate
        self.hard = hard_float(info.elf) if info is not None else False

    def call(self, signature: Signature, arguments: list, disable_interrupts: bool = False) -> dict:
        lock = self.runner.lock
        started = time.monotonic()
        with lock:
            saved = self._save_registers()
            values, blobs = self._values(signature, arguments)
            old_sp = self.target.read_core_register("sp")             # いま使っているスタック（MSP か PSP）
            top = old_sp & ~7
            addresses = []
            for blob in blobs:
                top = (top - len(blob.data)) & ~7
                addresses.append(top)
            result_type = signature.result
            hidden = None
            if result_type is not None and _returns_in_memory(result_type, self.hard):
                top = (top - result_type.size) & ~7
                hidden = top
            values = [addresses[v.index] if isinstance(v, _BlobRef) else v for v in values]
            fixed = len(signature.parameters) if signature.known else 0
            placement = allocate([t for _, t in signature.parameters][:fixed], values[:fixed], self.hard,
                                 values[fixed:], hidden)
            sp_call = (top - len(placement.stack)) & ~7
            if old_sp - sp_call > SCRATCH_LIMIT:
                raise CallError(f"引数の置き場所が大きすぎる（{old_sp - sp_call} バイト。sp の下に使う上限は "
                                f"SCRATCH_LIMIT = {SCRATCH_LIMIT} バイト）")
            original = bytes(self.target.read_memory_block8(sp_call, old_sp - sp_call)) if old_sp > sp_call else b""
            trampoline = self._trampoline()
            if trampoline == signature.address:
                raise CallError("リセットの入口の関数は呼べない（踏み台に使う）")
        out: dict = {"function": signature.name, "address": signature.address,
                     "arguments": self._describe_arguments(signature, values)}
        restored = False
        try:                                                     # 途中で失敗しても（比較器が足りない等）必ず元に戻す
            with lock:
                for blob, address in zip(blobs, addresses):
                    self.target.write_memory_block8(address, list(blob.data))
                if placement.stack:
                    self.target.write_memory_block8(sp_call, list(placement.stack))
                self._enter(saved, signature.address, sp_call, trampoline, placement, disable_interrupts)
            halt = self._run(trampoline, sp_call)
            with lock:
                out["returned"] = halt.reason == "arrived"
                if halt.reason == "arrived":
                    out["reason"] = "returned"
                    out["return"] = (read_return_value(self.target, self.info, result_type, self.hard, hidden)
                                     if result_type is not None else self._raw_return())
                else:
                    out["reason"] = halt.reason                       # 止まった理由のまま（時間切れは timeout）
                    out["ipsr"] = halt.regs.ipsr                      # 止まった時の例外の番号（故障の中か）
                    out["stopped_at"] = self._place(halt.regs.pc)
                    if halt.regs.ipsr in FAULTS or halt.reason == "vector_catch":
                        out["fault"] = fault.analyze(self.target, self.symbols)
                if blobs:
                    out["buffers"] = [self._buffer(i, blob, address) for i, (blob, address)
                                      in enumerate(zip(blobs, addresses))]
        finally:
            with lock:
                restored = self._undo(saved, sp_call, original)
        out["restored"] = restored
        if not restored:
            out["note"] = ("呼んだ関数の中で例外（故障）に入ったまま: レジスタとメモリは書き戻したが例外は有効のまま。"
                           "続けるなら target_reset を勧める")
        out["elapsed_s"] = round(time.monotonic() - started, 4)
        return out

    def _undo(self, saved: dict[str, int], sp_call: int, original: bytes) -> bool:
        """スタックの下のメモリとレジスタを書き戻し、呼んだ関数が立てた故障の状態のビットを消す。
        例外の状態（IPSR）が呼ぶ前と同じなら True（例外に入ったままはレジスタの書き込みでは戻せない）。"""
        if original:
            self.target.write_memory_block8(sp_call, list(original))
        same = self.target.read_core_register("xpsr") & 0x1FF == saved["xpsr"] & 0x1FF
        if same:
            self._clear_new_fault_status(saved)
        self._restore_registers(saved)
        return same

    # --- 引数 --------------------------------------------------------------------------------------

    def _values(self, signature: Signature, arguments: list) -> tuple[list, list[_Blob]]:
        if signature.known and not signature.variadic and len(arguments) != len(signature.parameters):
            raise CallError(f"{signature.name} の引数は {len(signature.parameters)} 個"
                            f"（{', '.join(type_name(t) + ' ' + n for n, t in signature.parameters) or 'void'}）")
        if signature.known and len(arguments) < len(signature.parameters):
            raise CallError(f"{signature.name} の引数は {len(signature.parameters)} 個以上")
        values, blobs = [], []
        for i, argument in enumerate(arguments):
            typed = signature.known and i < len(signature.parameters)
            t = signature.parameters[i][1] if typed else None
            if isinstance(argument, dict):
                blob = _blob(argument)
                if t is not None and not isinstance(strip(t), PointerType):
                    raise CallError(f"引数 {i}（{type_name(t)}）にはメモリ（string / bytes）を渡せない")
                values.append(_BlobRef(len(blobs)))
                blobs.append(blob)
            elif isinstance(argument, str):
                if self.evaluate is None:
                    raise CallError(f"式の引数 {argument!r} には ELF の DWARF が要る")
                _, number = self.evaluate(argument)
                values.append(number)
            elif isinstance(argument, (int, float)):
                values.append(argument)
            else:
                raise CallError(f"引数 {i} は数・C の式（文字列）・{{\"string\": ...}}・{{\"bytes\": 16 進}} のどれか")
            if t is None and isinstance(values[-1], float) and not signature.known:
                raise CallError("DWARF の無い関数に浮動小数は渡せない（置き場所が型で変わる）")
        return values, blobs

    @staticmethod
    def _describe_arguments(signature: Signature, values: list) -> list[dict]:
        out = []
        for i, value in enumerate(values):
            entry: dict = {"value": value}
            if signature.known and i < len(signature.parameters):
                entry = {"name": signature.parameters[i][0], "type": type_name(signature.parameters[i][1]), **entry}
            out.append(entry)
        return out

    def _buffer(self, index: int, blob: _Blob, address: int) -> dict:
        after = bytes(self.target.read_memory_block8(address, len(blob.data)))
        out = {"index": index, "address": address, "after": after.hex()}
        if blob.text:
            out["string"] = after.split(b"\0", 1)[0].decode("utf-8", errors="replace")
        return out

    # --- レジスタ ------------------------------------------------------------------------------------

    def _available(self, names: list[str]) -> list[str]:
        known = getattr(getattr(self.target, "core_registers", None), "by_name", None)
        return [n for n in names if known is None or n in known]

    def _save_registers(self) -> dict[str, int]:
        if self.target.get_state().name != "HALTED":
            raise CallError("ターゲットが止まっていない（target_halt の後に呼ぶ）")
        names = self._available(CORE_REGISTERS)
        saved = dict(zip(names, self.target.read_core_registers_raw(names)))
        saved.setdefault("control", 0)
        fpu = self._available(FPU_REGISTERS)
        if fpu:
            try:
                saved.update(zip(fpu, self.target.read_core_registers_raw(fpu)))
            except Exception:                                    # FPU が無効で読めない: 浮動小数は使えない
                pass
        for name in ("CFSR", "HFSR"):
            saved[name] = self.target.read32(CFSR if name == "CFSR" else HFSR)
        return saved

    def _restore_registers(self, saved: dict[str, int]) -> None:
        names = [n for n in saved if n not in ("CFSR", "HFSR", "control")]
        if "control" in self._available(["control"]):
            self.target.write_core_registers_raw(["control"], [saved["control"]])
        self.target.write_core_registers_raw(names, [saved[n] for n in names])
        self.target.flush()

    def _clear_new_fault_status(self, saved: dict[str, int]) -> None:
        """呼んだ関数が立てた故障の状態のビット（CFSR・HFSR。1 を書いて消す）だけを消す。"""
        for address, name in ((CFSR, "CFSR"), (HFSR, "HFSR")):
            new = self.target.read32(address) & ~saved[name]
            if new:
                self.target.write32(address, new)

    def _enter(self, saved: dict, address: int, sp: int, trampoline: int, placement: Placement,
               disable_interrupts: bool) -> None:
        names, values = [], []
        for n, w in placement.core.items():
            names.append(f"r{n}")
            values.append(w)
        for n, w in placement.vfp.items():
            names.append(f"s{n}")
            values.append(w)
        if placement.vfp and f"s{max(placement.vfp)}" not in self._available([f"s{max(placement.vfp)}"]):
            raise CallError("このコアに FPU が無い（ハード浮動小数の ELF の浮動小数の引数は渡せない）")
        names += ["sp", "lr", "pc", "xpsr"]
        values += [sp, trampoline | 1, address, (saved["xpsr"] & ~XPSR_ICI_IT) | XPSR_T]
        if disable_interrupts and "primask" in saved:
            names.append("primask")
            values.append(1)
        self.target.write_core_registers_raw(names, values)
        self.target.flush()

    def _trampoline(self) -> int:
        """踏み台: リセットの入口（いま動いているファームウェアのベクタ表の 2 語目）。"""
        table = self.target.read32(VTOR) & ~0x7F
        entry = self.target.read32(table + 4) & ~1
        if entry == 0:
            raise CallError("リセットの入口が読めない（VTOR のベクタ表が空）: 踏み台を置けない")
        return entry

    def _run(self, trampoline: int, sp_call: int) -> Halt:
        """踏み台に着くまで走らせる。ウォッチポイントは止まらずに続ける。"""
        while True:
            halt = self.runner.run_to({trampoline: lambda r: r.sp >= sp_call})
            if halt.reason != "watchpoint":
                return halt

    def _raw_return(self) -> dict:
        """型の分からない戻り値（DWARF の無い関数）: 値にせず r0・r1 をそのまま返す。"""
        r0, r1 = self.target.read_core_registers_raw(["r0", "r1"])
        return {"type": None, "r0": r0, "r1": r1}

    def _place(self, address: int) -> dict:
        out: dict = {"pc": address}
        if self.symbols is not None:
            out["where"] = str(self.symbols.describe(address))
        return out


@dataclass(frozen=True)
class _BlobRef:
    index: int


def _blob(argument: dict) -> _Blob:
    if set(argument) == {"string"} and isinstance(argument["string"], str):
        return _Blob(argument["string"].encode("utf-8") + b"\0", True)
    if set(argument) == {"bytes"} and isinstance(argument["bytes"], str):
        try:
            return _Blob(bytes.fromhex(argument["bytes"]), False)
        except ValueError:
            raise CallError(f"bytes は 16 進の文字列（{argument['bytes']!r}）") from None
    raise CallError(f"メモリの引数は {{\"string\": 文字列}} か {{\"bytes\": 16 進}}（{argument!r}）")
