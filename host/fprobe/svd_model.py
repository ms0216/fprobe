"""CMSIS-SVD の結果の型（Device・Peripheral・Register・Field）と、レジスタの値の解読・組み立て・番地引き。

fprobe.svd が再公開する。XML の読み込みは fprobe.svd_parser。ここは XML にも探索にも依らない。
"""

import bisect
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path

from .memview import RegisterAccess

_NEUTRAL_ZERO = {"oneToClear", "oneToSet", "oneToToggle"}
_NEUTRAL_ONES = {"zeroToClear", "zeroToSet", "zeroToToggle"}


# --- 結果の形 ---------------------------------------------------------------------------------

@dataclass(frozen=True)
class EnumValue:
    name: str
    value: int | None            # isDefault なら None
    mask: int                    # 意味のあるビット（"#1x0" の x は 0）
    description: str = ""
    is_default: bool = False

    def matches(self, value: int) -> bool:
        return self.value is not None and value & self.mask == self.value


@dataclass(frozen=True)
class Field:
    name: str
    bit_offset: int
    bit_width: int
    access: str
    description: str = ""
    enums: tuple[EnumValue, ...] = ()          # 読んだ値の名前（usage: read / read-write）
    write_enums: tuple[EnumValue, ...] = ()    # 書く値の名前（usage: write / read-write）
    read_action: str | None = None             # 読むと状態が変わる（clear / set / modify / modifyExternal）
    modified_write_values: str | None = None   # oneToClear など

    @property
    def mask(self) -> int:
        return ((1 << self.bit_width) - 1) << self.bit_offset

    @property
    def bits(self) -> str:
        msb = self.bit_offset + self.bit_width - 1
        return f"[{msb}:{self.bit_offset}]" if self.bit_width > 1 else f"[{self.bit_offset}]"

    def extract(self, register_value: int) -> int:
        return (register_value & self.mask) >> self.bit_offset

    def insert(self, register_value: int, value: int) -> int:
        if not 0 <= value < 1 << self.bit_width:
            raise ValueError(f"{self.name} は {self.bit_width} ビット: {value}")
        return register_value & ~self.mask | value << self.bit_offset

    def name_of(self, value: int, *, write: bool = False) -> str | None:
        enums = (self.write_enums or self.enums) if write else (self.enums or self.write_enums)
        for e in enums:
            if e.matches(value):
                return e.name
        return next((e.name for e in enums if e.is_default), None)

    def value_of(self, name: str) -> int:
        for e in (*self.write_enums, *self.enums):
            if e.name.lower() == name.lower():
                full = (1 << self.bit_width) - 1
                if e.value is None or e.mask != full:
                    raise ValueError(f"{self.name} の {e.name} は値が 1 つに決まらない（数で指定する）")
                return e.value
        known = ", ".join(e.name for e in (*self.write_enums, *self.enums) if not e.is_default)
        raise ValueError(f"{self.name} に {name!r} は無い（{known or '列挙なし'}）")


@dataclass(frozen=True)
class FieldValue:
    field: Field
    value: int
    name: str | None             # 列挙の名前

    @property
    def bits(self) -> str:
        return self.field.bits

    def as_dict(self) -> dict:
        return {"name": self.field.name, "bits": self.bits, "value": self.value, "enum": self.name}


@dataclass(frozen=True)
class Register:
    name: str                    # "MODER" / "CH[2]" / "BUF[1].LEN"（クラスタは点つなぎ）
    peripheral: str
    address: int                 # 絶対番地
    size: int                    # ビット
    access: str
    reset_value: int
    reset_mask: int
    description: str = ""
    fields: tuple[Field, ...] = ()
    read_action: str | None = None
    alternate_of: str | None = None   # alternateRegister（同じ番地の別の見方）

    @property
    def path(self) -> str:
        return f"{self.peripheral}.{self.name}"

    @property
    def has_read_side_effects(self) -> bool:
        return self.read_action is not None or any(f.read_action for f in self.fields)

    def field(self, name: str) -> Field:
        for f in self.fields:
            if f.name.lower() == name.lower():
                return f
        raise KeyError(f"{self.path} にフィールド {name!r} は無い")

    def decode(self, value: int) -> list[FieldValue]:
        return [FieldValue(f, f.extract(value), f.name_of(f.extract(value))) for f in self.fields]

    def encode(self, current: int, values: Mapping[str, int | str]) -> int:
        """current の上で values のフィールドだけ変えた値。書き込むと効くフィールド（oneToClear 等）の
        変えない部分は、効かない値（0 / 1）にして書き戻しで消さない。"""
        out = current
        for f in self.fields:
            if f.modified_write_values in _NEUTRAL_ZERO:
                out &= ~f.mask
            elif f.modified_write_values in _NEUTRAL_ONES:
                out |= f.mask
        for key, value in values.items():
            f = self.field(key)
            if f.access == "read-only":
                raise ValueError(f"{self.path}.{f.name} は読むだけ")
            out = f.insert(out, f.value_of(value) if isinstance(value, str) else value)
        return out & ((1 << self.size) - 1)

    def as_dict(self) -> dict:
        return {"path": self.path, "name": self.name, "address": self.address, "size": self.size,
                "access": self.access, "reset_value": self.reset_value, "description": self.description,
                "read_side_effects": self.has_read_side_effects,
                "fields": [{"name": f.name, "bit_offset": f.bit_offset, "bit_width": f.bit_width, "access": f.access,
                            "description": f.description,
                            "enums": [{"name": e.name, "value": e.value} for e in f.enums or f.write_enums]}
                           for f in self.fields]}


@dataclass(frozen=True)
class Peripheral:
    name: str
    base_address: int
    description: str = ""
    group: str | None = None
    derived_from: str | None = None
    address_blocks: tuple[tuple[int, int], ...] = ()     # (絶対番地, 大きさ)
    registers: tuple[Register, ...] = ()

    def contains(self, address: int) -> bool:
        if self.address_blocks:
            return any(start <= address < start + size for start, size in self.address_blocks)
        return any(r.address <= address < r.address + r.size // 8 for r in self.registers)


@dataclass(frozen=True)
class RegisterHit:
    peripheral: Peripheral
    register: Register
    offset: int                  # レジスタの先頭からのバイト

    def __str__(self) -> str:
        return self.register.path if self.offset == 0 else f"{self.register.path}+{self.offset}"


@dataclass(frozen=True)
class RegisterValue:
    register: Register
    value: int
    fields: tuple[FieldValue, ...]

    def as_dict(self) -> dict:
        return {"path": self.register.path, "address": self.register.address, "size": self.register.size,
                "value": self.value, "fields": [f.as_dict() for f in self.fields]}


# --- デバイス -------------------------------------------------------------------------------

class Device:
    def __init__(self, name: str, description: str, cpu: dict, width: int, peripherals: tuple[Peripheral, ...]):
        self.name, self.description, self.cpu, self.width = name, description, cpu, width
        self.peripherals = peripherals
        self._peripherals = {p.name.upper(): p for p in peripherals}
        self._registers = {r.path.upper(): (p, r) for p in peripherals for r in p.registers}
        self._order = {key: i for i, key in enumerate(self._registers)}     # 定義の順（同じ番地の別名を並べる）
        spans = sorted(((r.address, r.address + max(r.size // 8, 1), p, r) for p, r in self._registers.values()),
                       key=lambda s: s[0])
        self._spans = spans
        self._span_starts = [s[0] for s in spans]
        self._widest = max((s[1] - s[0] for s in spans), default=1)

    @classmethod
    def parse(cls, text: str | bytes) -> "Device":
        from .svd_parser import parse_device       # 読み込み側がこの層の型を使うので、循環を避けて使うときに読む
        return parse_device(text)

    @classmethod
    def load(cls, path: str | Path) -> "Device":
        return cls.parse(Path(path).read_bytes())

    # --- 名前で引く --------------------------------------------------------------------------

    def peripheral(self, name: str) -> Peripheral:
        try:
            return self._peripherals[name.upper()]
        except KeyError:
            raise KeyError(f"周辺 {name!r} が {self.name} に無い") from None

    def register(self, path: str) -> Register:
        """"RCC.CR" / "TIMER0.BUF[1].LEN"（大文字小文字を区別しない）。"""
        try:
            return self._registers[path.upper()][1]
        except KeyError:
            raise KeyError(f"レジスタ {path!r} が {self.name} に無い") from None

    # --- 番地で引く --------------------------------------------------------------------------

    def registers_at(self, address: int) -> list[Register]:
        """address を含むすべてのレジスタ（同じ番地の別の見方も）。"""
        i = bisect.bisect_right(self._span_starts, address)
        out = []
        for start, end, _, r in reversed(self._spans[:i]):
            if address - start >= self._widest:
                break
            if address < end:
                out.append(r)
        return sorted(out, key=lambda r: (r.address, self._order[r.path.upper()]))

    def find(self, address: int) -> RegisterHit | None:
        hits = self.registers_at(address)
        if not hits:
            return None
        r = hits[0]
        return RegisterHit(self._registers[r.path.upper()][0], r, address - r.address)

    def peripheral_at(self, address: int) -> Peripheral | None:
        return next((p for p in self.peripherals if p.contains(address)), None)

    # --- 読み書き -----------------------------------------------------------------------------

    def read_register(self, access: RegisterAccess, path: str, *, allow_side_effects: bool = False) -> RegisterValue:
        """レジスタの幅で 1 回読み、フィールドに解読する。読むと状態が変わるレジスタは allow_side_effects が要る。"""
        r = self.register(path)
        if r.has_read_side_effects and not allow_side_effects:
            raise PermissionError(f"{r.path} は読むと状態が変わる（readAction）。読むなら allow_side_effects=True")
        value = access.read(r.address, r.size)
        return RegisterValue(r, value, tuple(r.decode(value)))

    def write_fields(self, access: RegisterAccess, path: str, values: Mapping[str, int | str], *,
                     allow_side_effects: bool = False) -> int:
        """読み・変更・書き込み。書いた値を返す。書くだけのレジスタはリセット値の上で変える。"""
        r = self.register(path)
        if r.access == "write-only" or r.access == "writeOnce":
            current = r.reset_value
        else:
            current = self.read_register(access, path, allow_side_effects=allow_side_effects).value
        value = r.encode(current, values)
        access.write(r.address, r.size, value)
        return value
