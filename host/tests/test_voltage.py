"""ターゲットの電圧の事実（fprobe.voltage）: 統計・使う人が渡した電圧以下の区間・デーモンが読んだ電圧の履歴、
記録の電圧（recording_voltage）。基準の水準は推定しない。"""

from fprobe import voltage
from fprobe.mcp_server import Probe
from fprobe.recording import Recorder, Recording


def test_dips_are_the_runs_at_or_below_the_given_voltage():
    points = [(0, 3.3), (1, 3.0), (2, 2.9), (3, 3.3), (4, 3.1), (5, 3.0)]
    assert voltage.dips(points, 3.1) == [
        {"t": 1, "t_end": 3, "at": 2, "min": 2.9, "under": 3.1 - 2.9},
        {"t": 4, "t_end": 5, "at": 5, "min": 3.0, "under": 3.1 - 3.0}]
    assert voltage.dips(points, 2.0) == []


def test_history_keeps_every_reading_and_counts_only_measured_volts():
    h = voltage.VoltageHistory(limit=3)
    for t, r in enumerate([{"volts": 3.3, "live": True}, {"volts": None, "over_range": True, "live": True},
                           {"volts": 0, "live": False}, {"volts": 3.1, "live": True}]):
        h.add(r, now=float(t))
    out = h.read()
    assert [r["time"] for r in out["readings"]] == [1.0, 2.0, 3.0] and out["dropped"] == 1
    assert out["stats"] == {"count": 1, "min": 3.1, "max": 3.1, "mean": 3.1, "readings": 3, "over_range": 1,
                            "not_live": 1}
    assert [r["time"] for r in h.read(since=2.0)["readings"]] == [3.0]


def test_recording_voltage_counts_every_event_and_dips_only_when_asked(tmp_path):
    rec = Recorder(tmp_path / "r", timebase_hz=1_000_000, meta={"name": "r"})
    rec.add_many([("aux", "mv", t, 0, mv, None) for t, mv in enumerate([3300, 2900, 2900, 3300])]
                 + [("uart", "rx", 1, 0, 0, None)])
    rec.close()
    p = Probe()
    p._view = Recording.open(tmp_path / "r")
    out = p.recording_voltage()
    assert out["stats"]["count"] == 4 and out["lowest"] == {"t": 1, "volts": 2.9} and out["dips"] is None
    assert [d["t"] for d in p.recording_voltage(below_v=3.0)["dips"]] == [1]
    assert p.recording_voltage(t0=2)["stats"]["count"] == 2
