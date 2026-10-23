"""events_read（MCP の出来事の読み）: 割り込み書き込みは kind "inject" と転送数（R20）を返す。機器は使わない。"""

from types import SimpleNamespace

from probe_fakes import FakeDevice

from fprobe.events import KIND_INJECT, KIND_INJECT_TRANSFERS, DapEvent
from fprobe.mcp_server import Probe


def test_events_read_reports_inject_with_its_transfer_count(tmp_path, monkeypatch):
    monkeypatch.setenv("FPROBE_HOME", str(tmp_path))
    p = Probe("unused")
    p._session = SimpleNamespace(device=FakeDevice())
    events = [DapEvent(100, KIND_INJECT_TRANSFERS, True, False, 0xC, 1, False, 0xE000EDF0, 0xA05F0003, transfers=4),
              DapEvent(200, KIND_INJECT, True, False, 0xC, 1, False, 0xE000EDF0, 0xA05F0003)]
    monkeypatch.setattr(type(p), "_read_events", lambda self, last, since: (2, 0, 0, events, {}))
    items = p.events_read()["items"]
    assert [(i["kind"], i["transfers"]) for i in items] == [("inject", 4), ("inject", None)]
    raw = p.events_read(raw=True)["items"]
    assert [i["transfers"] for i in raw] == [4, None]
