"""Probe の道具: Flash の操作（書き込み・消去・検証・ファイルとの比較・読み出して保存・速度の計測）、外付け Flash の
書き込みアルゴリズム（.FLM）、CMSIS-Pack の導入と選択。J-Link Commander の loadfile / erase / verifybin / savebin、
OpenOCD の flash write_image / erase_sector / verify_image / read_bank、pyOCD の load / erase / pack の相当。

Probe（fprobe.probe。共通の部品の型は fprobe.probe_host.ProbeHost）に混ぜて使う mixin。部品は fprobe.flash（Flash の操作）と fprobe.packs（Pack）。

- 進捗: 書き込み・消去・検証・読み出しの進み具合を通知 "flash.progress"（operation・phase・fraction・elapsed_s）で
  送る（デーモンが attach_publisher で送り先をつなぐ。MCP の道具には結果だけが返る）。
- 速度: どの操作も段ごとのバイト数・秒・bytes_per_s を返す。flash_benchmark は RAM の書き込み・読み出し、Flash の
  読み出し、指定したセクタへの書き込み（元の中身・RAM・レジスタ・走る状態を戻す）を計る。
- 記録: 記録中なら stream "flash" に操作の要約を残す（共通の時間軸で「いつ書いたか」）。
- 外付けの .FLM（flash_algo_add）は覚えておき、接続し直しても次の操作の前に足し直す。

Flash の書き込み・消去は、書き込みアルゴリズムをコアで走らせる（RAM とレジスタを使う）。そのあとのプログラムの状態は
壊れているので、flash は既定でリセットして走らせ、flash_erase は止めたままにする。Flash の読み出し・検証は止めない。
"""

import random
import time
from pathlib import Path

from .flash import (FlashMemory, Image, Progress, Rate, RegisterGuard, Segment, add_flm_region, crc32, remove_region,
                    save_image)
from .packs import Packs

__all__ = ["FlashTools"]

PROGRESS_TOPIC = "flash.progress"
ERASE_MODES = ("range", "chip", "mass")


class FlashTools:
    def _init_flash(self) -> None:
        self._publish = None                                   # 通知の送り先 publish(topic, data)（デーモン）
        self._flash_algos: dict[str, dict] = {}                # 名前 → add_flm_region の引数
        self._flash_guard: dict | None = None                  # 書き込みアルゴリズムの間の守り（使う側が渡す）
        self._packs: Packs | None = None

    def attach_publisher(self, publish) -> None:
        """通知の送り先をつなぐ（デーモンが起動時に呼ぶ）。publish(topic, data) は作業スレッドから呼ばれる。"""
        self._publish = publish

    def _flash_progress(self, operation: str) -> Progress:
        publish = self._publish
        return Progress(None if publish is None else lambda data: publish(PROGRESS_TOPIC, data), operation)

    def _flash_memory(self) -> FlashMemory:
        """いまのターゲットの Flash（覚えている .FLM の領域が無ければ足し直す）。_lock の中で。"""
        target = self.target
        present = {r.name for r in target.memory_map.regions}
        for name, spec in self._flash_algos.items():
            if name not in present:
                add_flm_region(target, name=name, **spec)
        return FlashMemory(target, guards=self._flash_guards())

    def _flash_guards(self) -> list[RegisterGuard]:
        return [RegisterGuard.from_dict(self._flash_guard)] if self._flash_guard is not None else []

    def flash_algorithm_guard_set(self, guard: dict | None = None) -> dict:
        """書き込みアルゴリズムを動かす間だけチップのレジスタを決めた状態にし、抜けるときに戻す手順（fprobe.flash
        .RegisterGuard の {enter, exit}。None で外す）。機器固有の守り（例: STM32F2 / F4 / F7 で走っていたプログラムの
        PLL のままアルゴリズムを動かさない）は本体に持たず、使う側が渡す（プラグイン fprobe.plugins.stm32_flash_clock）。"""
        if guard is not None:
            RegisterGuard.from_dict(guard)                     # 形を確かめる（誤りは ValueError）
        with self._lock:
            self._flash_guard = guard
            fbp = self._flash_bp
            if fbp is not None:
                fbp._memory.guards = self._flash_guards()
            return {"guard": guard}

    def flash_algorithm_guard_get(self) -> dict:
        """flash_algorithm_guard_set で渡した守り（無ければ None）。"""
        return {"guard": self._flash_guard}

    def _note_flash(self, kind: str, body: dict) -> None:
        self._record_host("flash", kind, time.monotonic(), body=body)

    # --- 書く・消す ------------------------------------------------------------------------

    def flash(self, path: str, address: int | None = None, erase: str = "auto", verify: bool = True,
              smart: bool = True, reset: bool = True, preserve: bool = False) -> dict:
        """ELF / HEX / BIN を Flash に書く（BIN は address が要る）。erase: auto（速い方）/ sector（書く範囲のセクタ
        だけ。ほかの中身は保つ）/ chip（全体を消して書く）。smart: 同じ中身のページは書かない。verify: 書いた後に
        読み比べ、違えば誤り。reset: 書いた後にリセットして走らせる（False なら止めたまま）。段ごとの時間と速度、
        検証の結果を返す。ELF ならシンボルも読む。preserve: 書く前にリセットしない（走っているプログラムのまま書く。
        既定はリセットしてから書く: 書き込みアルゴリズムはリセット直後のクロック・割り込み・DMA が止まった状態を前提に
        する。2026-10-18 に実機で、PLL・割り込みのあるファームのまま書くと失敗した）。"""
        image = Image.load(path, address)
        with self._lock:
            self._require_halt_mode("Flash の書き込み")
            memory = self._flash_memory()
            self._watch_stops(False)
            if not preserve:
                self.target.reset_and_halt()
            progress = self._flash_progress("program")
            started = time.monotonic()
            report = self._flash_bp_program(memory, image, erase=erase, smart=smart, progress=progress)
            comparison = memory.verify(image, progress) if verify else None
            if comparison is not None and not comparison.match:
                first = comparison.differences[0]
                raise RuntimeError(f"検証で違いがある: {comparison.differing} バイト（最初は 0x{first.address:08X}、"
                                   f"{len(comparison.differences)} 箇所）。target_flash_verify で詳細")
            if image.format == "elf":
                self.elf_load(path)                       # 止まった場所を関数名と行で返せるように
            self._image_written(elf=image.format == "elf")
            if reset:
                self.target.reset()
                self._watch_stops(True)
            out = {"image": image.describe(), "program": report.as_dict(),
                   "verify": None if comparison is None else comparison.as_dict(),
                   "seconds": round(time.monotonic() - started, 3)}
            self._note_flash("program", {"source": image.source, "bytes": image.size, "seconds": out["seconds"]})
            return {**out, **self.target_status()}

    def flash_erase(self, mode: str = "range", address: int | None = None, length: int | None = None,
                    preserve: bool = False) -> dict:
        """Flash を消す。mode: range（address から length バイトにかかるセクタ。セクタの境界まで広がる）/ chip（Flash の
        領域すべて）/ mass（チップの mass erase。保護の解除等を伴うことがある）。ターゲットは止まったまま。
        preserve: 消す前にリセットしない（既定はリセットしてから。flash と同じ理由）。"""
        if mode not in ERASE_MODES:
            raise ValueError(f"mode は {' / '.join(ERASE_MODES)}（{mode!r}）")
        if mode == "range" and (address is None or length is None):
            raise ValueError("mode range は address と length が要る")
        with self._lock:
            self._require_halt_mode("Flash の消去")
            memory = self._flash_memory()
            self._watch_stops(False)
            if not preserve and mode != "mass":
                self.target.reset_and_halt()
            progress = self._flash_progress("erase")
            if mode == "range":
                out = memory.erase(address, length, progress)
            elif mode == "chip":
                out = memory.erase_chip(progress)
            else:
                out = memory.mass_erase()
            self._flash_bp_erased(mode, address, length)       # 消したセクタの Flash の BKPT の記録を捨てる
            self._note_flash("erase", {"mode": mode, **out})
            return out

    # --- 比べる・読む ------------------------------------------------------------------------

    def flash_verify(self, path: str, address: int | None = None) -> dict:
        """ファイル（ELF / HEX / BIN。BIN は address）とターゲットのメモリを比べる（書かない・止めない）。違う箇所を
        番地・期待値・実際の値で返す（多ければ先頭 32 箇所）。"""
        image = Image.load(path, address)
        with self._lock:
            comparison = self._flash_memory().verify(image, self._flash_progress("verify"))
            return {**comparison.as_dict(), "image": image.describe()}

    def flash_read(self, path: str, address: int | None = None, length: int | None = None) -> dict:
        """ターゲットのメモリを読んでファイルに保存する（.hex / .ihex は Intel HEX、ほかは BIN）。省略時は起動する Flash の
        全体（起動する領域から続く Flash の領域すべて）。止めない。CRC-32 と読み出しの速度を返す。"""
        with self._lock:
            memory = self._flash_memory()
            if address is None or length is None:
                main = memory.main_flash()
                address = main.address if address is None else address
                length = main.end - address if length is None else length
            if length <= 0:
                raise ValueError(f"読む長さが 0 以下（address 0x{address:08X}、length {length}）。"
                                 "address は Flash の終わりより手前に")
            data, rate = memory.read(address, length, self._flash_progress("read"))
        fmt = save_image(path, address, data)
        return {"path": str(Path(path)), "format": fmt, "address": address, "bytes": len(data), "crc32": crc32(data),
                "read": rate.as_dict()}

    # --- 領域・外付けのアルゴリズム -------------------------------------------------------------

    def flash_regions(self) -> dict:
        """Flash の領域（番地・大きさ・ページ・セクタの並び・消した値・アルゴリズムの出所 builtin / pack / flm:<path>）と、
        flash_algo_add で足したアルゴリズム。"""
        with self._lock:
            return {"regions": self._flash_memory().describe(),
                    "algorithms": [{"name": n, **{k: s.get(k) for k in ("flm", "address", "size", "ram_address",
                                                                          "ram_size")}}
                                   for n, s in self._flash_algos.items()]}

    def flash_algo_add(self, flm: str, address: int | None = None, size: int | None = None,
                       ram_address: int | None = None, ram_size: int | None = None, name: str | None = None) -> dict:
        """CMSIS の書き込みアルゴリズム（.FLM）で Flash の領域を足す（外付けの QSPI NOR 等。以後 flash・flash_erase・
        flash_read がその番地に使える）。address・size は省略時に FLM の値、アルゴリズムを置く RAM は ram_address
        （省略時は既定の RAM）。接続し直しても足し直す。未接続なら覚えておき、接続後の最初の操作で足す。"""
        name = name or Path(flm).stem
        if name in self._flash_algos:
            raise ValueError(f"{name!r} はもう足してある（flash_algo_remove で外す）")
        spec = {"flm": str(flm), "address": address, "size": size, "ram_address": ram_address, "ram_size": ram_size}
        with self._lock:
            if self._debug is None:
                self._flash_algos[name] = spec
                return {"name": name, "pending": True}
            region = add_flm_region(self.target, name=name, **spec)
            self._flash_algos[name] = spec
            return next(r for r in FlashMemory(self.target).describe() if r["name"] == region.name)

    def flash_algo_remove(self, name: str) -> dict:
        """flash_algo_add で足した領域を外す。"""
        with self._lock:
            if name not in self._flash_algos:
                raise KeyError(f"足したアルゴリズム {name!r} は無い")
            del self._flash_algos[name]
            if self._debug is not None and any(r.name == name for r in self.target.memory_map.regions):
                remove_region(self.target, name)
            return {"removed": name}

    # --- 速度の計測 ------------------------------------------------------------------------

    def flash_benchmark(self, ram_length: int = 0x4000, read_length: int = 0x10000,
                        program_address: int | None = None, program_length: int | None = None) -> dict:
        """書き込み・読み出しの速度を計る。ram_length: 既定の RAM の先頭へ書いて読む（元の中身と走る状態を戻す。0 で省く）。
        read_length: 起動する Flash の先頭から読む（0 で省く）。program_address・program_length: そのセクタ（境界に
        合わせる）を消して書いて読み比べ、元の中身・RAM・レジスタ・走る状態に戻す（指定したときだけ。使っていないセクタを
        選ぶ）。SWD の周波数とともに、各段のバイト数・秒・bytes_per_s を返す。"""
        with self._lock:
            self._require_halt_mode("速度の計測")
            memory = self._flash_memory()
            options = getattr(self._debug, "options", None) or {}
            out = {"frequency_hz": options.get("frequency"), "ram": None, "flash_read": None, "flash_program": None}
            if ram_length:
                out["ram"] = self._bench_ram(memory, ram_length)
            if read_length:
                main = memory.main_flash()
                out["flash_read"] = {"address": main.address,
                                     **memory.read(main.address, min(read_length, main.size))[1].as_dict()}
            if program_address is not None:
                out["flash_program"] = self._bench_program(memory, program_address, program_length)
            self._note_flash("benchmark", out)
            return out

    def _bench_ram(self, memory: FlashMemory, length: int) -> dict:
        from pyocd.core.memory_map import MemoryType
        target = self.target
        region = (memory.memory_map.get_default_region_of_type(MemoryType.RAM)
                  or next(iter(memory.memory_map.iter_matching_regions(type=MemoryType.RAM)), None))
        if region is None:
            raise RuntimeError("ターゲットに RAM の領域が無い")
        length = min(length, region.length)
        pattern = random.Random(length).randbytes(length)
        running = target.get_state().name != "HALTED"
        target.halt()
        saved = bytes(target.read_memory_block8(region.start, length))
        try:
            started = time.perf_counter()
            target.write_memory_block8(region.start, list(pattern))
            target.flush()
            write = Rate(length, time.perf_counter() - started)
            started = time.perf_counter()
            back = bytes(target.read_memory_block8(region.start, length))
            read = Rate(length, time.perf_counter() - started)
        finally:
            target.write_memory_block8(region.start, list(saved))
            target.flush()
            if running:
                target.resume()
        return {"address": region.start, "write": write.as_dict(), "read": read.as_dict(), "verified": back == pattern}

    def _bench_program(self, memory: FlashMemory, address: int, length: int | None) -> dict:
        if length is None:
            length = memory.sector_at(address).size
        blocks = memory.sectors(address, length)
        if blocks[0].address != address or blocks[-1].end != address + length:
            raise ValueError(f"0x{address:08X}+0x{length:X} はセクタの境界に合っていない"
                             f"（0x{blocks[0].address:08X}..0x{blocks[-1].end:08X}）")
        original, _ = memory.read(address, length)
        pattern = Image([Segment(address, random.Random(address).randbytes(length))])
        with memory.preserving([address]):
            try:
                report = self._flash_bp_program(memory, pattern, erase="sector", smart=False)
                verified = memory.verify(pattern).match
            finally:
                self._flash_bp_program(memory, Image([Segment(address, original)]), erase="sector", smart=True)
            restored = memory.verify(Image([Segment(address, original)])).match
        return {"address": address, "length": length,
                "erase": Rate(report.erased_bytes, report.erase_s).as_dict(),
                "program": Rate(report.programmed_bytes, report.program_s).as_dict(),
                "total": Rate(length, report.seconds).as_dict(), "verified": verified, "restored": restored}

    # --- CMSIS-Pack ------------------------------------------------------------------------

    @property
    def packs(self) -> Packs:
        if self._packs is None:
            self._packs = Packs()
        return self._packs

    def pack_find(self, pattern: str, update: bool = False, limit: int = 200) -> dict:
        """CMSIS-Pack の索引を部品番号で探す（グロブ。ワイルドカードが無ければ部分一致）。索引が無いか update なら
        取ってくる（ネットに出る）。部品・Pack・版・入れてあるか（名前の順に limit 個まで。総数 total・切ったか
        truncated・使った上限 limit）。"""
        return self.packs.find(pattern, update, limit)

    def pack_install(self, pattern: str, update: bool = False) -> dict:
        """部品番号に合う部品の CMSIS-Pack を落として入れる（ネットに出る）。以後 target_connect に targets の名前が
        使える（Pack の Flash の書き込みアルゴリズム・メモリマップ・SVD）。"""
        from pyocd.target import normalise_target_type_name
        out = self.packs.install(pattern, update)
        return {**out, "targets": sorted({normalise_target_type_name(p) for p in out["parts"]})}

    def pack_list(self) -> dict:
        """入れてある CMSIS-Pack（名前・版・ファイル）。"""
        return {"packs": self.packs.installed()}

    def pack_load(self, path: str) -> dict:
        """手元の .pack ファイルのデバイスをターゲットとして登録する（この実行の間）。target_connect に使える名前を返す。"""
        return self.packs.load(path)
