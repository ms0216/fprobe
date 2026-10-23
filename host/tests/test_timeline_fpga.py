"""時間軸の FPGA の観測の段（fprobe.timeline_fpga）: 記録の組の生の値（stream fpga）を、記録の情報の信号の定義
（fpga.signals）で信号ごとの段に展開する。生の段・要約の段・範囲の前の値の持ち込みが同じ段になる。"""

import pytest

from fprobe.recording import Recorder, Recording
from fprobe.timeline import recording_tile

HZ = 100_000_000
DEFS = {"groups": {"0": [{"name": "mark", "lsb": 0, "width": 1},
                         {"name": "idle", "lsb": 1, "width": 3, "format": "dec"},           # ずっと 0
                         {"name": "state", "lsb": 4, "width": 2, "format": "enum", "enum": {"0": "IDLE", "1": "RUN"}},
                         {"name": "count", "lsb": 8, "width": 8, "format": "dec"}]}}


@pytest.fixture
def recorded(tmp_path):
    rec = Recorder(tmp_path / "r", timebase_hz=HZ, meta={"fpga.signals": DEFS})
    rec.add("fpga", "group", 1000, a=0)
    for k, t in enumerate(range(1000, 20_000, 1000)):                       # 組 0: 1000 刻みで変わる
        rec.add("fpga", "value", t, a=0, b=(k << 8) | ((k % 2) << 4) | (k % 2))
    rec.add("fpga", "gap", 12_500, a=3)
    rec.add("fpga", "value", 5000, a=2, b=0xBEEF)                           # 定義の無い組 2
    rec.flush()
    return Recording.open(rec.directory)


def lane(tile, stream="fpga"):
    [i] = [k for k, ln in enumerate(tile.result["lanes"]) if ln["stream"] == stream]
    return i, tile.result["lanes"][i]


def rows_by_title(ln):
    return {r["title"]: r for r in ln["rows"]}


def test_signals_become_rows_with_names_and_enum_labels(recorded):
    tile = recording_tile(recorded, 0, 20_000)
    i, ln = lane(tile)
    rows = rows_by_title(ln)
    assert {"mark", "state", "count", "2"} <= set(rows) and {"gap", "group"} <= {r["rule"] for r in ln["rows"]}
    assert rows["mark"]["type"] == "state" and rows["state"]["type"] == "state" and rows["count"]["type"] == "values"
    assert {"IDLE", "RUN"} <= set(ln["labels"]) and "0xBEEF" in ln["labels"]       # 列挙の名前・定義の無い組は 16 進
    # count の段の値は信号のビットだけ（組の値の 15:8）
    row_index = [r["id"] for r in ln["rows"]].index(rows["count"]["id"])
    values = [v for r, v in zip(tile.sections[f"lane.{i}.row"], tile.sections[f"lane.{i}.value"]) if r == row_index]
    assert values[:4] == [0.0, 1.0, 2.0, 3.0]


def test_the_summary_has_the_same_rows_as_the_raw_lane(recorded):
    raw = {r["id"] for r in lane(recording_tile(recorded, 0, 20_000))[1]["rows"]}
    summary_tile = recording_tile(recorded, 0, 20_000, bin=64, max_events=5)
    i, ln = lane(summary_tile)
    assert ln["mode"] == "summary" and {r["id"] for r in ln["rows"]} == raw
    count_row = [r["id"] for r in ln["rows"]].index(rows_by_title(ln)["count"]["id"])
    bins = ln["bins"]
    highs = summary_tile.sections[f"lane.{i}.row_max"][count_row * bins:(count_row + 1) * bins]
    assert max(h for h in highs if h == h) == 18.0                          # 最後の count（k = 18）


def test_renaming_after_the_recording_changes_the_rows_of_the_past(recorded):
    recorded.update_meta(**{"fpga.signals": {"groups": {"0": [{"name": "pulse", "lsb": 0, "width": 1}]}}})
    titles = set(rows_by_title(lane(recording_tile(recorded, 0, 20_000))[1]))
    assert "pulse" in titles and "mark" not in titles and "state" not in titles


def test_values_before_the_range_are_carried_in(recorded):
    # 範囲の始め（15_500）の前の最後の値（15_000）から、状態の帯が続いて見える
    i, ln = lane(recording_tile(recorded, 15_500, 15_900))
    assert ln["items"] > 0 and {"RUN", "IDLE"} & set(ln["labels"])


@pytest.fixture
def switched(tmp_path):
    """組 0 を観測 → 5000 で組 1 に切り替え → 8000 で溢れの切れ目 → 組 1 の値。"""
    defs = {"groups": {"0": [{"name": "a", "lsb": 0, "width": 1}], "1": [{"name": "b", "lsb": 0, "width": 4, "format": "dec"}]}}
    rec = Recorder(tmp_path / "s", timebase_hz=HZ, meta={"fpga.signals": defs})
    rec.add("fpga", "group", 1000, a=0)
    rec.add("fpga", "value", 1000, a=0, b=1)
    rec.add("fpga", "group", 5000, a=1)
    rec.add("fpga", "value", 5000, a=1, b=3)
    rec.add("fpga", "gap", 8000, a=2)
    rec.add("fpga", "value", 8000, a=1, b=4)
    rec.flush()
    return Recording.open(rec.directory)


def labels_of(tile, title):
    i, ln = lane(tile)
    row = [r["id"] for r in ln["rows"]].index(rows_by_title(ln)[title]["id"])
    return [(t, ln["labels"][k] if k != 0xFFFF_FFFF else None)
            for r, t, k in zip(tile.sections[f"lane.{i}.row"], tile.sections[f"lane.{i}.t"],
                               tile.sections[f"lane.{i}.label"]) if r == row]


def test_switching_the_group_closes_the_rows_of_the_other_group(switched):
    # 観測していない組の帯を続けない（観測しているように見せない）: 切り替え・溢れで「—」で閉じる
    tile = recording_tile(switched, 0, 10_000)
    assert labels_of(tile, "a") == [(1000.0, "0x1"), (5000.0, "—")]
    assert labels_of(tile, "b") == [(5000.0, "3"), (8000.0, "—"), (8000.0, "4")]


def test_only_the_observed_group_is_carried_in(switched):
    titles = set(rows_by_title(lane(recording_tile(switched, 6000, 7000))[1]))
    assert "b" in titles and "a" not in titles


def test_summary_counts_are_the_changes_of_each_signal(recorded):
    # 要約の段の件数 = その信号が変わった数（組の値の数ではない）。生の段の出来事の数と同じ
    raw_tile = recording_tile(recorded, 0, 20_000)
    ri, raw = lane(raw_tile)
    raw_counts = {}
    for r, k in zip(raw_tile.sections[f"lane.{ri}.row"], raw_tile.sections[f"lane.{ri}.label"]):
        if k != 0xFFFF_FFFF and raw["labels"][k] == "—":                 # 閉じた印（不明）は値の出来事ではない
            continue
        rid = raw["rows"][r]["id"]
        raw_counts[rid] = raw_counts.get(rid, 0) + 1
    tile = recording_tile(recorded, 0, 20_000, bin=64, max_events=5)
    i, ln = lane(tile)
    bins = ln["bins"]
    counts = tile.sections[f"lane.{i}.row_counts"]
    summary = {r["id"]: sum(counts[k * bins:(k + 1) * bins]) for k, r in enumerate(ln["rows"])}
    assert summary == raw_counts
    assert summary[rows_by_title(ln)["idle"]["id"]] == 2                 # 始めと、溢れの切れ目（12500）の後の描き直し


def test_hidden_changes_between_snapshots_are_a_row_that_does_not_close_the_signals(tmp_path):
    # 標本型の「標本の間の見えない変化」（hidden、a = 回数）は失った記録（gap）と違い、信号の段を「—」で閉じない
    rec = Recorder(tmp_path / "r", timebase_hz=HZ, meta={"fpga.signals": DEFS})
    rec.add("fpga", "group", 1000, a=0)
    rec.add("fpga", "value", 1000, a=0, b=0x0010)
    rec.add("fpga", "hidden", 2000, a=3)
    rec.add("fpga", "value", 2000, a=0, b=0x0011)
    rec.flush()
    tile = recording_tile(Recording.open(rec.directory), 0, 5000)
    i, ln = lane(tile)
    assert "hidden" in {r["rule"] for r in ln["rows"]} and "—" not in ln["labels"]


def test_the_one_pass_summary_matches_the_sql_summary(tmp_path):
    # 要約の段の信号ごとの数・最小・最大を、出来事を 1 回だけ読んで全信号まとめて数える（信号ごとの窓関数の SQL は
    # 大きな記録で信号 1 つ 4 秒かかった）。溢れの切れ目・組の切り替え・別の組・数の信号・名前の無い組を混ぜて SQL と比べる
    import random

    from fprobe import timeline_fpga
    rng = random.Random(5)
    signals = {"groups": {"0": [{"name": "state", "lsb": 0, "width": 2, "format": "enum", "enum": {"1": "BUSY"}},
                                {"name": "left", "lsb": 8, "width": 4, "format": "dec"}],
                          "1": [{"name": "flag", "lsb": 0, "width": 1}]}}
    rec = Recorder(tmp_path / "r", timebase_hz=1e6, meta={"fpga.signals": signals})
    rows, t = [], 0
    for _ in range(3000):
        t += rng.randint(1, 30)
        r = rng.random()
        if r < 0.02:
            rows.append(("fpga", "gap", t, rng.randint(1, 5), 0))
        elif r < 0.04:
            rows.append(("fpga", "group", t, rng.choice([0, 1, 2]), 0))
        elif r < 0.06:
            rows.append(("fpga", "hidden", t, 1, 0))
        else:
            rows.append(("fpga", "value", t, rng.choice([0, 0, 1, 2]), rng.randrange(1 << 12)))
    rec.add_many(rows)
    rec.close()
    view = Recording.open(tmp_path / "r")
    for start, width, count in [(0, 1000, t // 1000 + 1), (5000, 333, 50), (0, 1, t + 1)]:
        end = start + width * count
        sql = [q for q in timeline_fpga.summary_sql(view, start, width, count) if "LAG" in q[0]]
        expected = view.query_events(start, end, sql)
        assert timeline_fpga.summary_rows(view, start, width, count, fast=False) == expected
        assert timeline_fpga.summary_rows(view, start, width, count, fast=True) == expected      # numpy でまとめて
