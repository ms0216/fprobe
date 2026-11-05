"""CMSIS-SVD（XML）を fprobe.svd_model の Device に読み込む（derivedFrom・dim・クラスタを平らに展開する）。"""

import copy
import re
import xml.etree.ElementTree as ET
from collections.abc import Iterable

from .svd_model import Device, EnumValue, Field, Peripheral, Register

_INHERITED = ("size", "access", "resetValue", "resetMask")
_NEUTRAL_ZERO = {"oneToClear", "oneToSet", "oneToToggle"}
_NEUTRAL_ONES = {"zeroToClear", "zeroToSet", "zeroToToggle"}


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


def parse_device(text: str | bytes) -> Device:
    root = ET.fromstring(text)
    _strip_namespaces(root)
    if root.tag != "device":
        raise ValueError(f"SVD ではない（根が {root.tag!r}）")
    return _Parser(root).device()
