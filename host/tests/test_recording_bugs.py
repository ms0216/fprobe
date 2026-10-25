"""不具合の再現試験（修正済み。台帳: docs/reports/bugs/correlate-recording.md）。領域: recording / recording_long。"""

import pytest

from fprobe.recording import Event, Recorder, Recording


def test_distinct_is_not_stale_after_replace_derived_removes_a_middle_row(tmp_path):
    rec = Recorder(tmp_path / "r", timebase_hz=1000.0)
    rec.add("dap", "plain", 1)
    rec.close()
    r = Recording.open(tmp_path / "r")
    r.replace_derived("rules", "src", [Event(2, "rules", "violation", 0, 0)])
    r.add("dap", "later", 3)                                    # 派生の行より後ろに行を足す（最大の rowid はこれ）
    assert "violation" in r.distinct("kind")                    # ここで答えを覚える
    r.replace_derived("rules", "src", [])                       # 派生の行だけ消える。最大の rowid は変わらない
    assert "violation" not in r.distinct("kind")
