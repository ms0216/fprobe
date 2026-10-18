"""機器なしで画面の各パネルの働きを見せるデモ（fake_daemon.py --demo）: 偽の機器（FakeProbe）に、デモの ELF を読んで
接続・停止した状態と、ソース・レジスタ・メモリ・呼び出し履歴・ローカル・RTOS・周辺レジスタ・RTT・ステップ・
捕捉と記録（Timeline）の偽の値を足したもの。

デモの ELF は host/firmware/sram_demo/sram_demo.elf（本物の行番号・逆アセンブル・SVD を使い、実行と値だけ偽物）。
仕様: docs/superpowers/specs/2026-10-18-ui-layout-design.md §4
"""

import tempfile
import threading
import time
from pathlib import Path

from fake_daemon import FakeProbe
from fake_demo_io import IoDemo
from fake_demo_rtos import RtosDemo
from fake_demo_sampling import SamplingDemo
from fake_demo_search import SearchDemo
from fake_demo_target import TargetDemo
from fake_values import DemoMemory, DemoValues, GLOBALS
from fprobe.dwarf.cexpr import UnknownNameError
from fprobe.memview import format_hexdump, hexdump
from fprobe.dwarf.inspector import LOCAL_HINT, check_expression
from fprobe.probe_elf_files import DEMO_ELF
from fprobe.symbols import Symbols

SOURCE = "sram_demo.c"
# 関数ごとの行の流れ（ステップの模擬）。tick の 19 行は counter が 256 の倍数のときだけ
FLOW = {"reset_handler": [28], "tick": [17, 18, 19, 21], "reached_milestone": [13, 14]}
TASKS = [  # FreeRTOS 風のタスク（デモの値）
    ("main", 3, "running", 0x20001000, 1024, 380, 512),
    ("sensor", 2, "blocked", 0x20001400, 512, 140, 300,      # logger の持つ log_mutex を待つ（優先度の逆転のデモ）
     {"reason": "receive", "object": 0x20003080, "name": "log_mutex", "kind": "mutex", "holder": "logger"}),
    ("comm", 2, "blocked", 0x20001600, 768, 210, 420, {"reason": "receive", "name": "rx_queue", "kind": "queue"}),
    ("logger", 1, "ready", 0x20001900, 512, 96, 180),
    ("IDLE", 0, "ready", 0x20001b00, 256, 64, 88),
]


COUNTER = next(v.address for v in GLOBALS if v.name == "counter")
MILESTONE = next(v.address for v in GLOBALS if v.name == "milestone")


class DemoProbe(SamplingDemo, RtosDemo, SearchDemo, TargetDemo, IoDemo, FakeProbe):
    """偽の機器のデモ。ビューごとのデモの値は fake_demo_*.py の mixin（ビューのまとまりごとに分けた）。"""

    def __init__(self, fatal_after_s: float | None = None):
        """fatal_after_s: 致命的エラーのデモ（止めると壊れる手がかりのある ELF として、走り出して fatal_after_s 秒後に
        故障の処理の無限ループを回り続ける。fake_demo_target.TargetDemo）。"""
        self._fatal_after_s = fatal_after_s
        self._mem = DemoMemory(Symbols(str(DEMO_ELF)).segments)     # counter は RAM の値（FakeProbe が初めに書く）
        self._view_previous: dict[tuple[int, int], bytes] = {}
        self._regs: dict[str, int] = {}                             # 書いた周辺レジスタ（無ければリセット値）
        self._reg_written: dict[str, int] = {}                      # 書いたコアレジスタ（pc 以外）
        super().__init__()
        self._values = DemoValues(self._mem, self._function_at, self._describe)
        self.function, self.line = "reset_handler", 28
        self.started = time.monotonic()
        self.elf_load(str(DEMO_ELF))
        self.connected, self.state = True, "HALTED"
        self.link = {"target": "stm32f401xe", "frequency_hz": 4_000_000, "wire": "swd"}
        self.pc = self._line_address(28)
        self.counter = 0x2A
        self.la_capture()
        for init in (self._init_sampling, self._init_rtos, self._init_search, self._init_target, self._init_io):
            init()

    @property
    def counter(self) -> int:
        return self._mem.word(COUNTER)

    @counter.setter
    def counter(self, value: int) -> None:
        self._mem.set_word(COUNTER, value)

    def _function_at(self, pc: int | None) -> str | None:
        f = self._elf._symbols.function_at(pc) if pc is not None else None
        return f.name if f is not None else None

    def _describe(self, address: int) -> str | None:
        found = self._values.describe(address)
        if found is None and (hit := self._elf._symbols.symbol_at(address & ~1)) is not None:
            symbol, offset = hit
            found = symbol.name if offset == 0 else f"{symbol.name}+{offset}"
        return found

    # --- 実行（行の流れの模擬） ---------------------------------------------------------------------

    def _line_address(self, line: int) -> int:
        return self._elf._symbols.resolve(f"{SOURCE}:{line}")

    def _go(self, function: str, line: int) -> dict:
        self.function, self.line, self.pc, self.state = function, line, self._line_address(line), "HALTED"
        if function == "tick" and line == 17:
            self.counter += 1
        if function == "reached_milestone":
            self._values.write("value", self.counter, self.pc)       # 引数 value = counter
            self._mem.set_word(MILESTONE, self.counter)
        return {"reason": "step", "pc": self.pc, **self._elf._place_of(self.pc)}

    def _next_in(self, function: str, line: int) -> tuple[str, int]:
        flow = [n for n in FLOW[function] if not (n == 19 and (self.counter & 0xFF))]
        after = [n for n in flow if n > line]
        if after:
            return function, after[0]
        return {"tick": ("reset_handler", 28), "reached_milestone": ("tick", 21)}.get(function, (function, flow[0]))

    def step_line(self, mode="over", location=None, timeout_s=5.0, mask_interrupts=True):
        self._need()
        f, n = self.function, self.line
        if mode == "until" and location:
            line = int(str(location).rsplit(":", 1)[-1])
            func = next((k for k, v in FLOW.items() if line in v), "tick")
            return self._go(func, line)
        if mode == "into" and (f, n) == ("reset_handler", 28):
            return self._go("tick", 17)
        if mode == "into" and (f, n) == ("tick", 19):
            return self._go("reached_milestone", 13)
        if mode == "out":
            return self._go(*{"tick": ("reset_handler", 28), "reached_milestone": ("tick", 21)}.get(f, (f, n)))
        if (f, n) == ("reset_handler", 28):
            self.counter += 1                               # tick() を 1 回実行して同じ行へ
            return self._go(f, n)
        return self._go(*self._next_in(f, n))

    def target_step(self, count=1):
        return self.step_line("into")

    def target_halt(self):
        self._need()
        self._fatal_pc = None
        self.counter += int((time.monotonic() - self.started) * 1000) & 0xFF
        self._go("tick", 18)
        return self.target_status()

    def target_reset(self, halt=False):
        self._need()
        self.counter = 0
        self._fatal_pc = None
        if halt:
            self._go("reset_handler", 24)
        else:
            self.state = "RUNNING"

    def target_connect(self, target="stm32f401xe", frequency_hz=4_000_000, connect_mode="halt", jtag_chain=None,
                       script=None):
        self.connected = True
        self.link = {"target": target, "frequency_hz": frequency_hz, "wire": "jtag" if jtag_chain else "swd"}
        self._go(self.function, self.line)
        return {"target": target, "frequency_hz": frequency_hz}

    def target_status(self):
        out = super().target_status()
        if self.connected:
            out.update(target="stm32f401xe", frequency_hz=4_000_000, core="Cortex-M4")
            fatal = self._demo_fatal()
            if fatal is not None:
                out["fatal"] = fatal
        return out

    # --- 止まったときの値 --------------------------------------------------------------------------

    def read_registers(self, names=None):
        regs = {f"r{i}": v for i, v in enumerate([self.counter, 0x20000060, 0xFF, 0, 0x20000060, 0, 0, 0x20017FE8,
                                                    0, 0, 0, 0, 0x2A])}
        regs.update(sp=0x20017FE8, lr=self._line_address(28) | 1, pc=self.pc, xpsr=0x61000000,
                    msp=0x20017FE8, psp=0x20001380, primask=0, basepri=0, faultmask=0, control=0)
        regs.update(self._reg_written)
        return {k: v for k, v in regs.items() if not names or k in names}

    def write_register(self, name, value):
        """コアレジスタに書く（停止中）。pc は実行の模擬の場所も動かす。"""
        self._need()
        if self.state != "HALTED":
            raise RuntimeError("ターゲットが止まっていない")
        if name not in self.read_registers():
            raise ValueError(f"レジスタ {name!r} は無い")
        if name == "pc":
            self.pc = value
        else:
            self._reg_written[name] = value & 0xFFFF_FFFF

    def backtrace(self, variables=False, max_frames=64, max_elements=64):
        self._need()
        # default_handler: 故障の模擬（fake_demo_target.py）。例外で中断された reached_milestone から先
        frames = {"reset_handler": [], "tick": ["reset_handler"], "reached_milestone": ["tick", "reset_handler"],
                  "default_handler": ["reached_milestone", "reset_handler"]}
        lines = {"reset_handler": 28, "tick": 19, "reached_milestone": 13}
        pcs = [self.pc] + [self._line_address(lines[f]) for f in frames[self.function]]
        return [{"index": i, "pc": pc, "note": "exception" if self.function == "default_handler" and i == 1 else "",
                 "cfa": 0x20007FF0 - 0x20 * i, **self._elf._place_of(pc)} for i, pc in enumerate(pcs)][:max_frames]

    def _frame_pc(self, frame: int) -> int:
        frames = self.backtrace()
        if not 0 <= frame < len(frames):
            raise IndexError(f"フレーム {frame} は無い（{len(frames)} 段）")
        return frames[frame]["pc"]

    def frame_variables(self, frame=0, max_elements=64):
        self._need()
        pc = self._frame_pc(frame)
        return self._values.variables(self._function_at(pc), max_elements)

    def variable_read(self, name, index=None):
        self._need()
        expression = name if index is None else f"{name}[{index}]"
        return {"name": name, "value": self._values.read(expression, self.pc)["value"]}

    def variable_write(self, name, value, index=None):
        self._need()
        self._values.write(name if index is None else f"{name}[{index}]", value, self.pc)

    def evaluate(self, expression, frame=0, max_elements=64, live=False):
        self._need()
        if live:                                                     # 走らせたまま: 大域・静的だけ（本物と同じ）
            try:
                return self._values.read(expression, None, max_elements)
            except UnknownNameError as e:
                raise UnknownNameError(f"{e}{LOCAL_HINT}") from None
        return self._values.read(expression, self._frame_pc(frame), max_elements)

    def expression_check(self, expression):
        return check_expression(self._values.info, expression)          # デモの型（本物と同じ調べ方）

    def write_expression(self, expression, value, frame=0):
        self._need()
        pc = self._frame_pc(frame)
        self._values.write(expression, value, pc)
        return self._values.read(expression, pc)

    def type_info(self, name):
        return self._values.type_info(name)

    def disasm_around(self, address=None, before=8, after=8, from_elf=False):
        return self._elf.disasm_around(self.pc if address is None else address, before, after, from_elf=True)

    def disasm_range(self, address, length, from_elf=False, offset=0, limit=256):
        return self._elf.disasm_range(address, length, True, offset, limit)

    def disasm_function(self, location, from_elf=False, offset=0, limit=256):
        return self._elf.disasm_function(location, True, offset, limit)

    def memory_view(self, address, length, unit=8, width=16, compare=False, from_elf=False):
        self._need()
        data = self._mem.read(address, length)
        previous = self._view_previous.get((address, length)) if compare else None
        self._view_previous[(address, length)] = data
        rows = hexdump(data, address, width=width, unit=unit, previous=previous)
        return {"memory": "target", "address": address, "length": length, "unit": unit,
                "rows": [{"address": r.address, "values": list(r.values), "text": r.text, "changed": list(r.changed)}
                         for r in rows], "dump": format_hexdump(rows, unit),
                "names": self._elf._names_in(address, length) + self._values.names_in(address, length)}

    def write_memory(self, address, data_hex):
        self._need()
        self._mem.write(address, bytes.fromhex(data_hex))

    def read_memory(self, address, length):
        self._need()
        return self._mem.read(address, length).hex()

    # --- 周辺レジスタ（同梱の SVD。値はリセット値） ------------------------------------------------------

    def _svd(self):
        if self._elf._svd is None:
            self._elf.svd_load(device="STM32F401")
        if getattr(self._elf._svd, "_demo_enums", False) is False:
            self._elf._svd = _with_gpio_enums(self._elf._svd)
        return self._elf._svd

    def svd_load(self, device=None, path=None):
        out = self._elf.svd_load(device=device or "STM32F401", path=path)
        self._svd()
        return out

    def svd_peripherals(self, filter=None):
        self._svd()
        return self._elf.svd_peripherals(filter)

    def svd_registers(self, peripheral):
        self._svd()
        return self._elf.svd_registers(peripheral)

    def peripheral_read(self, path, allow_side_effects=False):
        dev = self._svd()
        regs = [dev.register(path)] if "." in path else list(dev.peripheral(path).registers)
        out = []
        for r in regs:
            value = self._regs.get(r.path, r.reset_value or 0)
            out.append({"path": r.path, "address": r.address, "value": value,
                        "fields": [f.as_dict() for f in r.decode(value)]})
        return {"registers": out, "skipped": []}

    def peripheral_write(self, path, fields=None, value=None, allow_side_effects=False):
        """書いた値を覚える（読み・変更・書き込み。値の形は本物と同じ）。"""
        self._need()
        if (fields is None) == (value is None):
            raise ValueError("fields か value のどちらか 1 つを指定する")
        r = self._svd().register(path)
        before = self.peripheral_read(r.path)["registers"][0]
        if fields is not None:
            value = r.encode(before["value"], fields)
        self._regs[r.path] = value & ((1 << r.size) - 1)
        return {"path": r.path, "written": value, "before": before, "after": self.peripheral_read(r.path)["registers"][0]}

    # --- RTOS ------------------------------------------------------------------------------------

    def rtos_info(self):
        return {"rtos": "FreeRTOS", "port": "ARM_CM4F", "tasks": len(TASKS)}

    def rtos_tasks(self, live=False):
        tasks = []
        for i, (name, prio, state, base, size, used, peak, *wait) in enumerate(TASKS):
            t = {"handle": 0x20002000 + 0x60 * i, "name": name, "priority": prio, "state": state, "number": i + 1,
                 "stack": {"base": base, "end": base + size, "pointer": base + size - used, "untouched": size - peak,
                           "fill": "intact", "overflow": False, "size": size, "used": used, "peak_used": peak}}
            if wait:
                t["wait"] = wait[0]
            tasks.append(t)
        return {"rtos": "FreeRTOS", "scheduler": "running", "tick": 1000 + self.counter, "current": "main",
                "tasks": tasks, "warnings": [], "halted": self.state == "HALTED", "consistent": True}

    # 端末の源（RTT・SWO・UART・セミホスティング）は fake_demo_io.py（IoDemo）

    def target_resume(self):
        self._need()
        if self._demo_resume():                 # ベクタキャッチ・故障・動作のあるブレークポイント（fake_demo_target.py）
            return
        self.state = "RUNNING"
        if self.breakpoints:
            def hit():
                address, location = next(iter(self.breakpoints.items()))
                line = int(location.rsplit(":", 1)[-1]) if ":" in location else 13
                func = next((k for k, v in FLOW.items() if line in v), "reached_milestone")
                self.counter = (self.counter | 0xFF) + 1
                self._go(func, line)
            threading.Timer(0.3, hit).start()


# GPIO の MODER・OSPEEDR の各フィールドの値の名前（同梱の STM32F401 の SVD には列挙が無い。多くの SVD にはある。
# 画面の周辺レジスタの選択肢を機器なしで見せる）
_GPIO_ENUMS = {"MODER": ("Input", "Output", "Alternate", "Analog"), "OSPEEDR": ("Low", "Medium", "High", "VeryHigh")}


def _with_gpio_enums(device):
    from dataclasses import replace

    from fprobe.svd import Device, EnumValue

    def field(register, f):
        names = _GPIO_ENUMS.get(register.name) if register.peripheral.startswith("GPIO") else None
        if not names or f.bit_width != 2:
            return f
        return replace(f, enums=tuple(EnumValue(n, v, 3) for v, n in enumerate(names)))

    peripherals = tuple(replace(p, registers=tuple(replace(r, fields=tuple(field(r, f) for f in r.fields))
                                                   for r in p.registers)) for p in device.peripherals)
    out = Device(device.name, device.description, device.cpu, device.width, peripherals)
    out._demo_enums = True
    return out


def start(probe: DemoProbe) -> None:
    """デモの記録（Timeline の「記録」）も開く。"""
    probe.open_demo_recording(Path(tempfile.mkdtemp(prefix="fprobe-demo-")) / "demo")
