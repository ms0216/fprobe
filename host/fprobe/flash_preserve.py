"""FlashMemory の、止まっているプログラムの状態を保つ書き換え（patch / preserving）の部品。

書き込みアルゴリズムが壊すもの（コアのレジスタと、アルゴリズムが使う RAM）を保存・復元し、走っていたら走らせ直す。
FlashMemory（fprobe.flash）に mixin される。
"""

from __future__ import annotations

import contextlib
from typing import TYPE_CHECKING, Iterable

from .flash_image import Image, Progress, Segment

if TYPE_CHECKING:
    from .flash import ProgramReport

# 書き込みアルゴリズムが壊すコアの状態（patch の preserve で保存・復元する）。sp は msp / psp の別名なので戻さない
CONTEXT_REGISTERS = (*(f"r{i}" for i in range(13)), "lr", "pc", "xpsr", "msp", "psp",
                     "primask", "basepri", "faultmask", "control")
ANALYZER_SIZE = 0x600          # pyOCD の CRC 解析のコードが analyzer_address から使う大きさ


class _PreserveMixin:
    """Flash の書き換えの間、コアのレジスタとアルゴリズムの RAM を保つ。"""

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
