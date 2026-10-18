"""CSR（32 ビットレジスタ）の定義モデル。

ブロック（CSR 空間の窓）> レジスタ（ワード）> フィールド（ビット範囲）。生成器はこれから
ゲートウェアのレジスタバンク・マップパッケージ・ホスト定数・Markdown 表を作る。
"""

import re
from dataclasses import dataclass, field
from typing import Literal

__all__ = ["Block", "CsrMap", "Field", "Register", "host_names"]

Access = Literal["RW", "RO", "CONST", "PULSE"]   # PULSE: 1 を書いたビットが次の周期に 1 周期だけ 1（読むと 0）
_NAME = re.compile(r"^[a-z][a-z0-9_]*$")

# VHDL-2008 の予約語（生成する識別子に使えない）
VHDL_RESERVED = frozenset("""
abs access after alias all and architecture array assert assume assume_guarantee attribute begin block
body buffer bus case component configuration constant context cover default disconnect downto else
elsif end entity exit fairness file for force function generate generic group guarded if impure in
inertial inout is label library linkage literal loop map mod nand new next nor not null of on open or
others out package parameter port postponed procedure process property protected pure range record
register reject release rem report restrict restrict_guarantee return rol ror select sequence severity
shared signal sla sll sra srl strong subtype then to transport type unaffected units until use variable
vmode vprop vunit wait when while with xnor xor
""".split())

# 生成するレジスタバンクの固定の識別子（ポート・内部信号）
BANK_IDENTIFIERS = frozenset({"clk", "rst", "bus_req", "bus_we", "bus_addr", "bus_wdata", "bus_ack",
                              "bus_err", "bus_rdata", "ack", "err", "rdata", "start", "index"})

# 手書きの CSR 関連 entity / package と衝突するブロック名（csr_<name>）
RESERVED_BLOCK_NAMES = frozenset({"decoder", "bus_pkg", "map_pkg"})


def _check_name(name: str) -> None:
    if not _NAME.match(name) or "__" in name or name.endswith("_"):
        raise ValueError(f"名前は小文字・数字・単一の _ のみ: {name!r}")


@dataclass(frozen=True)
class Field:
    name: str
    lsb: int
    width: int
    access: Access
    value: int = 0          # RW: リセット値、CONST: 値、RO: 未使用
    doc: str = ""

    def __post_init__(self):
        _check_name(self.name)
        if not (0 <= self.lsb and self.width >= 1 and self.lsb + self.width <= 32):
            raise ValueError(f"{self.name}: ビット範囲が 32 ビットを超える")
        if not 0 <= self.value < 1 << self.width:
            raise ValueError(f"{self.name}: 値 {self.value:#x} が幅 {self.width} に収まらない")

    @property
    def mask(self) -> int:
        return ((1 << self.width) - 1) << self.lsb


@dataclass(frozen=True)
class Register:
    name: str
    offset: int                         # ブロック先頭からのバイト位置（4 の倍数）
    fields: tuple[Field, ...]
    doc: str = ""
    read_strobe: bool = False           # 読まれた周期にパルス <reg>_read を出す
    # 書いた語を同じブロックのレジスタ write_to（の RW・PULSE のフィールド）に書く。読みはこのレジスタのフィールド。
    # マイコンのポートのレジスタ（書くと出力の値、読むと端子）のように、1 つの番地で「書いて読む」ためのもの
    write_to: str = ""

    def __post_init__(self):
        _check_name(self.name)
        if self.offset % 4:
            raise ValueError(f"{self.name}: offset はワード境界")
        used = 0
        for f in self.fields:
            if used & f.mask:
                raise ValueError(f"{self.name}.{f.name}: フィールドが重なる")
            used |= f.mask
        if len({f.name for f in self.fields}) != len(self.fields):
            raise ValueError(f"{self.name}: フィールド名が重複")
        if self.write_to and self.writable:
            raise ValueError(f"{self.name}: write_to のレジスタは自分に書けるフィールドを持たない")

    @property
    def writable(self) -> bool:
        """書くと値が残る・脈が出るフィールド（RW・PULSE）がある。"""
        return any(f.access in ("RW", "PULSE") for f in self.fields)

    def port_name(self, f: Field) -> str:
        """生成するレジスタバンクでのフィールドのポート名。"""
        return self.name if self.single else f"{self.name}_{f.name}"

    def identifiers(self) -> list[str]:
        """このレジスタのためにバンクに作る識別子（ポートと RW の保持信号 <port>_r）。"""
        names = []
        for f in self.fields:
            if f.access != "CONST":
                names.append(self.port_name(f))
            if f.access in ("RW", "PULSE"):
                names.append(f"{self.port_name(f)}_r")
        if self.read_strobe:
            names.append(f"{self.name}_read")
        return names

    @classmethod
    def word(cls, name: str, offset: int, access: Access, value: int = 0, doc: str = "",
             **kwargs) -> "Register":
        """32 ビット 1 フィールド（フィールド名 = レジスタ名）のレジスタ。"""
        return cls(name, offset, (Field(name, 0, 32, access, value),), doc, **kwargs)

    @property
    def single(self) -> bool:
        """フィールドがレジスタ全体の 1 つだけ（ポート名はレジスタ名だけにする）。"""
        return len(self.fields) == 1 and self.fields[0].name == self.name


@dataclass(frozen=True)
class Block:
    name: str
    base: int                           # バイトアドレス
    size: int                           # バイト数（2 の冪、base はその倍数）
    registers: tuple[Register, ...] = ()
    doc: str = ""
    external: bool = False              # レジスタバンクを生成しない窓（ROM 等を手書きで実装）

    def __post_init__(self):
        _check_name(self.name)
        if self.size < 4 or self.size & (self.size - 1) or self.base % self.size:
            raise ValueError(f"{self.name}: size は 4 以上の 2 の冪、base はその倍数")
        offsets = [r.offset for r in self.registers]
        if len(set(offsets)) != len(offsets):
            raise ValueError(f"{self.name}: レジスタの位置が重複")
        if any(o >= self.size for o in offsets):
            raise ValueError(f"{self.name}: レジスタが窓の外")
        if len({r.name for r in self.registers}) != len(self.registers):
            raise ValueError(f"{self.name}: レジスタ名が重複")
        if self.name in RESERVED_BLOCK_NAMES:
            raise ValueError(f"{self.name}: 既存の entity（csr_{self.name}）と衝突する")
        identifiers = [n for r in self.registers for n in r.identifiers()]
        for name in identifiers:
            if name in VHDL_RESERVED or name in BANK_IDENTIFIERS:
                raise ValueError(f"{self.name}: {name!r} は VHDL の予約語か生成コードの識別子と衝突する")
        clashes = {n for n in identifiers if identifiers.count(n) > 1}
        if clashes:
            raise ValueError(f"{self.name}: 生成される識別子が重複: {sorted(clashes)}")
        for reg in self.registers:
            if reg.write_to:
                target = next((r for r in self.registers if r.name == reg.write_to), None)
                if target is None or not target.writable or target.write_to:
                    raise ValueError(f"{self.name}.{reg.name}: 書き先 {reg.write_to!r} は同じブロックの書ける"
                                     "レジスタでない")

    @property
    def address_bits(self) -> int:
        """窓内のワード位置のビット数。"""
        return (self.size // 4 - 1).bit_length()


@dataclass(frozen=True)
class CsrMap:
    blocks: tuple[Block, ...] = field(default_factory=tuple)

    def __post_init__(self):
        spans = sorted((b.base, b.base + b.size, b.name) for b in self.blocks)
        for (_, end, a), (start, _, b) in zip(spans, spans[1:]):
            if start < end:
                raise ValueError(f"窓が重なる: {a} と {b}")
        if len({b.name for b in self.blocks}) != len(self.blocks):
            raise ValueError("ブロック名が重複")
        names = [n for b in self.blocks for n in host_names(b)]
        clashes = {n for n in names if names.count(n) > 1}
        if clashes:
            raise ValueError(f"ホスト定数名が重複: {sorted(clashes)}")

    def block(self, name: str) -> Block:
        return next(b for b in self.blocks if b.name == name)


def host_names(block: Block) -> list[str]:
    """ホスト定数モジュールにこのブロックが作る名前（csr/host.py と同じ規則）。"""
    prefix = block.name.upper()
    names = [f"{prefix}_BASE", f"{prefix}_SIZE"]
    for reg in block.registers:
        name = f"{prefix}_{reg.name.upper()}"
        names.append(name)
        if reg.single:
            if reg.fields[0].access == "CONST":
                names.append(f"{name}_VALUE")
        else:
            names += [f"{name}_{f.name.upper()}" for f in reg.fields]
    return names
