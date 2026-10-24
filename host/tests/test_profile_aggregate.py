"""プロファイルの集計（fprobe.profile.aggregate、U18）: 時間の区間に限る（t0・t1）、関数のグループ（名前の型・ファイル・
ディレクトリ・名前の型の指定）、CPU 使用率とグループの推移、テストポイントの呼び出しの区間の集計。"""

import pytest

from fprobe.profile import PcSample, PcSamples, Profiler
from fprobe.profile.aggregate import Grouping, call_stats, group_profile, group_rows, timeline, window
from fprobe.symbols import Location, Symbol
from fprobe.testpoint import Call

FUNCS = {"HAL_GPIO_Init": (0x1000, "Drivers/hal/gpio.c"), "HAL_UART_Tx": (0x1100, "Drivers/hal/uart.c"),
         "bt_send": (0x1200, "app/bt/send.c"), "bt_recv": (0x1300, "app/bt/recv.c"),
         "motor::Pid::step": (0x1400, "app/motor/pid.cpp"), "main": (0x1500, "app/main.c")}


class FakeSymbols:
    def describe(self, address):
        for name, (start, file) in FUNCS.items():
            if start <= address < start + 0x100:
                return Location(address, name, address - start, file, 10 + (address - start) // 16)
        return Location(address)

    def symbol(self, name):
        return Symbol(name, FUNCS[name][0], 0x100, "function")


def at(name, offset=0):
    return FUNCS[name][0] + offset


def samples(pcs, period=10):
    return PcSamples(tuple(PcSample(pc, t * period) for t, pc in enumerate(pcs)), "pcsr")


@pytest.fixture
def profiler():
    return Profiler(FakeSymbols())


def test_window_keeps_samples_in_the_interval():
    s = samples([at("main"), at("bt_send"), None, at("main")])
    w = window(s, 10, 30)
    assert [x.t for x in w.samples] == [10, 20] and w.source == "pcsr"
    assert len(window(s, None, 20)) == 2 and len(window(s, 20, None)) == 2
    with pytest.raises(ValueError, match="時刻"):
        window(PcSamples.from_pcs([at("main")]), 0, 10)


def test_group_by_name_prefix_file_and_directory(profiler):
    prof = profiler.profile(samples([at("HAL_GPIO_Init"), at("HAL_UART_Tx", 32), at("HAL_UART_Tx"), at("bt_send"),
                                     at("motor::Pid::step"), at("main"), None, 0x9000]))
    prefix = group_profile(prof, Grouping("prefix", separator="_"))
    rows = {r["group"]: r for r in prefix["rows"]}
    assert rows["HAL"]["samples"] == 3 and rows["HAL"]["functions"][0] == {"function": "HAL_UART_Tx", "samples": 2}
    assert rows["motor::Pid::step"]["samples"] == 1 and rows["bt"]["samples"] == 1 and rows["main"]["samples"] == 1
    scoped = {r["group"]: r["samples"] for r in group_profile(prof, Grouping("prefix", separator="::"))["rows"]}
    assert scoped["motor"] == 1 and scoped["HAL_UART_Tx"] == 2           # 区切りの無い名前はそのまま
    assert prefix["rows"][0]["group"] == "HAL" and abs(rows["HAL"]["fraction"] - 3 / 8) < 1e-9
    assert rows["HAL"]["active_fraction"] == pytest.approx(3 / 7) and "ci" not in rows["HAL"]
    assert prefix["categories"]["sleep"]["samples"] == 1 and prefix["categories"]["unknown"]["samples"] == 1
    files = {r["group"]: r["samples"] for r in group_profile(prof, Grouping("file"))["rows"]}
    assert files["Drivers/hal/uart.c"] == 2
    dirs = {r["group"]: r["samples"] for r in group_profile(prof, Grouping("directory"))["rows"]}
    assert dirs == {"Drivers/hal": 3, "app/bt": 1, "app/motor": 1, "app": 1}


def test_named_groups_by_name_and_file_patterns(profiler):
    prof = profiler.profile(samples([at("bt_send"), at("bt_recv"), at("HAL_GPIO_Init"), at("main")]))
    grouping = Grouping("groups", {"Bluetooth": ["bt_*"], "Drivers": ["file:Drivers/*"]})
    rows = {r["group"]: r["samples"] for r in group_profile(prof, grouping)["rows"]}
    assert rows == {"Bluetooth": 2, "Drivers": 1, "(other)": 1}
    with pytest.raises(ValueError, match="groups"):
        Grouping("groups")
    with pytest.raises(ValueError, match="by"):
        Grouping("color")


def test_prefix_needs_an_explicit_separator():
    # 名前の型（最初の _ の前を型とみなす等）の規則を本体に持たない。区切りは使う側が明示する
    with pytest.raises(ValueError, match="separator"):
        Grouping("prefix")
    assert Grouping("prefix", separator=".").key("a.b.c", None) == "a"
    assert Grouping("prefix", separator="_").key("__libc_init", None) == ""      # 先頭の区切りも飛ばさない


def test_timeline_of_load_and_groups(profiler):
    pcs = [at("bt_send"), None, at("main"), None, at("bt_send"), at("bt_send"), at("bt_recv"), at("main")]
    out = timeline(samples(pcs), profiler, bucket=40, grouping=Grouping("prefix", separator="_"), top=1)
    assert [b["t0"] for b in out["buckets"]] == [0, 40]
    first, second = out["buckets"]
    assert first["samples"] == 4 and first["load"] == 0.5 and first["groups"] == {"bt": 0.25, "(other)": 0.25}
    assert second["load"] == 1.0 and second["groups"] == {"bt": 0.75, "(other)": 0.25}
    assert out["groups"] == ["bt"]


def test_call_stats_clip_calls_to_the_interval():
    calls = [Call("g", 15, 20, 2, 5, 3, 4), Call("f", 10, 30, 1, 15, 2, 5), Call("f", 50, 90, 1, 40, 2, 5),
             Call("main", 0, 100, 0, 40, 1, 6)]
    rows = {r["function"]: r for r in call_stats(calls, 20, 60)}
    assert rows["main"]["inclusive"] == 40 and rows["main"]["exclusive"] == 20
    assert rows["f"]["inclusive"] == 20 and rows["f"]["exclusive"] == 20 and rows["f"]["started"] == 1
    assert rows["f"]["active"] == 2 and "g" not in rows
    whole = {r["function"]: r for r in call_stats(calls, None, None)}
    assert whole["main"]["exclusive"] == 40 and whole["g"]["inclusive"] == 5


def test_group_rows_of_call_stats():
    rows = [{"function": "bt_send", "inclusive": 10, "exclusive": 6, "started": 1, "active": 1},
            {"function": "bt_recv", "inclusive": 4, "exclusive": 4, "started": 2, "active": 2},
            {"function": "main", "inclusive": 30, "exclusive": 20, "started": 0, "active": 1}]
    grouped = group_rows(rows, Grouping("prefix", separator="_"), lambda name: None)
    assert grouped[0] == {"group": "main", "exclusive": 20, "started": 0, "functions": ["main"]}
    assert grouped[1] == {"group": "bt", "exclusive": 10, "started": 3, "functions": ["bt_send", "bt_recv"]}


def test_group_profile_returns_the_limits_it_used(profiler):
    """グループ（top）・グループの中の関数（members）を切ったら、切る前の数と使った上限を返す。"""
    prof = profiler.profile(samples([at("HAL_GPIO_Init"), at("HAL_UART_Tx"), at("bt_send"), at("main")]))
    out = group_profile(prof, Grouping("prefix", separator="_"), top=2, members=1)
    assert out["top"] == 2 and out["members"] == 1 and out["groups_total"] == 3 and len(out["rows"]) == 2
    hal = next(r for r in out["rows"] if r["group"] == "HAL")
    assert len(hal["functions"]) == 1 and hal["functions_total"] == 2


def test_timeline_returns_top_and_groups_total(profiler):
    pcs = [at("bt_send"), at("main"), at("HAL_GPIO_Init")]
    out = timeline(samples(pcs), profiler, bucket=40, grouping=Grouping("prefix", separator="_"), top=1)
    assert out["top"] == 1 and out["groups_total"] == 3
