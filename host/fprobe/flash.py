"""Flash の操作の部品（プローブにも MCP にも依らない。pyOCD のターゲットの Flash の記述と書き込みアルゴリズムを使う）。

- Image: 書く中身（ELF の PT_LOAD を物理番地で・Intel HEX・BIN）。重なりは誤り、隣接は 1 つにまとめる。
- compare: 中身とターゲットのメモリを比べ、違う箇所を連続した区間にまとめて返す（検証・ファイルとの比較）。
- Progress: 進捗（operation・phase・fraction・elapsed_s）を間引いて送り先に渡す。段の始まりと終わりは必ず送る。
- FlashMemory: ターゲットの Flash。構成（セクタ・ページ）、読み出し、消去（範囲・全体・mass）、書き込み（pyOCD の
  FlashBuilder で、同じ中身のページは書かない smart、セクタの書かない部分を保つ keep_unwritten）、検証。
  ページ単位の口（U28 の Flash ブレークポイント用）: sector_at / page_at、erase_sector / program_page（低水準）、
  patch（数バイトの書き換え。セクタを読み・消し・書き戻す。既定でコアのレジスタとアルゴリズムが使う RAM を保存・復元し、
  走っていたら走らせ直す）。
- add_flm_region: CMSIS の書き込みアルゴリズム（.FLM）で Flash の領域を足す（外付けの QSPI NOR 等）。

速度: 読み出し・消去・書き込み・検証はどれも Rate（バイト数と秒）を返す。書き込みは pyOCD が段ごとに測った時間
（消去・書き込み・中身の比較）を ProgramReport にまとめる。

pyOCD の Flash の操作はコアで書き込みアルゴリズムを走らせる（RAM に命令を置き、レジスタを使う）。止まっている
プログラムの状態を保ちたいとき（デバッグ中の書き換え）は patch(preserve=True) を使う。
"""

import contextlib
import functools
import re
import time
import zlib
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Iterable

__all__ = ["Block", "Comparison", "Difference", "FlashMemory", "Image", "image_format", "ProgramReport", "Progress", "Rate",
           "RegisterGuard", "Segment", "add_flm_region", "compare", "crc32", "remove_region", "save_image"]

READ_CHUNK = 0x4000            # 読み出しの 1 回の大きさ（進捗の細かさ。DAP の転送は pyOCD がさらに分ける）
FLM_ATTRIBUTE = "fprobe_flm"   # add_flm_region で足した領域の印（FlashRegion.attributes）

# 書き込みアルゴリズムが壊すコアの状態（patch の preserve で保存・復元する）。sp は msp / psp の別名なので戻さない
CONTEXT_REGISTERS = (*(f"r{i}" for i in range(13)), "lr", "pc", "xpsr", "msp", "psp",
                     "primask", "basepri", "faultmask", "control")
ANALYZER_SIZE = 0x600          # pyOCD の CRC 解析のコードが analyzer_address から使う大きさ


_GUARD_OPS = {"write": ("address", "mask", "value"), "wait": ("address", "mask", "value"),
              "restore": ("address", "mask"), "wait_restored": ("address", "mask")}


class RegisterGuard:
    """書き込みアルゴリズムの間だけチップのレジスタを決めた状態にし、抜けるときに戻す手順（使う側が渡す。本体は機器
    固有の守りを持たない。例: STM32F2 / F4 / F7 のクロックはプラグイン fprobe.plugins.stm32_flash_clock）。
    {"enter": [手順], "exit": [手順]}。手順（op）:
    - write {address, mask, value}: 読んで mask のビットを value にして書く（mask = 0xFFFFFFFF なら value をそのまま）
    - wait {address, mask, value}: (読んだ値 & mask) == value になるまで読む（tries 回で終わらなければ誤り）
    - restore {address, mask}: enter の前に読んでおいた値の mask のビットに戻す
    - wait_restored {address, mask}: (読んだ値 & mask) == (enter の前の値 & mask) になるまで読む
    restore・wait_restored の番地は enter の前に読んで覚える。"""

    def __init__(self, enter: list[dict], exit: list[dict], tries: int = 1000):
        self.enter_steps, self.exit_steps, self.tries = enter, exit, tries
        self.saved_addresses = sorted({s["address"] for s in enter + exit if s["op"] in ("restore", "wait_restored")})

    @classmethod
    def from_dict(cls, spec: dict) -> "RegisterGuard":
        if not isinstance(spec, dict) or set(spec) - {"enter", "exit"}:
            raise ValueError(f"守りは {{enter, exit}}: {spec!r}")
        steps = {}
        for part in ("enter", "exit"):
            steps[part] = []
            for step in spec.get(part) or []:
                need = _GUARD_OPS.get(step.get("op")) if isinstance(step, dict) else None
                if need is None or set(step) != {"op", *need} or not all(isinstance(step[k], int) for k in need):
                    raise ValueError(f"守りの手順は {', '.join(f'{op} {{{', '.join(k)}}}' for op, k in _GUARD_OPS.items())}"
                                     f" のどれか: {step!r}")
                steps[part].append(dict(step))
        return cls(steps["enter"], steps["exit"])

    def enter(self, target) -> dict[int, int]:
        saved = {address: target.read32(address) for address in self.saved_addresses}
        self._run(target, self.enter_steps, saved)
        return saved

    def exit(self, target, saved: dict[int, int]) -> None:
        self._run(target, self.exit_steps, saved)

    def _run(self, target, steps: list[dict], saved: dict[int, int]) -> None:
        for s in steps:
            address, mask, op = s["address"], s["mask"], s["op"]
            if op in ("write", "restore"):
                value = s["value"] if op == "write" else saved[address]
                current = 0 if mask == 0xFFFFFFFF else target.read32(address)
                target.write32(address, (current & ~mask | value & mask) & 0xFFFFFFFF)
            else:
                want = s["value"] if op == "wait" else saved[address] & mask
                for _ in range(self.tries):
                    if target.read32(address) & mask == want:
                        break
                else:
                    raise RuntimeError(f"守りの待ち（0x{address:08X} & 0x{mask:08X} == 0x{want:X}）が終わらない")


def _algorithm(method):
    """書き込みアルゴリズムを動かす操作: 使う側が渡した守り（FlashMemory.guards）の中で行う（入れ子なら外側だけ）。"""
    @functools.wraps(method)
    def wrapper(self, *args, **kwargs):
        with self._clock_guarded():
            return method(self, *args, **kwargs)
    return wrapper


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


# --- Flash ------------------------------------------------------------------------------

@dataclass(frozen=True)
class Block:
    """消去の単位（セクタ）か書き込みの単位（ページ）。"""
    address: int
    size: int

    @property
    def end(self) -> int:
        return self.address + self.size


@dataclass
class ProgramReport:
    """書き込みの結果（pyOCD の ProgrammingInfo を領域にわたって足したもの）。"""
    bytes: int = 0                 # 書くよう渡した Flash のバイト数
    programmed_bytes: int = 0      # 実際に書いたバイト数（同じ中身のページは書かない）
    programmed_pages: int = 0
    skipped_bytes: int = 0
    erased_bytes: int = 0
    erased_sectors: int = 0
    erase_method: str = "sector"   # sector / chip
    erase_s: float = 0.0
    program_s: float = 0.0
    analyze_s: float = 0.0         # 今の中身との比較（smart）にかかった時間
    ram_bytes: int = 0             # Flash でない書ける領域（RAM）へ直接書いたバイト数
    seconds: float = 0.0           # 全体（アルゴリズムの読み込み等を含む）

    def as_dict(self) -> dict:
        return {"bytes": self.bytes, "programmed_bytes": self.programmed_bytes, "programmed_pages": self.programmed_pages,
                "skipped_bytes": self.skipped_bytes, "erase_method": self.erase_method,
                "erase": {**Rate(self.erased_bytes, self.erase_s).as_dict(), "sectors": self.erased_sectors},
                "program": Rate(self.programmed_bytes, self.program_s).as_dict(),
                "analyze_s": round(self.analyze_s, 6), "ram_bytes": self.ram_bytes,
                "seconds": round(self.seconds, 6), "bytes_per_s": Rate(self.bytes, self.seconds).bytes_per_s}


class FlashMemory:
    """ターゲット（pyOCD の CoreSightTarget）の Flash。どの操作もターゲットを止める（書き込みアルゴリズムがコアで走る）。"""

    def __init__(self, target, read_chunk: int = READ_CHUNK, guards: list[RegisterGuard] | None = None):
        """guards: 書き込みアルゴリズムの間のレジスタの守り（RegisterGuard。使う側が渡す。無ければチップのレジスタに
        触らない）。"""
        self.target = target
        self.read_chunk = read_chunk
        self.guards: list[RegisterGuard] = list(guards or [])
        self._guard_depth = 0

    @contextlib.contextmanager
    def _clock_guarded(self):
        """アルゴリズムを動かす間、割り込みを止め、使う側が渡した守り（guards）でチップのレジスタを前提の状態にし、
        抜けるときに戻す（入れ子なら外側だけ）。プログラムを止めずに書く fprobe では、走っていたプログラムの状態（例:
        PLL 84 MHz のクロック）のままになっている。どの状態が前提かは書き込みアルゴリズム（チップ）ごとなので本体は決めない。"""
        outermost = self._guard_depth == 0
        guards = list(self.guards) if outermost else []
        primask = None
        entered: list = []
        if outermost:
            # 割り込みを止める（PRIMASK = 1）: 走っていたプログラムの割り込み（SysTick 等）がアルゴリズムの間に入ると、
            # プログラムのハンドラがアルゴリズムの置かれた RAM を書き換えて書き込みが失敗する（2026-10-18 に実機で）
            self.target.halt()
            primask = self.target.read_core_registers_raw(["primask"])[0]
            self.target.write_core_registers_raw(["primask"], [1])
        self._guard_depth += 1
        try:
            for g in guards:                               # enter が途中で失敗しても、入れた守りと PRIMASK は下で戻す
                entered.append((g, g.enter(self.target)))
            yield
        finally:
            self._guard_depth -= 1
            try:
                if outermost:
                    self.target.halt()
                errors = []
                for guard, saved in reversed(entered):
                    try:
                        guard.exit(self.target, saved)
                    except Exception as e:                 # 1 つの守りの失敗で残りの守りと PRIMASK の復元を飛ばさない
                        errors.append(e)
            finally:
                if primask is not None:
                    self.target.write_core_registers_raw(["primask"], [primask])
            if errors:
                raise errors[0]

    # --- 構成 ---

    @property
    def memory_map(self):
        return self.target.memory_map

    def region_at(self, address: int):
        region = self.memory_map.get_region_for_address(address)
        if region is None or not region.is_flash:
            raise ValueError(f"0x{address:08X} は Flash ではない")
        if region.flash is None:
            raise RuntimeError(f"Flash の領域 {region.name} に書き込みアルゴリズムが無い（ターゲットの定義・Pack を確認）")
        return region

    def sector_at(self, address: int) -> Block:
        info = self.region_at(address).flash.get_sector_info(address)
        return Block(info.base_addr, info.size)

    def page_at(self, address: int) -> Block:
        info = self.region_at(address).flash.get_page_info(address)
        return Block(info.base_addr, info.size)

    def sectors(self, address: int, length: int) -> list[Block]:
        """[address, address + length) にかかるセクタ（Flash の外にはみ出すと誤り）。"""
        if length <= 0:
            raise ValueError("長さは 1 以上")
        blocks, at, end = [], address, address + length
        while at < end:
            block = self.sector_at(at)
            blocks.append(block)
            at = block.end
        return blocks

    def main_flash(self) -> Block:
        """主の Flash: 起動する領域（無ければ最初の Flash の領域）から、隙間なく続く Flash の領域の終わりまで
        （pyOCD の定義はセクタの大きさごとに領域を分けることがある）。"""
        regions = sorted(self.memory_map.iter_matching_regions(is_flash=True), key=lambda r: r.start)
        if not regions:
            raise RuntimeError("ターゲットに Flash の領域が無い")
        first = next((r for r in regions if r.is_boot_memory), regions[0])
        end = first.end + 1
        for region in regions[regions.index(first) + 1:]:
            if region.start != end:
                break
            end = region.end + 1
        return Block(first.start, end - first.start)

    def describe(self) -> list[dict]:
        """Flash の領域: 番地・大きさ・ページ・セクタの並び（同じ大きさの連続をまとめる）・消した値・アルゴリズムの出所。"""
        out = []
        for region in self.memory_map.iter_matching_regions(is_flash=True):
            sectors: list[dict] = []
            at = region.start
            while region.flash is not None and at <= region.end:
                info = region.flash.get_sector_info(at)
                if info is None or info.size <= 0:
                    break
                if sectors and sectors[-1]["size"] == info.size:
                    sectors[-1]["count"] += 1
                else:
                    sectors.append({"start": info.base_addr, "count": 1, "size": info.size})
                at = info.base_addr + info.size
            out.append({"name": region.name, "start": region.start, "size": region.length,
                        "page_size": region.page_size, "sectors": sectors, "erased_value": region.erased_byte_value,
                        "boot": bool(region.is_boot_memory), "algorithm": _algorithm_source(region),
                        "usable": region.flash is not None})
        return out

    # --- 読む ---

    def read(self, address: int, length: int, progress: Progress | None = None) -> tuple[bytes, Rate]:
        if length < 0:
            raise ValueError(f"読む長さが負（{length}）")
        out = bytearray()
        started = time.perf_counter()
        if progress is not None:
            progress.report("read", 0.0)
        while len(out) < length:
            n = min(self.read_chunk, length - len(out))
            out += bytes(self.target.read_memory_block8(address + len(out), n))
            if progress is not None:
                progress.report("read", len(out) / length)
        return bytes(out), Rate(length, time.perf_counter() - started)

    def verify(self, image: Image, progress: Progress | None = None) -> Comparison:
        return compare(image, lambda a, n: self.read(a, n)[0], progress)

    # --- 消す ---

    @_algorithm
    def erase(self, address: int, length: int, progress: Progress | None = None) -> dict:
        """[address, address + length) にかかるセクタをすべて消す（セクタの境界に合わせて広がる）。"""
        blocks = self.sectors(address, length)
        started, flash = time.perf_counter(), None
        try:
            for i, block in enumerate(blocks):
                region_flash = self.region_at(block.address).flash
                if region_flash is not flash:
                    if flash is not None:
                        flash.cleanup()
                    flash = region_flash
                    flash.init(flash.Operation.ERASE)
                if progress is not None:
                    progress.report("erase", i / len(blocks))
                flash.erase_sector(block.address)
        finally:
            if flash is not None:
                flash.cleanup()
        if progress is not None:
            progress.report("erase", 1.0)
        return {"start": blocks[0].address, "end": blocks[-1].end, "sectors": len(blocks),
                "rate": Rate(sum(b.size for b in blocks), time.perf_counter() - started).as_dict()}

    @_algorithm
    def erase_chip(self, progress: Progress | None = None) -> dict:
        """Flash の領域をすべて消す（アルゴリズムの全消去、無ければセクタごと）。"""
        started, size = time.perf_counter(), 0
        regions = [r for r in self.memory_map.iter_matching_regions(is_flash=True) if r.flash is not None]
        for i, region in enumerate(regions):
            if progress is not None:
                progress.report("erase", i / len(regions))
            if region.flash.is_erase_all_supported:
                region.flash.init(region.flash.Operation.ERASE)
                try:
                    region.flash.erase_all()
                finally:
                    region.flash.cleanup()
            else:
                self.erase(region.start, region.length)
            size += region.length
        if progress is not None:
            progress.report("erase", 1.0)
        return {"method": "chip", "rate": Rate(size, time.perf_counter() - started).as_dict()}

    def mass_erase(self) -> dict:
        """デバイスの mass erase（チップ固有。保護の解除・設定領域の消去を伴うことがある）。"""
        started = time.perf_counter()
        self.target.mass_erase()
        return {"method": "mass", "seconds": round(time.perf_counter() - started, 6)}

    # --- ページ単位の低水準の口 ---

    @_algorithm
    def erase_sector(self, address: int) -> None:
        """1 セクタを消す（address はセクタの先頭）。アルゴリズムの読み込みから毎回行う。数を書き換えるなら patch。"""
        block = self.sector_at(address)
        if block.address != address:
            raise ValueError(f"0x{address:08X} はセクタの先頭ではない（セクタは 0x{block.address:08X}）")
        flash = self.region_at(address).flash
        flash.init(flash.Operation.ERASE, address)
        try:
            flash.erase_sector(address)
        finally:
            flash.cleanup()

    @_algorithm
    def program_page(self, address: int, data: bytes) -> None:
        """消してあるページに書く（address はページの先頭、data はページの大きさ以下）。"""
        page = self.page_at(address)
        if page.address != address or len(data) > page.size:
            raise ValueError(f"ページの先頭 0x{page.address:08X} から {page.size} バイト以下で書く")
        flash = self.region_at(address).flash
        flash.init(flash.Operation.PROGRAM, address)
        try:
            flash.program_page(address, bytes(data))
        finally:
            flash.cleanup()

    # --- 書く ---

    @_algorithm
    def program(self, image: Image, erase: str = "auto", smart: bool = True, keep_unwritten: bool | None = None,
                progress: Progress | None = None) -> ProgramReport:
        """image を書く。erase: auto（速い方）/ sector / chip。smart: 同じ中身のページは書かない。keep_unwritten:
        消すセクタのうち書かない部分の中身を保つ（省略時は chip 以外で保つ。chip は「全体を消して書く」の意味にする）。
        Flash でない書ける領域（RAM）の区画はそのまま書く。"""
        if erase not in ("auto", "sector", "chip"):
            raise ValueError(f"erase は auto / sector / chip（{erase!r}）")
        if keep_unwritten is None:
            keep_unwritten = erase != "chip"
        from pyocd.flash.builder import FlashBuilder
        started = time.perf_counter()
        report = ProgramReport()
        builders, ram = self._split(image, report)
        ordered = sorted(builders.values(), key=lambda b: b.region.start)
        total = sum(b.buffered_data_size for b in ordered)
        delegate = getattr(self.target, "debug_sequence_delegate", None)
        infos = []
        for phase in ("erase", "program"):
            offset = 0.0
            for builder in ordered:
                share = builder.buffered_data_size / total
                cb = None if progress is None else (lambda f, o=offset, s=share, p=phase: progress.report(p, o + f * s))
                if phase == "erase":
                    builder.erase(chip_erase=erase, progress_cb=cb, smart_flash=smart, keep_unwritten=keep_unwritten)
                else:
                    infos.append(builder.program(progress_cb=cb, smart_flash=smart, keep_unwritten=keep_unwritten))
                offset += share
            done = "FlashEraseDone" if phase == "erase" else "FlashProgramDone"
            if ordered and delegate is not None and delegate.has_sequence_with_name(done):
                delegate.run_sequence(done)
        for seg in ram:
            self.target.write_memory_block8(seg.address, list(seg.data))
        if ram:
            self.target.flush()
        for info in infos:
            report.programmed_bytes += info.program_byte_count
            report.programmed_pages += info.program_page_count
            report.skipped_bytes += info.skipped_byte_count
            report.erased_bytes += info.erase_byte_count
            report.erased_sectors += info.erase_sector_count
            report.erase_s += info.erase_time
            report.program_s += info.program_time
            report.analyze_s += info.analyze_time or 0.0
            if info.program_type == FlashBuilder.FLASH_CHIP_ERASE:
                report.erase_method = "chip"
        report.seconds = time.perf_counter() - started
        return report

    def _split(self, image: Image, report: ProgramReport) -> tuple[dict, list[Segment]]:
        """区画を領域ごとの FlashBuilder（Flash）と、直接書く区画（RAM 等）に分ける。"""
        builders: dict = {}
        ram: list[Segment] = []
        # CMSIS-Pack のターゲットは二重バッファを使わない（pyOCD の FlashLoader と同じ扱い）
        single_buffer = any(hasattr(self.target, a) for a in ("_cbuild_device", "_pack_device"))
        for seg in image.segments:
            at, data = seg.address, seg.data
            while data:
                region = self.memory_map.get_region_for_address(at)
                if region is None:
                    raise ValueError(f"0x{at:08X} はターゲットのメモリマップに無い")
                n = min(len(data), region.end - at + 1)
                if region.is_flash:
                    builder = builders.get(region)
                    if builder is None:
                        builder = self.region_at(at).flash.get_flash_builder()
                        builder.log_performance = False
                        if single_buffer:
                            builder.enable_double_buffer(False)
                        builders[region] = builder
                    builder.add_data(at, data[:n])
                    report.bytes += n
                elif region.is_writable:
                    ram.append(Segment(at, data[:n]))
                    report.ram_bytes += n
                else:
                    raise ValueError(f"0x{at:08X}（{region.name}）は書けない領域")
                at, data = at + n, data[n:]
        return builders, ram

    def patch(self, changes: Iterable[tuple[int, bytes]], preserve: bool = True,
              progress: Progress | None = None) -> ProgramReport:
        """Flash の数バイトを書き換える（Flash ブレークポイント等）。かかるセクタを読み、消し、書き戻す（ほかの中身は保つ）。
        preserve: preserving の中で書く（止まっているプログラムの状態を保ち、走っていたら走らせ直す）。"""
        image = Image(Segment(a, bytes(d)) for a, d in changes)
        for seg in image.segments:
            self.region_at(seg.address)                      # Flash の外は断る（RAM の書き換えは patch の役ではない）
            self.region_at(seg.end - 1)
        if not preserve:
            return self.program(image, erase="sector", smart=True, keep_unwritten=True, progress=progress)
        with self.preserving([s.address for s in image.segments]):
            return self.program(image, erase="sector", smart=True, keep_unwritten=True, progress=progress)

    @contextlib.contextmanager
    def preserving(self, addresses: Iterable[int]):
        """書き込みアルゴリズムが壊すもの（コアのレジスタと、addresses の Flash の領域のアルゴリズムが使う RAM）を
        保存し、抜けるときに戻す。走っていたら走らせ直す。ponytail: FPU のレジスタは保存しない（アルゴリズムは使わない）。"""
        spans = self._algorithm_ram({self.region_at(a) for a in addresses})
        running = self.target.get_state().name != "HALTED"
        self.target.halt()
        try:
            names = self._context_names()
            registers = self.target.read_core_registers_raw(names)
            saved_ram = [(a, bytes(self.target.read_memory_block8(a, b - a))) for a, b in spans]
            guard = self._clock_guarded()
            guard.__enter__()
        except BaseException:
            if running:
                self.target.resume()                   # 保存・守りの開始が失敗したら、走っていたものを止めたまま放置しない
            raise
        try:
            yield
        finally:
            self.target.halt()
            guard.__exit__(None, None, None)
            self._fetch_elsewhere(spans[0][0], registers[names.index("pc")])
            for address, data in saved_ram:
                self.target.write_memory_block8(address, list(data))
            restore = [n for n in names if n != "sp"]
            self.target.write_core_registers_raw(restore, [registers[names.index(n)] for n in restore])
            self.target.flush()
            if running:
                self.target.resume()

    def _fetch_elsewhere(self, scratch: int, pc: int) -> None:
        """CPU に pc と別の Flash の行を 1 度読ませる（RAM の scratch で ldr pc, [pc, #0] を 1 命令）。Flash の読みの
        行の保持（STM32F4 の Flash の取り込みの 128 ビット等）は書き換えでは捨てられず、止めた場所（pc）に置いたまま
        再開すると古い命令を実行する（2026-10-18 に実機で、戻した BKPT を実行し続けた。別の行を通れば読み直す。ART の
        キャッシュのリセット・待ちの切り替えでは捨てられなかった）。レジスタと scratch は呼び手が戻す。
        別の行は、Flash の領域の pc から遠い方の端（領域の半分以上離れた所。行の大きさを仮定しない）。"""
        region = next(iter(self.memory_map.iter_matching_regions(is_flash=True)))
        half = region.length // 2
        other = region.start if pc - region.start >= half else (region.start + region.length - 4) & ~3
        self.target.write_memory_block8(scratch, list(bytes.fromhex("dff800f0") + (other | 1).to_bytes(4, "little")))
        self.target.write_core_register("pc", scratch)
        self.target.step(disable_interrupts=True)

    def algorithm_ram(self) -> list[tuple[int, int]]:
        """Flash の書き込みアルゴリズムが使う RAM の範囲（アルゴリズムのある Flash の領域すべて。重なりはまとめる）。"""
        return self._algorithm_ram({r for r in self.memory_map.iter_matching_regions(is_flash=True)
                                    if r.flash is not None})

    def _context_names(self) -> list[str]:
        core = getattr(self.target, "selected_core", None) or self.target
        index = getattr(core, "core_registers", None)
        known = getattr(index, "by_name", None)
        return [n for n in CONTEXT_REGISTERS if known is None or n in known]

    @staticmethod
    def _algorithm_ram(regions) -> list[tuple[int, int]]:
        """書き込みアルゴリズムが使う RAM の範囲（命令・ページバッファ・スタック・静的データ・CRC 解析の端の点を覆う範囲）。
        重なりはまとめる。"""
        spans = []
        for region in regions:
            algo = getattr(region.flash, "flash_algo", None)
            if not algo:
                raise RuntimeError(f"{region.name} のアルゴリズムが使う RAM が分からない（preserve=False で書く）")
            load = algo["load_address"]
            points = [load, load + 4 * len(algo["instructions"])]
            for buffer in algo.get("page_buffers", []):
                points += [buffer, buffer + (region.page_size or 0)]
            points += [algo[k] for k in ("begin_stack", "end_stack", "static_base") if algo.get(k) is not None]
            if algo.get("analyzer_supported") and algo.get("analyzer_address") is not None:
                points += [algo["analyzer_address"], algo["analyzer_address"] + ANALYZER_SIZE]
            spans.append((min(points), max(points)))
        merged: list[list[int]] = []
        for a, b in sorted(spans):
            if merged and a <= merged[-1][1]:
                merged[-1][1] = max(merged[-1][1], b)
            else:
                merged.append([a, b])
        return [(a, b) for a, b in merged]


def _algorithm_source(region) -> str:
    flm = region.attributes.get(FLM_ATTRIBUTE)
    if flm:
        return f"flm:{flm}"
    if getattr(region, "flm", None) is not None:
        return "pack"
    return "builtin"


# --- 外付けの Flash（CMSIS の .FLM）----------------------------------------------------------

def add_flm_region(target, flm: str | Path, address: int | None = None, size: int | None = None,
                   ram_address: int | None = None, ram_size: int | None = None, name: str | None = None):
    """CMSIS の書き込みアルゴリズム（.FLM。Keil・メーカーの Pack に入っている形式）で Flash の領域をターゲットに足す。
    address・size は省略時に FLM の FlashDevice の値。アルゴリズムを置く RAM は ram_address（省略時は既定の RAM）。
    足した領域は接続し直すと消える（呼び手が覚えて足し直す）。"""
    from pyocd.core.memory_map import FlashRegion
    from pyocd.flash.flash import Flash
    from pyocd.target.pack.flash_algo import PackFlashAlgo
    from pyocd.target.pack.flm_region_builder import FlmFlashRegionBuilder

    path = Path(flm)
    algo = PackFlashAlgo(str(path))
    start = algo.flash_start if address is None else address
    length = algo.flash_size if size is None else size
    if length <= 0:
        raise ValueError(f"{path.name}: Flash の大きさが分からない（size を指定）")
    # 置き場所を変えたとき: セクタの並び（FLM では先頭からの相対）もそこから数える。アルゴリズムには実際の番地が
    # 渡る（Keil の Flash Download の Start / Size を変えたときと同じ）
    algo.flash_start, algo.flash_size = start, length
    for existing in target.memory_map.regions:
        if existing.intersects_range(start=start, length=length):
            raise ValueError(f"0x{start:08X}+0x{length:X} は既存の領域 {existing.name} と重なる")
    region = FlashRegion(name=name or path.stem, start=start, length=length, flm=algo, flash_class=Flash,
                         page_size=algo.page_size, erased_byte_value=algo.flash_info.value_empty)
    region.attributes[FLM_ATTRIBUTE] = str(path)
    if ram_address is not None:
        region._RAMstart = ram_address
        if ram_size is not None:
            region._RAMsize = ram_size
    if not FlmFlashRegionBuilder(target, target.memory_map).finalise_region(region) or region.algo is None:
        raise RuntimeError(f"{path.name} を書き込みアルゴリズムにできない（pyOCD のログを確認）")
    flash = Flash(target, region.algo)
    flash.region = region
    region.flash = flash
    target.memory_map.add_region(region)
    _update_core_maps(target)
    return region


def remove_region(target, name: str) -> None:
    """add_flm_region で足した領域を外す。"""
    for region in target.memory_map.regions:
        if region.name == name and region.attributes.get(FLM_ATTRIBUTE):
            target.memory_map.remove_region(region)
            _update_core_maps(target)
            return
    raise KeyError(f"足した領域 {name!r} は無い")


def _update_core_maps(target) -> None:
    """コアごとのメモリマップを作り直す（pyOCD の CoreSightTarget.create_flash の終わりと同じ）。"""
    from pyocd.core.memory_map import MemoryMap
    for core in getattr(target, "cores", {}).values():
        core.memory_map = MemoryMap([m for m in target.memory_map
                                     if m.attributes.get("pname") in (None, core.node_name)])


def crc32(data: bytes) -> str:
    """読み出した中身の CRC-32（zlib と同じ。16 進 8 桁）。"""
    return f"{zlib.crc32(data) & 0xFFFFFFFF:08x}"
