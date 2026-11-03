"""2026-11-03 のレビュー（PS 直読みの流れの位置）の B1: PS が読む流れ（ps_read、STREAM_EVENTS）で、run を下げた後もイベントが
来続けても、流れは止めの命令が効いた時点で終わる（reading・ACTIVE が下がる）。流れのイベントはその時点までで、それより後の
イベントは流れに入れずイベントのリングに残る（events.read の窓経由で読める）。
前は PS（relay.c と同じ約束: 公開された ps_ev_wpos まで読む）が mem の ev_lim（止めた時点の書き位置）を越えて EV_CONSUMED を
返し、ev_unread_r = ev_lim - ev_rpos が mod 2^EV_BITS で 0 にならず、reading（と ACTIVE）が 1 のまま残った。"""

import cocotb
from cocotb.triggers import ClockCycles, ReadOnly, RisingEdge

from tests.tb.la_deep_buffer_mix_tb import EventSource
from tests.tb.la_deep_buffer_ps_tb import EV_BASE, EV_CAP, Ps, setup, start


async def _run(dut, keep_events):
    mem, window = await setup(dut)
    dut.ev_enable.value = 1
    await ClockCycles(dut.clk, 5)
    first = ps = Ps(dut, mem, window, every=60)
    source = EventSource(dut, 97)
    await start(dut, rate=7, events_from=0)
    while len(source.sent) < 30:
        await RisingEdge(dut.clk)
    if not keep_events:
        source.stop()
    ps.stop()                                           # PS がしばらく読まない（中継が他の仕事・CPU を取られている間）
    await ClockCycles(dut.clk, 300)                     # その間に未読のロジアナの拍が溜まる（止めた時点で未読がある）
    await ReadOnly()
    unread_at_stop = (int(dut.ps_la_written.value) - int(dut.ps_la_consumed.value)) % (1 << 32)
    await RisingEdge(dut.clk)
    assert unread_at_stop > 0
    dut.run.value = 0                                   # 止める。keep_events なら、DAP のイベントは来続ける（events.enable = 1 のまま）
    sent_at_stop = len(source.sent)
    await ClockCycles(dut.clk, 400)
    source.stop()                                       # 少し後にイベントも止まる
    ps = Ps(dut, mem, window, every=60)                 # PS が読みに戻る（公開された ev_wpos まで読む。relay.c と同じ）
    low = False
    for _ in range(20_000):
        await RisingEdge(dut.clk)
        await ReadOnly()
        if dut.reading.value == 0:
            low = True
            break
    active = int(dut.ps_active.value)
    ps.stop()
    assert low, ("reading が下がらない", "ps_active", active, "PS が読んだイベント", len(ps.events),
                 "送ったイベント", len(source.sent))
    # 流れのイベント（最初の PS と戻った PS が読んだ分）は、送った順の先頭で、止めの命令が効いた時点まで（命令から効くまでの
    # 数周期に 1 件入りうる。イベントは 97 周期に 1 件）
    streamed = first.events + ps.events
    assert streamed == source.sent[:len(streamed)], "流れのイベントが送った順の先頭でない"
    assert sent_at_stop - 1 <= len(streamed) <= sent_at_stop + 1, ("流れのイベントの数", len(streamed), "止めた時点", sent_at_stop)
    # それより後のイベントは流れに入れず、イベントのリングに残る（窓経由で読める）
    later = source.sent[len(streamed):]
    if keep_events:
        assert len(later) >= 3, len(later)
    ring = [mem.memory.get(EV_BASE + i % EV_CAP, 0) for i in range(len(streamed), len(source.sent))]
    assert ring == later, "止めた後のイベントがリングに無い"


@cocotb.test()
async def reading_falls_when_events_stop_with_the_run(dut):
    # 対照: イベントが run と同時に止まれば下がる
    await _run(dut, keep_events=False)


@cocotb.test()
async def reading_falls_even_if_events_keep_coming_after_the_run_is_stopped(dut):
    await _run(dut, keep_events=True)
