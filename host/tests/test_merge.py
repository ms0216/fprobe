from array import array

from fprobe import generated as g
from fprobe.la import Transitions
from fprobe.merge import merge
from fprobe.recording import Recorder, Recording

HZ = 100_000_000
RX, TX = g.SYNC_RX_LATENCY_TICKS + g.SYNC_RX_PHASE_SPAN_TICKS, g.SYNC_TX_LATENCY_TICKS
OFFSET = 5_000                                  # 子の時刻 = 親の時刻 + OFFSET（同じ速さ）


def make(directory, rows, waves=()):
    rec = Recorder(directory, timebase_hz=HZ)
    rec.add_many(rows)
    for tr, t0 in waves:
        rec.add_waveform(tr, t0)
    rec.close()
    return directory


def pair_rows(parents):
    """親の時刻 p に送った組を子が受けた行（子の時刻 = 線の辺 + RX）。"""
    return [("sync", "pair", p + TX + OFFSET + RX, p & 0xFFFF_FFFF, p >> 32, {"parent": p}) for p in parents]


def test_child_events_move_to_the_parent_time(tmp_path):
    parent = make(tmp_path / "p", [("dap", "ap_read", 1_500_000, 0x2000_0000, 1)])
    child = make(tmp_path / "c", pair_rows(range(1_000_000, 2_000_001, 100_000))
                 + [("uart", "rx", 1_234_567 + OFFSET, 0, 0x41)])
    got = merge(parent, child, tmp_path / "m", child_name="b")
    events = [(e.t, e.stream, e.kind, e.b) for e in Recording.open(tmp_path / "m").events(streams=["dap", "b.uart"])]
    assert events == [(1_234_567, "b.uart", "rx", 0x41), (1_500_000, "dap", "ap_read", 1)]
    assert got["pairs"] == 11 and got["segments"] == 1 and got["outside"] == 0
    assert got["uncertainty"] == {"quantization_ticks": 1, "residual_max_ticks": 0.0}
    (u,) = [e.uncertainty for e in Recording.open(tmp_path / "m").events(streams=["b.uart"])]
    assert u == 1.0                                                   # 量子化 1 + 残差の最大 0


def test_child_events_outside_pairs_are_counted(tmp_path):
    parent = make(tmp_path / "p", [])
    child = make(tmp_path / "c", pair_rows(range(1_000_000, 1_500_001, 100_000))
                 + [("uart", "rx", 10 + OFFSET, 0, 1), ("uart", "rx", 9_000_000, 0, 2)])
    got = merge(parent, child, tmp_path / "m", child_name="b")
    assert got["outside"] == 2
    assert Recording.open(tmp_path / "m").events(streams=["b.uart"]) == []


def test_child_waveforms_go_to_the_boards_recording(tmp_path):
    # 波形のかたまりにはボードの区別が無いので、子の波形は合成の記録の下のボードごとの記録 boards/<名前> に、
    # 親の時刻に換算して入れる（合成の記録そのものの波形は親のものだけ）
    tr = Transitions(array("H", [0, 1]), array("Q", [0, 50]), 100, 0, 0, 1 / HZ, 16)
    parent = make(tmp_path / "p", [], waves=[(tr, 1_300_000)])
    child = make(tmp_path / "c", pair_rows(range(1_000_000, 2_000_001, 100_000)),
                 waves=[(tr, 1_200_000 + OFFSET), (tr, 1_999_950 + OFFSET)])        # 2 つ目は終わりが組の外
    got = merge(parent, child, tmp_path / "m", child_name="b")
    assert (got["child_waveforms"], got["child_waveforms_outside"]) == (1, 1)
    merged = Recording.open(tmp_path / "m")
    assert [t0 for t0, _, _ in merged.waveforms()] == [1_300_000]
    assert merged.meta["merged"]["children"][0]["waveforms"] == "boards/b"
    ((t0, tps, wave),) = list(Recording.open(tmp_path / "m" / "boards" / "b").waveforms())
    assert t0 == 1_200_000 and abs(tps - 1.0) < 1e-9 and list(wave.starts) == [0, 50]


def test_events_carry_the_distance_to_the_nearest_pair(tmp_path):
    parent = make(tmp_path / "p", [])
    child = make(tmp_path / "c", pair_rows(range(1_000_000, 2_000_001, 100_000))
                 + [("uart", "rx", 1_230_000 + TX + OFFSET + RX, 0, 1), ("note", "x", 1_250_000 + TX + OFFSET + RX, 0, 2,
                                                                     {"text": "a"})])
    merge(parent, child, tmp_path / "m", child_name="b")
    got = [e.body for e in Recording.open(tmp_path / "m").events(streams=["b.uart", "b.note"])]
    assert got == [{"sync_nearest_pair_ticks": 30_000}, {"text": "a", "sync_nearest_pair_ticks": 50_000}]


def test_the_largest_gap_between_pairs_is_reported(tmp_path):
    # 線が抜けた等: 閾値で区間を分けない既定でも、組の間隔の最大とその位置（親の刻み）を事実として返す
    parent = make(tmp_path / "p", [])
    child = make(tmp_path / "c", pair_rows([1_000_000, 1_100_000, 1_200_000, 6_200_000, 6_300_000]))
    got = merge(parent, child, tmp_path / "m", child_name="b")
    assert got["segments"] == 1
    assert got["pair_gap_max_ticks"] == 5_000_000 and got["pair_gap_max_at"] == 1_200_000 + TX


def test_events_whose_end_leaves_the_pairs_are_outside(tmp_path):
    parent = make(tmp_path / "p", [])
    rec = Recorder(tmp_path / "c", timebase_hz=HZ)
    rec.add_many(pair_rows(range(1_000_000, 1_500_001, 100_000)))
    rec.add("span", "x", 1_400_000 + OFFSET, t_end=9_000_000)            # 終わりが組の範囲の外
    rec.close()
    got = merge(parent, tmp_path / "c", tmp_path / "m", child_name="b")
    assert got["outside"] == 1 and Recording.open(tmp_path / "m").events(streams=["b.span"]) == []


def test_cable_delay_is_explicit(tmp_path):
    parent = make(tmp_path / "p", [])
    child = make(tmp_path / "c", pair_rows(range(1_000_000, 2_000_001, 100_000)) + [("uart", "rx", 1_500_000, 0, 1)])
    merge(parent, child, tmp_path / "m", child_name="b")
    (info,) = Recording.open(tmp_path / "m").meta["merged"]["children"]
    assert info["cable_delay_ticks"] is None and info["cable_delay_included"] is False
    merge(parent, child, tmp_path / "m2", child_name="b", cable_delay_ticks=3)
    (e,) = Recording.open(tmp_path / "m2").events(streams=["b.uart"])
    assert e.t == 1_500_000 - OFFSET + 3
    assert Recording.open(tmp_path / "m2").meta["merged"]["children"][0]["cable_delay_included"] is True


def test_the_cli_merges_and_prints_the_counts(tmp_path, capsys):
    import json

    from fprobe.__main__ import main
    parent = make(tmp_path / "p", [])
    child = make(tmp_path / "c", pair_rows(range(1_000_000, 2_000_001, 100_000)) + [("uart", "rx", 1_500_000, 0, 1)])
    try:
        main(["merge", str(parent), str(child), "--child-name", "b", "--out", str(tmp_path / "m"), "--cable-delay-ns", "30"])
    except SystemExit as e:
        assert e.code == 0
    assert json.loads(capsys.readouterr().out)["converted"] == 12
    (e,) = Recording.open(tmp_path / "m").events(streams=["b.uart"])
    assert e.t == 1_500_000 - OFFSET + 3                                  # 30 ns = 3 刻み


def test_python_dash_m_runs_merge(tmp_path):
    # python -m fprobe merge（モジュールを上から実行する経路。関数の定義の順に依る）
    import subprocess
    import sys
    parent = make(tmp_path / "p", [])
    child = make(tmp_path / "c", pair_rows(range(1_000_000, 2_000_001, 100_000)))
    done = subprocess.run([sys.executable, "-m", "fprobe", "merge", str(parent), str(child), "--child-name", "b",
                           "--out", str(tmp_path / "m")], capture_output=True, text=True, encoding="utf-8")
    assert done.returncode == 0, done.stderr


def test_gap_ms_is_counted_in_the_child_ticks(tmp_path, capsys):
    # 区間を分けるのは子の時刻の間隔: --gap-ms は子の周波数で刻みに直す（親と子で周波数が違う。Primer と PYNQ）
    import json

    from fprobe.__main__ import main
    parent = make(tmp_path / "p", [])
    rec = Recorder(tmp_path / "c", timebase_hz=2 * HZ)                    # 子は 200 MHz
    rec.add_many(pair_rows(range(1_000_000, 50_000_001, 10_000_000)))    # 組の間隔は子の 1000 万刻み = 50 ms
    rec.close()
    try:
        main(["merge", str(parent), str(tmp_path / "c"), "--child-name", "b", "--out", str(tmp_path / "m"),
              "--gap-ms", "75"])
    except SystemExit as e:
        assert e.code == 0
    assert json.loads(capsys.readouterr().out)["segments"] == 1


def test_the_child_sync_counts_are_reported(tmp_path):
    parent = make(tmp_path / "p", [])
    rec = Recorder(tmp_path / "c", timebase_hz=HZ, meta={"sync": {"pairs_mod_65536": 11, "crc_errors": 2}})
    rec.add_many(pair_rows(range(1_000_000, 2_000_001, 100_000)))
    rec.close()
    assert merge(parent, tmp_path / "c", tmp_path / "m", child_name="b")["child_sync"] == {
        "pairs_mod_65536": 11, "crc_errors": 2}
    assert merge(parent, make(tmp_path / "c2", []), tmp_path / "m2", child_name="b")["child_sync"] is None
