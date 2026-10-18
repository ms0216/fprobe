"""CoreSight の識別子の解読と、部品の判別（U29）。機器に触らない（読む関数を受け取る）。

- DPIDR / JTAG の IDCODE / TARGETID（DPv2）・AP の IDR・部品の CIDR / PIDR・CPUID を解読する。
- ROM テーブルをたどって部品（SCS・DWT・ITM・ETM 等）を並べる（ADIv5 の class 1 と、ADIv6 / CoreSight の class 9）。
- 部品の判別: STM32 は DBGMCU の IDCODE（DEV_ID）、nRF52 は FICR の INFO.PART、RP2040 / RP2350 は SYSINFO の CHIP_ID。
  pyOCD のターゲット名の候補を返す（実在する名前だけ。複数なら利用者が選ぶ）。

読む関数 read(address) -> int は失敗（フォールト等）で例外を投げる。判別は失敗を「その種類ではない」とみなして続ける。
"""

from collections.abc import Callable
from dataclasses import dataclass, field

__all__ = ["CORE_NAMES", "Component", "Part", "STM32_PARTS", "StmPart", "candidate_targets", "decode_ap_idr",
           "decode_cpuid", "decode_dpidr", "designer11", "decode_idcode", "decode_targetid", "designer_name", "identify_part",
           "read_component", "walk_rom_table"]

Read = Callable[[int], int]

# JEP106 の (継続コードの数, ID)。確かなものだけ
_DESIGNERS = {(4, 0x3B): "Arm", (0, 0x20): "STMicroelectronics", (2, 0x44): "Nordic Semiconductor",
              (9, 0x13): "Raspberry Pi", (0, 0x15): "NXP", (0, 0x17): "Texas Instruments", (0, 0x1F): "Microchip (Atmel)"}
ARM = (4, 0x3B)
ST = (0, 0x20)
NORDIC = (2, 0x44)
RASPBERRY_PI = (9, 0x13)


def designer_name(designer: tuple[int, int]) -> str:
    return _DESIGNERS.get(designer, f"JEP106 {designer[0]}:0x{designer[1]:02X}")


def designer11(value: int) -> tuple[int, int]:
    """11 ビットの設計者（上位 4 ビット = 継続コードの数、下位 7 ビット = ID）。"""
    return (value >> 7) & 0xF, value & 0x7F


def _designer_dict(designer: tuple[int, int]) -> dict:
    return {"designer": designer_name(designer), "designer_code": (designer[0] << 8) | designer[1]}


def decode_dpidr(value: int) -> dict:
    """SW-DP / JTAG-DP の DPIDR。version 0 は DPv0（SWD 無し）、1 = DPv1、2 = DPv2（TARGETID・複数ドロップ）、3 = DPv3（ADIv6）。"""
    designer = designer11((value >> 1) & 0x7FF)
    return {"value": value, "valid": bool(value & 1), **_designer_dict(designer), "version": (value >> 12) & 0xF,
            "min": bool(value >> 16 & 1), "partno": (value >> 20) & 0xFF, "revision": (value >> 28) & 0xF,
            "adi": "ADIv6" if (value >> 12) & 0xF >= 3 else "ADIv5"}


def decode_idcode(value: int) -> dict:
    """JTAG の IDCODE（IEEE 1149.1）。"""
    designer = designer11((value >> 1) & 0x7FF)
    return {"value": value, "valid": bool(value & 1), **_designer_dict(designer), "partno": (value >> 12) & 0xFFFF,
            "version": (value >> 28) & 0xF}


def decode_targetid(value: int) -> dict:
    """DPv2 の TARGETID（複数ドロップの TARGETSEL に使う値の元）。"""
    designer = designer11((value >> 1) & 0x7FF)
    return {"value": value, **_designer_dict(designer), "partno": (value >> 12) & 0xFFFF,
            "revision": (value >> 28) & 0xF}


_AP_TYPES = {0: "JTAG-AP / COM-AP", 1: "AHB3", 2: "APB2/3", 4: "AXI3/4", 5: "AHB5", 6: "APB4/5", 7: "AXI5",
             8: "AHB5 (HPROT)"}


def decode_ap_idr(value: int) -> dict:
    """ADIv5 の AP の IDR（0 なら AP が無い）。class 8 = MEM-AP。"""
    designer = designer11((value >> 17) & 0x7FF)
    cls = (value >> 13) & 0xF
    ap_type = value & 0xF
    kind = "none" if value == 0 else "mem-ap" if cls == 8 else "jtag-ap" if cls == 0 and ap_type == 0 else "other"
    return {"value": value, "present": value != 0, "kind": kind, "class": cls, "type": ap_type,
            "bus": _AP_TYPES.get(ap_type, f"type {ap_type}") if cls == 8 else None, "variant": (value >> 4) & 0xF,
            "revision": (value >> 28) & 0xF, **_designer_dict(designer)}


CORE_NAMES = {0xC20: "Cortex-M0", 0xC21: "Cortex-M1", 0xC23: "Cortex-M3", 0xC24: "Cortex-M4", 0xC27: "Cortex-M7",
              0xC60: "Cortex-M0+", 0xD20: "Cortex-M23", 0xD21: "Cortex-M33", 0xD22: "Cortex-M55", 0xD23: "Cortex-M85",
              0xC07: "Cortex-A7", 0xB76: "ARM1176"}


def decode_cpuid(value: int) -> dict:
    """SCB の CPUID（0xE000ED00）。"""
    partno = (value >> 4) & 0xFFF
    implementer = value >> 24
    return {"value": value, "implementer": implementer, "partno": partno,
            "core": CORE_NAMES.get(partno, f"partno 0x{partno:03X}") if implementer == 0x41 else f"implementer 0x{implementer:02X}",
            "variant": (value >> 20) & 0xF, "revision": value & 0xF, "architecture": (value >> 16) & 0xF}


# --- 部品（ROM テーブル） ---------------------------------------------------------------------

CLASS_ROM, CLASS_CORESIGHT, CLASS_GENERIC = 0x1, 0x9, 0xE
_PREAMBLE = 0xB105000D
_ROM_DEVARCH = 0x47700AF7                     # class 9 の ROM テーブル
_DEVARCH = 0xFBC
_ARM_PARTS = {0x000: "SCS (Cortex-M3)", 0x001: "ITM", 0x002: "DWT", 0x003: "FPB", 0x008: "SCS (Cortex-M0)",
              0x00A: "DWT (Cortex-M0)", 0x00B: "BPU (Cortex-M0)", 0x00C: "SCS (Cortex-M4)", 0x4C0: "ROM table (Cortex-M0+)",
              0x4C3: "ROM table (Cortex-M3)", 0x4C4: "ROM table (Cortex-M4)", 0x906: "CTI", 0x907: "ETB", 0x908: "Funnel",
              0x912: "TPIU", 0x913: "ITM", 0x914: "SWO", 0x923: "TPIU (Cortex-M3)", 0x924: "ETM (Cortex-M3)",
              0x925: "ETM (Cortex-M4)", 0x9A1: "TPIU (Cortex-M4)"}
_SCS_PARTS = {0x000, 0x008, 0x00C}


@dataclass
class Component:
    address: int
    cls: int
    designer: tuple[int, int]
    part: int
    revision: int
    devarch: int | None = None
    children: list["Component"] = field(default_factory=list)
    error: str | None = None

    @property
    def name(self) -> str:
        if self.error:
            return "unreadable"
        if self.is_rom_table:
            return "ROM table"
        if self.designer == ARM and self.part in _ARM_PARTS:
            return _ARM_PARTS[self.part]
        return f"{designer_name(self.designer)} part 0x{self.part:03X}"

    @property
    def is_rom_table(self) -> bool:
        return self.cls == CLASS_ROM or (self.cls == CLASS_CORESIGHT and self.devarch == _ROM_DEVARCH)

    @property
    def is_scs(self) -> bool:
        return self.designer == ARM and self.part in _SCS_PARTS

    def as_dict(self) -> dict:
        out = {"address": self.address, "name": self.name, "class": self.cls, "part": self.part,
               **_designer_dict(self.designer), "revision": self.revision}
        if self.devarch is not None:
            out["devarch"] = self.devarch
        if self.error:
            out["error"] = self.error
        if self.children:
            out["children"] = [c.as_dict() for c in self.children]
        return out

    def flatten(self) -> list["Component"]:
        return [self] + [d for c in self.children for d in c.flatten()]


def read_component(read: Read, base: int) -> Component:
    """部品の 4 KB の窓の末尾（CIDR・PIDR・DEVARCH）を読む。"""
    try:
        cidr = [read(base + 0xFF0 + 4 * i) & 0xFF for i in range(4)]
        pidr = [read(base + 0xFE0 + 4 * i) & 0xFF for i in range(4)] + [read(base + 0xFD0) & 0xFF]
    except Exception as e:
        return Component(base, 0, (0, 0), 0, 0, error=str(e) or type(e).__name__)
    preamble = cidr[0] | (cidr[1] & 0x0F) << 8 | cidr[2] << 16 | cidr[3] << 24
    cls = cidr[1] >> 4
    if preamble != _PREAMBLE:
        return Component(base, cls, (0, 0), 0, 0, error=f"CIDR の前置きが違う（0x{preamble:08X}）")
    designer = (pidr[4] & 0xF, (pidr[1] >> 4) | (pidr[2] & 0x7) << 4)
    part = pidr[0] | (pidr[1] & 0xF) << 8
    devarch = None
    if cls == CLASS_CORESIGHT:
        try:
            devarch = read(base + _DEVARCH)
        except Exception:
            devarch = None
    return Component(base, cls, designer, part, pidr[2] >> 4, devarch)


def walk_rom_table(read: Read, base: int, depth: int = 4, limit: int = 128) -> Component:
    """base の部品を読み、ROM テーブルなら入口をたどる（深さ depth、全体で limit 個まで）。"""
    seen: set[int] = set()

    def visit(address: int, level: int) -> Component:
        seen.add(address)
        comp = read_component(read, address)
        if comp.error or not comp.is_rom_table or level >= depth:
            return comp
        entries = 960 if comp.cls == CLASS_ROM else 512
        for i in range(entries):
            if len(seen) >= limit:
                break
            try:
                entry = read(address + 4 * i)
            except Exception as e:
                comp.error = f"入口 {i} を読めない: {e}"
                break
            if entry == 0:
                break
            present = entry & 1 if comp.cls == CLASS_ROM else (entry & 3) == 3
            if not present:
                continue
            offset = entry & 0xFFFFF000
            if offset & 0x80000000:
                offset -= 1 << 32
            child = (address + offset) & 0xFFFFFFFF
            if child in seen:
                continue
            comp.children.append(visit(child, level + 1))
        return comp

    return visit(base & 0xFFFFF000, 0)


# --- 部品の判別 ----------------------------------------------------------------------------------

@dataclass(frozen=True)
class StmPart:
    name: str
    option_family: str | None = None        # stm32_option_bytes の系列（f4 / l4 / g4 / g0）
    targets: tuple[str, ...] = ()           # pyOCD のターゲット名の候補


# DBGMCU の IDCODE の DEV_ID（参照マニュアルの DBGMCU の章）
STM32_PARTS = {
    0x410: StmPart("STM32F10x medium-density"), 0x412: StmPart("STM32F10x low-density"),
    0x414: StmPart("STM32F10x high-density", targets=("stm32f103rc",)), 0x418: StmPart("STM32F105/107"),
    0x430: StmPart("STM32F10x XL-density"), 0x420: StmPart("STM32F100 value line"),
    0x411: StmPart("STM32F2"),
    0x413: StmPart("STM32F405/407/415/417", "f4"), 0x419: StmPart("STM32F42x/43x", "f4",
                                                                ("stm32f429xg", "stm32f429xi", "stm32f439xg", "stm32f439xi")),
    0x423: StmPart("STM32F401xB/C", "f4"), 0x433: StmPart("STM32F401xD/E", "f4", ("stm32f401xe",)),
    0x431: StmPart("STM32F411xC/E", "f4"), 0x421: StmPart("STM32F446", "f4"),
    0x441: StmPart("STM32F412", "f4", ("stm32f412xe", "stm32f412xg")), 0x458: StmPart("STM32F410", "f4"),
    0x463: StmPart("STM32F413/423", "f4"), 0x434: StmPart("STM32F469/479"),
    0x449: StmPart("STM32F74x/75x"), 0x451: StmPart("STM32F76x/77x", targets=("stm32f767zi",)),
    0x452: StmPart("STM32F72x/73x"),
    0x450: StmPart("STM32H74x/75x", targets=("stm32h743xx", "stm32h750xx")),
    0x480: StmPart("STM32H7A3/7B3/7B0", targets=("stm32h7b0xx",)), 0x483: StmPart("STM32H72x/73x", targets=("stm32h723xx",)),
    0x440: StmPart("STM32F05x", targets=("stm32f051",)), 0x444: StmPart("STM32F03x"), 0x445: StmPart("STM32F04x"),
    0x448: StmPart("STM32F07x"), 0x442: StmPart("STM32F09x"),
    0x422: StmPart("STM32F302xB/C / F303xB/C"), 0x432: StmPart("STM32F37x"), 0x438: StmPart("STM32F303x6/8"),
    0x446: StmPart("STM32F303xD/E"),
    0x460: StmPart("STM32G07x/08x", "g0"), 0x466: StmPart("STM32G03x/04x", "g0"), 0x467: StmPart("STM32G0B1/0C1", "g0"),
    0x456: StmPart("STM32G05x/06x", "g0"),
    0x468: StmPart("STM32G431/441", "g4"), 0x469: StmPart("STM32G47x/48x", "g4"), 0x479: StmPart("STM32G491/4A1", "g4"),
    0x415: StmPart("STM32L47x/48x", "l4", ("stm32l475xc", "stm32l475xe", "stm32l475xg")),
    0x435: StmPart("STM32L43x/44x", "l4", ("stm32l432kc",)), 0x462: StmPart("STM32L45x/46x", "l4"),
    0x461: StmPart("STM32L49x/4Ax", "l4"), 0x470: StmPart("STM32L4R/4S", "l4"), 0x464: StmPart("STM32L41x/42x", "l4"),
    0x417: StmPart("STM32L05x/06x"), 0x425: StmPart("STM32L031/041", targets=("stm32l031x6",)),
    0x447: StmPart("STM32L07x/08x"), 0x457: StmPart("STM32L01x/02x"),
}
# DBGMCU の IDCODE の番地（系列で違う）
_DBGMCU = (0xE0042000, 0x40015800, 0x5C001000)
_NRF_INFO_PART = 0x10000100
_NRF52 = {0x52805, 0x52810, 0x52811, 0x52820, 0x52832, 0x52833, 0x52840}
_RP_CHIP_ID = 0x40000000
_RP_PARTS = {0x0002: ("RP2040", ("rp2040",)), 0x0004: ("RP2350", ("rp2350",))}


@dataclass
class Part:
    vendor: str
    name: str
    source: str                             # 何を読んで分かったか
    id: int
    candidates: list[str] = field(default_factory=list)   # pyOCD のターゲット名（実在するもの）
    option_family: str | None = None

    @property
    def suggested_target(self) -> str | None:
        return self.candidates[0] if len(self.candidates) == 1 else None

    def as_dict(self) -> dict:
        out = {"vendor": self.vendor, "name": self.name, "source": self.source, "id": self.id,
               "candidates": self.candidates, "suggested_target": self.suggested_target}
        if self.option_family:
            out["option_family"] = self.option_family
        return out


def candidate_targets(names) -> list[str]:
    """pyOCD に実在するターゲット名だけ（fprobe が足す名前を含む）。"""
    from pyocd.target import TARGET

    from .pyocd_targets import register
    register()
    return [n for n in names if n in TARGET]


def _try(read: Read, address: int) -> int | None:
    try:
        return read(address)
    except Exception:
        return None


def identify_part(read: Read, designers: set[tuple[int, int]] = frozenset()) -> Part | None:
    """部品を判別する。designers は ROM テーブル・TARGETID で見えた設計者（読む順の手がかり。空でもすべて試す）。"""
    probes = [_identify_stm32, _identify_nrf, _identify_rp]
    preferred = {ST: _identify_stm32, NORDIC: _identify_nrf, RASPBERRY_PI: _identify_rp}
    order = [preferred[d] for d in designers if d in preferred]
    for probe in order + [p for p in probes if p not in order]:
        part = probe(read)
        if part is not None:
            return part
    return None


def _identify_stm32(read: Read) -> Part | None:
    for address in _DBGMCU:
        value = _try(read, address)
        if value is None or value in (0, 0xFFFFFFFF):
            continue
        dev_id = value & 0xFFF
        part = STM32_PARTS.get(dev_id)
        if part is not None:
            return Part("STMicroelectronics", part.name, f"DBGMCU_IDCODE (0x{address:08X}) = 0x{value:08X}", dev_id,
                        candidate_targets(part.targets), part.option_family)
    return None


def _identify_nrf(read: Read) -> Part | None:
    value = _try(read, _NRF_INFO_PART)
    if value in _NRF52:
        name = f"nRF{value:X}"
        return Part("Nordic Semiconductor", name, f"FICR.INFO.PART = 0x{value:X}", value,
                    candidate_targets([f"nrf{value:x}"]) or candidate_targets(["nrf52"]))
    return None


def _identify_rp(read: Read) -> Part | None:
    value = _try(read, _RP_CHIP_ID)
    if value is None or value & 0xFFF != 0x927:
        return None
    known = _RP_PARTS.get((value >> 12) & 0xFFFF)
    if known is None:
        return None
    name, targets = known
    return Part("Raspberry Pi", name, f"SYSINFO.CHIP_ID = 0x{value:08X}", (value >> 12) & 0xFFFF,
                candidate_targets(targets))
