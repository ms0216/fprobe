"""DAP の時刻が線上の点の回路（docs/superpowers/specs/2026-11-01-time-accuracy-design.md 段階 S2、ケイパビリティ
DAP_LINE_TIME）: 種類 7 の WAIT の回数・16（PHY の設定）・17（値を返した転送の点）・18（誤りの区間）の解読、サンプラの語の
窓（R6）とポストされた読みの範囲の式、記録の行、JTAG の K 語の確認の設定（bandwidth-observation 2.3）。"""

import pytest

from fprobe import generated as g
from fprobe.collector import add_events, flush_carry
from fprobe.events import (KIND_DAP_CONFIG, KIND_HOST, KIND_SAMPLE, KIND_SAMPLE_BOUND, KIND_SAMPLE_GAP, DapEvent,
                           decode_events, resolve)
from fprobe.recording import Recorder, Recording
from fprobe.sampler import Sampler
from fprobe.timing import DapConfig, access_start_bound, decode_dap_config, sampler_window, swd_header_to_point_ticks
from caps_fakes import FakeDeviceBase, caps_for

M48 = (1 << 48) - 1
OUT = 1                                   # 端子の段（PYNQ-Z2 の IOB の FF。ケイパビリティ dap_output_ticks）


def cfg_payload(half_period=5, idle=2, turnaround=1, data_phase=0, swd_sd=1, jtag_sd=3, jtag=0, enable=1,
                dr_before=0, dr_after=1, ir_before=0, ir_after=5, ir_len=4) -> int:
    """イベント 16 の [119:48]（fprobe_core.vhd の dap_cfg_word と同じ並び）。"""
    return (half_period | idle << 16 | (turnaround - 1) << 24 | data_phase << 26 | (swd_sd & 3) << 27 | jtag_sd << 29
            | (swd_sd >> 2) << 57                                       # SWD の遅れの bit2（0〜7 に広げた分）
            | jtag << 32 | enable << 33 | dr_before << 34 | dr_after << 37 | ir_before << 40 | ir_after << 46
            | (ir_len - 1) << 52)


def record(kind: int, word: int) -> bytes:
    return (kind << 120 | word).to_bytes(16, "little")


def sample_word(t, addr, value, waits=0, ack=1) -> int:
    return waits << 115 | ack << 112 | value << 80 | addr << 48 | t


def bound_word(t, addr, request) -> int:
    return request << 112 | addr << 48 | t


def gap_word(first, end, index, words, ack, sticky, dropped) -> int:
    return dropped << 116 | sticky << 115 | ack << 112 | words << 103 | index << 96 | end << 48 | first


def test_config_payload_is_decoded_field_by_field():
    c = decode_dap_config(1234, cfg_payload(half_period=7, idle=9, turnaround=3, data_phase=1, swd_sd=2, jtag_sd=5,
                                            jtag=1, enable=1, dr_before=2, dr_after=3, ir_before=17, ir_after=33,
                                            ir_len=32))
    assert c == DapConfig(time=1234, half_period=7, idle_cycles=9, turnaround=3, data_phase=True, swd_sample_delay=2,
                          jtag_sample_delay=5, jtag=True, enable=True, dr_before=2, dr_after=3, ir_before=17,
                          ir_after=33, ir_len=32)
    assert decode_dap_config(0, cfg_payload(swd_sd=7)).swd_sample_delay == 7      # bit2 は [57]
    assert decode_dap_config(0, cfg_payload(swd_sd=5)).swd_sample_delay == 5


def test_swd_header_to_point_and_the_access_start_bound():
    assert swd_header_to_point_ticks(2, 1) == 11 * 4 + 2
    assert swd_header_to_point_ticks(5, 4) == 14 * 10 + 5
    swd = decode_dap_config(0, cfg_payload(half_period=5, turnaround=2))
    jtag = decode_dap_config(0, cfg_payload(jtag=1))
    assert access_start_bound(1000, swd, OUT) == (1000 - swd_header_to_point_ticks(5, 2) + OUT, "swd_header_start")
    assert access_start_bound(1000, jtag, 0) == (1000, "jtag_update_dr")
    assert access_start_bound(1000, None, OUT) == (None, None)
    assert access_start_bound(1000, swd, None) == (None, None)            # 端子の段が分からない: 出さない


def test_decoding_needs_line_time_for_the_sample_wait_count():
    data = record(KIND_SAMPLE, sample_word(100, 0x2000_0000, 0xAB, waits=7))
    new, = decode_events(data, line_time=True)
    assert (new.waits, new.ap, new.read, new.register, new.parity_error) == (7, True, True, 0xC, False)
    old, = decode_events(data)                                   # 古い意味: [118:115] は要求、[119] はパリティ
    assert old.waits is None and old.data == 0xAB


def test_new_kinds_are_decoded_without_the_flag():
    data = (record(KIND_DAP_CONFIG, cfg_payload(half_period=3) << 48 | 50)
            + record(KIND_SAMPLE_BOUND, bound_word(60, 0x2000_0004, 0b0110))
            + record(KIND_SAMPLE_GAP, gap_word(70, 90, 17, 3, 1, 1, 0)))
    c, b, gp = decode_events(data)
    assert c.kind == KIND_DAP_CONFIG and c.config.half_period == 3 and c.config.time == 50
    assert (b.kind, b.time, b.address, b.ap, b.read, b.register) == (KIND_SAMPLE_BOUND, 60, 0x2000_0004, False, True, 0x4)
    assert gp.time == 70 and (gp.gap.end, gp.gap.index, gp.gap.words, gp.gap.ack, gp.gap.sticky, gp.gap.dropped) \
        == (90, 17, 3, 1, True, False)


def test_sample_windows_end_at_the_next_sampler_record():
    """R6: 語の窓 = [読みに行った転送の始め（SWD は ACK の後の点 − ヘッダの式）, 次のサンプラの件の点]。並びの最後の語の後端は
    分からない（None）。17・18 は直前の語の窓を閉じる。サンプラ以外の件は窓を閉じない。"""
    hp, trn = 5, 1
    off = swd_header_to_point_ticks(hp, trn)
    data = b"".join([
        record(KIND_DAP_CONFIG, cfg_payload(half_period=hp, turnaround=trn) << 48 | 10),
        record(KIND_SAMPLE, sample_word(1000, 0x2000_0000, 1)),
        record(3, 0x41 << 80 | 1100),                            # UART のバイト（窓に関係しない）
        record(KIND_SAMPLE, sample_word(1500, 0x2000_0004, 2, waits=3)),
        record(KIND_SAMPLE_BOUND, bound_word(2100, 0x2000_0004, 0b1110)),
        record(KIND_SAMPLE, sample_word(3000, 0x2000_0000, 3)),
        record(KIND_SAMPLE_GAP, gap_word(3500, 4200, 16, 1, 4, 0, 0)),
        record(KIND_SAMPLE, sample_word(5000, 0x2000_0000, 4)),
    ])
    out = resolve(decode_events(data, line_time=True), line_time=True, output_ticks=OUT)
    samples = [a for a in out if a.kind == "sample"]
    assert [(a.time, a.time_lo, a.time_hi) for a in samples] == [
        (1000, 1000 - off + OUT, 1500 + OUT), (1500, 1500 - off + OUT, 2100 + OUT), (3000, 3000 - off + OUT, 3500 + OUT),
        (5000, 5000 - off + OUT, None)]
    assert samples[1].detail["waits"] == 3 and samples[0].detail["basis"]["t_hi"] == "swd_returning_ack"
    assert "returning_transfer" in samples[3].detail["unknown"]
    err, = [a for a in out if a.kind == "sample_error"]
    assert (err.time, err.time_hi, err.address, err.value, err.ack) == (3500, 4200 + OUT, 16, 1, 4)
    assert err.time_lo == 3500 - off + OUT and err.detail["sticky"] is False
    assert [a.kind for a in out if a.kind == "dap_config"] == ["dap_config"]


def test_without_a_config_the_lower_bound_is_unknown_and_jtag_uses_the_update_point():
    out = resolve(decode_events(record(KIND_SAMPLE, sample_word(1000, 0x2000_0000, 1))
                                + record(KIND_SAMPLE, sample_word(1300, 0x2000_0000, 2)), line_time=True),
                  line_time=True, output_ticks=0)
    assert out[0].time_lo is None and out[0].time_hi == 1300 and "dap_config" in out[0].detail["unknown"]
    jtag = decode_dap_config(0, cfg_payload(jtag=1))
    out = resolve(decode_events(record(KIND_SAMPLE, sample_word(1000, 0x2000_0000, 1))
                                + record(KIND_SAMPLE, sample_word(1300, 0x2000_0000, 2)), line_time=True),
                  line_time=True, config=jtag, output_ticks=OUT)
    assert (out[0].time_lo, out[0].time_hi) == (1000 + OUT, 1300 + OUT)
    out = resolve(decode_events(record(KIND_SAMPLE, sample_word(1000, 0x2000_0000, 1))
                                + record(KIND_SAMPLE, sample_word(1300, 0x2000_0000, 2)), line_time=True),
                  line_time=True, config=jtag)                                       # 端子の段の申告が無い
    assert (out[0].time_lo, out[0].time_hi) == (None, None) and "dap_output_ticks" in out[0].detail["unknown"]
    assert out[0].detail["basis"]["t_hi"] == "jtag_returning_update_dr"


def test_posted_host_reads_use_the_request_point_with_a_config():
    def host(t, ap, rnw, a, data):
        request = ap | rnw << 1 | (a >> 2) << 2
        return record(KIND_HOST, request << 115 | 1 << 112 | data << 80 | t)
    hp, trn = 4, 1
    data = (record(KIND_DAP_CONFIG, cfg_payload(half_period=hp, turnaround=trn) << 48 | 5)
            + host(900, 0, 0, 0x8, 0)                            # SELECT
            + host(1000, 1, 1, 0xC, 0)                           # DRW の読み（値は次で返る）
            + host(1500, 0, 1, 0xC, 0x1234))                     # RDBUFF
    out = [a for a in resolve(decode_events(data, line_time=True), line_time=True, output_ticks=OUT)
           if a.kind == "ap_read"]
    assert len(out) == 1 and out[0].requested == 1000 and out[0].time == 1500 and out[0].time_hi == 1500 + OUT
    assert out[0].time_lo == 1000 - swd_header_to_point_ticks(hp, trn) + OUT
    old = [a for a in resolve(decode_events(data)) if a.kind == "ap_read"]
    assert old[0].time_lo == 900                                  # 古い意味: 前の転送が下限


def test_sampler_window_reports_its_basis_and_unknowns():
    w = sampler_window(1000, None, None, 31, None)
    assert w["t_lo"] is None and w["t_hi"] is None and w["basis"]["waits"] == 31
    assert set(w["unknown"]) >= {"dap_config", "dap_output_ticks", "returning_transfer", "target_internal"}


def test_recording_rows_carry_the_window_the_error_interval_and_the_config(tmp_path):
    """記録: 語の本体に {t_lo, t_hi, waits}、誤りの区間は sample・error、設定は dap・config。かたまりをまたいで設定を持ち越す。"""
    rec = Recorder(tmp_path / "r", timebase_hz=100_000_000)
    ref = 1 << 40
    hp = 5
    off = swd_header_to_point_ticks(hp, 1)
    batch1 = decode_events(record(KIND_DAP_CONFIG, cfg_payload(half_period=hp) << 48 | 10)
                           + record(KIND_SAMPLE, sample_word(1000, 0x2000_0000, 7))
                           + record(KIND_SAMPLE, sample_word(1500, 0x2000_0000, 8, waits=2)), line_time=True)
    carry = add_events(rec, batch1, ref, line_time=True, output_ticks=OUT)
    assert carry is not None and carry.config.half_period == hp
    batch2 = decode_events(record(KIND_SAMPLE_GAP, gap_word(2000, 2600, 0, 2, 1, 1, 0))
                           + record(KIND_SAMPLE, sample_word(3000, 0x2000_0000, 9)), line_time=True)
    assert carry.sample is not None and carry.sample.time == 1500       # 最後の語は後端が次のかたまりにあるので持ち越す
    carry = add_events(rec, batch2, ref, carry=carry, line_time=True, output_ticks=OUT)
    flush_carry(rec, carry, ref, line_time=True, output_ticks=OUT)      # 記録の終わり: 持ち越した語を後端なしで書く
    rec.flush()
    got = sorted(((e.t & M48, e.stream, e.kind, e.a, e.b, e.body) for e in Recording.open(rec.directory).events()),
                 key=lambda r: r[0])
    assert got[0][:3] == (10, "dap", "config") and got[0][5]["half_period"] == hp
    assert got[1] == (1000, "sample", "word", 0x2000_0000, 7, {"t_lo": 1000 - off + OUT, "t_hi": 1500 + OUT, "waits": 0})
    # かたまりの最後の語の後端は次のかたまりの最初のサンプラの件（誤りの区間 2000）
    assert got[2] == (1500, "sample", "word", 0x2000_0000, 8, {"t_lo": 1500 - off + OUT, "t_hi": 2000 + OUT, "waits": 2})
    t_lo, t_hi = got[3][5]["t_lo"], got[3][5]["t_hi"]
    assert got[3][:5] == (2000, "sample", "error", 0, 2) and (t_lo, t_hi) == (2000 - off + OUT, 2600 + OUT)   # 設定を持ち越した
    assert got[3][5]["sticky"] is True
    assert got[4] == (3000, "sample", "word", 0x2000_0000, 9, {"t_lo": 3000 - off + OUT, "t_hi": None, "waits": 0})
    assert len(got) == 5                                                 # 持ち越した語を二度書かない


def test_a_held_word_closed_after_lost_records_is_marked(tmp_path):
    # かたまりの間で件が欠けた（リングが一周した等）: 持ち越した語の後端は次に読めた件で、確かな上限のままだが印を付ける
    rec = Recorder(tmp_path / "r", timebase_hz=100_000_000)
    ref = 1 << 40
    b1 = decode_events(record(KIND_DAP_CONFIG, cfg_payload() << 48 | 10)
                       + record(KIND_SAMPLE, sample_word(1000, 0x2000_0000, 7)), line_time=True)
    carry = add_events(rec, b1, ref, line_time=True, output_ticks=OUT)
    b2 = decode_events(record(KIND_SAMPLE, sample_word(9000, 0x2000_0000, 8)), line_time=True)
    add_events(rec, b2, ref, carry=carry, line_time=True, output_ticks=OUT, lost_before=True)
    rec.flush()
    words = [e for e in Recording.open(rec.directory).events() if e.kind == "word"]
    assert words[0].body["t_hi"] & M48 == 9000 + OUT and words[0].body["t_hi_after_loss"] is True


class FakeDevice(FakeDeviceBase):
    def __init__(self, line_time=True, check_max=16):
        self.csr = {g.SAMPLER_CHECK: check_max << g.SAMPLER_CHECK_MAX[0] | 1}
        self.caps = caps_for(sampler=True, sys_clock_hz=100_000_000, dap_line_time=line_time)

    def write32(self, address, value):
        self.csr[address] = value

    def write(self, address, words):
        for i, v in enumerate(words):
            self.write32(address + 4 * i, v)

    def read32(self, address):
        if address == g.SAMPLER_CHECK:                            # max は RO（書いても回路の値のまま）
            return self.csr[address] & ~(0x7F << g.SAMPLER_CHECK_MAX[0]) | 16 << g.SAMPLER_CHECK_MAX[0]
        return self.csr.get(address, 0)


def test_check_words_is_explicit_bounded_and_written():
    dev = FakeDevice()
    s = Sampler(dev)
    assert s.check_words_max() == 16
    s.configure([(0x2000_0000, 4)], period_s=1e-4)
    assert dev.csr[g.SAMPLER_CHECK] & 0x7F == 1                   # 既定は語ごと（今までの動き）
    s.configure([(0x2000_0000, 4)], period_s=1e-4, check_words=16)
    assert dev.csr[g.SAMPLER_CHECK] & 0x7F == 16
    with pytest.raises(ValueError):
        s.configure([(0x2000_0000, 4)], period_s=1e-4, check_words=17)
    with pytest.raises(ValueError):
        s.configure([(0x2000_0000, 4)], period_s=1e-4, check_words=0)
    old = FakeDevice(line_time=False)
    s = Sampler(old)
    assert s.check_words_max() == 1
    s.configure([(0x2000_0000, 4)], period_s=1e-4)
    with pytest.raises(ValueError):
        s.configure([(0x2000_0000, 4)], period_s=1e-4, check_words=2)
