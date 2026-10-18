"""デモ（fake_daemon.py --demo）の変数とメモリ: 偽の RAM の上に、型付きの大域変数・フレームのローカル変数を置き、
本物の C の式の評価（fprobe.dwarf.cexpr）と値の解読・符号化（fprobe.dwarf.values）で読み書きする。
画面の Watch・Locals（構造体・配列・ポインタの木と書き込み）と Memory（16 進の表示と書き込み）が同じ RAM を見る。

デモの ELF（sram_demo.elf）の counter・milestone・table は ELF の番地に、構造体などデモだけの変数は空いている RAM に置く。
"""

from __future__ import annotations

import struct
from collections.abc import Callable
from dataclasses import dataclass

from fprobe.dwarf.cexpr import evaluate
from fprobe.dwarf.info import FrameView
from fprobe.dwarf.location import EvalContext, MemoryPlace, NotAvailable, read_place
from fprobe.dwarf.typesys import (ArrayType, BaseType, CType, EnumType, FunctionType, Member, PointerType, QualifiedType,
                                  StructType, TypedefType, VoidType, strip, type_name)
from fprobe.dwarf.values import DecodeContext, decode, decode_bits, encode, unavailable

RAM_BASE, RAM_SIZE = 0x20000000, 0x8000          # デモの ELF のリンカスクリプトの RAM（32 KB、スタックは末尾）

# --- 型 -------------------------------------------------------------------------------------------

UINT8 = TypedefType("uint8_t", BaseType("unsigned char", 1, "unsigned_char"))
INT16 = TypedefType("int16_t", BaseType("short", 2, "signed"))
UINT16 = TypedefType("uint16_t", BaseType("unsigned short", 2, "unsigned"))
UINT32 = TypedefType("uint32_t", BaseType("unsigned int", 4, "unsigned"))
FLOAT = BaseType("float", 4, "float")
BOOL = BaseType("_Bool", 1, "bool")
CHAR = BaseType("char", 1, "unsigned_char")
MODE = EnumType("Mode", 1, False, (("MODE_IDLE", 0), ("MODE_RUN", 1), ("MODE_FAULT", 2)))
SENSOR = StructType("struct", "Sensor", 32)
SENSOR.members = [Member("id", UINT8, 0), Member("mode", MODE, 1), Member("gain", UINT16, 2),
                  Member("value", FLOAT, 4), Member("samples", ArrayType(INT16, 4), 8),
                  Member("name", ArrayType(CHAR, 8), 16), Member("next", PointerType(SENSOR), 24),
                  Member("enabled", UINT32, 28, bit_size=1, bit_offset=28 * 8),
                  Member("channel", UINT32, 28, bit_size=3, bit_offset=28 * 8 + 1)]
CONFIG = StructType("struct", "Config", 20)
CONFIG.members = [Member("baud", UINT32, 0), Member("verbose", BOOL, 4), Member("primary", PointerType(SENSOR), 8),
                  Member("label", PointerType(QualifiedType("const", CHAR)), 12),
                  Member("on_tick", PointerType(FunctionType(VoidType(), (UINT32,))), 16)]
TYPES = {"struct Sensor": SENSOR, "struct Config": CONFIG, "enum Mode": MODE, "uint8_t": UINT8, "int16_t": INT16,
         "uint16_t": UINT16, "uint32_t": UINT32}


@dataclass(frozen=True)
class DemoVariable:
    """cexpr が使う変数の約束（type と place）を満たす。"""
    name: str
    type: CType
    address: int
    kind: str = "global"

    def place(self, frame: FrameView | None = None) -> MemoryPlace:
        return MemoryPlace(self.address)


# 大域変数（ELF にあるものは ELF の番地）
GLOBALS = [
    DemoVariable("table", ArrayType(UINT32, 4), 0x200000C0),
    DemoVariable("milestone", QualifiedType("volatile", UINT32), 0x200000D0),
    DemoVariable("counter", QualifiedType("volatile", UINT32), 0x200000D4),
    DemoVariable("sensors", ArrayType(SENSOR, 3), 0x20000400),
    DemoVariable("config", CONFIG, 0x20000460),
    DemoVariable("active", PointerType(SENSOR), 0x20000480),
    DemoVariable("banner", ArrayType(CHAR, 24), 0x20000490),
    DemoVariable("log_buffer", ArrayType(UINT8, 100), 0x200004B0),
]
# フレームのローカル変数（関数ごと。スタックの番地）
LOCALS = {
    "reset_handler": [DemoVariable("p", PointerType(UINT32), 0x20007FE0, "local")],
    "tick": [DemoVariable("s", PointerType(SENSOR), 0x20007FC8, "local"),
             DemoVariable("mode", MODE, 0x20007FCC, "local")],
    "reached_milestone": [DemoVariable("value", UINT32, 0x20007FB0, "parameter"),
                          DemoVariable("report", SENSOR, 0x20007F80, "local")],
}


class DemoInfo:
    """cexpr が使う DWARF の情報の約束（lookup・type_named・enumerator・address_size）。"""
    address_size = 4

    def __init__(self, function_at: Callable[[int | None], str | None]):
        self._function_at = function_at

    def lookup(self, name: str, pc: int | None = None):
        for v in LOCALS.get(self._function_at(pc), []) if pc is not None else []:
            if v.name == name:
                return v
        for v in GLOBALS:
            if v.name == name:
                return v
        raise KeyError(f"シンボル {name!r} が無い")

    def type_named(self, name: str) -> CType:
        key = " ".join(name.split())
        if key not in TYPES:
            raise KeyError(f"型 {name!r} が無い")
        return TYPES[key]

    def enumerator(self, name: str) -> tuple[EnumType, int]:
        for n, v in MODE.enumerators:
            if n == name:
                return MODE, v
        raise KeyError(f"列挙子 {name!r} が無い")


# --- RAM ------------------------------------------------------------------------------------------

class DemoMemory:
    """偽の RAM（ELF の中身で初めにうめ、デモの変数の値を置く）。RAM の外は読めない（周辺は 0）。"""

    def __init__(self, segments):
        self.ram = bytearray(RAM_SIZE)
        for s in segments:
            if RAM_BASE <= s.address < RAM_BASE + RAM_SIZE:
                self.ram[s.address - RAM_BASE:s.address - RAM_BASE + len(s.data)] = s.data
        self._fill()

    def read(self, address: int, length: int) -> bytes:
        if RAM_BASE <= address and address + length <= RAM_BASE + RAM_SIZE:
            return bytes(self.ram[address - RAM_BASE:address - RAM_BASE + length])
        if 0x40000000 <= address < 0x60000000:
            return bytes(length)                                  # 周辺（デモでは 0）
        raise NotAvailable(f"0x{address:08X} は読めない（デモの RAM の外）")

    def write(self, address: int, data: bytes) -> None:
        if not (RAM_BASE <= address and address + len(data) <= RAM_BASE + RAM_SIZE):
            raise PermissionError(f"0x{address:08X} には書けない（デモの RAM の外）")
        self.ram[address - RAM_BASE:address - RAM_BASE + len(data)] = data

    def word(self, address: int) -> int:
        return int.from_bytes(self.read(address, 4), "little")

    def set_word(self, address: int, value: int) -> None:
        self.write(address, (value & 0xFFFFFFFF).to_bytes(4, "little"))

    def _fill(self) -> None:
        names = [b"accel", b"gyro", b"temp"]
        base = _address("sensors")
        for i in range(3):
            at = base + i * SENSOR.size
            nxt = base + (i + 1) * SENSOR.size if i < 2 else 0
            self.write(at, struct.pack("<BBHf4h8sI", i + 1, (1, 1, 0)[i], 100 * (i + 1), 1.5 * (i + 1),
                                       *(10 * i + k for k in range(4)), names[i], nxt))
            self.set_word(at + 28, 1 | (i << 1))
        cfg = _address("config")
        self.write(cfg, struct.pack("<I?3xIII", 115200, True, base + SENSOR.size, _address("banner"), 0x20000041))
        self.set_word(_address("active"), base + SENSOR.size)
        self.write(_address("banner"), b"fprobe demo v1.0\0")
        self.write(_address("log_buffer"), bytes((i * 7) & 0xFF for i in range(100)))
        self.set_word(LOCALS["reset_handler"][0].address, _address("table") + 8)
        self.set_word(LOCALS["tick"][0].address, base)
        self.write(LOCALS["tick"][1].address, b"\x01")
        self.write(LOCALS["reached_milestone"][1].address, self.read(base, SENSOR.size))


def _address(name: str) -> int:
    return next(v.address for v in GLOBALS if v.name == name)


# --- 式の読み書き（fprobe.dwarf.inspector と同じ形） --------------------------------------------------

class DemoValues:
    """式・フレームの変数を、デモの RAM と型で読む・書く。function_at(pc) = その番地の関数の名前。"""

    def __init__(self, memory: DemoMemory, function_at: Callable[[int | None], str | None],
                 describe_address: Callable[[int], str | None]):
        self.memory = memory
        self.info = DemoInfo(function_at)
        self._describe = describe_address

    def _view(self, pc: int | None) -> FrameView:
        return FrameView(pc, self.memory.read, _no_register)

    def _decode(self, value, max_elements: int) -> object:
        t = value.type
        size = t.size if not value.is_bitfield else (value.bit_offset + value.bit_size + 7) // 8
        if size is None:
            return unavailable(f"大きさの分からない型 {type_name(t)}")
        try:
            data = read_place(value.place, size, EvalContext(self.memory.read, _no_register))
        except NotAvailable as e:
            return unavailable(e.reason)
        if value.is_bitfield:
            return decode_bits(t, data, value.bit_offset, value.bit_size)
        return decode(t, data, DecodeContext(self.memory.read, self._describe, max_elements))

    def read(self, expression: str, pc: int | None, max_elements: int = 64) -> dict:
        value = evaluate(expression, self.info, self._view(pc))
        out = {"expression": expression, "type": type_name(value.type)}
        if value.address is not None:
            out["address"] = value.address
        out["value"] = self._decode(value, max_elements)
        return out

    def write(self, expression: str, new_value: object, pc: int | None) -> None:
        value = evaluate(expression, self.info, self._view(pc))
        if value.address is None:
            raise ValueError(f"{expression} はメモリ上に無い（レジスタ・定数には書かない）")
        if value.is_bitfield:
            s = strip(value.type)
            number = s.value_of(new_value) if isinstance(s, EnumType) and isinstance(new_value, str) else int(new_value)
            n = value.bit_size
            if not 0 <= number < (1 << n):
                raise ValueError(f"{number} は {n} ビットのビットフィールドに入らない")
            size = (value.bit_offset + n + 7) // 8
            raw = int.from_bytes(self.memory.read(value.address, size), "little")
            mask = ((1 << n) - 1) << value.bit_offset
            raw = (raw & ~mask) | (number << value.bit_offset)
            self.memory.write(value.address, raw.to_bytes(size, "little"))
            return
        self.memory.write(value.address, encode(value.type, new_value))

    def variables(self, function: str | None, max_elements: int = 64) -> dict:
        def describe(v: DemoVariable) -> dict:
            return {"name": v.name, "type": type_name(v.type), "address": v.address,
                    "value": self._decode(_Value(v.type, MemoryPlace(v.address)), max_elements)}
        own = LOCALS.get(function or "", [])
        return {"function": function, "arguments": [describe(v) for v in own if v.kind == "parameter"],
                "locals": [describe(v) for v in own if v.kind != "parameter"]}

    def type_info(self, name: str) -> dict:
        t = self.info.type_named(name)
        s = strip(t)
        out: dict = {"name": type_name(t), "size": t.size, "kind": type(s).__name__.removesuffix("Type").lower()}
        if isinstance(s, StructType):
            out["members"] = [{"name": m.name, "type": type_name(m.type), "offset": m.offset,
                               **({"bit_size": m.bit_size, "bit_offset": m.bit_offset} if m.bit_size else {})}
                              for m in s.members]
        if isinstance(s, EnumType):
            out["enumerators"] = dict(s.enumerators)
        return out

    def names_in(self, address: int, length: int) -> list[dict]:
        """範囲の中のデモの変数の名前（memory_view の names）。"""
        return [{"address": v.address, "name": v.name} for v in GLOBALS
                if address <= v.address < address + length and v.address >= 0x20000400]

    def describe(self, address: int) -> str | None:
        for v in GLOBALS:
            size = v.type.size or 0
            if v.address <= address < v.address + size:
                return v.name if address == v.address else f"{v.name}+{address - v.address}"
        return None


@dataclass(frozen=True)
class _Value:
    type: CType
    place: MemoryPlace
    bit_offset: int | None = None
    bit_size: int | None = None

    @property
    def is_bitfield(self) -> bool:
        return False


def _no_register(number: int) -> int:
    raise NotAvailable("デモではレジスタの変数は無い")
