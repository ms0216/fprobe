"""SWO の DWT・ITM の設定の計算（fprobe.swo_trace）: CPUID・比較器の割り当てと FUNCTION の符号化・書く順・
帯域の見積もり、データトレースのパケットの組み立て、イベントカウンタの溢れの合計、時刻付きの増分の解読。"""

import pytest

from fprobe import swo_trace as st
from fprobe.itm import (DataTraceAddress, DataTracePc, DataTraceValue, EventCounter, ItmDecoder, Overflow,
                        PcSample)
from fprobe.profile.dwt import DEMCR, DWT_CTRL, ITM_LAR, ITM_TCR, ITM_TER


def info(**kw):
    base = dict(numcomp=4, arch="v7m", trace_packets=True, profiling_counters=True, busy=frozenset(),
                value_match=frozenset({1}), linkable=frozenset({1}), max_mask=15)
    base.update(kw)
    return st.DwtInfo(**base)


def writes_of(plan):
    return {(w.address, w.mask): w.value for w in plan.setup + plan.enable}


# --- CPUID ------------------------------------------------------------------------------------

@pytest.mark.parametrize("cpuid,arch,name", [(0x410FC241, "v7m", "Cortex-M4"), (0x410FC270, "v7m", "Cortex-M7"),
                                             (0x410CC601, "v6m", "Cortex-M0+"), (0x410FD213, "v8m", "Cortex-M33"),
                                             (0x00000000, "unknown", "unknown")])
def test_core_info(cpuid, arch, name):
    core = st.core_info(cpuid)
    assert (core.arch, core.name) == (arch, name)


# --- 比較器の大きさ ------------------------------------------------------------------------------

@pytest.mark.parametrize("address,size,base,mask", [(0x20000004, 4, 0x20000004, 2), (0x20000001, 1, 0x20000001, 0),
                                                    (0x20000002, 2, 0x20000002, 1), (0x20000006, 4, 0x20000000, 4),
                                                    (0x20000010, 12, 0x20000010, 4)])
def test_address_mask_covers_the_variable(address, size, base, mask):
    assert st.address_mask(address, size) == (base, mask)


# --- 計画 ------------------------------------------------------------------------------------

def test_plan_ports_timestamps_exceptions_counters():
    req = st.TraceRequest(ports=(0, 31), timestamps=True, timestamp_prescale=4, exception_trace=True,
                          counters=("cpi", "lsu"))
    plan = st.plan_trace(req, info(), core_clock_hz=16e6, swo_baud=2e6)
    setup = [w.address for w in plan.setup]
    assert setup == [DEMCR, ITM_LAR]
    w = writes_of(plan)
    ter = next(v for (a, m), v in w.items() if a == ITM_TER)
    assert ter == (1 | 1 << 31)
    tcr = next((v, m) for (a, m), v in w.items() if a == ITM_TCR)
    assert tcr[0] & 0b1111 == 0b1111 and (tcr[0] >> 8) & 3 == 1 and (tcr[0] >> 16) & 0x7F == 1
    ctrl, ctrl_mask = next((v, m) for (a, m), v in w.items() if a == DWT_CTRL)
    assert ctrl & 1 and (ctrl >> 10) & 3 == 1 and ctrl & 1 << 16 and ctrl & 1 << 17 and ctrl & 1 << 20
    assert not ctrl & 1 << 12 and not ctrl_mask & 1 << 12             # PC サンプリングには触らない
    assert not ctrl_mask & (1 << 18 | 1 << 19 | 1 << 21)               # 頼まれていないカウンタには触らない
    # LSU・CPI は最悪 256 サイクルごとに 2 バイト: 16 MHz で 250 KB/s > 2 Mbaud の帯域
    assert any("イベントカウンタ" in x for x in plan.warnings)
    # 「帯域の 50 % を超えたら」の閾値で出す・出さないを決めない: 少ない（1 MHz・CPI だけ = 帯域の 4 %）ときも数字を書く
    low = st.plan_trace(st.TraceRequest(counters=("cpi",)), info(), core_clock_hz=1e6, swo_baud=2e6)
    assert any("イベントカウンタ" in x and "4%" in x for x in low.warnings)
    # ITM の有効化は DWT の出どころより先（DWT のパケットを ITM が流す）
    order = [w.address for w in plan.enable]
    assert order.index(ITM_TCR) < order.index(DWT_CTRL)


def test_plan_data_trace_functions_and_order():
    req = st.TraceRequest(data_traces=(
        st.DataTrace("counter", 0x20000004, 4, access="write", pc=True, value=True),
        st.DataTrace("flags", 0x20000008, 1, access="read", pc=False, value=True),
        st.DataTrace("buf", 0x20000010, 16, access="rw", pc=False, value=True, offset=True),
    ))
    plan = st.plan_trace(req, info(), swo_baud=2e6)
    uses = {u.name: u for u in plan.comparators}
    # 高い番号から（pyOCD は 0 から使う）。値の比較のできる 1 は値一致のために残す
    assert [uses[n].index for n in ("counter", "flags", "buf")] == [3, 2, 0]
    assert uses["counter"].function == 0b1111
    assert uses["flags"].function == 0b1100
    assert uses["buf"].function == 0b0011 | 1 << 5 and uses["buf"].mask == 4
    w = writes_of(plan)
    assert w[(st.dwt_comp(3), 0xFFFFFFFF)] == 0x20000004 and w[(st.dwt_mask(3), 0xFFFFFFFF)] == 2
    enable = [x.address for x in plan.enable]
    for u in plan.comparators:                                         # FUNCTION は COMP・MASK の後
        assert enable.index(st.dwt_function(u.index)) > enable.index(st.dwt_mask(u.index))
    tcr = next(v for (a, m), v in w.items() if a == ITM_TCR)
    assert tcr & 1 << 3                                                # DWTENA
    assert plan.bytes_per_access["counter"] == 5 + 5 and plan.bytes_per_access["buf"] == 3 + 5


def test_plan_value_watch_links_an_address_comparator():
    req = st.TraceRequest(value_watches=(st.ValueWatch("mode", 3, 1, address=0x20000009, access="write"),))
    plan = st.plan_trace(req, info(), swo_baud=None)
    value = next(u for u in plan.comparators if u.role == "value_watch")
    link = next(u for u in plan.comparators if u.role == "link")
    assert value.index == 1 and link.index == 3
    assert value.function == 6 | 1 << 8 | 0 << 10 | 3 << 12
    w = writes_of(plan)
    assert w[(st.dwt_comp(1), 0xFFFFFFFF)] == 0x03030303               # 1 バイトの値は全レーンに
    assert w[(st.dwt_comp(3), 0xFFFFFFFF)] == 0x20000009
    assert w[(st.dwt_function(3), 0xFFFFFFFF)] == 0
    # 止める値一致だけなら ITM・SWO は要らない
    assert not any(x.address in (ITM_TCR, ITM_TER) for x in plan.enable)


def test_plan_value_watch_trace_and_any_address():
    req = st.TraceRequest(value_watches=(st.ValueWatch("magic", 0xDEAD, 2, address=None, action="trace"),))
    plan = st.plan_trace(req, info(), swo_baud=2e6)
    (value,) = plan.comparators
    assert value.function == 0b0011 | 1 << 8 | 1 << 10 and value.emits == ("pc", "value")
    assert writes_of(plan)[(st.dwt_comp(1), 0xFFFFFFFF)] == 0xDEADDEAD


@pytest.mark.parametrize("request_,message", [
    (st.TraceRequest(value_watches=(st.ValueWatch("x", 1, 4, address=0x20000000),) * 2), "値の比較"),
    (st.TraceRequest(data_traces=(st.DataTrace("x", 0x20000000, 4),) * 5), "比較器が足りない"),
    (st.TraceRequest(data_traces=(st.DataTrace("x", 0x20000000, 1 << 20),)), "大きすぎる"),
    (st.TraceRequest(data_traces=(st.DataTrace("x", 0x20000000, 4, pc=True, offset=True),)), "pc と offset"),
    (st.TraceRequest(data_traces=(st.DataTrace("x", 0x20000000, 4, access="read", pc=True, value=False),)), "読み"),
    (st.TraceRequest(value_watches=(st.ValueWatch("x", 1, 8, address=0x20000000),)), "1 / 2 / 4"),
    (st.TraceRequest(value_watches=(st.ValueWatch("x", 1, 4, 0x20000000, access="read", action="trace"),)), "rw"),
])
def test_plan_errors(request_, message):
    with pytest.raises(ValueError, match=message):
        st.plan_trace(request_, info(), swo_baud=2e6)


def test_plan_skips_busy_comparators_and_checks_the_architecture():
    req = st.TraceRequest(data_traces=(st.DataTrace("x", 0x20000000, 4),))
    assert st.plan_trace(req, info(busy=frozenset({3})), swo_baud=2e6).comparators[0].index == 2
    with pytest.raises(ValueError, match="ARMv8-M"):
        st.plan_trace(req, info(arch="v8m"), swo_baud=2e6)
    with pytest.raises(ValueError, match="トレースのパケット"):
        st.plan_trace(req, info(trace_packets=False), swo_baud=2e6)
    with pytest.raises(ValueError, match="swo_baud"):
        st.plan_trace(req, info(), swo_baud=None)
    with pytest.raises(ValueError, match="カウンタ"):
        st.plan_trace(st.TraceRequest(counters=("cpi",)), info(profiling_counters=False), swo_baud=2e6)
    with pytest.raises(ValueError, match="cpi"):
        st.plan_trace(st.TraceRequest(counters=("bogus",)), info(), swo_baud=2e6)


# --- データトレースの組み立て ------------------------------------------------------------------

def uses_for(*traces):
    return st.plan_trace(st.TraceRequest(data_traces=traces), info(), swo_baud=2e6).comparators


def test_assembler_pairs_the_pc_with_the_following_value():
    (use,) = uses_for(st.DataTrace("counter", 0x20000004, 4, access="write"))
    asm = st.DataTraceAssembler([use])
    c = use.index
    out = asm.feed([(100, None, DataTracePc(c, 0x08000123)), (110, None, DataTraceValue(c, True, 7, 4)),
                    (200, None, DataTracePc(c, 0x08000125)), (205, 5, DataTraceValue(c, True, 8, 4)),
                    (300, None, DataTracePc(c, 0x08000127)), (400, None, DataTracePc(c, 0x08000129))])
    assert [(a.t, a.pc, a.value, a.complete) for a in out] == [(100, 0x08000123, 7, True), (200, 0x08000125, 8, True),
                                                              (300, 0x08000127, None, False)]
    assert out[1].local_time == 5 and out[0].name == "counter" and out[0].write
    assert [(a.t, a.pc) for a in asm.flush()] == [(400, 0x08000129)]


def test_assembler_offset_gives_the_address_and_drops_accesses_outside():
    (use,) = uses_for(st.DataTrace("arr", 0x20010006, 4, access="rw", pc=False, value=True, offset=True))
    assert use.mask == 4                                               # 0x20010000〜0x2001000F を見張る
    asm = st.DataTraceAssembler([use])
    c = use.index
    out = asm.feed([(1, None, DataTraceAddress(c, 0x0008)), (2, None, DataTraceValue(c, False, 0x55, 1)),
                    (3, None, DataTraceAddress(c, 0x0000)), (4, None, DataTraceValue(c, True, 1, 4))])
    assert [(a.address, a.value, a.write) for a in out] == [(0x20010008, 0x55, False)]
    assert asm.outside == 1


def test_assembler_overflow_and_unknown_comparators():
    (use,) = uses_for(st.DataTrace("counter", 0x20000004, 4))
    asm = st.DataTraceAssembler([use])
    c = use.index
    out = asm.feed([(1, None, DataTracePc(c, 0x100)), (2, None, Overflow()), (3, None, DataTraceValue(c, True, 1, 4)),
                    (4, None, DataTraceValue(0, True, 9, 4)), (5, None, PcSample(0x200))])
    assert [(a.pc, a.value, a.complete) for a in out] == [(None, 1, False)]
    assert asm.lost == 1 and asm.unknown == 1


def test_counter_totals():
    totals = st.CounterTotals()
    totals.feed([EventCounter(0x01), EventCounter(0x09), PcSample(1), EventCounter(0x10)])
    assert totals.as_dict() == {"cpi": 512, "exc": 0, "sleep": 0, "lsu": 256, "fold": 256}


# --- 時刻付きの増分の解読 ---------------------------------------------------------------------

def test_timed_stream_matches_one_shot_decoding_across_chunks():
    data = b"\x00" * 5 + b"\x80" + b"\x47\x23\x01\x00\x08" + b"\x8F\x07\x00\x00\x00" + b"\x01\x41"
    times = [(1000 + 10 * i, b) for i, b in enumerate(data)]
    stream = st.TimedItmStream()
    out = []
    for k in range(0, len(times), 3):
        out += stream.feed(times[k:k + 3])
    one = ItmDecoder().feed(data)
    assert [p for _, p in out] == one
    assert [t for t, _ in out] == [1000 + 10 * p.offset for p in one]
    assert stream.pending_bytes < 3
