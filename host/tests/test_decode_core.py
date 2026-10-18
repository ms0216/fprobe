"""fprobe.decode.core: 注釈の共通の形、チャンネルの取り出し、発生器の土台（ハードウェア不要）。"""

import json
from array import array

import pytest

from fprobe.decode.core import Annotation, ChannelLevels, Decoding, project
from fprobe.decode.synth import Wave
from fprobe.la import Transitions


def tr(values, starts, length, trigger=0, channels=4, period=1e-6):
    return Transitions(array("H", values), array("Q", starts), length, trigger, 0, period, channels)


def test_project_keeps_only_changes_of_the_chosen_channels():
    # ch0 は 0,0,1,1,1、ch2 は 0,1,1,1,0、ch1 は毎回変わる（選ばない）
    t = tr([0b000, 0b110, 0b101, 0b111, 0b011], [0, 2, 4, 6, 8], 10)
    starts, values = project(t, [2, 0])
    assert list(starts) == [0, 2, 4, 8]
    assert list(values) == [0b00, 0b01, 0b11, 0b10]          # ビット 0 = ch2、ビット 1 = ch0


def test_channel_levels_look_up_by_bisection():
    t = tr([0b0, 0b1, 0b0, 0b1], [0, 3, 5, 9], 12)
    c = ChannelLevels(t, 0)
    assert [c.level(i) for i in range(12)] == [0, 0, 0, 1, 1, 0, 0, 0, 0, 1, 1, 1]
    assert c.next_edge(0, 1) == 3 and c.next_edge(4, 0) == 5 and c.next_edge(4, 1) == 9 and c.next_edge(10, 0) is None
    assert c.next_edge(3, 1) == 3                           # sample 自身の変化を含む
    assert list(c.edges) == [3, 5, 9] and c.initial == 0


def test_decoding_times_filters_and_json():
    anns = (Annotation(10, 20, "data", "0x41", value=0x41), Annotation(30, 40, "data", "0x42", value=0x42, error="parity"),
            Annotation(50, 60, "break", "BREAK"))
    d = Decoding("uart", anns, sample_period_s=1e-6, trigger=25, length=100, params={"baud": 9600})
    assert d.seconds(10) == pytest.approx(10e-6) and d.since_trigger(10) == pytest.approx(-15e-6)
    assert [a.value for a in d.of("data")] == [0x41, 0x42]
    assert d.errors() == [anns[1]]
    assert len(d) == 3 and list(d) == list(anns)
    assert [a.text for a in d.between(15, 35)] == ["0x41", "0x42"]   # 区間 [15, 35) に重なる注釈
    assert d.between(20, 30) == []
    j = json.loads(d.to_json())
    assert j["protocol"] == "uart" and j["params"] == {"baud": 9600} and j["trigger"] == 25
    first = j["annotations"][0]
    assert first["start"] == 10 and first["end"] == 20 and first["kind"] == "data" and first["value"] == 0x41
    assert first["start_s"] == pytest.approx(10e-6) and first["end_rel_s"] == pytest.approx(-5e-6)
    assert first["error"] is None and first["fields"] is None
    assert j["annotations"][1]["error"] == "parity"


def test_annotation_must_have_positive_extent():
    with pytest.raises(ValueError):
        Annotation(5, 5, "data", "x")


def test_wave_builds_transitions_at_the_first_sample_after_each_change():
    w = Wave(channels=3, sample_rate_hz=1e6, idle={1: 1})
    w.set(0, 1, 2.5e-6)              # 2.5 us → サンプル 3
    w.set(2, 1, 3.0e-6)              # ちょうど 3 us → サンプル 3
    w.set(1, 0, 7.1e-6)
    w.set(1, 1, 7.2e-6)              # 同じサンプル 8 に落ちる変化は後が勝つ（グリッチは見えない）
    w.set(0, 0, 9e-6)
    t = w.build(end_s=12e-6, trigger_s=3e-6)
    assert list(t.starts) == [0, 3, 9] and list(t.values) == [0b010, 0b111, 0b110]
    assert t.length == 12 and t.trigger == 3 and t.sample_period_s == pytest.approx(1e-6) and t.channels == 3


def test_decode_by_name():
    from fprobe.decode import decode, decoders
    from fprobe.decode.synth import uart
    w = Wave(channels=8, sample_rate_hz=16 * 9600, idle={5: 1})
    end = uart(w, 5, b"ok", 1e-3, baud=9600)
    d = decode("uart", w.build(end_s=end), channel=5, baud=9600)
    assert d.protocol == "uart" and [a.value for a in d.of("data")] == list(b"ok")
    assert {"i2c", "spi", "swd", "uart"} <= {d.name for d in decoders()}
    with pytest.raises(ValueError):
        decode("no_such_protocol", w.build(end_s=end))


def test_wave_can_start_in_the_middle():
    w = Wave(channels=1, sample_rate_hz=1e6)
    w.set(0, 1, 1e-6)
    w.set(0, 0, 5e-6)
    t = w.build(start_s=3e-6, end_s=8e-6)          # 捕捉の途中から: 3 us が先頭
    assert t.length == 5 and list(t.starts) == [0, 2] and list(t.values) == [1, 0]
