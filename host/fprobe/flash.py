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

分け方: 守りの手順は flash_guard.py、止まっているプログラムの状態を保つ書き換え（patch・preserving）は flash_preserve.py（FlashMemory に
混ぜ込む部品）、.FLM の領域は flash_flm.py。
"""

import contextlib
import functools
import time
import zlib
from dataclasses import dataclass

from .flash_flm import FLM_ATTRIBUTE, _algorithm_source, add_flm_region, remove_region  # noqa: F401  (再公開)
from .flash_guard import RegisterGuard  # noqa: F401  (再公開)
from .flash_image import Comparison, Difference, Image, Progress, Rate, Segment, compare, image_format, save_image
from .flash_preserve import ANALYZER_SIZE, CONTEXT_REGISTERS, _PreserveMixin  # noqa: F401  (旧名を保つ)

__all__ = ["Block", "Comparison", "Difference", "FlashMemory", "Image", "image_format", "ProgramReport", "Progress", "Rate",
           "RegisterGuard", "Segment", "add_flm_region", "compare", "crc32", "remove_region", "save_image"]

READ_CHUNK = 0x4000            # 読み出しの 1 回の大きさ（進捗の細かさ。DAP の転送は pyOCD がさらに分ける）


def _algorithm(method):
    """書き込みアルゴリズムを動かす操作: 使う側が渡した守り（FlashMemory.guards）の中で行う（入れ子なら外側だけ）。"""
    @functools.wraps(method)
    def wrapper(self, *args, **kwargs):
        with self._clock_guarded():
            return method(self, *args, **kwargs)
    return wrapper


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


class FlashMemory(_PreserveMixin):
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


def crc32(data: bytes) -> str:
    """読み出した中身の CRC-32（zlib と同じ。16 進 8 桁）。"""
    return f"{zlib.crc32(data) & 0xFFFFFFFF:08x}"
