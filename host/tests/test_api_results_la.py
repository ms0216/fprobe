"""ロジアナの道具（la.arm・la.collect・la.capture・la.deep.*・la.stream）を Probe から呼び、応答を仕様の result で照合する
（tests/api_result_check.py）。ほかの試験はロジアナを LogicAnalyzer・DeepAnalyzer で直接呼ぶので、Probe の道具の応答の形は
ここで確かめる。機器は CSR の偽物（捕捉の中身は 0 のまま。形だけを見る）。"""

import random
from types import SimpleNamespace

from fprobe import generated as g
from fprobe.mcp_server import Probe
from test_la_stream import FakeStreamDevice, raw_source
from test_la_time import FakeDevice

DONE = 1 << g.LA_STATUS_DONE[0]
DEEP_DONE = 1 << g.LA_DEEP_STATUS_DONE[0] | 1 << g.LA_DEEP_STATUS_TRIGGERED[0]


def _probe(dev, tmp_path, monkeypatch) -> Probe:
    monkeypatch.setenv("FPROBE_HOME", str(tmp_path))
    p = Probe("unused")
    p._session = SimpleNamespace(device=dev)
    return p


def test_internal_capture_tools_return_their_documented_shapes(tmp_path, monkeypatch):
    dev = FakeDevice(la_depth=64)
    dev.regs[g.LA_STATUS] = DONE
    p = _probe(dev, tmp_path, monkeypatch)
    armed = p.la_arm(pretrigger=16, mask=1, value=1)
    assert armed["depth"] == 64 and armed["pretrigger"] == 16
    got = p.la_collect(timeout_s=0.1, vcd_path=str(tmp_path / "a.vcd"))
    assert got["samples"] == 64 and got["vcd"].endswith("a.vcd")
    both = p.la_capture(timeout_s=0.1, pretrigger=8)
    assert both["pretrigger"] == 8 and both["vcd"] is None


def test_deep_capture_tools_return_their_documented_shapes(tmp_path, monkeypatch):
    dev = FakeDevice(mem_bytes=1 << 20)
    dev.regs.update({g.LA_DEEP_STATUS: DEEP_DONE, g.MEM_STATUS: 1 << g.MEM_STATUS_READY[0]})
    p = _probe(dev, tmp_path, monkeypatch)
    armed = p.la_deep_arm(rle=False, after_beats=2)
    assert armed["after_beats"] == 2
    got = p.la_deep_collect(timeout_s=0.1, before_beats=2)
    assert got["before_beats"] == 2
    both = p.la_deep_capture(rle=False, before_beats=2, after_beats=2, timeout_s=0.1)
    assert both["after_beats"] == 2


def test_stream_returns_its_documented_shape(tmp_path, monkeypatch):
    monkeypatch.setattr("fprobe.la.time.sleep", lambda s: None)
    rng = random.Random(7)
    dev = FakeStreamDevice(raw_source(rng, 2000), rng=rng)
    p = _probe(dev, tmp_path, monkeypatch)
    got = p.la_stream(str(tmp_path / "s.vcd"), duration_s=0.05, rle=False)
    assert got["samples"] > 0 and got["gaps"] == [] and got["relay"] is None


def test_stream_raw_path_is_kept_and_la_raw_to_vcd_exports_it_identically(tmp_path, monkeypatch):
    monkeypatch.setattr("fprobe.la.time.sleep", lambda s: None)
    rng = random.Random(8)
    dev = FakeStreamDevice(raw_source(rng, 2000), rng=rng)
    p = _probe(dev, tmp_path, monkeypatch)
    raw = str(tmp_path / "s.fplaraw")
    got = p.la_stream(str(tmp_path / "s.vcd"), duration_s=0.05, rle=False, raw_path=raw)
    assert got["raw"] == raw and (tmp_path / "s.fplaraw").exists()
    assert p.la_stream(str(tmp_path / "t.vcd"), duration_s=0.05, rle=False)["raw"] is None   # 指定しなければ一時ファイルは残らない
    assert sorted(x.name for x in tmp_path.iterdir() if x.suffix != ".vcd") == ["s.fplaraw"]
    again = p.la_raw_to_vcd(raw, str(tmp_path / "again.vcd"))
    assert again["complete"] and again["samples"] == got["samples"] and again["rle"] is False and again["channels"] == 16
    assert (tmp_path / "again.vcd").read_bytes() == (tmp_path / "s.vcd").read_bytes()
    named = p.la_raw_to_vcd(raw, str(tmp_path / "named.vcd"), {"3": "rx"})
    assert named["vcd"].endswith("named.vcd") and " rx " in (tmp_path / "named.vcd").read_text()
