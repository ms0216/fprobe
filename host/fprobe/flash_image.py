"""Flash の書く中身・比較・進捗（fprobe.flash から分けた、ターゲットにも pyOCD にも依らない純粋な部品）。

- Image / Segment: 書く中身（ELF の PT_LOAD を物理番地で・Intel HEX・BIN）。重なりは誤り、隣接は 1 つにまとめる。
- compare: 中身とターゲットのメモリを比べ、違う箇所を連続した区間にまとめて返す。
- Rate: バイト数と秒。 Progress: 進捗を間引いて送り先に渡す。
公開の名前は fprobe.flash からも引ける（import 経路を保つ）。
"""

import re
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Iterable

__all__ = ["Comparison", "Difference", "Image", "Progress", "Rate", "Segment", "compare", "image_format", "save_image"]

# --- 書く中身 -------------------------------------------------------------------------------

@dataclass(frozen=True)
class Segment:
    address: int
    data: bytes

    @property
    def end(self) -> int:
        return self.address + len(self.data)


class Image:
    """書く中身: 番地の昇順、重ならない、隣接はまとめた Segment の列。"""

    def __init__(self, segments: Iterable[Segment], format: str = "", source: str = ""):
        merged: list[Segment] = []
        for seg in sorted((s for s in segments if s.data), key=lambda s: s.address):
            if merged and seg.address < merged[-1].end:
                raise ValueError(f"中身が重なっている: 0x{seg.address:08X}（前の区画は 0x{merged[-1].end:08X} まで）")
            if merged and seg.address == merged[-1].end:
                merged[-1] = Segment(merged[-1].address, merged[-1].data + seg.data)
            else:
                merged.append(Segment(seg.address, bytes(seg.data)))
        self.segments = tuple(merged)
        self.format, self.source = format, source

    @property
    def size(self) -> int:
        return sum(len(s.data) for s in self.segments)

    @property
    def start(self) -> int:
        return self.segments[0].address if self.segments else 0

    @property
    def end(self) -> int:
        return self.segments[-1].end if self.segments else 0

    def describe(self) -> dict:
        return {"source": self.source, "format": self.format, "bytes": self.size,
                "segments": [{"address": s.address, "bytes": len(s.data)} for s in self.segments]}

    @classmethod
    def load(cls, path: str | Path, address: int | None = None, format: str | None = None) -> "Image":
        """ファイルを読む。format（elf / hex / bin）は省略時に中身の先頭から決める。BIN は address が要る
        （HEX・ELF に address を渡すと誤り: 番地はファイルが持つ）。"""
        path = Path(path)
        fmt = format or _guess_format(path)
        if fmt == "elf":
            segments = _elf_segments(path)
        elif fmt == "hex":
            from intelhex import IntelHex
            ih = IntelHex(str(path))
            segments = [Segment(a, ih.tobinstr(start=a, end=b - 1)) for a, b in ih.segments()]
        elif fmt == "bin":
            if address is None:
                raise ValueError(f"BIN（{path.name}）は書く番地 address が要る")
            return cls([Segment(address, path.read_bytes())], fmt, str(path))
        else:
            raise ValueError(f"形式 {fmt!r} は読めない（elf / hex / bin）")
        if address is not None:
            raise ValueError(f"{fmt.upper()} は番地をファイルが持つ（address は BIN だけ）")
        return cls(segments, fmt, str(path))


def image_format(path: str | Path) -> str:
    """ファイルの形式（elf / hex / bin）。中身の先頭で決める（Image.load と同じ。拡張子 = 名前の慣習では決めない）。"""
    return _guess_format(Path(path))


def _guess_format(path: Path) -> str:
    """中身の先頭: ELF の印（0x7F "ELF"）なら elf、':'（Intel HEX の行の始まり）なら hex、どちらでもなければ生のバイト列
    （bin）。"""
    head = path.read_bytes()[:4]
    if head == b"\x7fELF":
        return "elf"
    if head[:1] == b":":
        return "hex"
    return "bin"


def _elf_segments(path: Path) -> list[Segment]:
    """PT_LOAD のファイルに中身のある部分を、物理番地（LMA。.data の初期値は Flash に置かれる）で。"""
    from elftools.elf.elffile import ELFFile
    with path.open("rb") as f:
        elf = ELFFile(f)
        return [Segment(seg["p_paddr"], seg.data()[:seg["p_filesz"]]) for seg in elf.iter_segments()
                if seg["p_type"] == "PT_LOAD" and seg["p_filesz"] > 0]


def save_image(path: str | Path, address: int, data: bytes) -> str:
    """読み出した中身をファイルに保存する。拡張子 .hex / .ihex は Intel HEX（番地を持つ）、ほかは BIN。形式を返す。"""
    path = Path(path)
    if _guess_format_by_suffix(path) == "hex":
        from intelhex import IntelHex
        ih = IntelHex()
        ih.puts(address, bytes(data))
        ih.write_hex_file(str(path))
        return "hex"
    path.write_bytes(bytes(data))
    return "bin"


def _guess_format_by_suffix(path: Path) -> str:
    return "hex" if path.suffix.lower() in (".hex", ".ihex", ".ihx") else "bin"


# --- 速度・比較・進捗 --------------------------------------------------------------------------

@dataclass(frozen=True)
class Rate:
    bytes: int
    seconds: float

    @property
    def bytes_per_s(self) -> float | None:
        return round(self.bytes / self.seconds, 1) if self.seconds > 0 else None

    def as_dict(self) -> dict:
        return {"bytes": self.bytes, "seconds": round(self.seconds, 6), "bytes_per_s": self.bytes_per_s}


@dataclass(frozen=True)
class Difference:
    address: int
    expected: bytes
    actual: bytes


@dataclass
class Comparison:
    bytes: int
    differing: int
    differences: list[Difference]
    rate: Rate = field(default_factory=lambda: Rate(0, 0.0))

    @property
    def match(self) -> bool:
        return self.differing == 0

    def as_dict(self, limit: int = 32) -> dict:
        return {"match": self.match, "bytes": self.bytes, "differing_bytes": self.differing,
                "differences": [{"address": d.address, "length": len(d.expected), "expected": d.expected[:64].hex(),
                                 "actual": d.actual[:64].hex()} for d in self.differences[:limit]],
                "more_differences": max(0, len(self.differences) - limit), "read": self.rate.as_dict()}


_DIFF_RUN = re.compile(rb"[^\x00]+")


def _differing_runs(expected: bytes, actual: bytes) -> list[tuple[int, int]]:
    """expected と actual で違うバイトの連続 [(始め, 終わり)]。全体を 1 つの整数の XOR にして 0 でない連続を C の正規表現で
    拾う（バイトごとの Python のループより 1 MiB で数十倍速い）。actual が短ければ読みの欠け（ValueError）。"""
    n = len(expected)
    if len(actual) < n:
        raise ValueError(f"読んだ中身が短い（{len(actual)} / {n} バイト）")
    actual = actual[:n]
    if expected == actual:
        return []
    mixed = int.from_bytes(expected, "little") ^ int.from_bytes(actual, "little")
    return [m.span() for m in _DIFF_RUN.finditer(mixed.to_bytes(n, "little"))]


def compare(image: Image, read: Callable[[int, int], bytes], progress: "Progress | None" = None) -> Comparison:
    """image とターゲットのメモリ（read(番地, 長さ)）を比べる。違うバイトの連続は 1 つの Difference にまとめる。"""
    differences: list[Difference] = []
    differing, done, started = 0, 0, time.perf_counter()
    for seg in image.segments:
        actual = bytes(read(seg.address, len(seg.data)))
        for i, j in _differing_runs(seg.data, actual):
            differences.append(Difference(seg.address + i, seg.data[i:j], actual[i:j]))
            differing += j - i
        done += len(seg.data)
        if progress is not None:
            progress.report("verify", done / image.size if image.size else 1.0)
    return Comparison(image.size, differing, differences, Rate(image.size, time.perf_counter() - started))


class Progress:
    """進捗を送り先 emit(dict) に渡す。interval_s より細かい更新は捨てる（段の始まり 0 と終わり 1 は必ず送る）。"""

    def __init__(self, emit: Callable[[dict], None] | None, operation: str, interval_s: float = 0.1,
                 clock: Callable[[], float] = time.monotonic):
        self._emit, self.operation, self._interval, self._clock = emit, operation, interval_s, clock
        self._started = clock()
        self._last: float | None = None

    def report(self, phase: str, fraction: float) -> None:
        if self._emit is None:
            return
        now = self._clock()
        fraction = min(1.0, max(0.0, fraction))
        if fraction not in (0.0, 1.0) and self._last is not None and now - self._last < self._interval:
            return
        self._last = now
        self._emit({"operation": self.operation, "phase": phase, "fraction": round(fraction, 4),
                    "elapsed_s": round(now - self._started, 3)})

    def callback(self, phase: str) -> Callable[[float], None]:
        """pyOCD の進捗の形（0..1 の小数を 1 つ受ける）。"""
        return lambda fraction: self.report(phase, fraction)
