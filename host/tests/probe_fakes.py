"""試験用: Probe の機器の偽物（CSR・タイムベース・イベント記録のリング・DAP サンプラ）と、偽の pyOCD ターゲット。

イベント記録は FakeRing（fprobe.events.EventLog と同じ口）。使う試験は、EventLog を名前で引くモジュール
（fprobe.probe_window・fprobe.collector 等）に monkeypatch で差し込む（`use_fake_ring`）。
"""

from types import SimpleNamespace

from pyocd.core.target import Target

from fprobe import generated as g
from fprobe.events import ACK_OK, KIND_SAMPLE, KIND_SWO, DapEvent

HZ = 100_000_000                                  # タイムベース・sys のクロック


class FakeDevice:
    """CSR（番地 → 値）と、進めて使うタイムベース、イベント記録のリング（ring）。"""

    def __init__(self, ring_capacity: int = 1 << 16):
        self.csr: dict[int, int] = {}
        self.ticks = 1_000_000
        self.ring: list[DapEvent] = []
        self.ring_written = 0
        self.ring_capacity = ring_capacity
        self.ring_running = False

    # CSR
    def write32(self, address, value):
        self.csr[address] = value

    def write(self, address, values):
        for i, v in enumerate(values):
            self.write32(address + 4 * i, v)

    def read32(self, address):
        return self.csr.get(address, 0)

    def read(self, address, count=1):
        return [self.csr.get(address + 4 * i, 0) for i in range(count)]

    def capabilities(self):
        return SimpleNamespace(sampler=True, sys_clock_hz=HZ, timebase_hz=HZ, board="fake", design="fprobe",
                               jtag=True, inject=True, xtrig=True)

    def time(self):
        return self.ticks

    # 出来事（時刻はタイムベース。リングには下位 48 ビットで入る）
    def _event(self, event: DapEvent) -> None:
        self.ticks = max(self.ticks, event.time + 1)
        if self.ring_running:
            self.ring.append(event)
            self.ring_written += 1
            del self.ring[:-self.ring_capacity]

    def sample(self, t: int, address: int, value: int, ack: int = ACK_OK) -> None:
        self._event(DapEvent(t, KIND_SAMPLE, True, True, 0xC, ack, False, address, value))

    def swo(self, t: int, data: bytes, spacing: int = 50) -> None:
        for i, b in enumerate(data):
            self._event(DapEvent(t + i * spacing, KIND_SWO, False, False, 0, 0, False, 0, b))

    def advance(self, ticks: int) -> None:
        self.ticks += ticks

    def sampler_running(self) -> bool:
        return bool(self.csr.get(g.SAMPLER_CONTROL, 0) & 1)


class FakeRing:
    """fprobe.events.EventLog の偽物（FakeDevice のリングを読む）。"""

    def __init__(self, dev: FakeDevice):
        self.dev = dev
        self.capacity = dev.ring_capacity

    def start(self):
        self.dev.ring, self.dev.ring_written, self.dev.ring_running = [], 0, True

    def running(self):
        return self.dev.ring_running

    def ensure_started(self):
        if not self.dev.ring_running:
            self.start()

    def stop(self):
        self.dev.ring_running = False

    def counts(self):
        return self.dev.ring_written, 0

    def read(self, last=None, end=None):
        base = self.dev.ring_written - len(self.dev.ring)            # リングに残っている最も古いものの番号
        events = self.dev.ring[:max(0, (self.dev.ring_written if end is None else end) - base)]
        return list(events if last is None else events[len(events) - last:] if last else [])


def use_fake_ring(monkeypatch, *modules) -> None:
    for module in modules:
        monkeypatch.setattr(module, "EventLog", FakeRing)


class FakeTarget:
    """pyOCD の CortexM の、メモリ（fprobe.dwarf の読み手に委ねる）・レジスタ・状態・32 ビットの読み書きだけ。
    write_log に 32 ビットの書き込み（番地, 値）を残す。"""

    def __init__(self, memory, registers: dict[str, int] | None = None, halted: bool = True):
        self.memory = memory                      # read_memory(address, length) / write_memory(address, data)
        self.registers = dict(registers or {})
        self.state = Target.State.HALTED if halted else Target.State.RUNNING
        self.write_log: list[tuple[int, int]] = []

    def get_state(self):
        return self.state

    def read_memory_block8(self, address, length):
        return list(self.memory.read_memory(address, length))

    def write_memory_block8(self, address, data):
        self.memory.write_memory(address, bytes(data))

    def read_memory(self, address, transfer_size=32):
        return int.from_bytes(self.memory.read_memory(address, transfer_size // 8), "little")

    def write_memory(self, address, value, transfer_size=32):
        self.write_log.append((address, value))
        self.memory.write_memory(address, value.to_bytes(transfer_size // 8, "little"))

    def flush(self):
        pass

    def read_core_register_raw(self, name):
        return self.registers[name]

    def read_core_register(self, name):
        return self.registers[name]


class DictMemory:
    """番地 → バイト の疎なメモリ（周辺レジスタ等。読んでいない番地は 0）。"""

    def __init__(self, words: dict[int, int] | None = None):
        self.bytes: dict[int, int] = {}
        for address, value in (words or {}).items():
            self.write_memory(address, value.to_bytes(4, "little"))

    def read_memory(self, address, length):
        return bytes(self.bytes.get(address + i, 0) for i in range(length))

    def write_memory(self, address, data):
        for i, b in enumerate(data):
            self.bytes[address + i] = b

    def u32(self, address):
        return int.from_bytes(self.read_memory(address, 4), "little")
