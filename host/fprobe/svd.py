"""CMSIS-SVD（周辺レジスタの定義）の読み込みと、レジスタの値の解読・組み立て。

読み込み時に平らに展開する: derivedFrom を解決し、dim（配列・リスト）を個々のレジスタにし、クラスタは
"CLUSTER.REG" の点つなぎの名前に、番地は絶対番地にする。

    dev = load_device("STM32F401RETx")             # 置き場所から探す（find_svd）
    reg = dev.register("RCC.CR")                   # Register（番地・大きさ・フィールド）
    reg.decode(0x83)                               # フィールドごとの値（列挙の名前つき）
    reg.encode(current, {"HSEON": 1})              # 読み・変更・書き込みの「変更」
    dev.find(0x40020014)                           # RegisterHit: GPIOA.ODR
    dev.read_register(TargetMemory(t), "GPIOA.MODER")   # レジスタの幅で 1 回読む

仕様: docs/superpowers/specs/2026-10-17-f2-disasm-svd-design.md
"""

import bisect
import copy
import importlib.util
import os
import re
import xml.etree.ElementTree as ET
import zipfile
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path

from .memview import RegisterAccess

__all__ = ["BUNDLED_DIR", "Device", "EnumValue", "Field", "FieldValue", "Peripheral", "Register", "RegisterHit",
           "RegisterValue", "SvdSource", "find_svd", "load_device"]

BUNDLED_DIR = Path(__file__).resolve().parent / "data" / "svd"
_INHERITED = ("size", "access", "resetValue", "resetMask")
# 書き込むと 1（0）が意味を持つフィールド: 読み・変更・書き込みで触らないフィールドには、効かない値を書く
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
        return {"path": self.register.path, "address": self.register.address, "value": self.value,
                "fields": [f.as_dict() for f in self.fields]}


# --- 数・文字 -------------------------------------------------------------------------------

def _number(text: str) -> int:
    """SVD の scaledNonNegativeInteger: 0x / 0X / # / 0b / 10 進、k・M・G の倍率。"""
    t = text.strip()
    scale = {"k": 1 << 10, "K": 1 << 10, "m": 1 << 20, "M": 1 << 20, "g": 1 << 30, "G": 1 << 30}.get(t[-1:], 1)
    if scale != 1 and not t.lower().startswith("0x"):
        t = t[:-1]
    if t[:2] in ("0x", "0X"):
        return int(t[2:], 16) * scale
    if t[:1] == "#":
        return int(t[1:], 2) * scale
    if t[:2] in ("0b", "0B"):
        return int(t[2:], 2) * scale
    return int(t, 10) * scale


def _enum_value(text: str, width: int) -> tuple[int, int]:
    """列挙の値 → (値, 意味のあるビット)。"#1x0" / "0b1x0" の x は無関係のビット。"""
    t = text.strip()
    full = (1 << width) - 1
    for prefix in ("#", "0b", "0B"):
        if t.startswith(prefix) and "x" in t.lower():
            bits = t[len(prefix):].lower()
            value = int(bits.replace("x", "0"), 2)
            mask = int("".join("0" if b == "x" else "1" for b in bits), 2)
            return value, mask & full
    return _number(t), full


def _text(el: ET.Element | None, tag: str, default: str | None = None) -> str | None:
    child = el.find(tag) if el is not None else None
    if child is None or child.text is None:
        return default
    return " ".join(child.text.split())


def _dim_instances(el: ET.Element) -> list[tuple[str, int]]:
    """dim があれば (名前, 番地・位置の増分) の列。無ければ [(名前, 0)]。"""
    name = _text(el, "name", "")
    dim = _text(el, "dim")
    if dim is None:
        return [(name, 0)]
    count, step = _number(dim), _number(_text(el, "dimIncrement", "0"))
    index = _text(el, "dimIndex")
    if index is None:
        labels = [str(i) for i in range(count)]
    elif re.fullmatch(r"\d+-\d+", index):
        a, b = map(int, index.split("-"))
        labels = [str(i) for i in range(a, b + 1)]
    elif re.fullmatch(r"[A-Z]-[A-Z]", index):
        labels = [chr(c) for c in range(ord(index[0]), ord(index[2]) + 1)]
    else:
        labels = [s.strip() for s in index.split(",")]
    if len(labels) != count:
        raise ValueError(f"{name}: dim {count} と dimIndex {index!r} の数が違う")
    out = []
    for i, label in enumerate(labels):
        text = name.replace("[%s]", f"[{label}]") if "[%s]" in name else name.replace("%s", label)
        out.append((text, i * step))
    return out


def _strip_namespaces(root: ET.Element) -> None:
    for el in root.iter():
        if isinstance(el.tag, str) and "}" in el.tag:
            el.tag = el.tag.split("}", 1)[1]


def _merge(base: ET.Element, derived: ET.Element) -> ET.Element:
    """derivedFrom: 元の写しに、派生側に書いた要素を重ねる。registers / fields / enumeratedValues の中身は名前ごとに。"""
    out = copy.deepcopy(base)
    out.attrib.pop("derivedFrom", None)
    for key, value in derived.attrib.items():
        if key != "derivedFrom":
            out.set(key, value)
    for tag in dict.fromkeys(child.tag for child in derived):
        kids = derived.findall(tag)
        if tag in ("registers", "fields"):
            target = out.find(tag)
            if target is None:
                target = ET.SubElement(out, tag)
            for kid in kids[0]:
                same = next((c for c in target if c.tag == kid.tag and _text(c, "name") == _text(kid, "name")), None)
                if same is not None:
                    target.remove(same)
                target.append(copy.deepcopy(kid))
        else:
            for old in out.findall(tag):
                out.remove(old)
            for kid in kids:
                out.append(copy.deepcopy(kid))
    return out


# --- 読み込み ---------------------------------------------------------------------------------

class _Parser:
    def __init__(self, root: ET.Element):
        self.root = root
        self.raw_peripherals = {_text(p, "name"): p for p in root.iter("peripheral")}
        self.enum_sets = {}                                     # 名前 → enumeratedValues（derivedFrom の元）
        for e in root.iter("enumeratedValues"):
            name = _text(e, "name")
            if name and e.get("derivedFrom") is None:
                self.enum_sets.setdefault(name, e)
        self._resolved: dict[str, ET.Element] = {}

    def defaults(self, el: ET.Element, inherited: dict[str, str]) -> dict[str, str]:
        out = dict(inherited)
        for tag in _INHERITED:
            value = _text(el, tag)
            if value is not None:
                out[tag] = value
        return out

    def peripheral_element(self, name: str, seen: frozenset = frozenset()) -> ET.Element:
        if name in self._resolved:
            return self._resolved[name]
        el = self.raw_peripherals.get(name)
        if el is None:
            raise ValueError(f"derivedFrom の元の周辺 {name!r} が無い")
        source = el.get("derivedFrom")
        if source:
            if source in seen:
                raise ValueError(f"derivedFrom が循環している: {name}")
            el = _merge(self.peripheral_element(source, seen | {name}), el)
        self._resolved[name] = el
        return el

    def find_path(self, path: str) -> ET.Element:
        """"PERIPH.REG" / "PERIPH.CLUSTER.REG" の元の要素。"""
        first, *rest = path.split(".")
        node = self.peripheral_element(first)
        for part in rest:
            container = node.find("registers") if node.tag == "peripheral" else node
            node = next((c for c in (container if container is not None else [])
                         if c.tag in ("register", "cluster") and _text(c, "name") == part), None)
            if node is None:
                raise ValueError(f"derivedFrom の元 {path!r} が無い")
        return node

    def resolve_child(self, el: ET.Element, siblings: Iterable[ET.Element]) -> ET.Element:
        source = el.get("derivedFrom")
        if not source:
            return el
        base = next((s for s in siblings if s is not el and s.tag == el.tag and _text(s, "name") == source), None)
        if base is None:
            base = self.find_path(source)
        return _merge(self.resolve_child(base, siblings) if base.get("derivedFrom") else base, el)

    def device(self) -> "Device":
        root = self.root
        defaults = self.defaults(root, {"size": "32", "access": "read-write", "resetValue": "0",
                                        "resetMask": "0xFFFFFFFF"})
        cpu_el = root.find("cpu")
        cpu = {child.tag: (child.text or "").strip() for child in cpu_el} if cpu_el is not None else {}
        peripherals = []
        for raw in root.find("peripherals") if root.find("peripherals") is not None else []:
            if raw.tag != "peripheral":
                continue
            el = self.peripheral_element(_text(raw, "name"))
            for name, step in _dim_instances(el):
                peripherals.append(self.peripheral(el, name, step, defaults, raw.get("derivedFrom")))
        return Device(_text(root, "name", ""), _text(root, "description", ""), cpu,
                      int(_text(root, "width", "32")), tuple(peripherals))

    def peripheral(self, el: ET.Element, name: str, step: int, inherited: dict[str, str],
                   derived_from: str | None) -> Peripheral:
        base = _number(_text(el, "baseAddress")) + step
        props = self.defaults(el, inherited)
        blocks = tuple((base + _number(_text(b, "offset", "0")), _number(_text(b, "size", "0")))
                       for b in el.findall("addressBlock"))
        registers: list[Register] = []
        container = el.find("registers")
        if container is not None:
            self.registers(container, base, props, "", name, registers)
        return Peripheral(name, base, _text(el, "description", ""), _text(el, "groupName"), derived_from,
                          blocks, tuple(registers))

    def registers(self, container: ET.Element, base: int, inherited: dict[str, str], prefix: str,
                  peripheral: str, out: list[Register]) -> None:
        siblings = list(container)
        for child in siblings:
            if child.tag not in ("register", "cluster"):
                continue
            el = self.resolve_child(child, siblings)
            props = self.defaults(el, inherited)
            offset = _number(_text(el, "addressOffset", "0"))
            for name, step in _dim_instances(el):
                address = base + offset + step
                if el.tag == "cluster":
                    self.registers(el, address, props, f"{prefix}{name}.", peripheral, out)
                else:
                    out.append(self.register(el, prefix + name, address, props, peripheral))

    def register(self, el: ET.Element, name: str, address: int, props: dict[str, str], peripheral: str) -> Register:
        size = _number(props["size"])
        access = props["access"]
        fields: list[Field] = []
        container = el.find("fields")
        if container is not None:
            siblings = list(container)
            for child in siblings:
                if child.tag == "field":
                    fields.extend(self.fields(self.resolve_child(child, siblings), access))
        return Register(name, peripheral, address, size, access, _number(props["resetValue"]),
                        _number(props["resetMask"]), _text(el, "description", ""), tuple(fields),
                        _text(el, "readAction"), _text(el, "alternateRegister"))

    def fields(self, el: ET.Element, register_access: str) -> list[Field]:
        if _text(el, "bitRange") is not None:
            msb, lsb = map(int, re.fullmatch(r"\[(\d+):(\d+)\]", _text(el, "bitRange")).groups())
            offset, width = lsb, msb - lsb + 1
        elif _text(el, "lsb") is not None:
            offset = int(_number(_text(el, "lsb")))
            width = int(_number(_text(el, "msb"))) - offset + 1
        else:
            offset, width = _number(_text(el, "bitOffset", "0")), _number(_text(el, "bitWidth", "1"))
        read_enums: list[EnumValue] = []
        write_enums: list[EnumValue] = []
        for values in el.findall("enumeratedValues"):
            if values.get("derivedFrom"):
                source = self.enum_sets.get(values.get("derivedFrom").split(".")[-1])
                if source is None:
                    raise ValueError(f"enumeratedValues の derivedFrom の元 {values.get('derivedFrom')!r} が無い")
                values = _merge(source, values)
            usage = _text(values, "usage", "read-write")
            parsed = []
            for ev in values.findall("enumeratedValue"):
                default = (_text(ev, "isDefault", "false") or "").lower() in ("true", "1")
                raw = _text(ev, "value")
                value, mask = _enum_value(raw, width) if raw is not None else (None, 0)
                parsed.append(EnumValue(_text(ev, "name", ""), value, mask, _text(ev, "description", ""),
                                        default or raw is None))
            if usage in ("read", "read-write"):
                read_enums.extend(parsed)
            if usage in ("write", "read-write"):
                write_enums.extend(parsed)
        out = []
        for name, step in _dim_instances(el):
            out.append(Field(name, offset + step, width, _text(el, "access", register_access),
                             _text(el, "description", ""), tuple(read_enums), tuple(write_enums),
                             _text(el, "readAction"), _text(el, "modifiedWriteValues")))
        return out


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
        root = ET.fromstring(text)
        _strip_namespaces(root)
        if root.tag != "device":
            raise ValueError(f"SVD ではない（根が {root.tag!r}）")
        return _Parser(root).device()

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


# --- SVD を探す -----------------------------------------------------------------------------

@dataclass(frozen=True)
class SvdSource:
    name: str                    # 合ったファイルの名前（拡張子なし）
    origin: str                  # "explicit" / "env" / "user" / "bundled" / "pyocd"
    path: Path                   # ファイル（pyocd なら zip）
    member: str | None = None    # zip の中の名前

    def read(self) -> bytes:
        if self.member is None:
            return self.path.read_bytes()
        with zipfile.ZipFile(self.path) as z:
            return z.read(self.member)


def _match_length(stem: str, device: str) -> int:
    """ファイル名（拡張子なし）がデバイス名の先頭に合えばその長さ、合わなければ 0。小文字の x は任意の 1 文字。"""
    if len(stem) > len(device):
        return 0
    for s, d in zip(stem, device):
        if s != "x" and s.upper() != d.upper():
            return 0
    return len(stem)


def _best(names: Iterable[str], device: str) -> str | None:
    scored = [(_match_length(Path(n).stem, device), n) for n in names
              if Path(n).suffix.lower() in (".svd", ".xml")]
    scored = [s for s in scored if s[0] > 0]
    return max(scored, key=lambda s: s[0])[1] if scored else None


def _pyocd_zip() -> Path | None:
    spec = importlib.util.find_spec("pyocd")
    if spec is None or not spec.submodule_search_locations:
        return None
    path = Path(next(iter(spec.submodule_search_locations))) / "debug" / "svd" / "svd_data.zip"
    return path if path.is_file() else None


def _search_dirs(paths: Sequence[str | Path]) -> list[tuple[str, Path]]:
    dirs = [("explicit", Path(p)) for p in paths]
    dirs += [("env", Path(p)) for p in os.environ.get("FPROBE_SVD_PATH", "").split(os.pathsep) if p]
    dirs += [("user", Path.home() / ".fprobe" / "svd"), ("bundled", BUNDLED_DIR)]
    return dirs


def find_svd(device: str, *, paths: Sequence[str | Path] = ()) -> SvdSource | None:
    """デバイス名（"STM32F401RETx" など）に合う SVD を探す。探す順は data/svd/README.md。"""
    for origin, place in _search_dirs(paths):
        if origin == "explicit" and place.is_file():
            return SvdSource(place.stem, origin, place)
        if place.is_dir():
            best = _best((p.name for p in place.iterdir() if p.is_file()), device)
            if best is not None:
                return SvdSource(Path(best).stem, origin, place / best)
    archive = _pyocd_zip()
    if archive is not None:
        with zipfile.ZipFile(archive) as z:
            best = _best(z.namelist(), device)
        if best is not None:
            return SvdSource(Path(best).stem, "pyocd", archive, best)
    return None


def load_device(device: str, *, paths: Sequence[str | Path] = ()) -> Device:
    source = find_svd(device, paths=paths)
    if source is None:
        places = [str(p) for _, p in _search_dirs(paths)] + ["pyOCD の内蔵"]
        raise FileNotFoundError(f"{device} の SVD が見つからない（探した場所: {', '.join(places)}）")
    return Device.parse(source.read())
