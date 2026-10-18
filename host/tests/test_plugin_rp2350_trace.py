"""プラグイン fprobe.plugins.rp2350_trace: RP2350 のトレース FIFO と DMA で、ETM のトレースをターゲットの SRAM の
リングに溜め、SWD で読んで解読する。本体の公開 API（ProbeCaller 経由）だけを使う。偽のターゲットのメモリで。"""

from types import SimpleNamespace

import pytest

import etm_encoder as enc
from etm_fakes import CSPSR, ETM, TRCPRGCTLR, CoreSightMemory
from fprobe.mcp_server import Probe
from fprobe.plugins import ProbeCaller
from fprobe.plugins.rp2350_trace import Rp2350Addresses, Rp2350TraceFifo
from fprobe.symbols import Symbols
from probe_fakes import FakeTarget
from test_trace_reconstruct import ELF, EXPECTED, SCENARIO

A = Rp2350Addresses()
BUFFER = 0x2007_8000
CH = 15
CH_BASE = A.dma + 0x40 * CH


@pytest.fixture
def memory():
    m = CoreSightMemory(tpiu=A.tpiu)
    m.write_memory(A.accessctrl_coresight_trace, (0x0000_00B0).to_bytes(4, "little"))   # 既定の許可（例）
    return m


@pytest.fixture
def probe(memory):
    p = Probe("unused")
    p._debug = SimpleNamespace(target=FakeTarget(memory, {"pc": 0x1000_0000}), probe=None, close=lambda: None)
    p._symbols = Symbols(ELF)
    return p


def test_start_sets_up_the_fifo_the_dma_and_the_etm(probe, memory):
    fifo = Rp2350TraceFifo(ProbeCaller(probe), buffer=BUFFER, size=1024, dma_channel=CH)
    fifo.start()
    # ACCESSCTRL.CORESIGHT_TRACE: 上位 16 ビットに 0xACCE、DMA（ビット 6）を足し、元の許可は残す
    assert memory.u32(A.accessctrl_coresight_trace) == 0xACCE_0000 | 0xB0 | 1 << 6
    assert memory.u32(A.trace_fifo + A.fifo_ctrl_status) & 1 == 0          # FLUSH を外して取り込む
    assert memory.u32(CH_BASE + 0x00) == A.trace_fifo + A.fifo_data        # READ_ADDR
    assert memory.u32(CH_BASE + 0x04) == BUFFER                            # WRITE_ADDR
    assert memory.u32(CH_BASE + 0x08) == 0xF000_0001                       # ENDLESS
    ctrl = memory.u32(CH_BASE + 0x0C)
    assert ctrl == (1 | 2 << 2 | 1 << 6 | 10 << 8 | 1 << 12 | CH << 13 | A.dreq << 17)
    assert memory.read_memory(BUFFER, 1024) == bytes(1024)                 # リングは 0 で埋める（周回前の印）
    assert memory.u32(ETM + TRCPRGCTLR) == 1 and memory.u32(A.tpiu + CSPSR) == 1 << 3


def test_stop_reads_the_ring_from_the_write_pointer_and_decodes(probe, memory):
    fifo = Rp2350TraceFifo(ProbeCaller(probe), buffer=BUFFER, size=1024, dma_channel=CH)
    fifo.start()
    stream = enc.FSYNC + enc.frames([(0x10, SCENARIO)])
    # DMA が書いたことにする: 周回して、書き込み位置は 300 バイト目
    linear = bytes([0x33]) * (1024 - len(stream)) + stream
    ring = linear[1024 - 300:] + linear[:1024 - 300]
    memory.write_memory(BUFFER, ring)
    memory.write_memory(CH_BASE + 0x04, (BUFFER + 300).to_bytes(4, "little"))
    out = fifo.stop()
    assert out["instructions"] == len(EXPECTED) and out["trace_id"] == 0x10
    assert memory.u32(ETM + TRCPRGCTLR) == 0                               # ETM を止めた
    assert memory.u32(CH_BASE + 0x10) & 1 == 0                             # DMA の EN を外した（AL1_CTRL）
    assert out["ring"]["oldest"] == 300 and out["ring"]["overflow"] is False


def test_target_setup_leaves_the_dma_to_the_firmware(probe, memory):
    fifo = Rp2350TraceFifo(ProbeCaller(probe), buffer=BUFFER, size=1024, dma_channel=CH, setup="target")
    fifo.start()
    assert memory.u32(A.accessctrl_coresight_trace) == 0xB0                # 触らない
    assert memory.u32(CH_BASE + 0x0C) == 0
    assert memory.u32(ETM + TRCPRGCTLR) == 1                               # ETM・TPIU はデバッガが設定する
    memory.write_memory(CH_BASE + 0x04, (BUFFER + 16).to_bytes(4, "little"))
    out = fifo.stop()
    assert out["ring"]["oldest"] == 16


def test_buffer_must_be_aligned_to_its_size(probe):
    with pytest.raises(ValueError):
        Rp2350TraceFifo(ProbeCaller(probe), buffer=BUFFER + 0x100, size=1024)
    with pytest.raises(ValueError):
        Rp2350TraceFifo(ProbeCaller(probe), buffer=BUFFER, size=1000)
    with pytest.raises(ValueError):
        Rp2350TraceFifo(ProbeCaller(probe), buffer=BUFFER, size=65536)        # DMA のリングは 32 KB まで


def test_the_plugin_is_not_imported_by_the_core():
    import subprocess
    import sys
    code = "import fprobe.mcp_server, sys; print('fprobe.plugins.rp2350_trace' in sys.modules)"
    out = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, check=True)
    assert out.stdout.strip() == "False"
