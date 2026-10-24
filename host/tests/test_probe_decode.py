"""Probe の解読の道具: 積み重ねの la_decode、la_decoders、la_decoder_load、la_export、la_load（偽の機器）。"""

import textwrap
from types import SimpleNamespace

import pytest

from fprobe.decode import decoders, unregister
from fprobe.decode.modbus import modbus_crc
from fprobe.decode.synth import Wave, can, uart
from fprobe.mcp_server import Probe
from fprobe.sigrok import read_sr

BAUD = 19200
F8N1 = {"data_bits": 8, "parity": "none", "stop_bits": 1, "invert": False, "lsb_first": True}   # UART の枠の設定（必須）


@pytest.fixture
def probe(tmp_path, monkeypatch):
    monkeypatch.setenv("FPROBE_HOME", str(tmp_path))
    p = Probe("unused")
    p._session = SimpleNamespace(device=None)
    yield p
    if p._recorder is not None:
        p.recording_stop()
    p._stop_observing()


def modbus_capture():
    def frame(*body):
        crc = modbus_crc(body)
        return bytes(body) + bytes([crc & 0xFF, crc >> 8])
    w = Wave(channels=10, sample_rate_hz=BAUD * 16, idle={5: 1})
    t = uart(w, 5, frame(1, 3, 0, 0, 0, 1), 1e-3, baud=BAUD) + 5e-3
    t = uart(w, 5, frame(1, 3, 2, 0x12, 0x34), t, baud=BAUD)
    return w.build(end_s=t + 1e-3)


def test_stacked_decode_uses_the_cached_lower_decoding(probe):
    probe._keep_capture(modbus_capture(), "la.capture")
    out = probe.la_decode("modbus", {"uart": {"channel": 5, "baud": BAUD, **F8N1}})
    assert [a["kind"] for a in out["annotations"]] == ["request", "response"]
    assert out["annotations"][1]["fields"]["values"] == [0x1234]
    assert out["params"]["uart"]["channel"] == 5
    lower = probe._decodings
    assert any(key[1] == "uart" for key in lower)                    # 下の解読も覚えて、次の読みに使う
    with pytest.raises(ValueError):
        probe.la_decode("modbus", {"channel": 5})                    # 下の設定のキーが無い


def test_default_kinds_come_from_the_registry(probe):
    w = Wave(channels=10, sample_rate_hz=500_000 * 16, idle={2: 1})
    t = can(w, 2, 12 / 500_000, bitrate=500_000, can_id=0x123, data=b"\x01")
    probe._keep_capture(w.build(end_s=t + 1e-5), "la.capture")
    out = probe.la_decode("can", {"channel": 2, "bitrate": 500_000, "sample_point": 0.75, "invert": False})
    assert out["counts"]["frame"] == 1 and out["matched"] == out["total"]
    probe._keep_capture(w.build(end_s=t + 1e-5), "la.capture")
    swd = probe.la_decode("swd", {"swclk": 0, "swdio": 3, "drive": None, "turnaround": 1,
                                     "orundetect": False})
    assert swd["matched"] == 0                                      # SWD は transfer だけ（変わらない振る舞い）
    # 絞った種類を返す: 画面は「件数と表の食い違い」を、既定の種類の外として説明する
    assert swd["kinds"] == ["transfer"] and out["kinds"] is None


def test_decoders_list_and_user_decoder_file(probe, tmp_path):
    listed = {d["name"]: d for d in probe.la_decoders()["decoders"]}
    assert {"uart", "can", "onewire", "jtag", "i2s", "parallel", "manchester", "spiflash", "eeprom24", "modbus",
            "jtag_arm"} <= set(listed)
    assert listed["modbus"]["lower"] == ["uart"] and listed["jtag"]["default_kinds"] == ["ir", "dr", "reset"]
    path = tmp_path / "count.py"
    path.write_text(textwrap.dedent('''
        from fprobe.decode import Annotation, decoder

        @decoder("frame_count", lower=("modbus",), summary="フレームを数える")
        def frame_count(lower, *, label="n"):
            a = lower.annotations
            return lower.derive("frame_count", [Annotation(a[0].start, a[-1].end, "count", f"{label}={len(a)}",
                                                           value=len(a))], {"label": label})
    '''), encoding="utf-8")
    try:
        loaded = probe.la_decoder_load(str(path))
        assert loaded["loaded"] == ["frame_count"] and loaded["origin"] == str(path.resolve())
        probe._keep_capture(modbus_capture(), "la.capture")
        out = probe.la_decode("frame_count", {"modbus": {"uart": {"channel": 5, "baud": BAUD, **F8N1}}, "label": "k"})
        assert out["annotations"][0]["text"] == "k=2"                # 3 段の積み重ね
        path.write_text(path.read_text(encoding="utf-8").replace('f"{label}=', 'f"{label}:'), encoding="utf-8")
        probe.la_decoder_load(str(path))                            # 読み直すと前の結果を使わない
        out = probe.la_decode("frame_count", {"modbus": {"uart": {"channel": 5, "baud": BAUD, **F8N1}}, "label": "k"})
        assert out["annotations"][0]["text"] == "k:2"
        with pytest.raises(FileNotFoundError):
            probe.la_decoder_load(str(tmp_path / "missing.py"))
    finally:
        if "frame_count" in {d.name for d in decoders()}:
            unregister("frame_count")


def test_export_and_load_sigrok_and_vcd(probe, tmp_path):
    with pytest.raises(RuntimeError):
        probe.la_export(str(tmp_path / "a.sr"))
    tr = modbus_capture()
    probe._keep_capture(tr, "la.capture")
    info = probe.la_export(str(tmp_path / "a.sr"), channels=[5, 8], names={"5": "rx"})
    assert info["format"] == "sr" and info["samples"] == tr.length and info["channels"] == [5, 8]
    back, names = read_sr(tmp_path / "a.sr")
    assert names == {0: "rx", 1: "probe_drives_swdio"} and back.length == tr.length
    vcd = probe.la_export(str(tmp_path / "a.vcd"), format="vcd")
    assert vcd["format"] == "vcd" and (tmp_path / "a.vcd").read_text().startswith("$timescale")
    with pytest.raises(ValueError):
        probe.la_export(str(tmp_path / "a.bin"), format="bin")
    probe.la_export(str(tmp_path / "full.sr"))
    loaded = probe.la_load(str(tmp_path / "full.sr"))
    assert loaded["capture"]["source"] == "file" and loaded["capture"]["length"] == tr.length
    assert loaded["names"]["5"] == "ch5"
    out = probe.la_decode("uart", {"channel": 5, "baud": BAUD, **F8N1})
    assert out["capture"]["id"] == loaded["capture"]["id"] and out["errors"] == 0
