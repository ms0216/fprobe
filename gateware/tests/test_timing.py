"""時刻の表（fprobe_gw.timing）: ロジアナの ch ごとの時刻のずれの申告（TLV LA_CH_TIMING）と、情報源ごとの点の申告（TLV TIMING）。

仕様: docs/superpowers/specs/2026-11-01-time-accuracy-design.md §2.1 の 2・3、§4.2、段階 S0・S1。回路との一致は cocotb の試験
（la_probe_inputs・la_cap_domain・la_deep_buffer・uart_rx・uart_tx・swo_receiver_modes）が確かめる。
"""

import dataclasses

from fprobe_gw import caps, timing
from fprobe.caps import parse

from tests.test_caps import TEST_CAPS


def _kinds(c):
    return [b >> 4 for b in caps.la_layout(c)]


def test_internal_channels_are_aligned_with_the_pins_by_the_circuit():
    # ピンはグリッチフィルタ（3 段）を通り、内部の ch（SWDIO を駆動中・DAP の処理中）は同じ段数のレジスタで遅らせて揃える
    # （R2。la_probe_inputs。段階 S0 では +LA_FILTER_DELAY と申告していた）。sys に同期した信号なので位相の幅は無い
    rows = timing.la_channel_timing(TEST_CAPS)
    assert len(rows) == TEST_CAPS.la_channels
    for kind, row in zip(_kinds(TEST_CAPS), rows):
        if kind in (caps.LA_CH_PIN, caps.LA_CH_INPUT):
            assert row == timing.LaChannelTiming(0, 0, timing.LA_PHASE_SPAN_TICKS)
        elif kind == caps.LA_CH_INTERNAL:
            assert row == timing.LaChannelTiming(0, 0, 0)
        else:
            assert row == timing.LaChannelTiming(None, None, None)                     # 何もつながっていない


def test_without_the_filter_every_channel_shares_the_same_stages():
    rows = timing.la_channel_timing(dataclasses.replace(TEST_CAPS, la_filter=False, la_pulse=False))
    internal = [r for k, r in zip(_kinds(TEST_CAPS), rows) if k == caps.LA_CH_INTERNAL]
    assert internal and all(r == timing.LaChannelTiming(0, 0, 0) for r in internal)


def test_the_capture_domain_internal_channels_are_aligned_too():
    # 取り込みの領域（cap_ratio > 1）の内部の ch は sys → cap の 1 段を含めてフィルタの段数に揃える（R2。la_cap_domain_tb が
    # ピンと同じサンプルに来ることを確かめる。段階 S0 では確かめておらず「分からない」と申告していた）
    cap = dataclasses.replace(TEST_CAPS, la_sample_hz=3 * TEST_CAPS.sys_clock_hz, cap_ratio=3)
    rows = timing.la_channel_timing(cap)
    internal = [r for k, r in zip(_kinds(cap), rows) if k == caps.LA_CH_INTERNAL]
    assert internal and all(r == timing.LaChannelTiming(0, 0, 0) for r in internal)
    pins = [r for k, r in zip(_kinds(cap), rows) if k == caps.LA_CH_PIN]
    assert all(r.internal == 0 for r in pins)


def test_the_declaration_round_trips_through_the_capabilities():
    got = parse(caps.encode(TEST_CAPS)).la_channel_timing
    assert got == tuple((r.internal, r.deep, r.span) for r in timing.la_channel_timing(TEST_CAPS))


def test_old_devices_without_the_declaration_have_none():
    data = caps.encode(dataclasses.replace(TEST_CAPS, la_channels=0, la_depth=0, la_inputs=0))
    assert parse(data).la_channel_timing == ()


def test_caps_keeps_the_old_names_from_the_table():
    # 既存の import（la_filter_tb・la_capture_tb・deep_axi_tb）は caps から読む。値の正は timing
    assert caps.LA_FILTER_DELAY is timing.LA_FILTER_DELAY
    assert caps.LA_INTERNAL_DELAY is timing.LA_INTERNAL_DELAY
    assert caps.LA_DEEP_DELAY is timing.LA_DEEP_DELAY
    assert caps.LA_PHASE_SPAN_TICKS is timing.LA_PHASE_SPAN_TICKS
    assert caps.SYNC_RX_LATENCY_TICKS is timing.SYNC_RX_LATENCY_TICKS


# --- TLV TIMING（段階 S1） -----------------------------------------------------------------------------------------

def _declared(c):
    return {p.source: p for p in timing.timing_points(c)}


def test_the_timing_declaration_round_trips_through_the_capabilities():
    for c in (TEST_CAPS, dataclasses.replace(TEST_CAPS, sync=True, event_ring_bits=0),
              dataclasses.replace(TEST_CAPS, la_sample_hz=3 * TEST_CAPS.sys_clock_hz, cap_ratio=3)):
        got = parse(caps.encode(c)).timing
        want = _declared(c)
        assert set(got) == set(want)
        for name, p in want.items():
            g = got[name]
            assert (g.point, g.unit, g.delay, g.span, set(g.excludes)) == (p.point, p.unit, p.delay, p.span, set(p.excludes))


def test_uart_and_swo_bytes_are_declared_at_the_start_bit_edge():
    d = _declared(TEST_CAPS)
    assert (d["uart_rx"].point, d["uart_rx"].delay, d["uart_rx"].span) == ("start_bit_edge", timing.UART_RX_START_DELAY, 1)
    assert (d["swo_uart"].point, d["swo_uart"].delay) == ("start_bit_edge", timing.UART_RX_START_DELAY)
    assert (d["uart_tx"].point, d["uart_tx"].delay, d["uart_tx"].span) == ("start_bit_edge", 0, 0)
    # Manchester は最初のビットの中央（R4、段階 S3）。送り手の速さに依らないので sender_rate を含まない項目に挙げない
    assert (d["swo_manchester"].point, d["swo_manchester"].delay, d["swo_manchester"].span) == (
        "first_bit_center", timing.SWO_MANCHESTER_BIT_DELAY, 1)
    assert "sender_rate" not in d["swo_manchester"].excludes and "target_internal" in d["swo_manchester"].excludes
    assert "swo_manchester" not in _declared(dataclasses.replace(TEST_CAPS, swo_manchester=False))


def test_the_deep_buffer_declares_its_recorded_stream_start():
    # 深いバッファのある構成（1 周期 1 サンプル）は連続記録の起点を記録する（R1）: トリガ時刻と同じ段
    deep = dataclasses.replace(TEST_CAPS, mem_bytes=1 << 20, la_depth=0, event_ring_bits=0)
    d = _declared(deep)
    assert d["la_deep_start"].delay == d["la_deep"].delay == caps.la_delay_ticks(deep)[1]
    assert "la_deep_start" not in _declared(TEST_CAPS)                     # 深いバッファの無い構成


def test_the_capture_domain_axis_is_declared_in_capture_cycles():
    cap = dataclasses.replace(TEST_CAPS, la_sample_hz=3 * TEST_CAPS.sys_clock_hz, cap_ratio=3)
    d = _declared(cap)
    assert (d["la_cap"].unit, d["la_cap"].delay) == ("cap_cycle", timing.la_cap_latency(3)) and "la" not in d


def test_the_vhdl_package_carries_the_table():
    text = timing.render_package()
    for name in ("LA_FILTER_DELAY", "LA_CAP_LATENCY_BASE", "UART_RX_START_DELAY", "SWO_MANCHESTER_BIT_DELAY",
                 "LA_FRONT_EXT_ALIGN", "LA_LANE_PIPE_CYCLES", "XTRIG_LA_EXT_DELAY"):
        assert f"constant {name} : integer := {getattr(timing, name)};" in text


# --- 段階 S1b ----------------------------------------------------------------------------------------------------

def _design(name):
    from fprobe_gw.designs import DESIGNS
    return DESIGNS[name].capabilities


def test_the_lane_designs_align_the_internal_channels():
    # N2: 1 周期に複数のサンプルの構成の内部の ch は la_lane_inputs が細かい ch の時間軸に揃える（ずれ 0・位相の幅 0。
    # la_lane_inputs_tb）。以前は「分からない」（None）だった
    for name, column in (("fprobe_pynq_fast", "internal"), ("fprobe_pynq_deep_fast", "deep")):
        c = _design(name)
        rows = timing.la_channel_timing(c)
        for kind, row in zip(_kinds(c), rows):
            if kind == caps.LA_CH_INTERNAL:
                assert getattr(row, column) == 0 and row.span == 0, (name, row)
            elif kind in (caps.LA_CH_PIN, caps.LA_CH_INPUT):
                assert getattr(row, column) == 0 and row.span == timing.LA_PHASE_SPAN_TICKS, (name, row)
    # sys で取るピン（la_fine_pins = False。製品の構成には無い）: 時間軸の次の刻みに来る（ずれ −1、位相の幅 1）
    c = dataclasses.replace(_design("fprobe_pynq_fast"), la_fine_pins=False)
    pins = [r for k, r in zip(_kinds(c), timing.la_channel_timing(c)) if k == caps.LA_CH_PIN]
    assert pins and all(r == timing.LaChannelTiming(-1, None, 1) for r in pins)


def test_the_lane_designs_declare_the_logic_analyzer_axis():
    # A9: TIMING にロジアナの点（単位 fine_sample。CSR の latency と同じ値）
    fast = _declared(_design("fprobe_pynq_fast"))
    n, age = 8, _design("fprobe_pynq_fast").la_lane_in_age
    assert age == 5
    assert (fast["la_lanes"].unit, fast["la_lanes"].delay, fast["la_lanes"].span) == ("fine_sample", timing.la_lane_latency(n, age), 1)
    assert (fast["la_lanes_xtrig"].delay, fast["la_lanes_xtrig"].span) == (timing.la_lane_latency(n, age) + n, n)
    deep = _declared(_design("fprobe_pynq_deep_fast"))
    assert deep["la_deep_lanes"].delay == timing.la_deep_lane_latency(n, age) == n + age
    assert "la_ext" not in deep and "la_ext" in fast                 # 垂れ流しだけの構成にトリガ取り込みは無い


def test_the_trigger_sequencer_is_declared_per_input_path():
    # A2: sys の構成はフィルタの段 + 4、取り込みの領域・1 周期に複数のサンプルの構成は素の値なので 4、事象は 2
    sys_ = _declared(_design("fprobe_pynq"))
    assert sys_["seq_pin"].delay == timing.TRIG_SEQ_PIN_DELAY + timing.LA_FILTER_DELAY == 7
    assert (sys_["seq_internal"].delay, sys_["seq_internal"].span) == (7, 0)
    assert (sys_["seq_event"].point, sys_["seq_event"].delay) == ("source_pulse", timing.TRIG_SEQ_EVENT_DELAY)
    assert _declared(_design("fprobe_pynq_cap"))["seq_pin"].delay == timing.TRIG_SEQ_PIN_DELAY
    fast = _declared(_design("fprobe_pynq_fast"))
    assert fast["seq_pin"].delay == timing.TRIG_SEQ_PIN_DELAY
    assert (fast["seq_input_lanes"].unit, fast["seq_input_lanes"].delay, fast["seq_input_lanes"].span) == ("fine_sample", 5 * 8 + 5, 8)
    assert "seq_pin" not in _declared(_design("fprobe_nano9k"))      # 多段トリガの無い構成


def test_the_cross_trigger_and_the_logic_analyzer_are_declared():
    # N3・B7: 外部トリガは源の周期 + 1 のピンの時刻のサンプルに付く。ロジアナのトリガを源にしたときの段数
    sys_ = _declared(_design("fprobe_pynq"))
    assert (sys_["la_ext"].point, sys_["la_ext"].delay, sys_["la_ext"].span) == ("source_pulse", timing.XTRIG_LA_EXT_DELAY, 0)
    assert sys_["la_xtrig"].delay == sys_["la"].delay + timing.LA_XTRIG_DELAY
    deep = _declared(_design("fprobe_pynq_deep"))
    assert deep["la_deep_xtrig"].delay == deep["la_deep"].delay
    assert deep["la_deep_packed"].delay == deep["la_deep"].delay + timing.LA_DEEP_PACK_DELAY
    assert "la_deep_packed" not in _declared(_design("fprobe"))     # 詰めの無い構成（Primer）
    cap = _declared(_design("fprobe_pynq_cap"))
    assert (cap["la_cap_xtrig"].unit, cap["la_cap_xtrig"].delay, cap["la_cap_xtrig"].span) == ("cap_cycle", 8 + 3 - 1, 3)


def test_every_design_round_trips_its_timing_declaration():
    from fprobe_gw.designs import DESIGNS
    for name, d in DESIGNS.items():
        if d.capabilities is None:
            continue
        got = parse(caps.encode(d.capabilities)).timing
        want = _declared(d.capabilities)
        assert set(got) == set(want), name
        for k, p in want.items():
            g = got[k]
            assert (g.point, g.unit, g.delay, g.span, set(g.excludes)) == (p.point, p.unit, p.delay, p.span, set(p.excludes)), (name, k)


# --- 段階 S3 ------------------------------------------------------------------------------------------------------

def test_every_output_carries_the_board_output_stage():
    """R9: ボードの出力の段（PYNQ-Z2 の IOB の FF 1 段）は DAP だけでなく UART 送信・同期の出力・クロストリガの出力にも入る。"""
    pynq = _design("fprobe_pynq_deep")
    assert timing.board_output_ticks(pynq.board) == 1 == timing.dap_output_ticks(pynq.board)
    for c, out in ((pynq, 1), (dataclasses.replace(TEST_CAPS, sync=True, aux=True), 0)):
        d = _declared(c)
        assert d["uart_tx"].delay == timing.UART_TX_START_DELAY - out
        assert d["dap_swd"].delay == -out
        if "sync_tx" in d:
            assert d["sync_tx"].delay == -timing.SYNC_TX_LATENCY_TICKS - out
        if "xtrig_out" in d:
            assert (d["xtrig_out"].point, d["xtrig_out"].delay, d["xtrig_out"].span) == (
                "output_edge", -timing.XTRIG_OUT_SOURCE_DELAY - out, 0)
            assert (d["xtrig_out_pins"].delay, d["xtrig_out_pins"].span) == (timing.XTRIG_OUT_PIN_DELAY + out, 1)


def test_the_aux_samples_are_declared_at_the_start_bit_of_the_first_byte():
    d = _declared(dataclasses.replace(TEST_CAPS, aux=True))
    assert (d["aux"].point, d["aux"].delay, d["aux"].span) == ("start_bit_edge", timing.UART_RX_START_DELAY, 1)
    assert "target_internal" in d["aux"].excludes
    assert "aux" not in _declared(dataclasses.replace(TEST_CAPS, aux=False))
