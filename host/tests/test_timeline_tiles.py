"""fprobe.timeline: 共通タイムラインのタイル（生データ・要約）が、今の読み（la.Waveform.view・Recording.waveform_view・
Recording.density）と同じ結果に間引けること。間引きは参照の実装（timeline_ref。画面の TS 版と同じ定義）で確かめる。"""

import random
from array import array

import pytest

from fprobe.la import Transitions, Waveform
from fprobe.recording import Recorder, Recording
from fprobe.recording_long import Policy, SegmentedRecorder
from fprobe.timeline import MAX_BINS, capture_tile, recording_tile
from timeline_ref import lane_counts, wave_states

HZ = 1_000_000


def _transitions(rng: random.Random, length: int, channels: int, gap: int = 60, period_s: float = 40e-9) -> Transitions:
    values, starts, v, i = array("H"), array("Q"), rng.randrange(1 << channels), 0
    while i < length:
        values.append(v)
        starts.append(i)
        i += rng.randrange(1, gap)
        v ^= 1 << rng.randrange(channels)
    return Transitions(values, starts, length, length // 4, 0, period_s, channels)


def _info(tr: Transitions) -> dict:
    return {"id": 1, "length": tr.length, "sample_period_ns": tr.sample_period_s * 1e9, "trigger_index": tr.trigger,
            "channels": tr.channels, "source": "la.capture"}


def _views(rng: random.Random, lo: int, hi: int, n: int) -> list[tuple[int, int, int]]:
    out = [(lo, hi, 300), (lo, lo + 7, 50), (hi - 1, hi, 3)]
    for _ in range(n):
        a = rng.randrange(lo, hi - 1)
        b = rng.randrange(a + 1, min(hi, a + rng.choice([10, 1000, hi])) + 1)
        out.append((a, b, rng.choice([1, 17, 300, 1500])))
    return out


def test_raw_capture_tile_decimates_to_waveform_view():
    rng = random.Random(1)
    tr = _transitions(rng, 30_000, 5)
    wave = Waveform(tr)
    tile = capture_tile(wave, _info(tr), 0)
    header = tile.result
    assert header["wave"]["mode"] == "raw" and header["extent"] == [0, 30_000] and header["source"] == "capture"
    assert header["tick_s"] == pytest.approx(40e-9)
    for v0, v1, width in _views(rng, 0, 30_000, 40):
        assert wave_states(header, tile.sections, v0, v1, width) == wave.view(v0, v1, width), (v0, v1, width)


def test_a_sub_range_tile_holds_only_its_edges_and_the_level_at_its_start():
    rng = random.Random(2)
    tr = _transitions(rng, 30_000, 4)
    wave = Waveform(tr)
    tile = capture_tile(wave, _info(tr), 12_345, 20_000, channels=[1, 3])
    header, data = tile.result, tile.sections
    assert header["wave"]["channels"] == [1, 3] and set(data) == {"wave.0.1", "wave.0.3"}
    assert all(12_345 < t < 20_000 for t in data["wave.0.1"])
    for v0, v1, width in _views(rng, 12_345, 20_000, 30):
        expected = wave.view(v0, v1, width)
        assert wave_states(header, data, v0, v1, width) == [expected[1], expected[3]], (v0, v1, width)


def test_capture_over_the_edge_budget_returns_aligned_summary_bins():
    rng = random.Random(3)
    tr = _transitions(rng, 100_003, 3, gap=8)
    wave = Waveform(tr)
    tile = capture_tile(wave, _info(tr), 1000, None, bin=100, max_edges=1000)
    w = tile.result["wave"]
    assert w["mode"] == "summary" and w["bin"] == 64 and w["start"] == 960 and w["start"] % w["bin"] == 0
    assert w["start"] + w["bin"] * w["bins"] >= 100_003 > w["start"] + w["bin"] * (w["bins"] - 1)
    states = tile.sections["wave.states"]
    assert len(states) == 3 * w["bins"]
    last = w["start"] + w["bin"] * (w["bins"] - 1)
    for ch in range(3):
        row = bytes(states[ch * w["bins"]:(ch + 1) * w["bins"]]).translate(bytes.maketrans(bytes([0, 1, 2, 3]), b"01x-"))
        assert row.decode()[:-1] == wave.view(w["start"], last, w["bins"] - 1)[ch]
        assert row.decode()[-1] == wave.view(last, 100_003, 1)[ch]          # 最後の区間は捕捉の終わりまで


def test_summary_bins_are_capped():
    tr = _transitions(random.Random(4), 1_000_000, 1, gap=4)
    tile = capture_tile(Waveform(tr), _info(tr), 0, bin=1, max_edges=10)
    assert tile.result["wave"]["bins"] <= MAX_BINS


def test_tile_rejects_empty_ranges_and_unknown_channels():
    tr = _transitions(random.Random(5), 1000, 2)
    with pytest.raises(ValueError):
        capture_tile(Waveform(tr), _info(tr), 500, 500)
    with pytest.raises(ValueError):
        capture_tile(Waveform(tr), _info(tr), 0, channels=[2])


@pytest.fixture
def recorded(tmp_path):
    """2 つの波形のかたまり（間に隙間、1 つは 1 サンプル = 2 タイムベース）と 3 段の出来事。"""
    rng = random.Random(6)
    rec = Recorder(tmp_path / "r", HZ)
    rec.add_waveform(_transitions(rng, 5000, 3, period_s=1 / HZ), 1000)
    rec.add_waveform(_transitions(rng, 4000, 3, period_s=2 / HZ), 9000)
    for i in range(3000):
        t = rng.randrange(0, 20_000)
        rec.add(rng.choice(["dap", "uart", "rtt"]), rng.choice(["rx", "tx"]), t, t_end=t + rng.randrange(0, 50))
    rec.close()
    view = Recording(tmp_path / "r")
    yield view
    view.close()


def test_raw_recording_tile_decimates_to_waveform_view_and_density(recorded):
    tile = recording_tile(recorded, 0, 20_000)
    header, data = tile.result, tile.sections
    assert header["wave"]["mode"] == "raw" and len(header["wave"]["pieces"]) == 2
    assert [lane["stream"] for lane in header["lanes"]] == ["dap", "rtt", "uart"]
    assert all(lane["mode"] == "raw" for lane in header["lanes"])
    rng = random.Random(7)
    for v0, v1, width in _views(rng, 0, 20_000, 30):
        assert wave_states(header, data, v0, v1, width) == recorded.waveform_view(v0, v1, width), (v0, v1, width)
        for i, lane in enumerate(header["lanes"]):
            assert lane_counts(lane, data, i, v0, v1, width) == recorded.density(v0, v1, width, [lane["stream"]])


def test_raw_events_include_those_overlapping_the_start(recorded):
    tile = recording_tile(recorded, 5000, 6000, streams=["uart"])
    lane, data = tile.result["lanes"][0], tile.sections
    t, t_end, kind = data["lane.0.t"], data["lane.0.t_end"], data["lane.0.kind"]
    expected = recorded.events(5000, 6000, ["uart"], limit=None)
    assert lane["count"] == len(expected)
    assert len(t) >= len(expected) and all(a < 5000 <= b for a, b in zip(t, t_end) if a < 5000)
    assert [lane["kinds"][k] for k in kind[len(t) - len(expected):]] == [e.kind for e in expected]


def test_recording_over_the_budgets_returns_summaries_equal_to_density(recorded):
    tile = recording_tile(recorded, 0, 20_000, bin=64, max_edges=10, max_events=10)
    header, data = tile.result, tile.sections
    w = header["wave"]
    assert w["mode"] == "summary" and w["bin"] == 64
    states = data["wave.states"]
    rows = recorded.waveform_view(w["start"], w["start"] + w["bin"] * w["bins"], w["bins"])
    for ch in range(3):
        row = bytes(states[ch * w["bins"]:(ch + 1) * w["bins"]]).translate(bytes.maketrans(bytes([0, 1, 2, 3]), b"01x-"))
        assert row.decode() == rows[ch]
    for i, lane in enumerate(header["lanes"]):
        assert lane["mode"] == "summary" and lane["bin"] == 64
        expected = recorded.density(lane["start"], lane["start"] + 64 * lane["bins"], lane["bins"], [lane["stream"]])
        assert list(data[f"lane.{i}.counts"]) == expected


def test_probe_serves_tiles_of_the_last_capture_and_of_the_open_recording(recorded):
    from fprobe.api.binary import BinaryResult
    from fprobe.mcp_server import Probe
    probe = Probe("unused")                                             # 機器は開かない（タイルは読みだけ）
    with pytest.raises(RuntimeError):
        probe.timeline_tiles()                                          # 捕捉が無い
    tr = _transitions(random.Random(9), 4000, 3)
    probe._keep_capture(tr, "la.capture")
    tile = probe.timeline_tiles(t0=100, t1=900)
    assert isinstance(tile, BinaryResult) and tile.result["capture"]["length"] == 4000
    assert wave_states(tile.result, tile.sections, 100, 900, 77) == probe.la_view(100, 900, 77)["levels"]
    with pytest.raises(ValueError):
        probe.timeline_tiles(streams=["uart"])                          # 捕捉に出来事の段は無い
    probe._view = recorded
    tile = probe.timeline_tiles(source="recording")                     # t1 を省くと記録の終わりまで
    assert tile.result["t1"] == recorded.span()[1] + 1 and len(tile.result["lanes"]) == 3


def test_the_ui_decimation_fixture_is_current():
    # 画面の間引きの試験（host/ui/tests/decimate.test.ts）が読む固定データは、いまのタイルと Python の結果と同じ
    import ui_fixture
    for name, data in ui_fixture.build().items():
        path = ui_fixture.OUT / name
        assert path.exists() and path.read_bytes() == data, f"{path} が古い（python host/tests/ui_fixture.py）"


def test_segmented_event_bins_come_from_the_precomputed_summary(tmp_path):
    class Clock:
        now = 1e6

        def __call__(self):
            return self.now
    clock = Clock()
    rng = random.Random(8)
    rec = SegmentedRecorder(tmp_path / "s", HZ, policy=Policy(segment_s=10), clock=clock)
    for hour in range(4):
        for _ in range(500):
            rec.add(rng.choice(["dap", "uart"]), "k", hour * 50_000_000 + rng.randrange(50_000_000))
        rec.flush()
        clock.now += 11
    rec.add("uart", "k", 4 * 50_000_000 + 5)                                 # 開いている分割
    rec.flush()
    view = Recording.open(tmp_path / "s")
    for level in (0, 2, 3, 4):
        width = view.grid.width(level)
        start = (12_345_678 // width) * width
        count = min(64, -(-(4 * 50_000_000 + 10 - start) // width))
        bins = view.event_bins(start, width, count)
        for stream in ("dap", "uart"):
            assert list(bins.get(stream, [0] * count)) == view.density(start, start + width * count, count, [stream])
    odd = view.event_bins(1000, 3000, 10)                                    # 段にそろわない幅: 生で数える
    assert list(odd["dap"]) == view.density(1000, 31_000, 10, ["dap"])
    tile = recording_tile(view, 0, 4 * 50_000_000, bin=view.grid.width(3), max_events=10)
    lane = tile.result["lanes"][0]
    assert lane["mode"] == "summary" and lane["bin"] == view.grid.width(3)
    rec.close()
    view.close()


def _board_recording(tmp_path):
    """合成した記録（merge の形）: 親の波形（2 ch）と、boards/b の子の波形（2 ch、親の時刻に換算済み）。"""
    parent = Recorder(tmp_path / "m", timebase_hz=HZ, meta={"merged": {"children": [{"name": "b", "waveforms": "boards/b"}]}})
    parent.add_waveform(Transitions(array("H", [0, 1, 3]), array("Q", [0, 10, 20]), 100, 0, 0, 1 / HZ, 2), 1000)
    parent.close()
    child = Recorder(tmp_path / "m" / "boards" / "b", timebase_hz=HZ)
    child.add_waveform(Transitions(array("H", [2, 0]), array("Q", [0, 50]), 100, 0, 0, 1 / HZ, 2), 1005)
    child.close()
    return Recording.open(tmp_path / "m")


def test_child_board_waveforms_follow_the_parent_channels_with_names(tmp_path):
    # 子のボードの波形（R25）: 親のチャンネルの後ろに続く番号で、名前は「ボード名.ch番号」
    tile = recording_tile(_board_recording(tmp_path), 1000, 1100)
    wave = tile.result["wave"]
    assert wave["mode"] == "raw" and wave["channels"] == [0, 1, 2, 3]
    assert wave["names"] == ["ch0", "ch1", "b.ch0", "b.ch1"]
    child = [p for p in wave["pieces"] if p["channels"] == [2, 3]]
    assert len(child) == 1 and child[0]["t0"] == 1005 and child[0]["levels"] == [0, 1]     # 子の値 2 = ch1 が 1
    index = wave["pieces"].index(child[0])
    assert list(tile.sections[f"wave.{index}.3"]) == [50.0]                                # 子の ch1 が 50 で 0 に


def test_child_board_waveforms_in_the_summary(tmp_path):
    tile = recording_tile(_board_recording(tmp_path), 1000, 1100, max_edges=0)
    wave = tile.result["wave"]
    assert wave["mode"] == "summary" and wave["channels"] == [0, 1, 2, 3]
    bins = wave["bins"]
    assert len(tile.sections["wave.states"]) == 4 * bins                                  # 4 本ぶんの状態の並び


def test_only_requested_board_channels(tmp_path):
    tile = recording_tile(_board_recording(tmp_path), 1000, 1100, channels=[3])
    assert tile.result["wave"]["channels"] == [3] and tile.result["wave"]["names"] == ["b.ch1"]
