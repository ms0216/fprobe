"""機器なしで画面を試すデモ（fake_daemon.py --demo）の一部: Fault Analyzer・Flash・Breakpoints（条件・ログ・ベクタキャッチ）のデモの値（G08・G11・G02）。

DemoProbe（fake_demo.py）に混ぜる mixin。DemoProbe の偽の RAM（self._mem）・本物の ELF（self._elf）・デモの記録
（self._elf._view。tests/demo_recording.py）・実行の模擬（self.counter・self.state）を使う。値の形は本物の Probe の
同じ名前のメソッドと同じにする（画面はデモと本物を区別しない）。

- 故障: ベクタキャッチの hard_fault を有効にして Continue すると、0.3 秒後に default_handler（ベクタ表の HardFault）で
  止まる。原因は reached_milestone の `milestone = value`（sram_demo.c:13）の str が SRAM の外（0x20018000）を指した
  正確なバスエラー（PRECISERR）が HardFault に格上げされたもの。target_fault は本物の fprobe.fault.analyze に偽のコア
  （故障レジスタと積まれたフレーム）を渡して作る。
- Flash: STM32F401 の 512 KB（16 KB × 4・64 KB・128 KB × 3 のセクタ）を偽の中身で持ち、書き込み・消去・検証・読み出し
  は本物の Image / compare / save_image を使う。速度は SWD 4 MHz のそれらしい値で計算し、進捗の通知 flash.progress を
  送る（実際の待ちは短くする）。RAM の番地の区画は偽の RAM に直接書く（ram_bytes）。
- ブレークポイントの動作（condition・ignore_count・log・message）を覚えて list で返す。走らせると、動作のある
  ブレークポイントは 0.2 秒ごとに reached_milestone に当たる模擬（ログは止まらずに記録、条件が真なら止まる）。
  起動時に、ログ・ブレークポイント（sram_demo.c:13）と、デモの記録の時刻 t を持つ過去のログを置く。
"""

import threading
import time
import zlib
from pathlib import Path

from fprobe import fault
from fprobe.debug import VECTOR_CATCH
from fprobe.dwarf.values import unavailable
from fprobe.flash import Image, Rate, Segment, compare, save_image
from fprobe.flash_bp import DORMANT_LIMIT
from fprobe.halt_risk import scan
from fprobe.liveness import Liveness
from fprobe.probe_breakpoints import _truth
from fprobe.symbols import Symbol

__all__ = ["TargetDemo"]

HZ = 10_000_000                                   # デモの記録のタイムベース（tests/demo_recording.py と同じ）
FLASH_BASE, FLASH_SIZE = 0x08000000, 0x80000      # STM32F401RE の Flash
SECTORS = [(0x08000000, 4, 0x4000), (0x08010000, 1, 0x10000), (0x08020000, 3, 0x20000)]
PAGE = 0x400                                      # smart で比べる単位
SWD_HZ = 4_000_000
# それらしい速さ（バイト/秒。SWD 4 MHz の STM32F4）
ERASE_BPS, PROGRAM_BPS, READ_BPS, RAM_WRITE_BPS = 96_000, 42_000, 380_000, 300_000
WAIT_LIMIT_S = 1.5                                # 1 回の操作で実際に待つ時間の上限（進捗を見せる程度）
FPB_COMPARATORS = 6                               # Cortex-M4 の FPB
# 故障の模擬（sram_demo.c:13 の str r0, [r1] が SRAM の外を指した）
FAULT_PC, FAULT_LR, BAD_ADDRESS = 0x2000004C, 0x200000B5, 0x20018000
FAULT_SP, EXC_RETURN = 0x20017FC8, 0xFFFFFFF9
CPUID_M4 = 0x410FC241
PACKS = [  # 索引の一部（部品・メーカー・Pack・版）
    ("STM32F401RETx", "STMicroelectronics", "Keil.STM32F4xx_DFP", "2.17.1"),
    ("STM32F401CCUx", "STMicroelectronics", "Keil.STM32F4xx_DFP", "2.17.1"),
    ("STM32F411RETx", "STMicroelectronics", "Keil.STM32F4xx_DFP", "2.17.1"),
    ("nRF52840_xxAA", "Nordic Semiconductor", "NordicSemiconductor.nRF_DeviceFamilyPack", "8.44.1"),
    ("RP2040", "Raspberry Pi", "RaspberryPi.RP2040_DFP", "1.0.0"),
]


class _FaultCore:
    """fprobe.fault.analyze に渡す偽のコア（read32・read_core_register）。故障していなければ例外の外で止まった様子。"""

    def __init__(self, faulted: bool, value: int):
        self.faulted = faulted
        frame = [value, BAD_ADDRESS, 0xFF, 0, 0x2A, FAULT_LR, FAULT_PC, 0x61000000]   # r0〜r3・r12・lr・pc・xpsr
        self.memory = {FAULT_SP + 4 * i: w for i, w in enumerate(frame)}
        self.memory.update({fault.CPUID: CPUID_M4, fault.CPACR: 0x00F00000})
        if faulted:
            self.memory.update({fault.CFSR: 1 << 9 | 1 << 15, fault.HFSR: 1 << 30, fault.DFSR: 1 << 3,
                                fault.BFAR: BAD_ADDRESS})
        else:
            self.memory.update({fault.DFSR: 1 << 0 | 1 << 1})

    def read32(self, address: int) -> int:
        return self.memory.get(address, 0)

    def read_core_register(self, name: str) -> int:
        regs = {"xpsr": 0x61000003, "lr": EXC_RETURN, "msp": FAULT_SP, "psp": 0x20001380} if self.faulted \
            else {"xpsr": 0x61000000, "lr": 0x200000B5, "msp": 0x20017FE8, "psp": 0x20001380}
        return regs[name]


class TargetDemo:
    def _init_target(self) -> None:
        """DemoProbe.__init__ から（ELF を読み、接続・停止した後）。"""
        self._vc: set[str] = set()
        self._bp_actions: dict[int, dict] = {}
        self._bp_log: list[dict] = []
        self._bp_seq = 0
        self._fault_value = 0
        self._flash_mem = bytearray(b"\xff" * FLASH_SIZE)
        image = bytes((i * 37 + (i >> 8)) & 0xFF for i in range(0x5000))      # 前に書いてあったファームウェア
        self._flash_mem[:len(image)] = image
        self._flash_bp = {"enabled": True, "dormant_limit": DORMANT_LIMIT, "scratch": None}
        self._packs = {"Keil.STM32F4xx_DFP"}
        self._flash_algos: dict[str, dict] = {}         # flash_algo_add で足したアルゴリズム（名前 → 指定）
        self._runner: threading.Thread | None = None
        # ログ・ブレークポイント（milestone の値を残す）と、デモの記録の中で当たった過去のログ
        address = self.breakpoint_set("sram_demo.c:13", log=["value", "counter"],
                                      message="milestone {value} (counter={counter})")["address"]
        now = time.monotonic()
        for k in range(1, 8):
            t = int(k * 0.25 * HZ) + 1234
            n = {"kind": "int", "value": 256 * k}
            self._add_log(address, k, {"value": n, "counter": n}, now - (2.0 - t / HZ), t)
        self._bp_actions[address]["hits"] = 7
        # 走らせたままの見張り: 走っている間の PC の見本（DWT_PCSR の代わり）を本物の fprobe.liveness に渡す
        self._clock = time.time
        self._fatal_pc: int | None = None
        self._demo_live = Liveness(self._function_at)
        self._demo_live_settings = {"functions": [], "patterns": [], "pc_samples": None, "tick_variable": None}
        if self._fatal_after_s is not None:
            # 致命的エラーのデモ: 利用者が明示する条件の例として、故障の処理の名前と、足した無線・RTOS の名前を渡しておく
            self.liveness_set(functions=["default_handler"], pc_samples=4)
            self.halt_risk_set(names=["RADIO_IRQHandler", "_kernel"])
            self.state = "RUNNING"
            threading.Timer(self._fatal_after_s, self._crash_running).start()

    # --- 故障・ベクタキャッチ -----------------------------------------------------------------------

    @property
    def _faulted(self) -> bool:
        return self.state == "HALTED" and self.function == "default_handler"

    def target_fault(self):
        self._need()
        if self.state != "HALTED":
            raise RuntimeError("ターゲットが止まっていない（target_halt の後に呼ぶ）")
        return fault.analyze(_FaultCore(self._faulted, self._fault_value), self._elf._symbols,
                             FAULT_SP if self._faulted else None)

    def vector_catch(self, catch=None):
        self._need()
        names = None
        if catch is not None:
            names = list(VECTOR_CATCH) if "all" in catch else list(dict.fromkeys(catch))
            unknown = [n for n in names if n not in VECTOR_CATCH]
            if unknown:
                raise ValueError(f"知らないベクタキャッチ {unknown}（{', '.join(VECTOR_CATCH)}、all）")
            self._vc = {n for n in names if n != "secure_fault"}               # ARMv7-M に SecureFault は無い
        out = {"enabled": [n for n in VECTOR_CATCH if n in self._vc], "available": list(VECTOR_CATCH)}
        if names is not None:
            out["unsupported"] = [n for n in names if n not in self._vc]
        return out

    def vector_catch_get(self):
        return self.vector_catch(None)

    # --- 致命的エラー（止めると壊れるターゲット） -------------------------------------------------------

    def halt_risk(self):
        """致命的エラーのデモでは、デモの ELF に無線の割り込みと RTOS のカーネルのシンボルを足して探す。"""
        out = self._elf.halt_risk()
        if self._fatal_after_s is not None:
            extra = [Symbol("RADIO_IRQHandler", 0, 0, "function"), Symbol("_kernel", 0, 0, "object")]
            out.update(scan([*self._elf._symbols.names(), *extra], self._elf._halt_names, self._elf._halt_patterns))
        return out

    def liveness_set(self, functions=None, patterns=None, pc_samples=None, tick_variable=None):
        self._demo_live = Liveness(self._function_at, functions or [], patterns or [])
        self._demo_live_settings = {"functions": list(functions or []), "patterns": list(patterns or []),
                                    "pc_samples": pc_samples, "tick_variable": tick_variable}
        return self.liveness_get()

    def liveness_get(self):
        return dict(self._demo_live_settings)

    def _crash_running(self) -> None:
        """走っている間に故障し、故障の処理（default_handler）の無限ループを回り続ける（止まらない）。"""
        if self.state == "RUNNING":
            self._fatal_pc = (self._elf._symbols.symbol("default_handler").address & ~1) + 2

    def _demo_fatal(self) -> dict | None:
        """走っている間の致命的エラーの見張り（本物の Probe の target_status の fatal と同じ形）。"""
        if self.state != "RUNNING" or self._fatal_pc is None:
            self._demo_live.reset()
            return None
        return self._demo_live.observe(False, [self._fatal_pc] * (self._demo_live_settings["pc_samples"] or 0), self.counter,
                                      self._clock())

    def _demo_resume(self) -> bool:
        """DemoProbe.target_resume の前に。故障のハンドラの中・ベクタキャッチで故障する・動作のあるブレークポイントの
        模擬を引き受けたら True（ふつうのブレークポイントだけなら False で DemoProbe に任せる）。"""
        if self.function == "default_handler":
            self.state = "RUNNING"                                    # ハンドラの無限ループを回り続ける
            self._crash_running()
            return True
        if "hard_fault" in self._vc:
            self.state = "RUNNING"

            def crash():
                self._fault_value = (self.counter | 0xFF) + 1
                self.counter = self._fault_value
                self.function, self.line = "default_handler", 32
                self.pc, self.state = self._elf._symbols.symbol("default_handler").address & ~1, "HALTED"
            threading.Timer(0.3, crash).start()
            return True
        if not self._bp_actions:
            return False
        self.state = "RUNNING"
        if self._runner is None or not self._runner.is_alive():
            self._runner = threading.Thread(target=self._run_actions, daemon=True)
            self._runner.start()
        return True

    def _run_actions(self) -> None:
        """走っている間、0.2 秒ごとにループを 1 周する（tick が counter を 256 進め、reached_milestone を通る）。通った
        ブレークポイントは本物と同じ順（ignore_count → condition → log）で扱い、止まるならその行で止める。"""
        while self.state == "RUNNING":
            time.sleep(0.2)
            if self.state != "RUNNING":
                return
            self.counter = (self.counter | 0xFF) + 1
            self._values.write("value", self.counter, self._line_address(13))
            for address in sorted(self.breakpoints.copy()):
                place = self._elf._place_of(address)
                if place.get("function") not in ("tick", "reached_milestone", "reset_handler"):
                    continue
                if self._break_here(address, address):
                    self._go(place["function"], place["line"])
                    return

    def _break_here(self, address: int, pc: int) -> bool:
        action = self._bp_actions.get(address)
        if action is None:
            return True
        action["hits"] += 1
        if action["hits"] <= action["ignore_count"]:
            return False
        if action["condition"]:
            try:
                if not _truth(self._values.read(action["condition"], pc)["value"]):          # 本物と同じ真偽
                    return False
            except Exception as e:                                     # 評価できない: 止めて知らせる
                action["last_error"] = f"{action['condition']}: {e}"
                return True
        if action["log"] is None:
            return True
        values = {}
        for expression in action["log"]:
            try:
                values[expression] = self._values.read(expression, pc)["value"]
            except Exception as e:
                values[expression] = unavailable(str(e))
        self._add_log(address, action["hits"], values, time.monotonic(), None)
        return False

    # --- ブレークポイント（動作つき） ----------------------------------------------------------------

    def breakpoint_set(self, location, condition=None, ignore_count=0, log=None, message=None):
        out = super().breakpoint_set(location)
        address = out["address"]
        if condition or ignore_count or log is not None:
            hits = self._bp_actions.get(address, {}).get("hits", 0)
            self._bp_actions[address] = {"condition": condition, "ignore_count": ignore_count,
                                         "log": None if log is None else list(log), "message": message, "hits": hits,
                                         "last_error": None}
        else:
            self._bp_actions.pop(address, None)
        return out

    def breakpoint_clear(self, location=None):
        if location is None:
            self._bp_actions.clear()
        else:
            self._bp_actions.pop(self._address(location), None)
        return super().breakpoint_clear(location)

    def breakpoint_list(self):
        out = super().breakpoint_list()
        for i, b in enumerate(x for x in out["breakpoints"] if x["enabled"]):
            b["kind"] = "hw" if i < FPB_COMPARATORS else "sw"            # デモの ELF は SRAM（比較器が尽きたら RAM の BKPT）
        for b in out["breakpoints"]:
            if b["address"] in self._bp_actions:
                b["action"] = dict(self._bp_actions[b["address"]])
        return out

    def _add_log(self, address: int, hit: int, values: dict, host_s: float, t: int | None) -> None:
        self._bp_seq += 1
        entry = {"seq": self._bp_seq, "host_s": host_s, "address": address, **self._elf._place_of(address),
                 "hit": hit, "message": self._bp_actions.get(address, {}).get("message"), "values": values}
        if t is not None:
            entry["t"] = t
        self._bp_log.append(entry)

    def breakpoint_log(self, since=0, limit=200):
        found = [e for e in self._bp_log if e["seq"] > since]
        items = found[:limit]
        return {"entries": items, "next": items[-1]["seq"] if items else since, "lost": 0,
                "more": len(found) > limit, "kept_limit": 10_000,
                "last": self._bp_seq}

    def flash_breakpoints(self, enabled=None, dormant_limit=None, scratch=None):
        if dormant_limit is not None and dormant_limit < 1:
            raise ValueError("dormant_limit は 1 以上")
        for key, value in (("enabled", enabled), ("dormant_limit", dormant_limit), ("scratch", scratch)):
            if value is not None:
                self._flash_bp[key] = value
        if not self.connected:
            return {**self._flash_bp, "available": False}
        hardware = sorted(self.breakpoints)[:FPB_COMPARATORS]
        return {"enabled": self._flash_bp["enabled"], "hardware": hardware, "flash": [], "dormant": [],
                "dormant_limit": self._flash_bp["dormant_limit"], "scratch": self._flash_bp["scratch"] or 0x20000000,
                "stats": {"commits": 0, "sector_writes": 0, "emulated": 0, "displaced": 0, "rewrites": 0,
                          "dormant_hits": 0, "restored": 0},
                "last_rewrite": None, "journal": None, "available": True}

    # --- Flash ---------------------------------------------------------------------------------

    def flash_upload(self, name, data_base64):
        return self._elf.flash_upload(name, data_base64)

    def _progress(self, operation: str, phase: str, seconds: float, started: float, wait: float) -> None:
        """段の進捗を 0 → 1 で送り、wait 秒（全体の WAIT_LIMIT_S の割り当て）だけ実際に待つ。"""
        steps = max(2, min(10, int(wait / 0.1)))
        for i in range(steps + 1):
            if self._publish is not None:
                self._publish("flash.progress", {"operation": operation, "phase": phase, "fraction": round(i / steps, 4),
                                                 "elapsed_s": round(time.monotonic() - started, 3)})
            if i < steps:
                time.sleep(wait / steps)

    def _waits(self, seconds: list[float]) -> list[float]:
        """模擬の秒（段ごと）を、実際に待つ秒に縮める（合わせて WAIT_LIMIT_S まで）。"""
        total = sum(seconds) or 1.0
        scale = min(1.0, WAIT_LIMIT_S / total)
        return [s * scale for s in seconds]

    @staticmethod
    def _in_flash(address: int) -> bool:
        return FLASH_BASE <= address < FLASH_BASE + FLASH_SIZE

    @staticmethod
    def _sectors(start: int, end: int) -> list[tuple[int, int]]:
        out = []
        for base, count, size in SECTORS:
            for i in range(count):
                a = base + i * size
                if a < end and a + size > start:
                    out.append((a, size))
        return out

    def _read(self, address: int, length: int) -> bytes:
        if self._in_flash(address):
            offset = address - FLASH_BASE
            return bytes(self._flash_mem[offset:offset + length])
        return self._mem.read(address, length)

    def flash(self, path, address=None, erase="auto", verify=True, smart=True, reset=True, preserve=False):
        self._need()
        image = Image.load(path, address)
        started = time.monotonic()
        flash_segments = [s for s in image.segments if self._in_flash(s.address)]
        ram_segments = [s for s in image.segments if not self._in_flash(s.address)]
        if erase == "chip":
            sectors = self._sectors(FLASH_BASE, FLASH_BASE + FLASH_SIZE)
        else:
            sectors = sorted({x for s in flash_segments for x in self._sectors(s.address, s.end)})
        # smart: 中身が同じページは書かない（chip は全体を消すので全部書く）。sector はページを書くセクタだけ消し、
        # セクタのほかの中身は保つ（pyOCD と同じ）
        pages, skipped = [], 0
        for s in flash_segments:
            for a in range(s.address, s.end, PAGE):
                data = s.data[a - s.address:a - s.address + PAGE]
                if smart and erase != "chip" and self._read(a, len(data)) == data:
                    skipped += len(data)
                else:
                    pages.append((a, data))
        if erase != "chip":
            sectors = [x for x in sectors if any(x[0] <= a < x[0] + x[1] for a, _ in pages)]
        erase_bytes = sum(n for _, n in sectors)
        program_bytes = sum(len(d) for _, d in pages)
        ram_bytes = sum(len(s.data) for s in ram_segments)
        times = [erase_bytes / ERASE_BPS, program_bytes / PROGRAM_BPS + ram_bytes / RAM_WRITE_BPS,
                 image.size / READ_BPS if verify else 0.0]
        waits = self._waits(times)
        self._progress("program", "erase", times[0], started, waits[0])
        if erase == "chip":
            self._flash_mem[:] = b"\xff" * FLASH_SIZE
        self._progress("program", "program", times[1], started, waits[1])
        for a, data in pages:
            self._flash_mem[a - FLASH_BASE:a - FLASH_BASE + len(data)] = data
        for s in ram_segments:
            try:
                self._mem.write(s.address, s.data)
            except Exception:
                pass                                                  # 偽の RAM の外（デモでは捨てる）
        comparison = None
        if verify:
            self._progress("program", "verify", times[2], started, waits[2])
            comparison = compare(image, self._read)
            comparison.rate = Rate(image.size, times[2])
        if image.format == "elf":
            self.elf_load(path)
        seconds = sum(times) + 0.05
        report = {"bytes": image.size - ram_bytes, "programmed_bytes": program_bytes,
                  "programmed_pages": len(pages), "skipped_bytes": skipped,
                  "erase_method": "chip" if erase == "chip" else "sector",
                  "erase": {**Rate(erase_bytes, times[0]).as_dict(), "sectors": len(sectors)},
                  "program": Rate(program_bytes, times[1]).as_dict(), "analyze_s": 0.004, "ram_bytes": ram_bytes,
                  "seconds": round(times[0] + times[1] + 0.02, 6),
                  "bytes_per_s": Rate(image.size - ram_bytes, times[0] + times[1] + 0.02).bytes_per_s}
        if reset:
            self.counter = 0
            self.function, self.line, self.state = "reset_handler", 28, "RUNNING"
        else:
            self._go("reset_handler", 24)
        return {"image": image.describe(), "program": report,
                "verify": None if comparison is None else comparison.as_dict(), "seconds": round(seconds, 3),
                **self.target_status()}

    def flash_erase(self, mode="range", address=None, length=None, preserve=False):
        self._need()
        if mode not in ("range", "chip", "mass"):
            raise ValueError(f"mode は range / chip / mass（{mode!r}）")
        if mode == "range" and (address is None or length is None):
            raise ValueError("mode range は address と length が要る")
        started = time.monotonic()
        if mode == "mass":
            self._progress("erase", "erase", 0.8, started, 0.4)
            self._flash_mem[:] = b"\xff" * FLASH_SIZE
            return {"method": "mass", "seconds": 0.8}
        sectors = self._sectors(address, address + length) if mode == "range" \
            else self._sectors(FLASH_BASE, FLASH_BASE + FLASH_SIZE)
        if not sectors:
            raise ValueError(f"0x{address:08X} は Flash の領域の外")
        size = sum(n for _, n in sectors)
        self._progress("erase", "erase", size / ERASE_BPS, started, self._waits([size / ERASE_BPS])[0])
        for a, n in sectors:
            self._flash_mem[a - FLASH_BASE:a - FLASH_BASE + n] = b"\xff" * n
        rate = Rate(size, size / ERASE_BPS).as_dict()
        if mode == "chip":
            return {"method": "chip", "rate": rate}
        return {"start": sectors[0][0], "end": sectors[-1][0] + sectors[-1][1], "sectors": len(sectors), "rate": rate}

    def flash_verify(self, path, address=None):
        self._need()
        image = Image.load(path, address)
        started = time.monotonic()
        self._progress("verify", "verify", image.size / READ_BPS, started, self._waits([image.size / READ_BPS])[0])
        comparison = compare(image, self._read)
        comparison.rate = Rate(image.size, image.size / READ_BPS)
        return {**comparison.as_dict(), "image": image.describe()}

    def flash_read(self, path, address=None, length=None):
        self._need()
        address = FLASH_BASE if address is None else address
        length = FLASH_BASE + FLASH_SIZE - address if length is None else length
        started = time.monotonic()
        self._progress("read", "read", length / READ_BPS, started, self._waits([length / READ_BPS])[0])
        data = self._read(address, length)
        fmt = save_image(path, address, data)
        return {"path": str(Path(path)), "format": fmt, "address": address, "bytes": len(data),
                "crc32": zlib.crc32(data), "read": Rate(length, length / READ_BPS).as_dict()}

    def flash_regions(self):
        self._need()
        return {"regions": [{"name": "flash", "start": FLASH_BASE, "size": FLASH_SIZE, "page_size": PAGE,
                             "sectors": [{"start": b, "count": c, "size": s} for b, c, s in SECTORS],
                             "erased_value": 0xFF, "boot": True, "algorithm": "builtin", "usable": True},
                            *(self._algo_region(n, s) for n, s in self._flash_algos.items())],
                "algorithms": [{"name": n, **s} for n, s in self._flash_algos.items()]}

    @staticmethod
    def _algo_region(name, spec):
        """外付けの領域（デモの FLM は中身を読まないので、番地・大きさは指定の値か QSPI NOR のいつもの値）。"""
        start, size = spec["address"] or 0x90000000, spec["size"] or 0x1000000
        return {"name": name, "start": start, "size": size, "page_size": 0x100,
                "sectors": [{"start": start, "count": size // 0x1000, "size": 0x1000}], "erased_value": 0xFF,
                "boot": False, "algorithm": f"flm:{spec['flm']}", "usable": True}

    def flash_algo_add(self, flm, address=None, size=None, ram_address=None, ram_size=None, name=None):
        name = name or Path(flm).stem
        if name in self._flash_algos:
            raise ValueError(f"{name!r} はもう足してある（flash_algo_remove で外す）")
        spec = {"flm": str(flm), "address": address, "size": size, "ram_address": ram_address, "ram_size": ram_size}
        self._flash_algos[name] = spec
        if not self.connected:
            return {"name": name, "pending": True}
        return self._algo_region(name, spec)

    def flash_algo_remove(self, name):
        if name not in self._flash_algos:
            raise KeyError(f"足したアルゴリズム {name!r} は無い")
        del self._flash_algos[name]
        return {"removed": name}

    def flash_benchmark(self, ram_length=0x4000, read_length=0x10000, program_address=None, program_length=None):
        self._need()
        out = {"frequency_hz": SWD_HZ, "ram": None, "flash_read": None, "flash_program": None}
        if ram_length:
            out["ram"] = {"address": 0x20000000, "write": Rate(ram_length, ram_length / RAM_WRITE_BPS).as_dict(),
                          "read": Rate(ram_length, ram_length / READ_BPS).as_dict(), "verified": True}
        if read_length:
            n = min(read_length, FLASH_SIZE)
            out["flash_read"] = {"address": FLASH_BASE, **Rate(n, n / READ_BPS).as_dict()}
        if program_address is not None:
            sectors = self._sectors(program_address, program_address + (program_length or 1))
            if not sectors or sectors[0][0] != program_address:
                raise ValueError(f"0x{program_address:08X} はセクタの境界に合っていない")
            n = sum(s for _, s in sectors) if program_length is None else program_length
            out["flash_program"] = {"address": program_address, "length": n,
                                    "erase": Rate(n, n / ERASE_BPS).as_dict(),
                                    "program": Rate(n, n / PROGRAM_BPS).as_dict(),
                                    "total": Rate(n, n / ERASE_BPS + n / PROGRAM_BPS).as_dict(),
                                    "verified": True, "restored": True}
        time.sleep(0.3)
        return out

    # --- CMSIS-Pack（ネットに出ない。索引はデモの一部） -------------------------------------------------

    def pack_list(self):
        return {"packs": [{"pack": p, "version": v, "path": f"C:/Users/demo/AppData/Local/cmsis-pack-manager/{p}.{v}.pack"}
                          for p, v in sorted({(pack, ver) for _, _, pack, ver in PACKS}) if p in self._packs]}

    def pack_find(self, pattern, update=False, limit=200):
        from fprobe.packs import glob_matcher
        match = glob_matcher(pattern)
        parts = [{"part": part, "vendor": vendor, "pack": pack, "version": ver, "installed": pack in self._packs}
                 for part, vendor, pack, ver in PACKS if match(part)]
        return {"parts": parts[:limit], "total": len(parts), "truncated": len(parts) > limit, "limit": limit}

    def pack_install(self, pattern, update=False):
        parts = self.pack_find(pattern)["parts"]
        if not parts:
            raise ValueError(f"{pattern!r} に合う部品が索引に無い（target_pack_find で探す）")
        packs = sorted({(p["pack"], p["version"]) for p in parts})
        self._packs.update(p for p, _ in packs)
        time.sleep(0.5)
        return {"packs": [{"pack": p, "version": v} for p, v in packs], "parts": [p["part"] for p in parts],
                "targets": sorted(p["part"].lower() for p in parts)}

    def pack_load(self, path):
        """デモの索引にある Pack の名前のファイルだけ読める（中身は読まない）。"""
        stem = Path(path).name
        parts = [part for part, _, pack, _ in PACKS if stem.startswith(pack + ".")]
        if not parts:
            raise ValueError(f"{path} にデバイスが無い（デモの索引にある Pack の名前のファイルだけ読める）")
        return {"path": str(path), "targets": sorted(p.lower() for p in parts)}
