"""「読む物がまだ無い」（捕捉・記録をまだ取っていない・開いていない）は NoData（E_NO_DATA）で返す。
画面は誤りの文の語（「捕捉が無い」等）でなく符号で「無い」と「読めなかった」を見分ける（機器は使わない）。"""

import pytest

from fprobe.api import NoData
from fprobe.mcp_server import Probe


@pytest.fixture
def probe(tmp_path, monkeypatch):
    monkeypatch.setenv("FPROBE_HOME", str(tmp_path))
    return Probe("unused")


def test_capture_tools_without_a_capture(probe):
    with pytest.raises(NoData):
        probe.timeline_tiles(source="capture")
    with pytest.raises(NoData):
        probe.la_decode(protocol="i2c", settings={"scl": 0, "sda": 1})
    with pytest.raises(NoData):
        probe.la_measure()


def test_recording_tools_without_a_recording(probe):
    with pytest.raises(NoData):
        probe.timeline_tiles(source="recording")
    with pytest.raises(NoData):
        probe.recording_events()
