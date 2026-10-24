"""fprobe.decode.swd: 発生器 → 解読の往復、並び、ACK、パリティ、状態の追跡、方向、端の条件（ハードウェア不要）。"""

import random
import time

import pytest

from fprobe.decode import decode_swd
from fprobe.decode.synth import SwdBus, Wave

SWCLK, SWDIO, DRIVE = 0, 3, 8                         # Primer 20K のロジアナのチャンネル
F = 1e6
DP, AP = False, True


def bus(rate=F * 8, **kw):
    w = Wave(channels=10, sample_rate_hz=rate, idle={SWCLK: 1, SWDIO: 1}, **kw)
    return w, SwdBus(w, swclk=SWCLK, swdio=SWDIO, drive=DRIVE, clock_hz=F, t=2e-6)


def decode(w, b, start_s=0.0, end_s=None, **kw):
    t = w.build(start_s=start_s, end_s=end_s if end_s is not None else b.t + 2e-6)
    return decode_swd(t, swclk=SWCLK, swdio=SWDIO, **({"drive": None, "turnaround": 1, "orundetect": False} | kw))


def transfers(d):
    return [a.text for a in d.of("transfer")]


def test_a_typical_connection():
    w, b = bus()
    b.jtag_to_swd()
    b.read(DP, 0x0, 0x2BA01477)                       # DPIDR
    b.write(DP, 0x0, 0x1E)                            # ABORT
    b.write(DP, 0x8, 0x00000000)                      # SELECT
    b.write(DP, 0x4, 0x50000000)                      # CTRL/STAT
    b.read(DP, 0x4, 0xF0000000)
    b.write(AP, 0x0, 0x23000012)                      # CSW
    b.write(AP, 0x4, 0xE000ED00)                      # TAR
    b.read(AP, 0xC, 0xDEADBEEF)                       # DRW（先送り: 値は前の読みのもの）
    b.read(DP, 0xC, 0x410FC241)                       # RDBUFF = DRW の値
    b.idle(8)
    d = decode(w, b, drive=DRIVE)
    assert [a.kind for a in d.of("line_reset", "jtag_to_swd")] == ["line_reset", "jtag_to_swd", "line_reset"]
    assert transfers(d) == [
        "DP R DPIDR 0x2BA01477 OK", "DP W ABORT 0x0000001E OK", "DP W SELECT 0x00000000 OK",
        "DP W CTRL/STAT 0x50000000 OK", "DP R CTRL/STAT 0xF0000000 OK", "AP0 W CSW 0x23000012 OK",
        "AP0 W TAR 0xE000ED00 OK", "AP0 R DRW 0xDEADBEEF OK", "DP R RDBUFF 0x410FC241 OK (= AP0 DRW)"]
    assert d.errors() == []
    drw = d.of("transfer")[-2]
    assert drw.fields == {"port": "AP", "rnw": True, "addr": 0xC, "reg": "DRW", "apsel": 0, "ack": "OK",
                          "data": 0xDEADBEEF, "value_of": None}
    assert [a.kind for a in d.between(drw.start, drw.end)] == ["transfer", "request", "ack", "data"]


def test_posted_ap_reads_name_the_register_the_value_belongs_to():
    w, b = bus()
    b.line_reset()
    b.idle()
    b.read(AP, 0x0, 1)
    b.read(AP, 0x4, 2)
    b.read(AP, 0xC, 3)
    b.read(DP, 0xC, 4)
    d = decode(w, b)
    assert [a.fields["value_of"] for a in d.of("transfer")] == [None, "AP0 CSW", "AP0 TAR", "AP0 DRW"]


def test_select_chooses_ap_and_bank_and_dp_bank():
    w, b = bus()
    b.write(DP, 0x8, 0x010000F0)                      # APSEL 1, APBANKSEL 0xF
    b.read(AP, 0xC, 0x24770011)                       # IDR
    b.read(AP, 0x8)                                   # BASE
    b.write(DP, 0x8, 0x00000002)                      # DPBANKSEL 2
    b.read(DP, 0x4, 0x01002927)                       # TARGETID
    d = decode(w, b)
    assert [(a.fields["apsel"], a.fields["reg"]) for a in d.of("transfer")] == [
        (None, "SELECT"), (1, "IDR"), (1, "BASE"), (None, "SELECT"), (None, "TARGETID")]
    assert transfers(d)[1] == "AP1 R IDR 0x24770011 OK"


def test_wait_fault_and_no_response():
    w, b = bus()
    b.read(AP, 0xC, ack=SwdBus.WAIT)
    b.write(AP, 0xC, 5, ack=SwdBus.FAULT)
    b.read(DP, 0x0, ack=SwdBus.NONE)
    b.idle(3)
    b.read(DP, 0x0, 0x0BC11477)
    d = decode(w, b)
    assert [(a.fields["ack"], a.error) for a in d.of("transfer")] == [
        ("WAIT", "wait"), ("FAULT", "fault"), ("NONE", "no_ack"), ("OK", None)]
    assert [a.text for a in d.of("ack")] == ["WAIT", "FAULT", "NONE", "OK"]
    assert transfers(d)[-1] == "DP R DPIDR 0x0BC11477 OK"


def test_data_parity_and_request_parity_errors():
    w, b = bus()
    b.read(DP, 0x0, 0x12345678, bad_data_parity=True)
    b.write(DP, 0x8, 0, bad_request_parity=True, ack=SwdBus.NONE)   # 誤った要求にターゲットは応答しない
    b.idle(2)
    b.write(DP, 0x8, 0xF0)
    d = decode(w, b)
    assert d.of("transfer")[0].error == "parity" and d.of("data")[0].error == "parity"
    assert [a.error for a in d.of("request")] == [None, "request_parity", None]
    assert transfers(d)[-1] == "DP W SELECT 0x000000F0 OK"


def test_wait_with_orundetect_has_a_data_phase():
    w, b = bus()
    b.write(DP, 0x4, 0x50000001)                      # CTRL/STAT: ORUNDETECT
    b.orundetect = True
    b.read(AP, 0xC, 0xFFFFFFFF, ack=SwdBus.WAIT)      # データの相がある（値に意味は無い）
    b.write(AP, 0xC, 0xAAAAAAAA, ack=SwdBus.WAIT)
    b.read(DP, 0x4, 0x50000001)
    d = decode(w, b)
    assert [(a.fields["ack"], a.fields["data"]) for a in d.of("transfer")] == [
        ("OK", 0x50000001), ("WAIT", None), ("WAIT", None), ("OK", 0x50000001)]
    # 追跡しないと、WAIT の後のデータの相を要求と取り違えて最後の読みを失う
    assert len(decode(w, b, orundetect=False).of("transfer")) == 4


def test_turnaround_from_dlcr():
    w, b = bus()
    b.write(DP, 0x8, 0x1)                             # DPBANKSEL 1
    b.write(DP, 0x4, 0x00000100)                      # DLCR: TURNROUND = 1 → 2 サイクル
    b.turnaround = 2
    b.write(DP, 0x8, 0x0)
    b.read(DP, 0x0, 0x2BA01477)
    d = decode(w, b)
    assert transfers(d) == ["DP W SELECT 0x00000001 OK", "DP W DLCR 0x00000100 OK", "DP W SELECT 0x00000000 OK",
                            "DP R DPIDR 0x2BA01477 OK"]


def test_dormant_wakeup_and_targetsel():
    w, b = bus()
    b.host([1] * 51)
    b.sequence16(0xE3BC)                              # SWD-to-DORMANT
    b.dormant_to_swd()
    b.write(DP, 0xC, 0x01002927)                      # TARGETSEL（ACK なし）
    b.read(DP, 0x0, 0x0BC12477)
    d = decode(w, b)
    kinds = [a.kind for a in d if a.kind not in ("transfer", "request", "ack", "data")]
    assert kinds == ["line_reset", "swd_to_dormant", "dormant_alert", "activation", "line_reset"]
    assert d.of("activation")[0].text == "ACTIVATE SWD"
    assert transfers(d) == ["DP W TARGETSEL 0x01002927", "DP R DPIDR 0x0BC12477 OK"]
    assert d.of("transfer")[0].fields["ack"] is None and d.errors() == []


def test_swd_to_jtag_sequence():
    w, b = bus()
    b.line_reset()
    b.sequence16(0xE73C)
    d = decode(w, b)
    assert [a.kind for a in d] == ["line_reset", "swd_to_jtag"]


def test_direction_mismatch_with_the_drive_channel():
    w, b = bus()
    b.read(DP, 0x0, 0x2BA01477)
    w.set(DRIVE, 1, b.t - 20 / (2 * F))               # データの相の途中からプローブも駆動している（衝突）
    b.idle(2)
    d = decode(w, b, drive=DRIVE)
    assert d.of("data")[0].error == "direction" and d.of("transfer")[0].error == "direction"
    assert decode(w, b).errors() == []                # drive を見なければ分からない


def test_capture_starting_mid_packet_and_ending_mid_packet():
    w, b = bus()
    values = [0x11111111 * k for k in range(1, 9)]
    for v in values:
        b.read(AP, 0xC, v)
        b.pause(5e-6)                                 # CMSIS-DAP はパケットの間でクロックを止める
    cut_start = 2e-6 + 30 / F                          # 最初のパケットのデータの途中
    d = decode(w, b, start_s=cut_start, end_s=b.t - 5e-6 - 20 / F)
    data = [a.fields["data"] for a in d.of("transfer") if a.error is None]
    assert data[-6:] == values[1:7]
    last = d.of("transfer")[-1]
    assert last.error == "truncated" and last.end <= d.length


# 2 倍（半周期 = 1 サンプル）は揺れが無いときだけ読める（辺が同じサンプルに重なるとクロックの辺そのものが消える）
@pytest.mark.parametrize("rate, jitter", [(2 * F, 0.0), (2.5 * F, 0.04 / F), (3 * F, 0.08 / F)])
def test_low_oversampling_and_jitter(rate, jitter):
    rng = random.Random(int(rate))
    w, b = bus(rate=rate, jitter=jitter, seed=2)
    values = [rng.getrandbits(32) for _ in range(30)]
    b.line_reset()
    b.idle()
    for v in values:
        b.write(AP, 0x4, v)
        b.read(AP, 0xC, v ^ 0xFFFFFFFF)
    d = decode(w, b, drive=DRIVE)
    assert [a.fields["data"] for a in d.of("transfer")] == [x for v in values for x in (v, v ^ 0xFFFFFFFF)]
    assert d.errors() == []


def test_ten_thousand_transfers_decode_quickly():
    rng = random.Random(9)
    w, b = bus(rate=F * 4)
    for _ in range(10_000):                           # 約 100 万の変化点
        b.read(AP, 0xC, rng.getrandbits(32))
    t = w.build(end_s=b.t + 1e-6)
    t0 = time.perf_counter()
    d = decode_swd(t, swclk=SWCLK, swdio=SWDIO, drive=DRIVE, turnaround=1, orundetect=False)
    elapsed = time.perf_counter() - t0
    assert len(d.of("transfer")) == 10_000
    assert elapsed < 5.0, elapsed                     # 手元で約 0.8 秒


def test_json_of_a_transfer():
    w, b = bus()
    b.read(DP, 0x0, 0x2BA01477)
    j = decode(w, b).to_dict()
    tr = [a for a in j["annotations"] if a["kind"] == "transfer"][0]
    assert tr["fields"]["reg"] == "DPIDR" and tr["value"] == 0x2BA01477 and tr["start_s"] > 0
