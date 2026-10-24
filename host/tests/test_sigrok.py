"""fprobe.sigrok: .sr（srzip 第 2 版）の書き出しと読み込み（ハードウェア不要）。"""

import zipfile
from array import array

import pytest

import fprobe.sigrok as sigrok
from fprobe.decode import decode
from fprobe.decode.synth import Wave, uart
from fprobe.la import Capture, Transitions
from fprobe.sigrok import read_sr, samplerate_string, write_sr


def test_samplerate_strings_match_libsigrok():
    assert samplerate_string(100e6) == "100 MHz" and samplerate_string(1e9) == "1 GHz"
    assert samplerate_string(250e3) == "250 kHz" and samplerate_string(1_500_000) == "1500 kHz"
    assert samplerate_string(33) == "33 Hz"


def test_layout_of_the_zip(tmp_path):
    cap = Capture([0, 1, 3, 3, 2, 0x200], trigger=1, trigger_time=0, sample_period_s=1e-8, channels=10)
    info = write_sr(cap, tmp_path / "a.sr", names={0: "clk"})
    with zipfile.ZipFile(tmp_path / "a.sr") as z:
        assert z.read("version") == b"2"
        meta = z.read("metadata").decode()
        assert "capturefile=logic-1" in meta and "total probes=10" in meta and "samplerate=100 MHz" in meta
        assert "probe1=clk" in meta and "probe10=dap_busy" in meta and "unitsize=2" in meta
        assert z.read("logic-1-1") == array("H", [0, 1, 3, 3, 2, 0x200]).tobytes()
    assert info["data_files"] == 1 and info["unitsize"] == 2


def test_round_trip_in_chunks_and_channel_subset(tmp_path, monkeypatch):
    monkeypatch.setattr(sigrok, "CHUNK", 1000)
    w = Wave(channels=8, sample_rate_hz=115200 * 16, idle={5: 1})
    end = uart(w, 5, b"sigrok!", 1e-4, baud=115200)
    w.set(2, 1, 3e-4)
    tr = w.build(end_s=end + 1e-4)
    info = write_sr(tr, tmp_path / "u.sr")
    assert info["data_files"] == -(-tr.length // 1000)
    back, names = read_sr(tmp_path / "u.sr")
    assert (back.length, back.channels, list(back.values), list(back.starts)) == \
        (tr.length, tr.channels, list(tr.values), list(tr.starts))
    assert back.sample_period_s == pytest.approx(tr.sample_period_s) and names[0] == "ch0"
    d = decode("uart", back, channel=5, baud=115200, data_bits=8, parity="none", stop_bits=1, invert=False,
               lsb_first=True)
    assert bytes(a.value for a in d.of("data")) == b"sigrok!"
    write_sr(tr, tmp_path / "s.sr", channels=[5, 2])
    sub, names = read_sr(tmp_path / "s.sr")
    assert sub.channels == 2 and names == {0: "ch5", 1: "ch2"}
    assert [sub.level_at(0, i) for i in (0, tr.length - 1)] == [1, 1] and sub.level_at(1, tr.length - 1) == 1
    with pytest.raises(ValueError):
        write_sr(tr, tmp_path / "x.sr", channels=[9])


def test_single_file_layout_is_also_read(tmp_path):
    path = tmp_path / "old.sr"
    with zipfile.ZipFile(path, "w") as z:
        z.writestr("version", "2")
        z.writestr("metadata", "[device 1]\ncapturefile=logic-1\ntotal probes=3\nsamplerate=1 MHz\nunitsize=1\n")
        z.writestr("logic-1", bytes([0, 0, 5, 5, 1]))
    tr, _ = read_sr(path)
    assert (list(tr.values), list(tr.starts), tr.length, tr.sample_period_s) == ([0, 5, 1], [0, 2, 4], 5, 1e-6)
    assert isinstance(tr, Transitions)
