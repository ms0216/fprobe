"""fprobe.profile: PC サンプルの取り込みとプロファイル（sram_demo.elf の関数の範囲から乱数で作った PC の列）。"""

import json
import random
from pathlib import Path

import pytest

from fprobe.disasm import Disassembler
from fprobe.itm import ExceptionAction, ExceptionTrace, ItmDecoder, Overflow, PcSample as ItmPc, Sync
from fprobe.memview import BytesMemory
from fprobe.profile import PcSamples, Profiler
from fprobe.profile.dwt import DWT_PCSR, PCSR_INVALID
from fprobe.recording import Event
from fprobe.symbols import Location, Symbol, Symbols

FIRMWARE = Path(__file__).resolve().parents[1] / "firmware"
SRAM_DEMO = FIRMWARE / "sram_demo" / "sram_demo.elf"
SOURCE = (SRAM_DEMO.parent / "sram_demo.c").read_text(encoding="utf-8").splitlines()


def line_of(text: str) -> int:
    return next(i for i, line in enumerate(SOURCE, start=1) if text in line)


@pytest.fixture(scope="module")
def syms():
    return Symbols(SRAM_DEMO)


@pytest.fixture(scope="module")
def dis():
    return Disassembler.from_elf(SRAM_DEMO)


def code_addresses(dis, name):
    return [i.address for i in dis.function(name).instructions if i.kind == "code"]


# --- 取り込み ----------------------------------------------------------------------------

def test_from_itm_attaches_exception_context():
    packets = [Sync(), ExceptionTrace(0, ExceptionAction.RETURN), ItmPc(0x20000050),
               ExceptionTrace(15, ExceptionAction.ENTER), ItmPc(0x20000054),
               ExceptionTrace(15, ExceptionAction.EXIT), ExceptionTrace(0, ExceptionAction.RETURN), ItmPc(None),
               Overflow(), ItmPc(0x20000058)]
    s = PcSamples.from_itm(packets)
    assert [x.pc for x in s.samples] == [0x20000050, 0x20000054, None, 0x20000058]
    assert [x.exception for x in s.samples] == [0, 15, 0, None]
    assert (s.source, s.overflows, s.exception_trace) == ("itm", 1, True)


def test_from_itm_bytes():
    # PC サンプル: ヘッダ 0x17 + 番地 4 バイト、眠り: 0x15 0x00
    stream = b"\x00" * 5 + b"\x80" + b"\x17" + (0x20000050).to_bytes(4, "little") + b"\x15\x00"
    s = PcSamples.from_itm(ItmDecoder().feed(stream))
    assert [x.pc for x in s.samples] == [0x20000050, None]
    assert not s.exception_trace


def test_from_pcsr_values_and_events():
    s = PcSamples.from_pcsr([0x20000051, PCSR_INVALID, 0x20000060])
    assert [x.pc for x in s.samples] == [0x20000050, 0x20000060] and s.unavailable == 1
    events = [Event(10, "sample", "word", DWT_PCSR, 0x20000050), Event(11, "sample", "word", 0x20000000, 5),
              Event(12, "uart", "rx", 0, 65), Event(20, "sample", "word", DWT_PCSR, PCSR_INVALID)]
    s = PcSamples.from_pcsr(events)
    assert [(x.pc, x.t) for x in s.samples] == [(0x20000050, 10)] and s.unavailable == 1
    assert s.source == "pcsr"


# --- プロファイル ----------------------------------------------------------------------------

def test_fractions_and_confidence_intervals(syms, dis):
    rng = random.Random(5)
    truth = {"tick": 0.6, "reset_handler": 0.25, "reached_milestone": 0.05, None: 0.10}   # None = 眠り
    pools = {name: code_addresses(dis, name) for name in truth if name}
    n = 4000
    pcs = []
    for _ in range(n):
        name = rng.choices(list(truth), weights=list(truth.values()))[0]
        pcs.append(None if name is None else rng.choice(pools[name]))
    prof = Profiler(syms, dis).profile(PcSamples.from_pcs(pcs))
    assert prof.total == n
    rows = {r["function"]: r for r in prof.functions()}
    assert [r["function"] for r in prof.functions()] == ["tick", "reset_handler", "reached_milestone"]
    for name, p in truth.items():
        if name is None:
            continue
        row = rows[name]
        assert row["fraction"] == row["samples"] / n and abs(row["fraction"] - p) < 4 * (p * (1 - p) / n) ** 0.5
        assert "ci" not in row                       # 信頼区間は二項の仮定の上の推定: 本体は出さない（プラグイン）
    sleep = prof.category_table()["sleep"]
    assert sleep["fraction"] == sleep["samples"] / n and "ci" not in sleep
    assert "confidence" not in prof.as_dict()
    assert rows["tick"]["active_fraction"] == pytest.approx(rows["tick"]["samples"] / prof.active)
    assert sum(r["samples"] for r in prof.lines()) == prof.categories["code"]
    assert sum(r["samples"] for r in prof.instructions()) == prof.categories["code"]


def test_line_and_instruction_attribution(syms, dis):
    tick = syms.resolve("tick")
    prof = Profiler(syms, dis).profile(PcSamples.from_pcs([tick + 4] * 3 + [tick] * 1))
    line = prof.top(1, by="line")[0]
    assert (line["function"], line["file"], line["line"], line["samples"]) == ("tick", "sram_demo.c",
                                                                              line_of("counter++;"), 3)
    insn = prof.top(1, by="instruction")[0]
    assert (insn["address"], insn["function"], insn["offset"], insn["samples"]) == (tick + 4, "tick", 4, 3)
    listing = {i.address: i for i in dis.function("tick").instructions}
    assert insn["text"] == f"{listing[tick + 4].mnemonic} {listing[tick + 4].operands}".strip()
    assert [r["address"] for r in prof.instructions("tick")] == [tick + 4, tick]
    assert prof.instructions("reset_handler") == []


def test_without_disassembler_has_no_text(syms):
    prof = Profiler(syms).profile(PcSamples.from_pcs([syms.resolve("tick")]))
    assert prof.instructions()[0]["text"] is None


def test_unknown_addresses(syms):
    prof = Profiler(syms).profile(PcSamples.from_pcs([0x08000000, 0x08000000, syms.resolve("tick")]))
    assert prof.categories["unknown"] == 2 and prof.categories["code"] == 1
    assert prof.unknown_pcs[0x08000000] == 2


class FakeSymbols:
    """番地 0x1000〜0x1006 の関数 idle だけ。"""
    def describe(self, address):
        if 0x1000 <= address < 0x1006:
            return Location(address, "idle", address - 0x1000, "idle.c", 10 + (address - 0x1000) // 2)
        return Location(address)

    def symbol(self, name):
        if name != "idle":
            raise KeyError(name)
        return Symbol("idle", 0x1000, 6, "function")


def test_pc_on_wfi_is_counted_as_wfi_not_assumed_sleep():
    # 「WFI / WFE とその次の番地に当たったら眠り」とみなさない（次の番地は起きて実行していることもある）。
    # 眠り（sleep）はコアが送る眠りのサンプル（PC 無し）だけ。PC が WFI / WFE の上なら wfi（事実）、次の番地は code
    code = bytes.fromhex("00bf" "30bf" "fee7")               # nop; wfi; b .
    dis = Disassembler(BytesMemory(0x1000, code))
    pcs = [0x1002] * 5 + [0x1004] * 3 + [0x1000] * 2 + [None]
    prof = Profiler(FakeSymbols(), dis).profile(PcSamples.from_pcs(pcs))
    assert (prof.categories["sleep"], prof.categories["wfi"], prof.categories["code"]) == (1, 5, 5)
    assert 0x1002 in prof.hit_addresses() and prof.active == 10


def test_exception_breakdown_and_json(syms, dis):
    tick = syms.resolve("tick")
    packets = [ExceptionTrace(0, ExceptionAction.RETURN), ItmPc(tick), ItmPc(tick),
               ExceptionTrace(15, ExceptionAction.ENTER), ItmPc(tick + 4)]
    prof = Profiler(syms, dis).profile(PcSamples.from_itm(packets))
    rows = prof.exceptions()
    assert [(r["name"], r["samples"]) for r in rows] == [("Thread", 2), ("SysTick", 1)]
    assert rows[0]["fraction"] == pytest.approx(2 / 3)
    d = prof.as_dict(top=5)
    json.dumps(d)
    assert d["total"] == 3 and d["functions"][0]["function"] == "tick"
    assert any("エイリアシング" in note for note in d["notes"])
    with pytest.raises(ValueError):
        prof.top(3, by="file")


def test_notes_mention_overflow_and_pcsr(syms):
    s = PcSamples.from_pcsr([syms.resolve("tick"), PCSR_INVALID])
    notes = Profiler(syms).profile(s).notes()
    assert any("0xFFFFFFFF" in n for n in notes) and any("DAP" in n for n in notes)


def test_each_distinct_pc_is_described_once(syms, dis):
    """同じ番地のサンプルは 1 回だけ関数・行に割り当てる（10 kHz で数十万のサンプル。実機で profile.result が 3〜5 秒）。"""
    calls = []

    class Counting:
        def describe(self, address):
            calls.append(address)
            return syms.describe(address)

        def symbol(self, name):
            return syms.symbol(name)

    pcs = code_addresses(dis, "tick")[:3] * 1000
    prof = Profiler(Counting(), dis).profile(PcSamples.from_pcs(pcs))
    assert prof.total == 3000 and prof.function_counts["tick"] == 3000
    assert sorted(calls) == sorted(set(pcs))
