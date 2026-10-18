"""画面の状態（fprobe.probe_ui_state）: 人の画面・別の窓・AI が共有する視点・カーソル・段・出来事・フレーム。
デーモン越しの通知（ui.state）と MCP の道具も。"""

import time

import pytest
from test_api_daemon import _serve

from fake_daemon import FakeProbe
from fprobe.api.client import DaemonClient
from fprobe.probe_ui_state import UiStateTools


class Holder(UiStateTools):
    def __init__(self):
        self._init_ui_state()
        self.sent = []
        self._publish = lambda topic, data: self.sent.append((topic, data))


def test_set_merges_and_reports_what_changed():
    h = Holder()
    assert h.ui_state_get() == {"rev": 0, "view": None, "cursors": {"selection": None, "mouse": None, "reference": None},
                                "rows": None, "event": None, "frame": 0, "sampling": None, "dashboard": None,
                                "pointers": []}
    out = h.ui_state_set(view={"source": "recording", "t0": 10, "t1": 20}, cursors={"selection": 12}, origin="w1")
    assert out["changed"] == ["view", "cursors"] and out["rev"] == 1
    out = h.ui_state_set(cursors={"reference": 11})
    assert out["cursors"] == {"selection": 12, "mouse": None, "reference": 11}       # 渡したカーソルだけ
    assert h.ui_state_set(cursors={"reference": 11})["changed"] == []                  # 同じ値は通知しない
    assert h.ui_state_get()["rev"] == 2
    topic, data = h.sent[0]
    assert topic == "ui.state" and data["origin"] == "w1" and data["changed"] == ["view", "cursors"]
    assert data["state"]["view"] == {"source": "recording", "t0": 10, "t1": 20}
    assert len(h.sent) == 2


def test_rows_event_frame_and_clear():
    h = Holder()
    h.ui_state_set(rows=["ch0", "rtt", "ch0"], event={"stream": "uart", "t": 5, "kind": "line", "text": "ok", "x": 1},
                   frame=2)
    s = h.ui_state_get()
    assert s["rows"] == ["ch0", "rtt"] and s["event"] == {"stream": "uart", "t": 5, "kind": "line", "text": "ok"}
    assert s["frame"] == 2
    out = h.ui_state_set(clear=["rows", "event", "frame"])
    assert out["rows"] is None and out["event"] is None and out["frame"] == 0
    assert out["changed"] == ["rows", "event", "frame"]


def test_each_window_and_the_ai_has_its_own_mouse_pointer():
    h = Holder()
    h.ui_state_set(view={"source": "recording", "t0": 0, "t1": 100})
    h.ui_state_set(cursors={"mouse": 40}, origin="w1", caller={"id": 1, "name": "gui", "role": "human"})
    out = h.ui_state_set(cursors={"mouse": 70}, caller={"id": 2, "name": "claude", "role": "ai"})
    assert "pointers" in out["changed"]
    pointers = {p["key"]: p for p in h.ui_state_get()["pointers"]}
    assert pointers["w1"]["t"] == 40 and pointers["w1"]["name"] == "gui" and pointers["w1"]["source"] == "recording"
    assert pointers["client:2"]["t"] == 70 and pointers["client:2"]["role"] == "ai"
    assert h.sent[-1][1]["pointers"] == out["pointers"]
    assert h.ui_state_set(cursors={"mouse": 70}, caller={"id": 2, "name": "claude", "role": "ai"})["changed"] == []
    h.ui_state_set(cursors={"mouse": None}, origin="w1")                    # 外れたら消える
    assert [p["key"] for p in h.ui_state_get()["pointers"]] == ["client:2"]


def test_pointers_of_a_client_that_left_are_dropped():
    h = Holder()
    h.ui_state_set(view={"source": "recording", "t0": 0, "t1": 100})
    h.ui_state_set(cursors={"mouse": 40}, origin="w1", caller={"id": 1, "name": "gui", "role": "human"})
    h.ui_state_set(cursors={"mouse": 70}, caller={"id": 2, "name": "claude", "role": "ai"})
    rev = h.ui_state_get()["rev"]
    assert h.ui_state_client_left(1) is True                                   # 窓を閉じた・切れた
    assert [p["key"] for p in h.ui_state_get()["pointers"]] == ["client:2"]
    topic, data = h.sent[-1]
    assert topic == "ui.state" and data["changed"] == ["pointers"] and data["rev"] == rev + 1
    assert [p["key"] for p in data["pointers"]] == ["client:2"]
    assert h.ui_state_client_left(1) is False and len(h.sent) == 4            # 無ければ通知しない


def test_data_sampling_expressions_and_rate_are_shared():
    h = Holder()
    out = h.ui_state_set(sampling={"expressions": [{"expr": "counter"}, {"expr": " adc ", "plot": False, "axis": "right"},
                                                   {"expr": "counter"}], "rate_hz": 100}, origin="w1")
    assert out["changed"] == ["sampling"]
    assert h.ui_state_get()["sampling"] == {"expressions": [{"expr": "counter", "plot": True, "axis": "left"},
                                                            {"expr": "adc", "plot": False, "axis": "right"}],
                                            "rate_hz": 100.0}
    assert h.sent[-1][1]["state"]["sampling"]["rate_hz"] == 100.0
    assert h.ui_state_set(clear=["sampling"])["sampling"] is None


def test_dashboard_layout_is_shared():
    # 操作パネル（G10）の部品の配置: 別の窓・遠隔（--lan）の画面・AI が同じ配置を見る。部品の中身の検証と直しは画面
    h = Holder()
    layout = {"version": 1, "widgets": [{"id": "w1", "kind": "gauge", "expr": "counter", "min": 0, "max": 100}]}
    out = h.ui_state_set(dashboard=layout, origin="w1")
    assert out["changed"] == ["dashboard"] and h.ui_state_get()["dashboard"] == layout
    assert h.sent[-1][1]["state"]["dashboard"] == layout
    assert h.ui_state_set(dashboard=layout)["changed"] == []
    assert h.ui_state_set(clear=["dashboard"])["dashboard"] is None


@pytest.mark.parametrize("kwargs", [
    {"dashboard": {"version": 2, "widgets": []}}, {"dashboard": {"version": 1, "widgets": "x"}},
    {"dashboard": {"version": 1, "widgets": [{"id": "a", "kind": 3, "expr": ""}]}},
    {"dashboard": {"version": 1, "widgets": [{"id": "a", "kind": "gauge", "expr": "x"}] * 65}},
    {"dashboard": {"version": 1, "widgets": [{"id": "a", "kind": "gauge", "expr": "x" * 70000}]}},
    {"sampling": {"expressions": "x"}}, {"sampling": {"expressions": [{"expr": ""}]}},
    {"sampling": {"expressions": [], "rate_hz": 0}}, {"sampling": {"expressions": [{"expr": "a", "axis": "up"}]}},
    {"view": {"t0": 5, "t1": 5}}, {"view": {"source": "x", "t0": 0, "t1": 1}}, {"cursors": {"other": 1}},
    {"cursors": {"mouse": 1.5}}, {"event": {"t": 1}}, {"frame": -1}, {"clear": ["everything"]}, {"rows": [""]},
])
def test_bad_values_are_refused(kwargs):
    with pytest.raises(ValueError):
        Holder().ui_state_set(**kwargs)


def test_windows_and_the_ai_share_the_state_through_the_daemon(tmp_path):
    url, _, stop = _serve(FakeProbe(), tmp_path)
    try:
        gui = DaemonClient(url, "gui", "human")
        ai = DaemonClient(url, "claude", "ai")
        seen = []
        gui.subscribe(["ui.state", "activity"], lambda topic, data: seen.append((topic, data)))
        ai.call("ui.state.set", view={"source": "capture", "t0": 100, "t1": 200}, cursors={"selection": 150})
        deadline = time.monotonic() + 2
        while not any(t == "ui.state" for t, _ in seen) and time.monotonic() < deadline:
            time.sleep(0.01)
        note = next(d for t, d in seen if t == "ui.state")
        assert note["state"]["cursors"]["selection"] == 150 and note["origin"] is None
        assert gui.call("ui.state.get")["view"] == {"source": "capture", "t0": 100, "t1": 200}
        while not any(t == "activity" and d["method"] == "ui.state.set" for t, d in seen) and time.monotonic() < deadline:
            time.sleep(0.01)
        entry = next(d for t, d in seen if t == "activity" and d["method"] == "ui.state.set")
        assert entry["client"] == "claude"                                             # 誰が変えたかは記録に
        ai.call("ui.state.set", cursors={"mouse": 160})                              # AI の指す時刻（名前はデーモンが付ける）
        assert [(p["name"], p["role"], p["t"]) for p in gui.call("ui.state.get")["pointers"]] == [("claude", "ai", 160)]
        gui.call("session.control.pause")                                              # 一時停止中も変えられる
        assert ai.call("ui.state.set", frame=1)["frame"] == 1
        ai.close()                                                                     # 切れたら AI のカーソルは消える
        deadline = time.monotonic() + 2
        while gui.call("ui.state.get")["pointers"] and time.monotonic() < deadline:
            time.sleep(0.01)
        assert gui.call("ui.state.get")["pointers"] == []
        assert any(t == "ui.state" and d["changed"] == ["pointers"] and not d["pointers"] for t, d in seen)
        gui.close()
    finally:
        stop()


def test_mcp_has_the_screen_state_tools():
    import asyncio

    from test_api_mcp import FakeClient

    from fprobe.api.spec import load_spec
    from fprobe.mcp_server import build_client_server
    tools = {t.name: t for t in asyncio.run(build_client_server(FakeClient(), load_spec()).list_tools())}
    assert {"ui_state_get", "ui_state_set"} <= set(tools)
    assert "origin" not in tools["ui_state_set"].input_schema["properties"]             # 窓の印は AI に出さない
