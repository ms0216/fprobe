"""記録の SQL 問い合わせ（fprobe.recording_sql）: 読み取り専用・件数と時間の上限・関数。"""

import pytest

from fprobe.recording import Recorder, Recording
from fprobe.recording_sql import QueryError, query

HZ = 100_000_000


@pytest.fixture
def recorder(tmp_path):
    rec = Recorder(tmp_path / "r", HZ)
    rec.add_many([("uart", "rx", 100, 0, 0x41), ("uart", "rx", 300, 0, 0x42), ("uart", "tx", 200, 0, 0x55),
                  ("annotation", "mark", 250, 0, 0, {"text": "化けた"})])
    rec.flush()
    yield rec
    rec.close()


def test_a_select_returns_columns_and_rows(recorder):
    out = query(recorder.directory, "SELECT kind, COUNT(*) AS n FROM events WHERE stream = ? GROUP BY kind ORDER BY kind",
                params=["uart"])
    assert out["columns"] == ["kind", "n"]
    assert out["rows"] == [["rx", 2], ["tx", 1]]
    assert out["truncated"] is False and out["count"] == 2


def test_json_bodies_and_the_timebase_functions_can_be_used(recorder):
    out = query(recorder.directory, "SELECT json_extract(body, '$.text'), seconds(t), timebase_hz() FROM events "
                                    "WHERE kind = 'mark'")
    assert out["rows"] == [["化けた", 250 / HZ, HZ]]


def test_named_parameters(recorder):
    out = query(recorder.directory, "SELECT b FROM events WHERE t >= :t0 ORDER BY t", params={"t0": 250})
    assert out["rows"] == [[0], [0x42]]


def test_rows_beyond_the_limit_are_cut_and_reported(recorder):
    out = query(recorder.directory, "SELECT t FROM events ORDER BY t", limit=2)
    assert out["rows"] == [[100], [200]] and out["truncated"] is True


@pytest.mark.parametrize("sql", [
    "DELETE FROM events",
    "UPDATE events SET a = 1",
    "INSERT INTO events (t, stream, kind) VALUES (1, 'x', 'y')",
    "DROP TABLE events",
    "CREATE TABLE x (a)",
    "CREATE TEMP TABLE x (a)",
    "ATTACH DATABASE 'other.sqlite' AS o",
    "PRAGMA journal_mode = DELETE",
    "SELECT 1; DELETE FROM events",
    "BEGIN",
])
def test_anything_but_reading_is_refused_and_the_recording_is_unchanged(recorder, sql):
    with pytest.raises(QueryError):
        query(recorder.directory, sql)
    view = Recording.open(recorder.directory)
    try:
        assert view.count() == 4
    finally:
        view.close()
    assert not (recorder.directory / "other.sqlite").exists()


def test_a_query_that_runs_too_long_is_stopped(recorder):
    slow = ("WITH RECURSIVE n(i) AS (SELECT 1 UNION ALL SELECT i + 1 FROM n) SELECT COUNT(*) FROM n")
    with pytest.raises(QueryError, match="時間"):
        query(recorder.directory, slow, timeout_s=0.2)


def test_a_wrong_query_is_a_query_error(recorder):
    with pytest.raises(QueryError, match="no such table"):
        query(recorder.directory, "SELECT * FROM missing_table")


def test_a_closed_recording_can_be_queried(recorder):
    recorder.close()
    out = query(recorder.directory, "SELECT COUNT(*) FROM events")
    assert out["rows"] == [[4]]


def test_blobs_are_returned_as_hex(recorder):
    out = query(recorder.directory, "SELECT x'00ff'")
    assert out["rows"] == [["00ff"]]
