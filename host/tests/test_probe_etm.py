"""Probe の ETM の道具（fprobe.probe_etm）: CoreSight の ETMv4・ファンネル・TPIU の設定と、取ったトレースの解読・
命令の一覧・事後の命令履歴。偽のターゲット（etm_fakes）と etm_demo.elf で。"""

from pathlib import Path
from types import SimpleNamespace

import pytest

import etm_encoder as enc
from etm_fakes import (CSPSR, DEMCR, ETM, FFCR, FSCR, TPIU, TRCCONFIGR, TRCPRGCTLR, TRCSYNCPR, TRCTRACEIDR,
                       TRCVICTLR, CoreSightMemory)
from fprobe.mcp_server import Probe
from fprobe.symbols import Symbols
from probe_fakes import FakeTarget
from test_trace_reconstruct import ELF, SCENARIO, B, EXPECTED


@pytest.fixture
def memory():
    return CoreSightMemory()


@pytest.fixture
def probe(memory):
    p = Probe("unused")
    p._debug = SimpleNamespace(target=FakeTarget(memory, {"pc": B}), probe=None, close=lambda: None)
    return p


def u32(memory, address):
    return memory.u32(address)


def test_start_programs_the_etm_and_the_tpiu(probe, memory):
    out = probe.etm_start(trace_id=0x22, sync_period=1024, timestamps=True)
    assert u32(memory, DEMCR) & 1 << 24                                     # TRCENA
    assert u32(memory, ETM + TRCPRGCTLR) == 1
    assert u32(memory, ETM + TRCCONFIGR) == 1 << 11                         # TS のみ（BB・CCI・RS は使わない）
    assert u32(memory, ETM + TRCTRACEIDR) == 0x22
    assert u32(memory, ETM + TRCSYNCPR) == 10                               # 2^10 バイトごとに A-sync
    assert u32(memory, ETM + TRCVICTLR) == 0x201                            # 常に真・開始済み
    assert u32(memory, TPIU + CSPSR) == 1 << 3                              # 4 ビット
    assert u32(memory, TPIU + FFCR) == 0x102                                # 継続モードのフォーマッタ
    assert u32(memory, TPIU + FSCR) > 0
    assert out["etm"]["version"] == "4.2" and out["trace_id"] == 0x22
    # ETM の設定は書く前に止め（IDLE を待つ）、最後に有効にする
    log = [a for a, _ in probe._debug.target.write_log if ETM <= a < ETM + 0x1000]
    assert log[0] == ETM + TRCPRGCTLR and log[-1] == ETM + TRCPRGCTLR


def test_start_with_a_funnel_and_without_the_tpiu(probe, memory):
    memory.write_memory(0x4014_7FB4, (0b11).to_bytes(4, "little"))         # LSR: ソフトのロックがあり、掛かっている
    probe.etm_start(tpiu_base=None, funnel_base=0x4014_7000, funnel_ports=0b0101, cycle_counting=True)
    assert u32(memory, 0x4014_7000) & 0xFF == 0b0101
    assert u32(memory, 0x4014_7FB0) == 0xC5AC_CE55                          # 書く前にロックを外す
    assert u32(memory, TPIU + FFCR) == 0                                     # TPIU は触らない
    assert u32(memory, ETM + TRCCONFIGR) == 1 << 4                          # CCI


def test_start_refuses_when_there_is_no_etmv4(probe):
    probe._debug.target.memory = CoreSightMemory(etm_arch=(3, 5))
    with pytest.raises(RuntimeError, match="ETMv4"):
        probe.etm_start()


def test_stop_disables_and_flushes(probe, memory):
    probe.etm_start()
    out = probe.etm_stop()
    assert u32(memory, ETM + TRCPRGCTLR) == 0 and memory.flushes == 1
    assert out["flushed"]


def decoded(probe, data: bytes, **kw):
    probe._symbols = Symbols(ELF)
    return probe.etm_decode(data_hex=data.hex(), **kw)


def test_decode_formatted_trace_and_list_instructions(probe):
    probe.etm_start(trace_id=0x10)
    data = enc.FSYNC + enc.frames([(0x10, SCENARIO[:20]), (0x01, b"\x01\x41"), (0x10, SCENARIO[20:])])
    out = decoded(probe, data)
    assert out["trace_id"] == 0x10 and out["streams"] == {"0x01": 2, "0x10": len(SCENARIO)}
    assert out["instructions"] == len(EXPECTED) and out["errors"] == []
    assert out["last"]["function"] == "reset_handler" and out["next_address"] == B
    listed = probe.etm_instructions(offset=0, limit=3)
    assert listed["total"] == len(EXPECTED)
    assert [i["address"] for i in listed["instructions"]] == [B, B + 2, B + 0x24]
    assert probe.etm_instructions(function="leaf")["total"] == 4             # leaf を 2 回（2 命令ずつ）
    packets = probe.etm_packets(limit=2)
    assert [p["kind"] for p in packets["packets"]] == ["async", "trace_info"] and packets["total"] > 10


def test_history_is_the_last_n_instructions_before_the_end(probe):
    decoded(probe, SCENARIO, formatted=False)
    out = probe.etm_history(count=3)
    assert [i["address"] for i in out["instructions"]] == [B + 0x46, B + 0x48, B + 0x1A]
    assert out["total"] == len(EXPECTED) and out["next_address"] == B
    assert "推定" in out["note"] or "データ" in out["note"]


def test_the_id_with_most_data_is_used_when_none_was_set(probe):
    data = enc.FSYNC + enc.frames([(0x01, b"\x01\x41\x42"), (0x12, SCENARIO)])
    out = decoded(probe, data)
    assert out["trace_id"] == 0x12 and out["instructions"] == len(EXPECTED)


def test_decode_a_ring_from_target_memory(probe, memory):
    # SRAM のリング: 書き込み位置（最も古いバイト）が途中にある。古い側の頭は前の周回の残り（切れたフレーム）
    stream = enc.FSYNC + enc.frames([(0x10, SCENARIO)])
    ring_size = 256
    oldest = 40
    tail = bytes([0x5A]) * (ring_size - len(stream))
    linear = tail + stream                                     # 古い順
    ring = linear[ring_size - oldest:] + linear[:ring_size - oldest]
    memory.write_memory(0x2007_8000, ring)
    probe._symbols = Symbols(ELF)
    out = probe.etm_decode(address=0x2007_8000, length=ring_size, oldest=oldest)
    assert out["instructions"] == len(EXPECTED) and out["bytes"] == ring_size


def test_decode_from_a_file(probe, tmp_path):
    path = tmp_path / "trace.bin"
    path.write_bytes(SCENARIO)
    probe._symbols = Symbols(ELF)
    assert probe.etm_decode(path=str(path), formatted=False)["instructions"] == len(EXPECTED)


def test_decode_needs_exactly_one_source(probe):
    with pytest.raises(ValueError):
        probe.etm_decode()
    with pytest.raises(ValueError):
        probe.etm_decode(data_hex="00", path="x.bin")


def test_reading_before_decoding_is_an_error(probe):
    with pytest.raises(RuntimeError):
        probe.etm_history()


def test_code_comes_from_the_target_when_no_elf_is_loaded(probe, memory):
    from fprobe.memview import ElfImage
    memory.write_memory(B, ElfImage(ELF).read_memory(B, 0x54))
    out = probe.etm_decode(data_hex=SCENARIO.hex(), formatted=False)
    assert out["code"] == "target" and out["instructions"] == len(EXPECTED)
