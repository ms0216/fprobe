"""記録の stream・kind ごとの件数・割合・1 秒あたり（recording_kinds）。画面が SQL の行から数えない（行の上限で欠けない）。"""

import pytest

from fprobe.mcp_server import Probe
from fprobe.recording import Recorder, Recording

HZ = 1_000_000


def _probe(tmp_path, rows):
    rec = Recorder(tmp_path / "r", timebase_hz=HZ, meta={"name": "r"})
    rec.add_many(rows)
    rec.close()
    p = Probe()
    p._view = Recording.open(tmp_path / "r")
    return p


def test_kinds_count_every_event_with_share_and_rate(tmp_path):
    rows = [("uart", "rx", t, 0, 0, None) for t in range(0, 3000, 1)] + [("dap", "read", 10, 0, 0, None)]
    p = _probe(tmp_path, rows)
    out = p.recording_kinds()
    assert out["total"] == 3001 and out["span"] == [0, 2999]
    first = out["kinds"][0]
    assert (first["stream"], first["kind"], first["count"]) == ("uart", "rx", 3000)        # 件数の多い順
    assert first["share"] == pytest.approx(3000 / 3001)
    assert first["per_second"] == pytest.approx(3000 / (3000 / HZ))                        # 範囲の長さ = span の幅 + 1
    assert out["kinds"][1]["first"] == 10 and out["kinds"][1]["last"] == 10


def test_kinds_in_a_range(tmp_path):
    p = _probe(tmp_path, [("uart", "rx", t, 0, 0, None) for t in range(100)])
    out = p.recording_kinds(t0=10, t1=20)
    assert out["total"] == 10 and out["span"] == [10, 20] and out["kinds"][0]["per_second"] == pytest.approx(10 / (10 / HZ))
