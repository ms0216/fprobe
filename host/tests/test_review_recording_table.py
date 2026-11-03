"""2026-11-03 のレビュー（記録の表。docs/reports/2026-11-03-cloud-A.md）: 版 2 の表で ROLLBACK の後に組の番号の覚えが残った不具合（直す前は落ちた）。"""
from fprobe.recording import Event, Recorder, Recording


def _make(tmp_path):
    r = Recorder(tmp_path, 1e6)
    r.add("cpu", "halt", 10)
    r.close()
    return Recording(tmp_path)


def test_rollback_after_new_kind_forgets_key(tmp_path):
    """閉じた記録に、新しい組の出来事を足すのが（本体が JSON にできず）失敗して ROLLBACK した後、正しい本体で足し直すと、
    出来事は読めるべき。今は組の番号の覚え（_key）が ROLLBACK で消えた names の行を指したままになり、足した出来事が
    events のビュー（JOIN names）から見えない。"""
    rec = _make(tmp_path)
    try:
        rec.add("analysis", "mark", 20, body={"bad": object()})       # JSON にできない → TypeError → ROLLBACK
    except TypeError:
        pass
    rec.add("analysis", "mark", 30, body={"ok": 1})
    assert [(e.stream, e.kind, e.t) for e in rec.events()] == [("cpu", "halt", 10), ("analysis", "mark", 30)]
    rec.close()


def test_rollback_key_reused_by_other_kind(tmp_path):
    """上の続き: 消えた番号を、別の組が names に足したときに受け継ぐと、前の組の出来事がその組の名前で見える。"""
    rec = _make(tmp_path)
    try:
        rec.add("analysis", "mark", 20, body={"bad": object()})
    except TypeError:
        pass
    rec.add("analysis", "mark", 30, body={"ok": 1})                  # 消えた番号で書かれる
    other = Recording(tmp_path)                                      # 別の接続（別の読み手）が新しい組を足す
    other.add("probe", "note", 40)
    other.close()
    rec2 = Recording(tmp_path)
    got = [(e.stream, e.kind, e.t) for e in rec2.events()]
    rec2.close()
    rec.close()
    assert ("probe", "note", 30) not in got, got


def test_replace_derived_retry_after_a_bad_body(tmp_path):
    """解析の派生イベント（replace_derived）で、本体に JSON にできない値（numpy の整数など）があって失敗した後、直して
    書き直すと、書いたと返った件数の出来事が読めるべき。"""
    np = __import__("pytest").importorskip("numpy")
    rec = _make(tmp_path)
    try:
        rec.replace_derived("rules", "check1", [Event(50, "rules", "violation", body={"n": np.int64(3)})])
    except TypeError:
        pass
    n = rec.replace_derived("rules", "check1", [Event(50, "rules", "violation", body={"n": 3})])
    got = [e for e in rec.events(streams=["rules"])]
    rec.close()
    assert n == 1 and len(got) == 1, got
