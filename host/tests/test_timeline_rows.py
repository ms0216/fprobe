"""fprobe.timeline_rows: 記録の出来事を共通タイムラインの段に分ける規則（生データの Python の関数と、要約の SQL が
同じ段に分ける）と、タイル（format 2）の段の区画。デモの記録（tests/demo_recording.py）はすべての段の型を持つ。"""

import math
from collections import Counter

import pytest

from demo_recording import HZ, SPAN, write_demo_recording
from fprobe.recording import Event, Recorder, Recording
from fprobe.recording_long import Policy, SegmentedRecorder
from fprobe.timeline import TILE_FORMAT, recording_tile
from fprobe.timeline_rows import NO_LABEL, ROW_TYPES, TONES, classify, row_id, rule_for, summarize, summary_sql


@pytest.fixture(scope="module")
def demo(tmp_path_factory):
    directory = tmp_path_factory.mktemp("demo") / "r"
    write_demo_recording(directory)
    view = Recording(directory)
    yield view
    view.close()


def test_the_demo_shows_every_row_type(demo):
    tile = recording_tile(demo, 0, SPAN)
    types = Counter(r["type"] for lane in tile.result["lanes"] for r in lane["rows"])
    assert set(types) == set(ROW_TYPES)
    rows = {r["id"]: r for lane in tile.result["lanes"] for r in lane["rows"]}
    assert {rows[f"rtos.task:0x{h:08X}"]["title"] for h in (0x20002000, 0x20002060)} == {"main", "sensor"}
    assert rows["rtos.isr:0x0000000F"]["title"] == "SysTick"
    assert rows["aux.voltage"]["unit"] == "V" and rows["sample.word:0x20000010"]["jump"] == "memory"
    assert rows["rtos.switch"]["hidden"] is True
    assert {"annotation.decode:uart", "annotation.decode:i2c", "host.call:ai", "host.call:human",
            "target.state"} <= set(rows)
    assert tile.result["wave"]["names"][0] == "UART_TX"


def test_raw_lane_sections_carry_rows_labels_values_depths_tones_and_addresses(demo):
    tile = recording_tile(demo, 0, SPAN)
    header, data = tile.result, tile.sections
    assert header["format"] == TILE_FORMAT == 2
    i = [lane["stream"] for lane in header["lanes"]].index("rtos")
    lane = header["lanes"][i]
    n = lane["items"]
    for name in ("row", "label", "value", "depth", "tone", "address"):
        assert len(data[f"lane.{i}.{name}"]) == n, name
    ids = [r["id"] for r in lane["rows"]]
    events = list(demo.iter_events(0, SPAN, ["rtos"], overlapping=True))
    for k in range(0, n, 97):                                 # 区画は出来事の並び（t の昇順）のまま
        e = events[k]
        rule = rule_for("rtos", e)
        assert ids[data[f"lane.{i}.row"][k]] == row_id(rule, rule.key(e) if rule.key else None)
    # 割り込みの入れ子: SysTick が USART2 の中に入るところは深さ 1
    isr = [k for k in range(n) if events[k].kind == "isr"]
    depths = [data[f"lane.{i}.depth"][k] for k in isr]
    assert max(depths) >= 1 and depths.count(0) > len(depths) // 2
    for k in isr:
        if data[f"lane.{i}.depth"][k]:
            inner = events[k]
            assert any(o.t <= inner.t and inner.t_end <= o.t_end for o in (events[j] for j in isr if j != k))
    labels = lane["labels"]
    assert {labels[data[f"lane.{i}.label"][k]] for k in isr} >= {"SysTick", "USART2"}
    assert data[f"lane.{i}.tone"][[e.kind for e in events].index("inversion")] == TONES["bad"]


def test_uart_bytes_are_labelled_and_values_and_jumps_come_through(demo):
    tile = recording_tile(demo, 0, SPAN, streams=["uart", "sample", "target", "aux"])
    header, data = tile.result, tile.sections
    lanes = {lane["stream"]: (i, lane) for i, lane in enumerate(header["lanes"])}
    i, lane = lanes["uart"]
    events = demo.events(0, SPAN, ["uart"])
    text = "".join(lane["labels"][data[f"lane.{i}.label"][k]] for k, e in enumerate(events) if e.kind == "tx")
    assert text.startswith("[␣␣0.1]␣tick␣counter=") and "\\r\\n" in text
    i, lane = lanes["sample"]
    first = demo.events(0, SPAN, ["sample"], limit=1)[0]
    assert data[f"lane.{i}.value"][0] == first.b and data[f"lane.{i}.address"][0] == first.a
    i, lane = lanes["aux"]
    assert 3.2 < data[f"lane.{i}.value"][0] < 3.4                       # mV → V
    i, lane = lanes["target"]
    states = [lane["labels"][data[f"lane.{i}.label"][k]] for k in range(lane["items"])]
    assert states[:2] == ["RUNNING", "HALTED breakpoint"]
    assert math.isnan(data[f"lane.{i}.address"][0]) and data[f"lane.{i}.address"][1] == 0x08000188
    assert data[f"lane.{i}.tone"][1] == TONES["warn"]


@pytest.mark.parametrize("stream", ["rtos", "uart", "annotation", "sample", "user", "host", "dap", "aux", "target"])
def test_the_summary_sql_splits_rows_like_the_python_rules(demo, stream):
    """要約（段 × 区間の件数・最小・最大）= 生の出来事を Python の規則で分けて数えたもの。"""
    start, width, count = 0, 1 << 15, -(-SPAN // (1 << 15))
    s = summarize(stream, demo.query_events(start, start + width * count, summary_sql(stream, start, width, count)),
                  count)
    events = demo.events(start, start + width * count, [stream])
    c = classify(stream, events)
    assert [r.id for r in s.table.rows] == [r.id for r in c.table.rows]
    assert [r.title for r in s.table.rows] == [r.title for r in c.table.rows]
    expected = [0] * len(s.counts)
    low, high = {}, {}
    for e, r, v in zip(events, c.row, c.value):
        b = (e.t - start) // width
        expected[r * count + b] += 1
        if not math.isnan(v):
            low[r * count + b] = min(low.get(r * count + b, v), v)
            high[r * count + b] = max(high.get(r * count + b, v), v)
    assert s.counts == expected
    if s.low is not None:
        for k, v in low.items():
            assert s.low[k] == pytest.approx(v) and s.high[k] == pytest.approx(high[k])


def test_summary_lanes_carry_row_counts(demo):
    tile = recording_tile(demo, 0, SPAN, streams=["rtos", "aux"], bin=1 << 14, max_events=10)
    header, data = tile.result, tile.sections
    for i, lane in enumerate(header["lanes"]):
        assert lane["mode"] == "summary"
        rows, bins = len(lane["rows"]), lane["bins"]
        counts = data[f"lane.{i}.row_counts"]
        assert len(counts) == rows * bins
        total = [sum(counts[r * bins + b] for r in range(rows)) for b in range(bins)]
        assert total == list(data[f"lane.{i}.counts"])
    aux = header["lanes"][1]
    assert aux["stream"] == "aux" and f"lane.1.row_min" in data
    assert min(v for v in data["lane.1.row_min"] if not math.isnan(v)) < 3.0          # 途中で電圧が下がる


def test_unknown_streams_get_a_points_row_per_kind(tmp_path):
    rec = Recorder(tmp_path / "r", HZ)
    for t, kind in ((10, "alpha"), (20, "beta"), (30, "alpha")):
        rec.add("custom", kind, t)
    rec.close()
    view = Recording(tmp_path / "r")
    try:
        tile = recording_tile(view, 0, 100)
        lane = tile.result["lanes"][0]
        assert [(r["id"], r["type"], r["title"]) for r in lane["rows"]] == [
            ("custom.kind:alpha", "points", "alpha"), ("custom.kind:beta", "points", "beta")]
        assert list(tile.sections["lane.0.row"]) == [0, 1, 0]
        assert list(tile.sections["lane.0.label"]) == [0, 1, 0] and lane["labels"] == ["alpha", "beta"]
    finally:
        view.close()


def test_events_without_a_label_or_value_say_so():
    c = classify("clock", [Event(5, "clock", "point")])
    assert c.label == [NO_LABEL] and math.isnan(c.value[0]) and c.depth == [0]


def test_state_and_value_rows_carry_the_last_event_before_the_range(demo):
    """拡大したタイル: 状態の段は範囲の始めの状態、値の段は保たれている値を描けるよう、t0 の前の最後の出来事を持つ。"""
    t0, t1 = int(1.0 * HZ), int(1.0 * HZ) + 2000
    tile = recording_tile(demo, t0, t1, streams=["target", "aux", "sample"])
    header, data = tile.result, tile.sections
    for i, lane in enumerate(header["lanes"]):
        t = list(data[f"lane.{i}.t"])
        assert t and t[0] < t0, lane["stream"]
        rows = {lane["rows"][r]["id"] for r in data[f"lane.{i}.row"]}
        assert rows == {r["id"] for r in lane["rows"]}
    target = header["lanes"][0]
    assert target["labels"][data["lane.0.label"][0]] == "RUNNING"            # 0.86 s から走っている
    assert target["count"] == 0                                                # 範囲に始まる出来事は無い


def test_segmented_recordings_summarize_rows_from_their_segments(tmp_path):
    rec = SegmentedRecorder(tmp_path / "long", HZ, policy=Policy(segment_s=0.001))
    for k in range(3000):
        rec.add("rtos", "run", k * 1000, a=0x100 + k % 3, body={"task": f"t{k % 3}"}, t_end=k * 1000 + 500)
        if k % 500 == 0:
            rec.flush()
    rec.close()
    view = Recording.open(tmp_path / "long")
    try:
        assert type(view).__name__ == "SegmentedRecording"
        tile = recording_tile(view, 0, 3_000_000, streams=["rtos"], bin=1 << 16, max_events=10)
        lane, data = tile.result["lanes"][0], tile.sections
        assert [r["title"] for r in lane["rows"]] == ["t0", "t1", "t2"]
        assert sum(data["lane.0.row_counts"]) == 3000
    finally:
        view.close()
