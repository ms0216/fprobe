"""変数のライブ観測の値の読み・組み立て: 式の番地の解決（大域だけ）・サンプラの語から値の組み立て。

ターゲットにもサービスにも依らない（読み方を渡して使う）。fprobe.probe_watch が再公開する。
"""

from dataclasses import dataclass

from .dwarf.cexpr import Value
from .dwarf.info import FrameView
from .dwarf.location import NotAvailable
from .dwarf.typesys import CType
from .dwarf.values import decode, decode_bits


class _NeedsMemory(Exception):
    """式の番地を決めるのにメモリの値が要る（ポインタをたどる式）。"""


def _no_memory(address: int, length: int) -> bytes:
    raise _NeedsMemory()


def _no_register(number: int) -> int:
    raise NotAvailable("走行中の観測ではレジスタ（ローカル変数）は使えない（大域・静的の式にする）")


def _frame(read_memory) -> FrameView:
    """大域だけを引くフレーム（レジスタは使えない）。"""
    return FrameView(None, read_memory, _no_register)


def _read_value(v: Value, read_memory) -> dict:
    size = (v.bit_offset + v.bit_size + 7) // 8 if v.bit_size is not None else v.type.size
    data = read_memory(v.address, size)
    return decode_bits(v.type, data, v.bit_offset, v.bit_size) if v.bit_size is not None else decode(v.type, data)


@dataclass(frozen=True)
class _Watched:
    name: str
    address: int | None                # chase なら None（周期ごとに式から引く）
    size: int
    type: CType | None = None
    bit_offset: int | None = None
    bit_size: int | None = None
    chase: bool = False

    @property
    def words(self) -> range:
        return range(self.address // 4 * 4, self.address + self.size, 4)

    def value(self, words: dict[int, int]) -> dict:
        data = b"".join(words[w].to_bytes(4, "little") for w in self.words)
        data = data[self.address % 4:self.address % 4 + self.size]
        if self.type is None:                          # 番地の式（型が無い）: 符号なし整数（fprobe.dwarf.values の int の形）
            return {"kind": "int", "value": int.from_bytes(data, "little")}
        if self.bit_size is not None:
            return decode_bits(self.type, data, self.bit_offset, self.bit_size)
        return decode(self.type, data)


class _Assembler:
    """サンプラの語（番地, 値）から変数の値を組み立てる。語は巡回の順に読むので、変数の最後の語が来た時点で
    同じ巡回の値がそろう。"""

    def __init__(self, watched: list[_Watched]):
        self.watched = [w for w in watched if not w.chase]
        self.words: dict[int, int] = {}
        self.last = {w.name: w.words[-1] for w in self.watched}

    def feed(self, address: int, value: int) -> list[tuple[str, dict]]:
        self.words[address] = value
        return [(w.name, w.value(self.words)) for w in self.watched
                if address == self.last[w.name] and all(x in self.words for x in w.words)]
