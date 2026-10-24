"""記録の stream rtos からのタスクの表・CPU 負荷・割り込み（fprobe.probe_recording_rtos）。画面が記録から自分で数えない
ように、時間軸（rtos_timeline_get / load）・profile_exceptions と同じ定義・同じ形で返す。"""

import pytest

from fprobe.mcp_server import Probe
from fprobe.recording import Recorder, Recording

HZ = 1_000_000                        # 1 タイムベース = 1 µs


def _probe(tmp_path, rows):
    rec = Recorder(tmp_path / "r", timebase_hz=HZ, meta={"name": "r"})
    rec.add_many(rows)
    rec.close()
    p = Probe()
    p._view = Recording.open(tmp_path / "r")
    return p


def run(t0, t1, handle, task, idle=False):
    body = {"task": task, "method": "itm", "resolution": 0}
    if idle:
        body["idle"] = True
    return ("rtos", "run", t0, handle, 0, body, t1)


def isr(t0, t1, number, name):
    return ("rtos", "isr", t0, number, 0, {"name": name}, t1)


def test_cpu_load_table_and_windows_use_only_the_recorded_time(tmp_path):
    # 0〜100 A、100〜200 IDLE、（200〜300 は run が無い = 記録していない）、300〜400 A、350〜360 は割り込み
    p = _probe(tmp_path, [run(0, 100, 1, "A"), run(100, 200, 2, "IDLE", idle=True), run(300, 400, 1, "A"),
                          isr(350, 360, 15, "SysTick")])
    out = p.recording_cpu_load(window_us=200)
    s = out["stats"]
    assert s["covered"] == 300                                 # 情報のある時間 = run のある時間（記録していない 100 を除く）
    assert s["tasks"]["A"]["total"] == 200 and s["tasks"]["A"]["runs"] == 2
    assert s["tasks"]["A"]["cpu"] == pytest.approx(200 / 300) and out["gaps_not_recorded"] == 1
    w = out["load"]["windows"]
    assert [x["covered"] for x in w] == [200, 100]
    assert w[0]["load"] == pytest.approx(0.5) and w[1]["load"] == pytest.approx(1.0)
    assert w[1]["isr"] == pytest.approx(0.1) and out["load"]["windows_total"] == 2
    assert out["load"]["idle_tasks"] == ["IDLE"]


def test_cpu_load_without_a_window_returns_the_table_only_and_nothing_without_runs(tmp_path):
    p = _probe(tmp_path, [run(0, 10, 1, "A")])
    assert "load" not in p.recording_cpu_load()
    q = _probe(tmp_path / "x", [("annotation", "mark", 5, 0, 0, {"text": "a"})])
    assert q.recording_cpu_load()["stats"] is None


def test_interrupts_self_time_subtracts_nested_activations_and_load_uses_the_rtos_span(tmp_path):
    p = _probe(tmp_path, [run(0, 1000, 1, "A"), isr(100, 200, 16, "UART"), isr(120, 150, 15, "SysTick"),
                          isr(500, 520, 15, "SysTick")])
    out = p.recording_interrupts()
    by = {r["exception"]: r for r in out["exceptions"]}
    assert by[16]["total_inclusive"] == 100 and by[16]["total_self"] == 70          # 中の SysTick 30 を引く
    assert by[15]["count"] == 2 and by[15]["total_self"] == 50
    assert by[16]["load"] == pytest.approx(70 / 1000) and by[16]["name"] == "UART"
    assert by[16]["max_at"] == 100 and by[16]["max_end"] == 200 and out["timebase_hz"] == HZ


def test_interrupts_distribution_and_the_numbers_active_at_a_time(tmp_path):
    rows = [run(0, 1000, 1, "A")] + [isr(t, t + d, 15, "SysTick") for t, d in ((0, 10), (100, 20), (200, 30), (300, 40))]
    p = _probe(tmp_path, rows)
    out = p.recording_interrupts(number=15, bins=3, longest=2, at=205)
    d = out["distribution"]
    assert [b["count"] for b in d["histogram"]] == [1, 1, 2]                          # 使う人の棒の数のまま（変えない）
    assert [(x["t"], x["inclusive"], x["bin"]) for x in d["longest"]] == [(300, 40, 2), (200, 30, 2)]
    assert [b["longest"]["t"] for b in d["histogram"]] == [0, 100, 300]              # 箱ごとのいちばん長い回（飛ぶ先）
    assert out["at_numbers"] == [15]


def test_interrupts_at_a_time_lists_the_nested_ones_from_outer_to_inner(tmp_path):
    p = _probe(tmp_path, [run(0, 1000, 1, "A"), isr(100, 200, 16, "UART"), isr(120, 150, 15, "SysTick")])
    assert p.recording_interrupts(at=130)["at_numbers"] == [16, 15]                  # 外 → 内（最後が実行中）
