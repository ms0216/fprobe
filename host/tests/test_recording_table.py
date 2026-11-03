"""fprobe.recording_table: 出来事の表の版（版 1 = stream・kind の文字列の表、版 2 = 組の番号と辞書の表）。

古い版の記録は移さずにそのまま読み書きする。同じ出来事を入れた版 1・版 2 の記録が、読みのすべての口で同じ答えを返すことを
（乱数の出来事で）確かめる。
"""

import random
import sqlite3

import pytest

from fprobe.recording import Event, Recorder, Recording, _events
from fprobe.recording_sql import query
from fprobe.recording_table import VERSION, EventTable, version_of

HZ = 1e8
# 版 1（2026-11-03 まで）の表そのもの（recording.py の _SCHEMA だったもの）
V1_SCHEMA = """
CREATE TABLE IF NOT EXISTS events (
    t INTEGER NOT NULL, t_end INTEGER, stream TEXT NOT NULL, kind TEXT NOT NULL,
    a INTEGER NOT NULL DEFAULT 0, b INTEGER NOT NULL DEFAULT 0, body TEXT, unc REAL);
CREATE INDEX IF NOT EXISTS events_t ON events (t);
CREATE INDEX IF NOT EXISTS events_stream_t ON events (stream, t);
CREATE INDEX IF NOT EXISTS events_stream_kind_t ON events (stream, kind, t);
CREATE INDEX IF NOT EXISTS events_t_end ON events (t_end) WHERE t_end IS NOT NULL;
CREATE TABLE IF NOT EXISTS chunks (
    t0 INTEGER NOT NULL, t1 INTEGER NOT NULL, path TEXT NOT NULL, ticks_per_sample REAL NOT NULL,
    channels INTEGER NOT NULL, length INTEGER NOT NULL);
CREATE INDEX IF NOT EXISTS chunks_t ON chunks (t0, t1);
"""


def v1_recorder(directory):
    """版 1 の表を先に作った記録の書き手（古い記録に書き足すのと同じ）。"""
    directory.mkdir(parents=True)
    db = sqlite3.connect(directory / "recording.sqlite")
    db.executescript(V1_SCHEMA)
    db.close()
    return Recorder(directory, HZ)


def random_rows(n, seed=7):
    rng = random.Random(seed)
    rows = []
    for i in range(n):
        stream = rng.choice(["dap", "dap", "fpga", "rtos", "uart", "日本語"])
        kind = rng.choice({"dap": ["ap_read", "ap_write"], "fpga": ["value", "group", "gap"], "rtos": ["run", "isr"],
                           "uart": ["rx"], "日本語": ["印"]}[stream])
        t = rng.randrange(0, 20_000)
        t_end = t + rng.choice([1, 5, 300, 4000]) if stream == "rtos" or rng.random() < 0.05 else None
        body = rng.choice([None, None, {"x": rng.randrange(9), "text": "é"}, {"source": "s1"}])
        rows.append((stream, kind, t, rng.randrange(4), rng.randrange(1 << 20), body, t_end,
                     rng.choice([None, 0.5])))
    rows.append(("dap", "ap_read", 5000, 1, 1))                   # 同じ時刻の出来事が入れた順に並ぶ
    rows.append(("dap", "ap_read", 5000, 2, 2))
    rows.append(("fpga", "value", 5000, 3, 3))
    return rows


@pytest.fixture
def pair(tmp_path):
    """同じ出来事を版 1・版 2 の記録へ（2 回に分けて flush）。"""
    rows = random_rows(3000)
    out = []
    for name, make in (("v1", v1_recorder), ("v2", lambda d: Recorder(d, HZ))):
        rec = make(tmp_path / name)
        rec.add_many(rows[:1500])
        rec.flush()
        for r in rows[1500:]:
            rec.add(*r[:3], *r[3:5], body=r[5] if len(r) > 5 else None, t_end=r[6] if len(r) > 6 else None,
                    uncertainty=r[7] if len(r) > 7 else None)
        rec.close()
        out.append(Recording.open(tmp_path / name))
    yield out
    for r in out:
        r.close()


def test_new_recordings_use_the_keyed_table_and_old_ones_stay_as_they_are(pair):
    v1, v2 = pair
    assert v1._table.version == 1 and v2._table.version == VERSION == 2
    assert v2._db.execute("PRAGMA user_version").fetchone()[0] == 3   # 記録の形式の版（表は版 2）
    tables = {name for name, in v1._db.execute("SELECT name FROM sqlite_master")}
    assert "names" not in tables and "event" not in tables          # 古い記録に新しい表を作らない（移さない）
    indexes = {name for name, in v2._db.execute("SELECT name FROM sqlite_master WHERE type = 'index' AND tbl_name = 'event'")}
    assert indexes == {"event_t", "event_key_t", "event_t_end"}
    assert [c[1] for c in v2._db.execute("PRAGMA table_info(event)")] == ["t", "t_end", "key", "a", "b", "body", "unc"]


def test_every_read_gives_the_same_answer_on_both_versions(pair):
    v1, v2 = pair
    cases = [dict(), dict(t0=4000, t1=9000), dict(streams=["dap"]), dict(streams=["dap", "fpga"], t0=100),
             dict(kinds=["value", "run"]), dict(streams=["fpga"], kinds=["value"], t1=12_000),
             dict(t0=6000, t1=7000, overlapping=True), dict(t0=6000, streams=["rtos"], overlapping=True),
             dict(t0=6000, t1=5000, overlapping=True), dict(t0=6000, t1=8000, overlapping=True, a=2),
             dict(streams=["nothing"]), dict(limit=17, t0=3000), dict(a=1, streams=["dap"]),
             dict(streams=["日本語"])]
    for c in cases:
        assert list(v1.iter_events(**c)) == list(v2.iter_events(**c)), c
    for t in (0, 4999, 5000, 5001, 19_999, 30_000):
        for before in (True, False):
            for kw in (dict(stream="dap"), dict(stream="dap", kind="ap_read"), dict(stream="fpga", a=3),
                       dict(stream="rtos", body={"x": 3}), dict(stream="none")):
                assert v1.nearest(t, before, **kw) == v2.nearest(t, before, **kw), (t, before, kw)
    assert v1.count() == v2.count()
    for c in (dict(), dict(t0=10, t1=9000, streams=["dap", "rtos"]), dict(kinds=["value"])):
        assert v1.count_events(**c) == v2.count_events(**c)
    assert v1.kind_counts() == v2.kind_counts() and v1.kind_counts(100, 9000) == v2.kind_counts(100, 9000)
    for col in ("stream", "kind", "a", "b"):
        assert v1.distinct(col) == v2.distinct(col) and v1.distinct(col, ["dap"]) == v2.distinct(col, ["dap"])
    assert v1.span() == v2.span()
    assert v1.density(0, 20_000, 97) == v2.density(0, 20_000, 97)
    assert v1.density(0, 20_000, 50, ["rtos"]) == v2.density(0, 20_000, 50, ["rtos"])
    assert v1.event_bins(0, 211, 100) == v2.event_bins(0, 211, 100)
    assert v1.find({"stream": "dap", "a": {"ge": 2}}) == v2.find({"stream": "dap", "a": {"ge": 2}})


def test_sql_queries_see_the_same_events_with_rowid_on_both_versions(pair):
    v1, v2 = pair
    for sql in ("SELECT * FROM events ORDER BY t, rowid",
                "SELECT stream, kind, COUNT(*), MIN(t), MAX(t_end) FROM events GROUP BY 1, 2 ORDER BY 1, 2",
                "SELECT t, rowid FROM events WHERE stream = 'fpga' AND kind = 'group' ORDER BY t DESC, rowid DESC"):
        a, b = (query(r.directory, sql, limit=100_000) for r in (v1, v2))
        assert a["columns"] == b["columns"] and a["rows"] == b["rows"], sql
    assert query(v2.directory, "SELECT * FROM events LIMIT 1")["columns"] == \
        ["t", "t_end", "stream", "kind", "a", "b", "body", "unc", "rowid"]


def test_writes_after_the_fact_keep_each_version(pair):
    for r in pair:
        r.add("note", "mark", 123, body={"text": "あ"})
        r.replace_derived("rules", "check", [Event(7, "rules", "violation", 1, 2, {"k": 1}, 9)])
        r.replace_derived("rules", "check", [Event(8, "rules", "violation", 1, 2, None, None)])
    v1, v2 = pair
    assert list(v1.iter_events()) == list(v2.iter_events())
    assert [e.t for e in v2.iter_events(streams=["rules"])] == [8]
    assert v1._table.version == 1 and version_of(v1._db) == 1


def test_overlapping_finds_long_intervals_only_through_the_bounded_window(tmp_path):
    # 区間の長さの上限（names.longest）で [t0 − 上限, t0) だけを探す。上限より長い区間は無いので取りこぼさない
    rec = Recorder(tmp_path / "r", HZ)
    rec.add_many([("rtos", "run", 0, 0, 0, None, 1_000_000), ("rtos", "run", 10, 0, 0, None, 20)]
                 + [("dap", "ap_read", t) for t in range(0, 2_000_000, 100)])
    rec.close()
    r = Recording.open(tmp_path / "r")
    try:
        assert [(e.t, e.t_end) for e in r.iter_events(500_000, 500_001, ["rtos"], overlapping=True)] == [(0, 1_000_000)]
        assert r._table.longest(r._table.keys(["rtos"], None)[0]) == 1_000_000
        plan = " ".join(str(x) for x in r._db.execute(
            "EXPLAIN QUERY PLAN SELECT t FROM event INDEXED BY event_key_t WHERE key = 1 AND t >= 0 AND t < 5 "
            "AND t_end > 5 ORDER BY t, rowid"))
        assert "TEMP B-TREE" not in plan
    finally:
        r.close()


def test_a_reopened_old_recording_is_written_in_the_old_version(tmp_path):
    rec = v1_recorder(tmp_path / "r")
    rec.add("dap", "ap_read", 5, a=1)
    rec.close()
    again = Recorder(tmp_path / "r", HZ)
    again.add("dap", "ap_read", 6, a=2)
    again.close()
    r = Recording.open(tmp_path / "r")
    assert r._table.version == 1 and [e.a for e in r.iter_events()] == [1, 2]
    r.close()


def test_fast_event_rows_are_ordinary_events():
    row = (5, 1, "dap", "ap_read", 1, 2, '{"x": 1}', 9, 0.5)
    e = next(_events([row]))
    assert e == Event(5, "dap", "ap_read", 1, 2, {"x": 1}, 9, 0.5) and type(e) is Event
    assert repr(e) == repr(Event(5, "dap", "ap_read", 1, 2, {"x": 1}, 9, 0.5))
    with pytest.raises(AttributeError):
        e.t = 1                                                     # 凍ったまま


def test_names_added_by_another_connection_are_seen(tmp_path):
    rec = Recorder(tmp_path / "r", HZ)
    rec.add("dap", "ap_read", 1)
    rec.flush()
    r = Recording.open(tmp_path / "r")
    r.add("mark", "note", 2)                                       # 読み手が足した組
    rec.add("mark", "note", 3)                                     # 書き手は別の接続: 同じ組の番号を使う
    rec.add("uart", "rx", 4)
    rec.close()
    assert [(e.stream, e.t) for e in r.iter_events()] == [("dap", 1), ("mark", 2), ("mark", 3), ("uart", 4)]
    assert r._db.execute("SELECT COUNT(*) FROM names WHERE stream = 'mark'").fetchone()[0] == 1
    r.close()


def test_open_without_create_refuses_an_empty_database():
    db = sqlite3.connect(":memory:")
    with pytest.raises(sqlite3.OperationalError):
        EventTable.open(db, create=False)


def test_csv_and_json_exports_are_the_same_text_as_writing_each_event(pair, tmp_path):
    # 書き出しは本体の JSON の文字列をそのまま使う: 1 件ずつ Event を作って書いた前の実装と同じ文字列
    import csv
    import json
    from dataclasses import asdict
    for r in pair:
        for filters in ({}, {"t0": 3000, "t1": 9000, "streams": ["dap", "日本語"]}, {"limit": 5}):
            r.export_json(tmp_path / "a.json", **filters)
            expected = json.dumps([asdict(e) for e in r.events(**filters)], ensure_ascii=False)
            assert (tmp_path / "a.json").read_text(encoding="utf-8") == expected
            r.export_csv(tmp_path / "a.csv", **filters)
            with (tmp_path / "b.csv").open("w", newline="", encoding="utf-8") as f:
                w = csv.writer(f)
                w.writerow(["t", "t_end", "stream", "kind", "a", "b", "body", "uncertainty"])
                for e in r.events(**filters):
                    w.writerow([e.t, "" if e.t_end is None else e.t_end, e.stream, e.kind, e.a, e.b,
                                "" if e.body is None else json.dumps(e.body, ensure_ascii=False),
                                "" if e.uncertainty is None else e.uncertainty])
            assert (tmp_path / "a.csv").read_bytes() == (tmp_path / "b.csv").read_bytes()
