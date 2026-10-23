"""Probe の性能解析の道具（fprobe.probe_profile）: PC サンプリング（方法 itm: SWO の受信を共有、方法 pcsr: DAP サンプラ）
→ プロファイル・割り込みの時間・近似のカバレッジ、記録の 2 点間の時間。
機器は使わない（sram_demo.elf、偽のターゲットのレジスタ・SWO・デバイス・リング）。"""

from pathlib import Path
from types import SimpleNamespace

import pytest
from probe_fakes import DictMemory, FakeDevice, FakeTarget, use_fake_ring

from fprobe import collector, probe_window
from fprobe.api import NoData
from fprobe.mcp_server import Probe
from fprobe.profile import dwt
from fprobe.symbols import Location, Symbol, Symbols

SRAM_DEMO = Path(__file__).resolve().parents[1] / "firmware" / "sram_demo" / "sram_demo.elf"
DWT_CTRL_RESET = 0x4000_0000                     # NUMCOMP 等の読み出し専用のビットだけ


class FakeSwo:
    def __init__(self):
        self.chunks: list[bytes] = []
        self.started: list[int] = []

    def swo_stop(self):
        pass

    def swo_start(self, baud):
        self.started.append(baud)

    def swo_read(self):
        return self.chunks.pop(0) if self.chunks else b""


@pytest.fixture
def probe(tmp_path, monkeypatch):
    monkeypatch.setenv("FPROBE_HOME", str(tmp_path))
    use_fake_ring(monkeypatch, probe_window, collector)
    p = Probe("unused")
    p._session = SimpleNamespace(device=FakeDevice())
    p._symbols = Symbols(SRAM_DEMO)
    target = FakeTarget(DictMemory({dwt.DWT_CTRL: DWT_CTRL_RESET}))
    p._debug = SimpleNamespace(target=target, probe=FakeSwo(), close=lambda: None)
    yield p
    if p._recorder is not None:
        p.recording_stop()
    p._stop_observing()


def address_of(probe, name, offset=0):
    return probe._symbols.symbol(name).address + offset


# ITM のパケットのバイト列
SYNC = b"\x00" * 5 + b"\x80"


def pc(address: int | None) -> bytes:
    return b"\x15\x00" if address is None else b"\x17" + address.to_bytes(4, "little")


def exc(n: int, action: int) -> bytes:                       # action: 1 = 入る、2 = 出る、3 = 戻る
    return bytes([0x0E, n & 0xFF, (n >> 8) | action << 4])


def ts(delta: int) -> bytes:
    """ローカルのタイムスタンプ（差 delta クロック）。"""
    if 0 < delta <= 6:
        return bytes([delta << 4])
    groups = []
    while True:
        groups.append(delta & 0x7F)
        delta >>= 7
        if not delta:
            break
    return bytes([0xC0]) + bytes(g | 0x80 for g in groups[:-1]) + bytes([groups[-1]])


# --- 方法 itm --------------------------------------------------------------------------------

def test_itm_profile_shares_the_swo_receive_and_restores_dwt(probe):
    target, swo = probe.target, probe._debug.probe
    started = probe.profile_start(method="itm", rate_hz=10_000, core_clock_hz=16e6, swo_baud=2_000_000)
    plan = started["plan"]
    assert started["running"] and plan["interval_cycles"] == 2048 and plan["acpr"] == 7 and swo.started == [2_000_000]
    ctrl = [v for a, v in target.write_log if a == dwt.DWT_CTRL]
    assert ctrl[-1] & 1 << 12 and ctrl[-1] & 1 << 16                     # PCSAMPLENA・EXCTRCENA
    assert dict(target.write_log)[dwt.ITM_TCR] & 0b10                    # 割り込みの時間のためにタイムスタンプも
    tick = address_of(probe, "tick", 4)
    swo.chunks = [SYNC + pc(tick) + ts(100) + pc(tick) + ts(100)]
    decoded = probe.swo_decode(timeout_s=0)                              # 利用者が読んでも、プロファイルに届く
    assert decoded["bytes"] > 0
    swo.chunks = [exc(15, 1) + ts(10) + pc(address_of(probe, "reached_milestone", 2)) + ts(40)
                  + exc(15, 2) + exc(0, 3) + ts(30) + pc(None) + ts(100) + pc(tick) + ts(100)]
    live = probe.profile_result()
    assert live["running"] and live["total"] == 5
    rows = {r["function"]: r for r in live["rows"]}
    assert rows["tick"]["samples"] == 3 and rows["reached_milestone"]["samples"] == 1
    assert live["categories"]["sleep"]["samples"] == 1 and len(rows["tick"]["ci"]) == 2
    stopped = probe.profile_stop()
    assert not stopped["running"] and stopped["exceptions"][0]["name"] == "SysTick"
    assert [v for a, v in target.write_log if a == dwt.DWT_CTRL][-1] == DWT_CTRL_RESET    # 元に戻した
    assert probe._swo_listeners == []
    exceptions = probe.profile_exceptions()
    systick = exceptions["exceptions"][0]
    assert systick["count"] == 1 and exceptions["clock_hz"] == 16e6
    assert systick["inclusive"]["max"] == 70                            # 入った印（210）から出た印（280）まで
    by_line = probe.profile_result(by="line", function="tick")
    assert by_line["by"] == "line" and all(r["function"] == "tick" for r in by_line["rows"])
    cov = probe.profile_coverage()
    assert cov["approximate"] and {f["function"] for f in cov["functions"]} == {"tick", "reached_milestone"}
    assert probe.profile_coverage(all_functions=True)["functions"][-1]["function"] == "default_handler"


def test_itm_needs_the_core_clock_and_an_elf(probe):
    with pytest.raises(ValueError, match="core_clock_hz"):
        probe.profile_start(method="itm")
    probe._symbols = None
    with pytest.raises(RuntimeError, match="debug_elf_load"):
        probe.profile_start(method="pcsr")
    with pytest.raises(NoData, match="profile_start"):                  # まだ無い（画面は「無い」と出す）
        probe.profile_result()


def test_exceptions_need_the_exception_trace(probe):
    probe.profile_start(method="itm", core_clock_hz=16e6, exception_trace=False)
    assert not dict(probe.target.write_log)[dwt.ITM_TCR] & 0b10        # タイムスタンプは既定で切る
    probe.profile_stop()
    with pytest.raises(RuntimeError, match="exception_trace"):
        probe.profile_exceptions()


# --- 方法 pcsr -------------------------------------------------------------------------------

def test_pcsr_profile_reads_the_sampler_words_of_the_window(probe):
    dev = probe.session.device
    started = probe.profile_start(method="pcsr", rate_hz=5000)
    assert [tuple(r) for r in started["plan"]["sampler_ranges"]] == [(dwt.DWT_PCSR, 4)]
    assert probe._sampler_user == "profile" and dev.sampler_running()
    assert dict(probe.target.write_log)[dwt.DEMCR] & dwt.DEMCR_TRCENA
    with pytest.raises(RuntimeError, match="profile_stop"):
        probe.watch_set(["0x20000000"])
    t = dev.ticks
    values = [address_of(probe, "tick", 2)] * 3 + [dwt.PCSR_INVALID, address_of(probe, "reset_handler")]
    for i, value in enumerate(values):
        dev.sample(t + 10 + i * 20_000, dwt.DWT_PCSR, value)
    dev.sample(t + 500_000, 0x2000_0000, address_of(probe, "tick"))      # 別の番地の語は使わない
    stopped = probe.profile_stop()
    assert stopped["method"] == "pcsr" and stopped["total"] == 4 and stopped["unavailable"] == 1
    assert {r["function"]: r["samples"] for r in stopped["rows"]} == {"tick": 3, "reset_handler": 1}
    assert stopped["window"]["source"] == "event_log" and probe._sampler_user is None and not dev.sampler_running()


def test_reads_of_the_same_samples_reuse_the_built_profile(probe, monkeypatch):
    """取っている最中の読み（profile.result・coverage 等）は、サンプルが増えていなければ組み直さない（画面は 1 回の
    読みで 3〜4 回呼ぶ。実機で 1 回 3〜5 秒かかり、停止が 8 秒待たされた）。"""
    dev = probe.session.device
    probe.profile_start(method="pcsr", rate_hz=5000)
    dev.sample(dev.ticks + 10, dwt.DWT_PCSR, address_of(probe, "tick", 2))
    built = []
    original = probe._build_profile
    monkeypatch.setattr(probe, "_build_profile", lambda run, events=None: built.append(run) or original(run, events))
    first = probe.profile_result()
    probe.profile_result(by="line")
    probe.profile_coverage()
    assert first["total"] == 1 and len(built) == 1
    dev.sample(dev.ticks + 20_000, dwt.DWT_PCSR, address_of(probe, "tick", 2))
    assert probe.profile_result()["total"] == 2 and len(built) == 2       # 増えたら組み直す
    probe.profile_stop()


# --- 記録の 2 点間の時間 ------------------------------------------------------------------------

def test_timing_between_two_kinds_of_recorded_events(probe):
    probe.recording_start(name="timing")
    rec = probe._recorder
    for k, duration in enumerate([100, 110, 90, 105, 1000]):
        rec.add("uart", "rx", 10_000 * (k + 1))
        rec.add("uart", "tx", 10_000 * (k + 1) + duration)
    out = probe.timing_measure({"stream": "uart", "kind": "rx"}, {"stream": "uart", "kind": "tx"}, limit=3)
    assert out["count"] == 5 and out["min"] == 90 and out["max"] == 1000 and out["max_at"] == 50_000
    assert out["longest"][0]["duration"] == 1000 and out["seconds"]["max"] == pytest.approx(1000 / 1e8)
    assert len(out["intervals"]) == 3 and out["intervals_total"] == 5


# --- ベクタ表の名前 ---------------------------------------------------------------------------

def test_interrupt_names_come_from_the_vector_table_variable(probe):
    table = bytearray(4 * 20)
    table[4 * 17:4 * 18] = (0x100 | 1).to_bytes(4, "little")           # IRQ1 → usart_irq
    table[4 * 18:4 * 19] = (0x200).to_bytes(4, "little")               # Thumb でない: 名前を付けない
    funcs = {0x100: "usart_irq", 0x200: "data"}
    probe._symbols = SimpleNamespace(
        segments=[SimpleNamespace(address=0, data=bytes(table) + b"\x01\x01\x00\x00")],
        symbol_at=lambda a: (Symbol("vectors", 0, len(table), "object"), 0) if a == 0 else None,
        describe=lambda a: Location(a, funcs.get(a), 0 if a in funcs else None))
    assert probe._vector_names() == {17: "IRQ1 (usart_irq)"}


# --- 方法 pcsr の速さ（B9） ---------------------------------------------------------------------

def test_pcsr_plan_tells_the_swd_speed_and_the_summary_the_achieved_rate(probe):
    """DWT_PCSR の方式の速さは SWD の速さで決まる（B17: 4 MHz で 1 巡回 3 転送 約 37 µs = 約 27 kHz）。計画に見積もりと、
    求めた速さに要る SWD の速さを、結果に実際の速さを出す。"""
    dev = probe.session.device
    probe._link = {"target": "cortex_m", "frequency_hz": 4_000_000, "wire": "swd"}
    started = probe.profile_start(method="pcsr", rate_hz=50_000)
    swd = started["plan"]["swd"]
    assert swd["swd_hz"] == 4_000_000 and swd["transfers"] == 3 and 26_000 < swd["max_rate_hz"] < 28_000
    assert swd["swd_hz_for_rate"] >= 7.2e6                      # 50 kHz = 20 µs の巡回 → 144 / 20 µs
    assert started["plan"]["rate_hz"] == pytest.approx(swd["max_rate_hz"])      # 出せる見込みの速さ
    assert started["plan"]["requested_rate_hz"] == 50_000
    assert any("SWD" in w and "50000" in w for w in started["plan"]["warnings"])
    t = dev.ticks
    for i in range(11):                                           # 100 µs ごと = 10 kHz
        dev.sample(t + 10 + i * 10_000, dwt.DWT_PCSR, address_of(probe, "tick", 2))
    stopped = probe.profile_stop()
    sampling = stopped["sampling"]
    assert sampling["requested_hz"] == 50_000 and sampling["achieved_hz"] == pytest.approx(10_000)
    assert sampling["swd_hz"] == 4_000_000 and sampling["samples"] == 11
    assert any("サンプルの速さ" in n and "より遅い" in n for n in stopped["notes"])


def test_pcsr_plan_without_a_known_swd_speed_says_so(probe):
    started = probe.profile_start(method="pcsr", rate_hz=5000)    # 接続の情報が無い（_link なし）
    assert started["plan"]["swd"] is None and started["plan"]["rate_hz"] == 5000
    stopped = probe.profile_stop()
    assert stopped["sampling"]["achieved_hz"] is None and stopped["sampling"]["swd_hz"] is None
