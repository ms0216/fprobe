"""GDB に渡す記述: レジスタの並び（target.xml）、メモリマップ、スレッドの一覧の XML。機器に依らない。

レジスタの番号（regnum）は並びの位置で、g / G パケットの中の順もこれ。名前は pyOCD のコアレジスタの名前と同じ
（d0〜d15 は s の対から作る。fprobe.probe_gdb が読み書きする）。
"""

from dataclasses import dataclass
from xml.sax.saxutils import escape, quoteattr

__all__ = ["Region", "Register", "fill_gaps", "layout", "memory_map_xml", "regnum", "target_xml", "threads_xml"]

M_PROFILE = "org.gnu.gdb.arm.m-profile"
M_SYSTEM = "org.gnu.gdb.arm.m-system"
VFP = "org.gnu.gdb.arm.vfp"


@dataclass(frozen=True)
class Register:
    name: str
    bits: int
    type: str
    feature: str
    group: str | None = None


_CORE = [Register(f"r{i}", 32, "uint32", M_PROFILE, "general") for i in range(13)] + [
    Register("sp", 32, "data_ptr", M_PROFILE, "general"),
    Register("lr", 32, "uint32", M_PROFILE, "general"),
    Register("pc", 32, "code_ptr", M_PROFILE, "general"),
    Register("xpsr", 32, "uint32", M_PROFILE, "general"),
]
_SYSTEM = [Register("msp", 32, "data_ptr", M_SYSTEM, "system"), Register("psp", 32, "data_ptr", M_SYSTEM, "system")] + [
    Register(name, 32, "uint32", M_SYSTEM, "system") for name in ("primask", "basepri", "faultmask", "control")]
_FPU = [Register(f"d{i}", 64, "ieee_double", VFP, "float") for i in range(16)] + [
    Register("fpscr", 32, "uint32", VFP, "float")]


def layout(available: set[str]) -> list[Register]:
    """コアにあるレジスタ（pyOCD の名前の集合）から、GDB に見せる並び。FPU（s0〜s31・fpscr）があれば d0〜d15 も。"""
    regs = list(_CORE) + [r for r in _SYSTEM if r.name in available]
    if "fpscr" in available and all(f"s{i}" in available for i in range(32)):
        regs += _FPU
    return regs


def regnum(regs: list[Register], name: str) -> int:
    return next(i for i, r in enumerate(regs) if r.name == name)


def target_xml(regs: list[Register]) -> str:
    out = ['<?xml version="1.0"?>', '<!DOCTYPE target SYSTEM "gdb-target.dtd">', "<target>",
           "<architecture>arm</architecture>"]
    feature = None
    for n, r in enumerate(regs):
        if r.feature != feature:
            if feature is not None:
                out.append("</feature>")
            feature = r.feature
            out.append(f"<feature name={quoteattr(feature)}>")
        group = f" group={quoteattr(r.group)}" if r.group else ""
        out.append(f"<reg name={quoteattr(r.name)} bitsize=\"{r.bits}\" regnum=\"{n}\" type={quoteattr(r.type)}{group}/>")
    if feature is not None:
        out.append("</feature>")
    out.append("</target>")
    return "\n".join(out)


@dataclass(frozen=True)
class Region:
    kind: str                     # ram / rom / flash
    start: int
    length: int
    blocksize: int | None = None  # flash の消去の単位


def fill_gaps(regions: list[Region]) -> list[Region]:
    """32 ビットの空間全体を覆う区画の並び: 区画の間（周辺・システム等）を ram で埋める。メモリマップがあると GDB は
    載っていない番地を読まない（mem inaccessible-by-default）ので、周辺レジスタも読めるようにする（OpenOCD と同じ）。
    重なる区画は先のものを残す。"""
    out, at = [], 0
    for r in sorted(regions, key=lambda r: r.start):
        if r.start < at:
            continue
        if r.start > at:
            out.append(Region("ram", at, r.start - at))
        out.append(r)
        at = r.start + r.length
    if at < 1 << 32:
        out.append(Region("ram", at, (1 << 32) - at))
    return out


def memory_map_xml(regions: list[Region]) -> str:
    """GDB のメモリマップ。flash の blocksize（消去の単位）は機器の値（pyOCD の Flash の領域の情報）だけを申告する。
    分からない flash は単位を推さず（GDB はこの値で書き込みの区切りを決める）、書けない rom として申告する。"""
    out = ['<?xml version="1.0"?>',
           '<!DOCTYPE memory-map PUBLIC "+//IDN gnu.org//DTD GDB Memory Map V1.0//EN" '
           '"http://sourceware.org/gdb/gdb-memory-map.dtd">',
           "<memory-map>"]
    for r in regions:
        kind = "rom" if r.kind == "flash" and not r.blocksize else r.kind
        head = f'<memory type="{kind}" start="0x{r.start:x}" length="0x{r.length:x}"'
        if kind == "flash":
            out.append(f'{head}><property name="blocksize">0x{r.blocksize:x}</property></memory>')
        else:
            out.append(f"{head}/>")
    out.append("</memory-map>")
    return "\n".join(out)


def threads_xml(threads: list[dict]) -> str:
    """スレッドの一覧（{"id", "name", "extra"}）の qXfer:threads の XML。"""
    out = ['<?xml version="1.0"?>', "<threads>"]
    for t in threads:
        out.append(f'<thread id="{t["id"]:x}" name={quoteattr(t["name"])}>{escape(t.get("extra", ""))}</thread>')
    out.append("</threads>")
    return "\n".join(out)
