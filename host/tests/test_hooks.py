"""fprobe.hooks: デバッグの出来事のフックの登録簿（検証・一致・文の置き換え・実行の記録・保存と読み込み）。"""

import json

import pytest

from fprobe.hooks import HookError, HookTable, event_of, expand


def test_operations_map_to_events_and_phases():
    assert event_of("target.reset") == "reset" and event_of("gdb.reset") == "reset"
    assert event_of("target.flash") == "flash" and event_of("gdb.flash") == "flash"
    assert event_of("target.connect") == "connect" and event_of("debug.run_until_halt") == "resume"
    assert event_of("target.halt") == "halt" and event_of("gdb.interrupt") == "halt"
    assert event_of("target.memory.read") is None and event_of(None) is None


def test_hooks_are_validated():
    table = HookTable()
    with pytest.raises(HookError, match="event"):
        table.add("explode", "after", [{"mark": "x"}])
    with pytest.raises(HookError, match="phase"):
        table.add("reset", "during", [{"mark": "x"}])
    with pytest.raises(HookError, match="動作"):
        table.add("reset", "after", [])
    with pytest.raises(HookError, match="どれか 1 つ"):
        table.add("reset", "after", [{"mark": "x", "evaluate": "y"}])
    with pytest.raises(HookError, match="params"):
        table.add("reset", "after", [{"call": "target.halt", "params": 3}])
    with pytest.raises(HookError, match="フック"):
        table.add("reset", "after", [{"call": "hooks.remove"}])                 # フックからフックを変えない
    with pytest.raises(HookError, match="required"):
        table.add("reset", "after", [{"mark": "x"}], required=True)             # 止められるのは before だけ
    with pytest.raises(HookError, match="無い"):
        HookTable(methods={"target.halt"}).add("reset", "after", [{"call": "target.hlt"}])


def test_matching_respects_event_phase_and_enabled():
    table = HookTable()
    a = table.add("reset", "after", [{"mark": "a"}])
    table.add("reset", "before", [{"mark": "b"}])
    table.add("reset", "after", [{"mark": "c"}], enabled=False)
    assert [h.id for h in table.matching("reset", "after")] == [a.id]
    assert table.wants("reset", "after") and not table.wants("flash", "after")
    assert table.remove(a.id) == 1 and not table.matching("reset", "after")
    with pytest.raises(KeyError):
        table.remove(999)
    assert table.remove(None) == 2 and table.list() == []


def test_templates_replace_known_names_and_keep_others():
    text = expand("{event}/{phase} by {client} at {where} {unknown}", {"event": "halt", "phase": "after",
                                                                      "client": "gdb", "where": "main+4"})
    assert text == "halt/after by gdb at main+4 {unknown}"


def test_the_run_log_is_a_bounded_ring_with_sequence_numbers():
    table = HookTable(log_size=3)
    for n in range(5):
        table.record({"hook": n})
    log = table.read_log(10)
    assert [e["hook"] for e in log] == [2, 3, 4] and [e["seq"] for e in log] == [3, 4, 5]
    assert table.read_log(1)[0]["hook"] == 4


def test_hooks_are_saved_and_loaded(tmp_path):
    table = HookTable()
    table.add("flash", "before", [{"call": "probe.voltage"}], name="電圧", required=True)
    path = tmp_path / "hooks.json"
    path.write_text(json.dumps(table.dump(), ensure_ascii=False), encoding="utf-8")
    other = HookTable()
    other.add("reset", "after", [{"mark": "x"}])
    assert other.load(json.loads(path.read_text(encoding="utf-8")), replace=False) == 1
    assert [(h["event"], h["name"]) for h in other.list()] == [("reset", ""), ("flash", "電圧")]
    other.load(table.dump(), replace=True)
    assert [h["event"] for h in other.list()] == ["flash"] and other.list()[0]["required"]
