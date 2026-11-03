"""時刻の事実（段階 S1。docs/superpowers/specs/2026-11-01-time-accuracy-design.md §4.3 の R1・R3、§4.2 の TLV TIMING）。機器は使わない。

- 連続記録のサンプル 0: 回路が記録する構成（TIMING の la_deep_start）は CSR の 1 点。記録しない古い回路は S0 の範囲のまま。
- UART・SWO のバイト: 回路の申告（TIMING）から端子の上の点（開始ビットの立ち下がり・Manchester の最後のビットの中央）の範囲。
  申告の無い古い回路は受け渡しの時刻（範囲は分からない）。SWO は方式が分からなければ範囲を返さない。
"""

import random

from fprobe import generated as g
from fprobe.caps import TimingPoint
from fprobe.recording_stream import anchor_stream
from fprobe.timing import byte_edge_window, source_timing, stream_start_recorded

from test_la_stream import raw_source
from test_recording_stream import HZ, TimedStreamDevice
from caps_fakes import caps_for, with_caps

DECLARED = {
    "la_deep_start": TimingPoint("la_deep_start", "pin_edge", "tick", 7, 1, ("input_routing",)),
    "uart_rx": TimingPoint("uart_rx", "start_bit_edge", "tick", 2, 1, ("input_routing",)),
    "uart_tx": TimingPoint("uart_tx", "start_bit_edge", "tick", 0, 0, ("output_delay",)),
    "swo_uart": TimingPoint("swo_uart", "start_bit_edge", "tick", 2, 1, ("input_routing", "target_internal")),
    "swo_manchester": TimingPoint("swo_manchester", "last_bit_center", "tick", 3, 1,
                                  ("input_routing", "sender_rate", "target_internal")),
}


def caps(timing=None):
    return caps_for(timing=DECLARED if timing is None else timing)


# --- 連続記録のサンプル 0（R1） ----------------------------------------------------------------------------------

def test_the_recorded_stream_start_is_one_point_on_the_pin_axis():
    w = stream_start_recorded(10_000, delay=7, span=1, before=9_000, after=11_000)
    assert (w["t_lo"], w["t_hi"]) == (10_000 - 7, 10_000 - 7)              # トリガ時刻と同じ式（start_time − 段数。S1b）
    assert w["basis"]["start_time"] == 10_000 and w["basis"]["bounds"] == {"t_lo": "start_time", "t_hi": "start_time"}
    assert w["unknown"] == []


class RecordingStartDevice(TimedStreamDevice):
    """サンプル 0 の時刻を記録する回路（TIMING の la_deep_start）の偽物: run を書いた 200 刻み後を start_time にする。"""

    def __init__(self, *args, start_valid=True, **kwargs):
        super().__init__(*args, trigger=False, **kwargs)
        self.start_valid = start_valid
        self.start_time = 0

    def capabilities(self):
        return with_caps(super().capabilities(), timing={"la_deep_start": DECLARED["la_deep_start"]}, la_delay_deep=7,
                         la_phase_span=1)

    def write32(self, address, value):
        if address == g.LA_DEEP_CONTROL and value & 1 and not self.running:
            self.start_time = self.now + 200
        super().write32(address, value)

    def read32(self, address):
        if address == g.LA_DEEP_INFO:
            return int(self.start_valid) << g.LA_DEEP_INFO_START_VALID[0] | 1
        return super().read32(address)

    def read(self, address, count, *, timeout_retries=0):
        if address == g.LA_DEEP_START_TIME_LO and count == 2:
            return [self.start_time & 0xFFFF_FFFF, self.start_time >> 32]
        return super().read(address, count)


class Sample:
    sample_period_s = 1e-8


def test_the_anchor_is_the_circuits_start_time_when_the_circuit_records_it():
    dev = RecordingStartDevice(raw_source(random.Random(1), 10), rng=random.Random(1))

    def start():
        dev.write32(g.LA_DEEP_CONTROL, 1 | 1 << g.LA_DEEP_CONTROL_STREAM[0])
        return Sample()

    _, anchor = anchor_stream(dev, start, HZ)
    assert anchor.method == "circuit" and anchor.uncertainty == 0
    assert anchor.ticks == anchor.t_lo == anchor.t_hi == dev.start_time - 7
    assert anchor.basis["start_time"] == dev.start_time
    assert anchor.basis["before"] < dev.start_time                         # 往復の読みは根拠に残すだけ


def test_a_missing_start_time_falls_back_to_the_round_trip_window_and_says_so():
    dev = RecordingStartDevice(raw_source(random.Random(2), 10), rng=random.Random(2), start_valid=False)

    def start():
        dev.write32(g.LA_DEEP_CONTROL, 1 | 1 << g.LA_DEEP_CONTROL_STREAM[0])
        return Sample()

    _, anchor = anchor_stream(dev, start, HZ)
    assert anchor.method == "host" and anchor.t_hi > anchor.t_lo
    assert anchor.basis["start_valid"] is False


def test_old_circuits_without_the_declaration_have_no_recorded_start():
    assert source_timing(caps({}), "la_deep_start") is None


# --- UART・SWO のバイト（R3） --------------------------------------------------------------------------------------

def test_a_received_uart_byte_has_the_window_of_its_start_bit_edge():
    w = byte_edge_window(caps(), "uart_rx", 1000)
    assert (w["t_lo"], w["t_hi"]) == (998, 999)                            # 時刻 − 段数 以上、+ 位相の幅 未満（S1b）
    assert w["point"] == "start_bit_edge" and w["from"] == "TIMING" and w["unknown"] == ["input_routing"]


def test_an_old_circuit_without_the_declaration_has_no_start_bit_window():
    # 申告の無い古い回路の UART 受信は受け渡しの時刻（ホストへの経路の待ちが入り、記録されない）: 範囲は分からない
    w = byte_edge_window(caps({}), "uart_rx", 1000)
    assert w["t_lo"] is None and w["t_hi"] is None and w["point"] == "hand_off" and w["from"] == "legacy"
    assert "hand_off_wait" in w["unknown"]


def test_a_sent_uart_byte_is_the_edge_the_transmitter_starts_on():
    for c in (caps(), caps({})):                                           # 送信の意味は版で変わらない
        w = byte_edge_window(c, "uart_tx", 500)
        assert (w["t_lo"], w["t_hi"]) == (500, 500) and w["unknown"] == ["output_delay"]


def test_swo_needs_the_mode_to_know_the_point():
    assert byte_edge_window(caps(), "swo", 1000)["unknown"] == ["swo_mode"]
    uart = byte_edge_window(caps(), "swo", 1000, "uart")
    assert (uart["t_lo"], uart["t_hi"], uart["point"]) == (998, 999, "start_bit_edge")
    man = byte_edge_window(caps(), "swo", 1000, "manchester")
    assert (man["t_lo"], man["t_hi"], man["point"]) == (997, 998, "last_bit_center")
    assert "sender_rate" in man["unknown"]                                 # 開始の辺からの位置は送り手の速さで決まる
