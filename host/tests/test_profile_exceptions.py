"""fprobe.profile.exceptions: 例外トレースから処理中の例外と割り込みごとの時間。"""

import json

import pytest

from fprobe.itm import ExceptionAction, ExceptionTrace, ItmDecoder, Overflow, PcSample, TimedPacket, Timestamper
from fprobe.profile.exceptions import ExceptionState, exception_name, exception_profile

ENTER, EXIT, RETURN = ExceptionAction.ENTER, ExceptionAction.EXIT, ExceptionAction.RETURN


def timed(*items):
    """(時刻, パケット) の列 → TimedPacket の列。"""
    return [TimedPacket(p, t, None) for t, p in items]


def exc(n, action):
    return ExceptionTrace(n, action)


def test_names():
    assert exception_name(0) == "Thread"
    assert exception_name(15) == "SysTick"
    assert exception_name(3) == "HardFault"
    assert exception_name(16 + 5) == "IRQ5"
    assert exception_name(21, {21: "USART1_IRQHandler"}) == "USART1_IRQHandler"


def test_state_is_unknown_until_trace():
    s = ExceptionState()
    assert s.current is None
    s.feed(exc(15, ENTER))
    assert s.current == 15
    s.feed(exc(15, EXIT))
    assert s.current is None                       # 入る前がどこだったかは分からない
    s.feed(exc(0, RETURN))
    assert s.current == 0
    s.feed(exc(21, ENTER))
    s.feed(exc(15, ENTER))                         # 横取り
    assert s.current == 15
    s.feed(exc(15, EXIT))
    s.feed(exc(21, RETURN))
    assert s.current == 21
    s.feed(Overflow())
    assert s.current is None


def test_nested_inclusive_and_self_time():
    packets = timed(
        (0, exc(0, RETURN)),
        (100, exc(21, ENTER)),       # IRQ5 100〜400
        (150, exc(15, ENTER)),       # SysTick が横取り 150〜200
        (200, exc(15, EXIT)),
        (200, exc(21, RETURN)),
        (400, exc(21, EXIT)),
        (400, exc(0, RETURN)),
        (1000, exc(15, ENTER)),      # SysTick 1000〜1080
        (1080, exc(15, EXIT)),
        (1080, exc(0, RETURN)),
    )
    prof = exception_profile(packets, clock_hz=1e6)
    rows = {r["name"]: r for r in prof.exceptions()}
    irq = rows["IRQ5"]
    assert irq["count"] == 1
    assert irq["total_inclusive"] == 300 and irq["total_self"] == 250
    tick = rows["SysTick"]
    assert tick["count"] == 2
    assert tick["inclusive"]["min"] == 50 and tick["inclusive"]["max"] == 80
    assert tick["max_at"] == 1000 and tick["max_end"] == 1080
    assert tick["seconds"]["inclusive"]["max"] == pytest.approx(80e-6)
    assert prof.span == 1080
    assert irq["load"] == pytest.approx(250 / 1080)
    assert [r["name"] for r in prof.exceptions()] == ["IRQ5", "SysTick"]        # 自分の時間の多い順
    json.dumps(prof.as_dict())


def test_tail_chaining():
    packets = timed(
        (0, exc(0, RETURN)),
        (10, exc(15, ENTER)), (30, exc(15, EXIT)),
        (30, exc(14, ENTER)), (70, exc(14, EXIT)),                 # 戻らずに PendSV へ
        (70, exc(0, RETURN)),
    )
    rows = {r["name"]: r for r in exception_profile(packets).exceptions()}
    assert rows["SysTick"]["total_self"] == 20 and rows["PendSV"]["total_self"] == 40
    assert "seconds" not in rows["SysTick"]


def test_overflow_drops_open_activations():
    packets = timed(
        (0, exc(0, RETURN)),
        (10, exc(15, ENTER)),
        (20, Overflow()),
        (30, exc(15, EXIT)),                                        # 入るを失った回は数えない
        (30, exc(0, RETURN)),
        (50, exc(15, ENTER)), (60, exc(15, EXIT)), (60, exc(0, RETURN)),
    )
    prof = exception_profile(packets)
    assert prof.overflows == 1
    assert [(a.start, a.end) for a in prof.activations] == [(50, 60)]


def test_missing_exit_is_inconsistency():
    packets = timed(
        (0, exc(0, RETURN)),
        (10, exc(21, ENTER)), (20, exc(15, ENTER)),
        (40, exc(21, EXIT)),                                        # 15 の出るが欠けた
        (40, exc(0, RETURN)),
    )
    prof = exception_profile(packets)
    assert prof.inconsistencies == 1
    assert [(a.number, a.inclusive) for a in prof.activations] == [(21, 30)]


def test_from_itm_bytes_through_timestamper():
    # 例外のパケット: ヘッダ 0x0E、番号の下位 8 ビット、上位 1 ビット | 動作 << 4。ローカルのタイムスタンプ（形式 2: 差 1〜6）
    def exc_bytes(n, action):
        return bytes([0x0E, n & 0xFF, (n >> 8) | action << 4])
    stream = (b"\x00" * 5 + b"\x80"                              # 同期
              + exc_bytes(15, 1) + b"\x30"                       # SysTick に入る、+3
              + exc_bytes(15, 2) + exc_bytes(0, 3) + b"\x50")    # 出る・スレッドへ戻る、+5
    packets = ItmDecoder().feed(stream)
    stamped = Timestamper().feed(packets)
    prof = exception_profile(stamped)
    assert [(a.number, a.start, a.end) for a in prof.activations] == [(15, 3, 8)]


def test_untimed_packets_only_update_state():
    packets = [TimedPacket(exc(15, ENTER), None, None), TimedPacket(exc(15, EXIT), None, None),
               TimedPacket(PcSample(0x100), None, None)]
    prof = exception_profile(packets)
    assert prof.activations == [] and prof.span is None and prof.exceptions() == []
