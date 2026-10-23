"""fprobe.profile.timing: 2 点間の時間とばらつき（一時ディレクトリに記録を作って）。"""

import json
import random

import pytest

from fprobe.profile.timing import interval_report, measure, pair_times
from fprobe.recording import Recorder, Recording

START = {"stream": "mark", "kind": "start"}
END = {"stream": "mark", "kind": "end"}


@pytest.fixture
def recording(tmp_path):
    rng = random.Random(1)
    rec = Recorder(tmp_path / "rec", timebase_hz=100e6)
    t = 1000
    for k in range(200):
        rec.add("mark", "start", t)
        duration = 5000 if k == 137 else 1000 + rng.randint(-20, 20)
        rec.add("mark", "end", t + duration)
        rec.add("noise", "x", t + 10)                                 # 条件に合わない出来事
        t += 10_000
    rec.add("mark", "start", t)                                       # 終わりの来ない開始は捨てる
    rec.close()
    r = Recording(tmp_path / "rec")
    yield r
    r.close()


def test_measure_statistics_and_the_longest(recording):
    report = measure(recording, START, END)
    d = report.as_dict()
    assert d["count"] == 200 and d["timebase_hz"] == 100e6
    assert d["max"] == 5000 and d["max_at"] == 1000 + 137 * 10_000
    assert 980 <= d["min"] <= 1000 and 990 < d["median"] < 1010
    # 外れ値の経験則（1.5 IQR・3σ）は使わない: 長い順の上位を事実として（t は記録・タイムラインのその時刻）
    assert d["longest"][0]["t"] == 1000 + 137 * 10_000 and len(d["longest"]) == 10 and "outliers" not in d
    assert d["longest"][0]["t_end"] == d["longest"][0]["t"] + 5000
    assert [o["duration"] for o in d["longest"]] == sorted((o["duration"] for o in d["longest"]), reverse=True)
    assert sum(b["count"] for b in d["histogram"]) == 200 and len(d["histogram"]) == 20
    assert d["seconds"]["max"] == pytest.approx(50e-6)
    expected = recording.intervals(START, END)
    assert [tuple(p) for p in d["intervals"]] == expected["intervals"]
    assert d["mean"] == pytest.approx(expected["mean"]) and d["stdev"] == pytest.approx(expected["stdev"])
    json.dumps(d)


def test_pair_times_matches_recording_rule(recording):
    starts = [e.t for e in recording.find(START)]
    ends = [e.t for e in recording.find(END)]
    assert pair_times(starts, ends) == recording.intervals(START, END)["intervals"]


def test_pair_times_rules():
    # 2 つ目の開始の前に終わりが来ない最初の開始は捨てる。開始と同時の終わりは数える
    assert pair_times([0, 10, 20], [15, 20, 50]) == [(10, 5), (20, 0)]
    assert pair_times([5], []) == []


def test_interval_report_without_timebase_and_the_number_of_longest():
    pairs = [(k * 100, 50 + (k % 5)) for k in range(100)] + [(10_000, 500)]
    d = interval_report(pairs, longest=3, bins=5).as_dict()
    assert "seconds" not in d and len(d["histogram"]) == 5
    assert [o["t"] for o in d["longest"]] == [10_000, 400, 900]
    assert interval_report([]).as_dict() == {"count": 0, "unit": "ticks", "timebase_hz": None, "intervals": []}
