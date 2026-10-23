"""結果の直前に多い出来事（fprobe.correlate）: 記録の変数（MCU のメモリ・FPGA の信号・ピン）の条件と、結果の直前に候補が来る割合。
仕様: docs/superpowers/specs/2026-10-21-system-analysis-design.md §3。機器は使わない（記録の書き手で作る）。"""

from array import array

import pytest

from fprobe.correlate import ConditionError, correlate, parse_condition
from fprobe.la import Transitions
from fprobe.recording import Recorder, Recording

ERRORS = 0x20040808
SIGNALS = {"groups": {"0": [
    {"name": "state", "lsb": 0, "width": 2, "format": "enum", "enum": {"0": "IDLE", "1": "BUSY", "2": "DONE"}},
    {"name": "restart", "lsb": 3, "width": 1}]}}


def make(tmp_path, signals=SIGNALS, drop_reads=range(0), extra=(), pins_from=0):
    """1 周期 10 ms（タイムベース 1 MHz = 1 µs）を 10 周期。偶数の周期: BUSY の間に restart が変わり、2 ms 後に errors が
    増える。奇数の周期: IDLE で restart が変わり、errors は増えない。ピン ch2（mark）は各周期の始めに 0 → 1。
    MCU のメモリは 1 ms ごとに読む（DAP サンプラ）。"""
    rec = Recorder(tmp_path / "r", timebase_hz=1e6,
                   meta={"fpga.signals": signals, "sample.names": {f"0x{ERRORS:08X}": "errors"},
                         "pins": ["ch0", "ch1", "mark"]})
    errors, restart = 0, 0
    rows = []
    for k in range(10):
        base = k * 10_000
        busy = k % 2 == 0
        state = 1 if busy else 0
        rows.append(("fpga", "value", base + 50, 0, state | restart << 3))
        restart ^= 1
        rows.append(("fpga", "value", base + 100, 0, state | restart << 3))
        rows.append(("fpga", "value", base + 500, 0, 2 | restart << 3))
        for ms in range(10):
            t = base + ms * 1000
            if busy and ms == 2:
                errors += 1
            if t not in drop_reads:
                rows.append(("sample", "word", t, ERRORS, errors))
    rec.add_many(rows + list(extra))
    values, starts = array("H"), array("Q")
    values.append(0)
    starts.append(0)
    for k in range(10):                                     # 1 サンプル = 1 µs: 各周期の 10〜999 µs だけ mark = 1
        values.extend([0b100, 0])
        starts.extend([k * 10_000 + 10, k * 10_000 + 1000])
    rec.add_waveform(Transitions(values, starts, 100_000, 0, 0, 1e-6, 3), start_ticks=pins_from)
    rec.close()
    return Recording.open(tmp_path / "r")


def test_conditions_are_terms_joined_by_and():
    c = parse_condition("restart changed and state=BUSY")
    assert [(t.name, t.op, t.value) for t in c.terms] == [("restart", "changed", None), ("state", "=", "BUSY")]
    assert [t.op for t in parse_condition("mark rose かつ errors fell").terms] == ["rose", "fell"]
    with pytest.raises(ConditionError):
        parse_condition("state=BUSY")                       # 瞬間の項が無い
    with pytest.raises(ConditionError):
        parse_condition("state >= 2")


def test_causes_are_sorted_by_the_share_of_effects_they_precede(tmp_path):
    # 並びは事実だけで: 先行率の大きい順、同じなら条件の文字の順（経験則の順位づけをしない）。coverage = 記録のうち
    # 候補の後 window 以内に当たる時間の割合（仮定なしにデータから測る。先行率との比べ方は使う側が決める）
    rec = make(tmp_path)
    out = correlate(rec, "errors changed", ["restart changed and state=IDLE", "restart changed and state=BUSY",
                                            "mark rose"], window_s=0.005)
    assert out["effect"]["count"] == 5
    mark, busy, idle = out["causes"]
    assert busy["condition"] == "restart changed and state=BUSY"
    assert (busy["count"], busy["preceded"], busy["followed"]) == (5, 1.0, 1.0)
    assert busy["delay_s"]["median"] == pytest.approx(0.0019)
    assert busy["examples"][0] == {"effect_t": 2000, "cause_t": 100}
    assert busy["coverage"] == pytest.approx(5 * 5000 / 99_001, rel=0.01)       # 5 回 × 5 ms / 記録の長さ
    assert mark["coverage"] == pytest.approx(10 * 5000 / 99_001, rel=0.01)
    assert "lift" not in busy and "baseline" not in busy
    assert mark["count"] == 10 and mark["preceded"] == 1.0 and mark["followed"] == 0.5
    assert idle["preceded"] == 0.0 and idle["delay_s"] is None
    assert out["sampling"]["errors"] == pytest.approx(0.001)
    assert out["timebase_hz"] == 1e6


def test_names_shared_by_two_kinds_need_a_prefix(tmp_path):
    signals = {"groups": {"0": [*SIGNALS["groups"]["0"], {"name": "errors", "lsb": 8, "width": 4}]}}
    rec = make(tmp_path, signals)
    with pytest.raises(ConditionError, match="mcu.errors"):
        correlate(rec, "errors changed", ["mark rose"], window_s=0.01)
    assert correlate(rec, "mcu.errors changed", ["pin.mark rose"], window_s=0.01)["effect"]["count"] == 5
    with pytest.raises(ConditionError, match="nothing"):
        correlate(rec, "nothing changed", ["mark rose"], window_s=0.01)


def test_addresses_and_channel_numbers_work_without_names(tmp_path):
    rec = make(tmp_path)
    out = correlate(rec, "0x20040808 changed", ["ch2 rose"], window_s=0.005)
    assert out["effect"]["count"] == 5 and out["causes"][0]["preceded"] == 1.0


def test_the_probe_tool_reads_the_open_recording(tmp_path, monkeypatch):
    from fprobe.mcp_server import Probe
    monkeypatch.setenv("FPROBE_HOME", str(tmp_path))
    make(tmp_path / "recordings").close()                     # tmp/recordings/r
    p = Probe("unused")
    p.recording_open("r")
    out = p.recording_correlate("errors changed", ["restart changed and state=BUSY"], window_s=0.005)
    assert out["causes"][0]["preceded"] == 1.0


def test_a_new_recording_keeps_the_pin_names(tmp_path, monkeypatch):
    # 後から開いた記録でもピンを名前で指せる（correlate・時間軸の段の名前）
    from types import SimpleNamespace

    from probe_fakes import FakeDevice

    from fprobe.mcp_server import Probe
    monkeypatch.setenv("FPROBE_HOME", str(tmp_path))
    p = Probe("unused")
    p._session = SimpleNamespace(device=FakeDevice())
    monkeypatch.setattr("fprobe.mcp_server.channel_names", lambda caps: {8: "SWDIO drive", 10: "in0"})
    started = p.recording_start(name="pins")
    p.recording_stop()
    import json
    from pathlib import Path
    names = json.loads((Path(started["path"]) / "meta.json").read_text(encoding="utf-8"))["pins"]
    assert names[8] == "SWDIO drive" and names[10] == "in0" and names[0] == "ch0"


def test_names_with_spaces_or_equals_are_quoted():
    # 利用者の付けた名前（"Error count" 等）も条件で指せる: "…" で囲む
    c = parse_condition('"Error count" changed and "a=b"=3')
    assert [(t.name, t.op, t.value) for t in c.terms] == [("Error count", "changed", None), ("a=b", "=", "3")]


def test_quoted_names_resolve_in_a_recording(tmp_path):
    rec = make(tmp_path)
    rec.meta["sample.names"] = {f"0x{ERRORS:08X}": "error count"}
    assert correlate(rec, '"error count" changed', ["ch2 rose"], window_s=0.01)["effect"]["count"] == 5


def test_durations_per_value(tmp_path):
    # 値ごとの「続いた時間」: 偶数の周期は BUSY が 50〜500 µs（450 µs）、奇数の周期は IDLE が 50〜500 µs。DONE は残り。
    # 続いた時間は両端の見えている区間だけ（最初の BUSY はいつからか分からない・最後の DONE は続いている）
    from fprobe.correlate import durations
    rec = make(tmp_path)
    out = durations(rec, "state")
    busy = out["values"]["BUSY"]
    assert busy["count"] == 4 and busy["duration_s"]["median"] == pytest.approx(450e-6)
    assert out["values"]["DONE"]["count"] == 9 and out["values"]["DONE"]["duration_s"]["median"] == pytest.approx(9550e-6)
    assert sum(v["share"] for v in out["values"].values()) == pytest.approx(1.0)
    pin = durations(rec, "mark")["values"]
    assert pin["1"]["duration_s"]["max"] == pytest.approx(990e-6)        # 10〜999 µs


def test_candidates_are_found_automatically_when_none_are_given(tmp_path):
    # 結果だけを渡すと、記録の変数から候補（信号の変化・列挙の値に入った瞬間・変化 かつ 列挙の信号 = 値・ピンの
    # 立ち上がり / 立ち下がり・ほかの MCU のメモリの変化）を作り、偶然との比の大きい順に返す。結果そのものは除く
    rec = make(tmp_path)
    out = correlate(rec, "errors changed", None, window_s=0.005)
    conds = [c["condition"] for c in out["causes"]]
    assert out["auto"] is True and "errors changed" not in conds
    top = [c["condition"] for c in out["causes"] if c["preceded"] == 1.0]
    assert "restart changed and state=BUSY" in top                  # 演算中のリトライは毎回、結果の前に来る
    assert "mark rose" in conds and all(c["count"] > 0 for c in out["causes"])
    assert len(out["causes"]) <= 20


def test_frequent_variables_stay_and_only_an_explicit_coverage_limit_drops_them(tmp_path):
    # 経験則で候補を外さない（窓あたり何回で除外、をしない）。変化の多い変数の coverage は 1 に近いという事実を返し、
    # 絞るかどうかは使う側が max_coverage で明示する
    rec = make(tmp_path)
    out = correlate(rec, "errors changed", None, window_s=0.05)        # 50 ms: 10 ms ごとの mark は窓に必ず入る
    mark = [c for c in out["causes"] if c["condition"] == "mark rose"]
    assert mark and mark[0]["coverage"] == pytest.approx(1.0, abs=0.06) and "skipped" not in out
    narrowed = correlate(rec, "errors changed", None, window_s=0.05, max_coverage=0.9)
    assert not any(c["condition"].startswith("mark") for c in narrowed["causes"])


def test_a_condition_watcher_finds_the_same_instants_when_scanning_in_pieces(tmp_path):
    # 記録中に少しずつ調べる（recording.trigger）: 範囲の境の変化も落とさない（前の範囲の最後の値を持ち越す）
    from fprobe.correlate import ConditionWatcher
    rec = make(tmp_path)
    whole = ConditionWatcher("errors changed").scan(rec, 0, 100_000)
    w = ConditionWatcher("errors changed")
    pieces = [t for a, b in [(0, 2000), (2000, 22_000), (22_000, 50_000), (50_000, 100_000)] for t in w.scan(rec, a, b)]
    assert pieces == whole and len(whole) == 5
    fpga = ConditionWatcher("restart changed and state=BUSY")
    split = [t for a, b in [(0, 100), (100, 100_000)] for t in fpga.scan(rec, a, b)]
    assert split == ConditionWatcher("restart changed and state=BUSY").scan(rec, 0, 100_000) and split[0] == 100


def _recording_probe(tmp_path, monkeypatch):
    from types import SimpleNamespace

    from probe_fakes import FakeDevice

    from fprobe.mcp_server import Probe
    monkeypatch.setenv("FPROBE_HOME", str(tmp_path))
    p = Probe("unused")
    p._session = SimpleNamespace(device=FakeDevice())
    p.recording_start(name="trig")
    p._collector.poll_s = 3600                                       # 周期の取り込みは使わない（行を直に足す）
    return p


def test_a_trigger_marks_the_recording_each_time_the_condition_holds(tmp_path, monkeypatch):
    p = _recording_probe(tmp_path, monkeypatch)
    try:
        p._recorder.add_many([("sample", "word", t, ERRORS, v) for t, v in
                              [(1000, 0), (2000, 0), (3000, 1), (4000, 1), (5000, 2)]])
        p.recording_trigger_set("0x20040808 changed", action="mark")
        p._trigger_poll(now=3500)                                    # 遅れ（lag）の前までを調べる
        p._trigger_poll(now=10_000)
        st = p.recording_trigger_status()
        assert st["hits"] == 2 and st["first_t"] == 3000 and st["last_t"] == 5000 and st["state"] == "fired"
        marks = p.recording_events(streams=["annotation"])["events"]
        assert [m["t"] for m in marks if m["kind"] == "mark"] == [3000, 5000]
        p.recording_trigger_clear()
        assert p.recording_trigger_status()["state"] == "off"
    finally:
        p.recording_stop()


def test_a_stop_trigger_stops_the_recording_after_post_s(tmp_path, monkeypatch):
    import time as _time
    p = _recording_probe(tmp_path, monkeypatch)
    sent = []
    p.attach_publisher(lambda topic, data: sent.append((topic, data)))
    hz = p._recorder.timebase_hz
    p._recorder.add_many([("sample", "word", t, ERRORS, v) for t, v in [(1000, 0), (2000, 1)]])
    p.recording_trigger_set("0x20040808 changed", action="stop", post_s=0.5)
    p._trigger_poll(now=2000 + int(0.2 * hz))                      # まだ post_s が入っていない
    assert p._recorder is not None and p.recording_trigger_status()["state"] == "fired"
    p._trigger_poll(now=2000 + int(0.6 * hz))
    deadline = _time.monotonic() + 5
    while p._recorder is not None and _time.monotonic() < deadline:
        _time.sleep(0.01)
    assert p._recorder is None and p.recording_trigger_status()["state"] == "stopped"
    while not sent and _time.monotonic() < deadline:
        _time.sleep(0.01)
    # 誰が・なぜ止めたかをほかの窓・AI に知らせる（画面の時間軸・記録の一覧が読み直す）
    assert sent[-1][0] == "recording" and sent[-1][1]["action"] == "stop"
    assert sent[-1][1]["client"]["name"] == "trigger" and "0x20040808 changed" in sent[-1][1]["reason"]


def test_the_trigger_keeps_watching_the_live_recording_when_another_is_opened(tmp_path, monkeypatch):
    # 記録中に古い記録を開いて見ても（recording_open は読む記録を替える）、見張りは記録中の記録を調べる
    p = _recording_probe(tmp_path, monkeypatch)
    try:
        p._recorder.add_many([("sample", "word", t, ERRORS, v) for t, v in [(1000, 0), (1500, 1)]])
        p.recording_trigger_set("0x20040808 changed", action="mark")
        make(tmp_path / "recordings").close()                        # 別の記録（tmp/recordings/r）
        p._view = Recording.open(tmp_path / "recordings" / "r")     # 画面・AI が古い記録を開いた
        p._trigger_poll(now=10_000)
        st = p.recording_trigger_status()
        assert st["state"] == "fired" and st["hits"] == 1 and st["first_t"] == 1500     # 古い記録なら 2000 から 5 回
    finally:
        p.recording_stop()


def test_auto_candidates_quote_enum_names_with_spaces(tmp_path):
    # 列挙の名前に空白・= があっても、自動の候補で全体がエラーにならない（値も "…" で囲む）
    signals = {"groups": {"0": [
        {"name": "state", "lsb": 0, "width": 2, "format": "enum", "enum": {"0": "IDLE", "1": "WAIT ACK", "2": "a=b"}},
        {"name": "restart", "lsb": 3, "width": 1}]}}
    rec = make(tmp_path, signals)
    c = parse_condition('restart changed and state="WAIT ACK"')
    assert c.terms[1].value == "WAIT ACK"
    out = correlate(rec, "errors changed", None, window_s=0.005)
    assert any(x["condition"] == 'restart changed and state="WAIT ACK"' for x in out["causes"])


def test_auto_candidates_that_fire_at_the_same_times_are_listed_once():
    # 自動の候補で、成立する時刻がすべて同じもの（"restart changed and state=BUSY" と "… and addr=CTRL" 等）は 1 つに
    # まとめ、短い方を残して残りを same に（AI の試験で、数値が完全に同じ候補が並んで読み取りの手間になった）
    from fprobe.correlate import _merge_same
    a = {"condition": "restart changed and state=BUSY", "_times": (1, 5, 9), "lift": 4.0}
    b = {"condition": "restart changed and addr=CTRL", "_times": (1, 5, 9), "lift": 4.0}
    c = {"condition": "in0 rose", "_times": (2, 6), "lift": 1.0}
    out = _merge_same([a, b, c])
    assert [x["condition"] for x in out] == ["restart changed and addr=CTRL", "in0 rose"]
    assert out[0]["same"] == ["restart changed and state=BUSY"] and "_times" not in out[0]
    assert "same" not in out[1]


def test_transactions_split_the_recording_and_group_by_the_value_at_the_start(tmp_path):
    # 区間（リクエスト等）ごとに: 開始時の値（by）・区間の中で条件が成り立った回数（counts）・結果（effect）が起きたか。
    # by の値ごとに数・結果の割合・回数の平均（AI の試験で、手で全件を読んで「B が大きい要求だけ誤る」を出した集計）
    from fprobe.correlate import transactions
    rec = make(tmp_path)
    try:
        out = transactions(rec, "mark rose", by="restart", counts=["restart changed and state=BUSY"],
                           effect="errors changed")
    finally:
        rec.close()
    assert out["transactions"] == 10                      # 最後の区間は記録の終わりまで
    groups = {g["by"]: g for g in out["groups"]}
    assert groups["0"]["count"] == 4 and groups["0"]["effect_rate"] == 1.0         # 最初の区間は値がまだ無い（None）
    assert groups[None]["count"] == 1
    assert groups["1"]["count"] == 5 and groups["1"]["effect_rate"] == 0.0
    assert groups["0"]["counts"] == {"restart changed and state=BUSY": 1.0}
    assert groups["1"]["counts"] == {"restart changed and state=BUSY": 0.0}
    first = out["rows"][0]
    assert first["by"] is None and first["effect"] is True and first["counts"] == [1] and first["t"] == 10
    with pytest.raises(ConditionError):
        transactions(rec := make(tmp_path / "x"), "state=BUSY", by="restart")   # 開始は瞬間の項が要る
    rec.close()


def test_variables_without_values_in_the_range_are_reported(tmp_path):
    # 名前はあるが範囲の中に値が 1 件も無い変数（止まった watch の名前だけ写った等）は no_values で知らせる
    rec = make(tmp_path)
    try:
        rec.update_meta(**{"sample.names": {f"0x{ERRORS:08X}": "errors", "0x20040900": "ghost"}})
        out = correlate(rec, "errors changed", ["ghost changed", "restart changed"], window_s=0.01)
    finally:
        rec.close()
    assert out["no_values"] == ["ghost"]


def test_transactions_can_group_by_the_value_at_the_end(tmp_path):
    # by_at="end": 区間の終わり（次の始めの直前）の値で分ける（ファームウェアが要求の最後に書く値で分けるとき）
    from fprobe.correlate import transactions
    rec = make(tmp_path)
    try:
        start = transactions(rec, "mark rose", by="restart")
        end = transactions(rec, "mark rose", by="restart", by_at="end")
    finally:
        rec.close()
    assert [r["by"] for r in start["rows"][:3]] == [None, "1", "0"]
    assert [r["by"] for r in end["rows"][:3]] == ["1", "0", "1"]                 # 各周期の中で反転した後の値
    with pytest.raises(ValueError):
        transactions(make(tmp_path / "y"), "mark rose", by="restart", by_at="middle")


def test_a_cause_at_the_same_instant_as_the_effect_does_not_precede_it(tmp_path):
    # 同じ読みの中で変わった 2 つの値（ファームウェアが requests と errors をほぼ同時に書き、メモリの読みが同じ時刻に
    # 入る）は前後が分からない: 結果の直前に多い出来事に数えない（数えると、読みの間隔を広げたとき自明な候補が 1 位になった）
    rec = Recorder(tmp_path / "s", timebase_hz=1e6,
                   meta={"sample.names": {"0x20000000": "requests", "0x20000004": "errors"}})
    rows = []
    for k in range(20):
        t = k * 5000
        rows.append(("sample", "word", t, 0x20000000, k))                  # 毎回増える
        rows.append(("sample", "word", t, 0x20000004, k // 2))             # 2 回に 1 回、同じ時刻に増える
    rec.add_many(rows)
    rec.close()
    view = Recording.open(tmp_path / "s")
    try:
        out = correlate(view, "errors changed", ["requests changed"], window_s=0.004)
    finally:
        view.close()
    assert out["causes"][0]["preceded"] == 0.0 and out["causes"][0]["followed"] == 0.0


def test_the_order_is_preceded_then_the_condition_text():
    from fprobe.correlate import _rank
    rare = {"condition": "rare", "preceded": 0.01}
    real = {"condition": "real", "preceded": 1.0}
    also = {"condition": "also", "preceded": 1.0}
    none = {"condition": "none", "preceded": None}
    assert [c["condition"] for c in _rank([rare, none, real, also])] == ["also", "real", "rare", "none"]


def test_the_read_interval_is_reported_as_a_fact_without_a_rule_of_thumb(tmp_path):
    # 「窓が読みの間隔の 2 倍より短い」等の経験則の警告はしない。読みの間隔は事実（sampling・reads）で返す
    rec = make(tmp_path)                                       # errors は 1 ms ごとに読む
    try:
        out = correlate(rec, "errors changed", ["restart changed"], window_s=0.0015)
    finally:
        rec.close()
    assert out["sampling"]["errors"] == pytest.approx(0.001) and out["warnings"] == []
    reads = out["reads"]["errors"]
    assert reads["count"] == 100 and reads["interval_s"]["max"] == pytest.approx(0.001)


def test_holes_in_the_data_are_reported(tmp_path):
    # 欠けを黙って数え落とさない: 読みの空白・読めなかった語・FPGA の欠落・ピンの無い区間・取り込みの失敗を gaps で示す
    extra = [("sample", "word", 60_500, ERRORS, 0, {"ack": 2}), ("fpga", "gap", 70_000, 3, 0),
             ("annotation", "events_error", 80_000, 0, 0, {"error": "timeout"})]
    rec = make(tmp_path, drop_reads=range(30_000, 50_000), extra=extra, pins_from=35_000)
    try:
        out = correlate(rec, "errors changed", ["restart changed", "mark rose"], window_s=0.005, gap_s=0.005)
        clean = correlate(rec, "errors changed", ["restart changed"], window_s=0.005, t0=0, t1=29_000, gap_s=0.005)
    finally:
        rec.close()
    got = {(g["reason"], g.get("variable")): g for g in out["gaps"]}
    reads = [(g["t0"], g["t1"]) for g in out["gaps"] if g["reason"] == "reads_missing"]
    assert reads == [(29_000, 50_000), (99_000, 135_001)]     # 読みの止まった後（ピンだけ続いた所）も
    assert got[("read_failed", "errors")]["count"] == 1
    assert got[("fpga_lost", None)]["t0"] == 70_000 and got[("fpga_lost", None)]["count"] == 3
    assert (got[("pins_missing", "mark")]["t0"], got[("pins_missing", "mark")]["t1"]) == (0, 35_000)
    assert got[("events_error", None)]["t0"] == 80_000
    assert any("欠け" in w and "長さ 86.001 ms" in w for w in out["warnings"])  # 重なりは 1 回: 0〜50・99〜135.001 ms
    assert clean["gaps"] == [] and not any("欠け" in w for w in clean["warnings"])


def test_the_same_condition_given_twice_is_listed_once(tmp_path):
    # 画面は FPGA の条件の表と追加の候補を並べて渡す: 同じ式が 2 回来ても 1 行（画面の一覧のキーが重なって描画が止まった）
    rec = make(tmp_path)
    try:
        out = correlate(rec, "errors changed", ["restart changed", " restart changed ", "mark rose"], window_s=0.01)
    finally:
        rec.close()
    assert sorted(c["condition"] for c in out["causes"]) == ["mark rose", "restart changed"]


def test_effects_inside_a_hole_of_the_cause_are_not_judged(tmp_path):
    # 候補の変数に値が無い区間の結果は、候補が先に来たか分からない: 先行率の分母に入れない（judged = 判定できた結果の数）
    rec = make(tmp_path, pins_from=30_000)                   # ピンは 30 ms から: 2・22 ms の結果は判定できない
    try:
        out = correlate(rec, "errors changed", ["restart changed", "mark rose"], window_s=0.005)
    finally:
        rec.close()
    by = {c["condition"]: c for c in out["causes"]}
    assert by["restart changed"]["judged"] == 5
    assert by["mark rose"]["judged"] == 3
    assert by["mark rose"]["preceded"] == 1.0                # ピンのある所では毎回先に来る


def test_transactions_and_durations_leave_out_what_falls_in_a_hole(tmp_path):
    # 欠けにかかる区間・続いた時間は数えない（excluded）。欠けそのものは gaps で示す
    from fprobe.correlate import durations, transactions
    rec = make(tmp_path, drop_reads=range(30_000, 50_000))   # errors の読みが 29〜50 ms で空く
    try:
        tr = transactions(rec, "mark rose", by="restart", effect="errors changed", gap_s=0.005)
        du = durations(rec, "errors", gap_s=0.005)
    finally:
        rec.close()
    assert tr["transactions"] == 10 and tr["excluded"] == 3          # 20・30・40 ms からの区間
    assert sum(g["count"] for g in tr["groups"]) == 7
    assert [g["reason"] for g in tr["gaps"]] == ["reads_missing"]
    assert du["excluded"] == 2 and du["values"]["2"]["count"] == 0  # 22 ms からの 2 と、欠けの直後（50 ms）に見えた 3
    assert du["values"]["3"]["count"] == 0
    assert [g["reason"] for g in du["gaps"]] == ["reads_missing"]
    assert tr["excluded_by"] == {"errors": 3}                      # どの変数の欠けで外したか
    assert du["values"]["2"]["share"] == 0.0                      # 欠けの時間は割合に入れない
    assert sum(v["share"] for v in du["values"].values()) == pytest.approx(1.0)


def test_transactions_compare_those_with_the_effect_and_those_without(tmp_path):
    # 失敗した回と普段の回を並べる（判断はしない）: 結果が起きた区間と起きなかった区間の数・条件の平均回数
    from fprobe.correlate import transactions
    rec = make(tmp_path)
    try:
        out = transactions(rec, "mark rose", counts=["restart changed and state=BUSY", "restart changed"],
                           effect="errors changed")
    finally:
        rec.close()
    w, wo = out["by_effect"]["with"], out["by_effect"]["without"]
    assert (w["count"], wo["count"]) == (5, 5)
    assert w["counts"] == {"restart changed and state=BUSY": 1.0, "restart changed": 1.0}
    assert wo["counts"] == {"restart changed and state=BUSY": 0.0, "restart changed": 1.0}


def test_an_effect_seen_right_after_a_hole_of_its_own_variable_is_not_judged(tmp_path):
    # 欠けの直後の読みで見えた変化は、本当はいつ（何回）変わったか分からない: 判定に入れない
    rec = make(tmp_path, drop_reads=range(30_000, 50_000))   # 42 ms の増加は 50 ms の読みで見える
    try:
        out = correlate(rec, "errors changed", ["restart changed"], window_s=0.005, gap_s=0.005)
    finally:
        rec.close()
    assert out["effect"]["count"] == 5 and out["causes"][0]["judged"] == 4
    assert any("重なりを除いた長さ" in w for w in out["warnings"])


def test_transactions_report_when_in_the_interval_the_effect_comes(tmp_path):
    # 結果が区間の始めの直後に来るなら、前の区間の結果かもしれない: 判断はせず、始めからの時間と区間の長さを返す
    from fprobe.correlate import transactions
    rec = make(tmp_path)
    try:
        out = transactions(rec, "mark rose", effect="errors changed")
    finally:
        rec.close()
    assert out["effect_offset_s"]["median"] == pytest.approx(0.00199)   # 10 µs の始め → 2 ms の結果
    assert out["length_s"]["median"] == pytest.approx(0.01)


def test_a_pause_in_reads_tells_how_much_the_value_moved_unseen(tmp_path):
    # 読みの空白の前後の値: 欠けの間に増えた量が分かる（回数は分からないが、数え落とした下限が分かる）
    rec = make(tmp_path, drop_reads=range(30_000, 50_000))   # 29 ms で 2、50 ms で 3
    try:
        out = correlate(rec, "errors changed", ["restart changed"], window_s=0.005, gap_s=0.005)
    finally:
        rec.close()
    g = [g for g in out["gaps"] if g["reason"] == "reads_missing"][0]
    assert (g["before"], g["after"]) == (2, 3)


def test_transactions_can_give_an_effect_to_the_interval_just_before(tmp_path):
    # 結果が区間の始めの直後に読まれるとき（前の要求の結果）: effect_lag_s だけ前の時刻で区間に割り当てる
    from fprobe.correlate import transactions
    rec = make(tmp_path)                                       # 結果は 2 ms、周期の始め（mark）は 0.01 ms
    try:
        late = transactions(rec, "restart changed", effect="errors changed")           # 区間 = 0.1 ms からの 10 ms
        lag = transactions(rec, "restart changed", effect="errors changed", effect_lag_s=0.0025)
    finally:
        rec.close()
    assert late["effect_offset_s"]["median"] == pytest.approx(0.0019)
    assert lag["by_effect"]["with"]["count"] == 4                 # 最初の結果は最初の区間より前になる
    assert lag["effect_offset_s"]["median"] == pytest.approx(0.0094)
    assert lag["effect_lag_s"] == 0.0025


def test_transactions_warn_when_gaps_leave_intervals_out(tmp_path):
    from fprobe.correlate import transactions
    rec = make(tmp_path, drop_reads=range(30_000, 50_000))
    try:
        tr = transactions(rec, "mark rose", effect="errors changed", gap_s=0.005)
    finally:
        rec.close()
    assert any("3 区間" in w and "errors 3" in w for w in tr["warnings"])


def test_memory_events_carry_the_watch_name(tmp_path, monkeypatch):
    # recording.events の sample は番地だけだと、どの変数か手で引くことになる: 記録の名前を name で付ける
    from fprobe.mcp_server import Probe
    monkeypatch.setenv("FPROBE_HOME", str(tmp_path))
    make(tmp_path / "recordings").close()
    p = Probe("unused")
    p.recording_open("r")
    ev = p.recording_events(streams=["sample"], limit=2)["events"]
    assert [e["name"] for e in ev] == ["errors", "errors"]


def test_the_effect_tells_how_much_a_memory_counter_moved(tmp_path):
    # 変化の回数（count）と増えた量は違いうる（1 回の読みで +2 等）: MCU のメモリの結果は範囲の最初と最後の値の差も返す
    extra = [("sample", "word", 85_500, ERRORS, 7)]           # 82 ms の 5 から 85.5 ms で 7 へ（1 回で +2）
    rec = make(tmp_path, extra=extra)
    try:
        out = correlate(rec, "errors changed", ["restart changed"], window_s=0.005)
    finally:
        rec.close()
    assert out["effect"]["delta"] == {"errors": 5}            # 最後の読み（99 ms）は 5 に戻る: 0 → 5
    assert out["effect"]["count"] == 7


def test_the_gap_warning_lists_the_length_per_variable(tmp_path):
    # 欠けの合計は使った変数で変わる（自動探索は返す候補の変数）: 変数ごとの長さを書き、どの変数の欠けかを分かるように
    rec = make(tmp_path, drop_reads=range(30_000, 50_000), pins_from=30_000)
    try:
        out = correlate(rec, "errors changed", ["restart changed", "mark rose"], window_s=0.005, gap_s=0.005)
    finally:
        rec.close()
    w = [w for w in out["warnings"] if "欠け" in w][0]
    assert "errors 52.001 ms" in w and "mark 30 ms" in w        # errors: 29〜50 ms と、読みの終わりから記録の終わりまで


def test_durations_list_enum_values_that_never_appear(tmp_path):
    # 定義にあるが 1 度も出なかった列挙の値も 0 で並べる（「出なかった」と「定義に無い」を区別できるように）
    from fprobe.correlate import durations
    signals = {"groups": {"0": [{"name": "state", "lsb": 0, "width": 2, "format": "enum",
                                 "enum": {"0": "IDLE", "1": "BUSY", "2": "DONE", "3": "ERROR"}},
                                {"name": "restart", "lsb": 3, "width": 1}]}}
    rec = make(tmp_path, signals)
    try:
        out = durations(rec, "state")
    finally:
        rec.close()
    assert out["values"]["ERROR"] == {"count": 0, "share": 0.0, "duration_s": None}


def test_read_pauses_are_not_gaps_unless_the_caller_gives_gap_s(tmp_path):
    # 「何秒空いたら欠けか」は経験則なので既定で決めない: gap_s が無ければ読みの空白は gaps に入れず、事実（読みの回数・
    # 間隔の分布・いちばん長い空白の時刻）を reads で返す
    rec = make(tmp_path, drop_reads=range(30_000, 50_000))
    try:
        out = correlate(rec, "errors changed", ["restart changed"], window_s=0.005)
    finally:
        rec.close()
    assert not [g for g in out["gaps"] if g["reason"] == "reads_missing"]
    reads = out["reads"]["errors"]
    assert reads["longest"] == [29_000, 50_000] and reads["interval_s"]["max"] == pytest.approx(0.021)
    assert out["causes"][0]["judged"] == 5


def test_the_window_is_always_given_by_the_caller(tmp_path):
    # preceded・coverage を左右する窓を、暗黙の既定（10 ms）で決めない
    rec = make(tmp_path)
    with pytest.raises(ValueError, match="window_s"):
        correlate(rec, "errors changed", ["mark rose"])


def test_followed_and_coverage_match_the_direct_definitions_on_random_data():
    # 多い候補（ピンのクロック等で数百万回）でも速く数えるための形が、定義どおりの数え方と一致する
    import random
    from fprobe.correlate import _coverage, _followed
    rng = random.Random(3)
    for _ in range(50):
        cs = sorted(rng.sample(range(10_000), rng.randrange(0, 300)))
        eff = sorted(rng.sample(range(10_000), rng.randrange(0, 30)))
        window = rng.choice([1, 7, 50, 400])
        direct = sum(1 for c in cs if any(c < e <= c + window for e in eff))
        assert _followed(cs, eff, window) == direct
        holes = [(2000, 2500)] if rng.random() < 0.5 else []
        covered = set()
        for c in cs:
            covered.update(t for t in range(max(0, c), min(10_000, c + window)) if not any(a <= t < b for a, b in holes))
        seen = 10_000 - sum(b - a for a, b in holes)
        got = _coverage(cs, window, 0, 10_000, holes)
        assert got == pytest.approx(len(covered) / seen, abs=1e-9)


@pytest.mark.parametrize("use_numpy", [True, False])
def test_state_terms_filter_the_same_with_and_without_numpy(monkeypatch, use_numpy):
    # 「変化 かつ 状態 = 値」の照合（瞬間ごとに、その時刻の状態の値）は numpy の有無で同じ結果
    import random
    import fprobe.correlate as C
    if not use_numpy:
        monkeypatch.setattr(C, "_np", None)
    rng = random.Random(5)
    times = sorted(rng.sample(range(1, 100_000), 2000))
    state = C._Series(times, [rng.randrange(3) for _ in times])
    instants = sorted(rng.sample(range(0, 100_000), 5000))
    expected = [t for t in instants if state.at(t) == 1]
    assert C._holding(instants, [(state, 1)]) == expected


@pytest.mark.parametrize("use_numpy", [True, False])
def test_coverage_is_the_same_with_and_without_numpy(monkeypatch, use_numpy):
    import random
    import fprobe.correlate as C
    if not use_numpy:
        monkeypatch.setattr(C, "_np", None)
    rng = random.Random(9)
    cs = sorted(rng.sample(range(10_000), 400))
    covered = set()
    for c in cs:
        covered.update(t for t in range(c, min(10_000, c + 37)) if not 3000 <= t < 3500)
    assert C._coverage(cs, 37, 0, 10_000, [(3000, 3500)]) == pytest.approx(len(covered) / 9500, abs=1e-9)
