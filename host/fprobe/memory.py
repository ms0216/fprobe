"""外部メモリ（DDR3 等）の状態とメモリ試験。

仕様: docs/superpowers/specs/2026-10-16-m8-ddr3-design.md
"""

import time
from dataclasses import dataclass

from . import generated as g
from .device import Device

__all__ = ["Memory", "MemoryTestResult"]


@dataclass(frozen=True)
class MemoryTestResult:
    beats: int
    errors: int
    first_error: int | None
    write_seconds: float
    read_seconds: float

    @property
    def write_bytes_per_s(self) -> float:
        return self.beats * g.MEM_BEAT_BYTES / self.write_seconds

    @property
    def read_bytes_per_s(self) -> float:
        return self.beats * g.MEM_BEAT_BYTES / self.read_seconds


class Memory:
    def __init__(self, dev: Device):
        caps = dev.capabilities()
        if not caps.mem_bytes:
            raise RuntimeError("このデバイスには外部メモリが無い")
        self.dev, self.size_bytes = dev, caps.mem_bytes
        self.beats = caps.mem_bytes // g.MEM_BEAT_BYTES

    @property
    def ready(self) -> bool:
        return bool(self.dev.read32(g.MEM_STATUS) >> g.MEM_STATUS_READY[0] & 1)

    def wait_ready(self, timeout_s: float = 2.0) -> None:
        deadline = time.monotonic() + timeout_s
        while not self.ready:
            if time.monotonic() > deadline:
                raise TimeoutError("外部メモリの初期化・較正が終わらない")
            time.sleep(0.01)

    def test(self, base: int = 0, count: int | None = None, seed: int = 0x5EED1234, *,
             mem_clock_hz: float, timeout_s: float = 30.0) -> MemoryTestResult:
        """base から count 拍に書いて読み戻す（既定は全容量）。mem_clock_hz は周期数を秒にするためのクロック。
        メモリ試験回路は基板立ち上げ用の構成（memtest）だけが持つ。"""
        if not self.dev.capabilities().mem_test:
            raise RuntimeError("この構成にはメモリ試験回路が無い（memtest 構成を書き込んで行う）")
        count = self.beats - base if count is None else count
        if not (0 <= base and 1 <= count and base + count <= self.beats):
            raise ValueError(f"範囲が容量（{self.beats} 拍）を超える: base={base}, count={count}")
        self.wait_ready()
        self.dev.write(g.MEM_TEST_BASE, [base, count, seed])
        self.dev.write32(g.MEM_COMMAND, 1 << g.MEM_COMMAND_TEST[0])
        deadline = time.monotonic() + timeout_s
        while True:
            status = self.dev.read32(g.MEM_STATUS)
            if status >> g.MEM_STATUS_DONE[0] & 1:
                break
            if time.monotonic() > deadline:
                raise TimeoutError("メモリ試験が終わらない")
            time.sleep(0.005)
        errors, first, wcycles, rcycles = self.dev.read(g.MEM_ERRORS, 4)
        return MemoryTestResult(beats=count, errors=errors, first_error=first if errors else None,
                                write_seconds=wcycles / mem_clock_hz, read_seconds=rcycles / mem_clock_hz)
