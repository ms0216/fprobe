"""コミット済みの生成物が単一の定義元と一致していること。"""

from fprobe_gw.generate import stale


def test_committed_artifacts_match_generators():
    assert stale() == [], "python -m fprobe_gw gen で再生成すること"
