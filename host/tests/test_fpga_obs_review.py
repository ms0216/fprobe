"""FPGA 観測のホスト側のレビュー指摘（自己デバッグ、2026-10-25）の再現と回帰。fprobe.fpga_obs・obs_condition。"""
import pytest
from test_fpga_obs import group_record, mark, snapshot, sync, value

from fprobe.fpga_obs import ObsDecoder, compile_match, fit_line, fit_sync
from fprobe.fpga_signals import FpgaSignals
from fprobe.obs_condition import count_condition


def kinds(out):
    return [(s.kind, s.value) for s in out]


def test_after_lost_events_the_same_value_and_the_group_are_reported_again():
    # 欠けた区間の変化は分からない: 欠けの後の最初の値は、同じ値でも「新しい情報」として組と一緒に出す（標本型と同じ）
    d = ObsDecoder(nominal_ratio=1.0)
    assert [k for k, _ in kinds(d.feed([group_record(0), value(0, 1), mark(5, 1), sync(1, 1000)]))] == ["group", "value"]
    d.events_lost()
    out = d.feed([mark(6, 2), group_record(0), value(3, 1), sync(2, 2000)])
    assert kinds(out) == [("group", None), ("value", 1)]


def test_a_lost_count_before_events_were_lost_does_not_leak_into_the_next_gap():
    from test_fpga_obs import overflow
    d = ObsDecoder(nominal_ratio=1.0)
    d.feed([group_record(0), value(0, 1), mark(5, 1), sync(1, 1000), overflow(7)])
    d.events_lost()
    out = d.feed([mark(6, 2), group_record(0), value(3, 2), sync(2, 2000)])
    assert "gap" not in [k for k, _ in kinds(out)]


SIGNALS = FpgaSignals.from_dict({"groups": {"0": [{"name": "flag", "lsb": 0, "width": 1}, {"name": "state", "lsb": 4, "width": 2}]}})


@pytest.mark.parametrize("bad", ["flag=0 and flag rose", "flag=1 and flag fell", "flag rose and flag=0", "flag fell and flag=1"])
def test_a_term_that_contradicts_an_edge_on_the_same_signal_is_refused(bad):
    # 「flag=0 かつ flag が立ち上がった」は成り立たない。黙って flag=0 を捨てて rose にしない
    with pytest.raises(ValueError):
        compile_match(bad, SIGNALS)


def test_consistent_terms_on_the_same_signal_compile():
    assert compile_match("flag=1 and flag rose", SIGNALS) == (0x1, 0x1, 0x1)
    assert compile_match("flag=0 and flag fell", SIGNALS) == (0x1, 0x0, 0x1)


def test_an_empty_condition_is_refused():
    # 空の条件は「常に一致」になりうる（マスク 0）。トリガにしない
    for empty in ("", "   "):
        with pytest.raises(ValueError):
            compile_match(empty, SIGNALS)


def v(t, fields, group=0):
    return {"kind": "value", "time_us": t, "group": group, "fields": fields}


def test_equality_compares_numbers_by_value_not_by_text():
    # compile_match は 0x10・16・列挙名を受ける。数える側も、数は値で比べる（"16" と "0x10" を別物にしない）
    items = [v(0, {"addr": 0}), v(10, {"addr": 16}), v(20, {"addr": 17})]
    assert count_condition(items, "addr=0x10 and addr changed")["count"] == 1
    assert count_condition(items, "addr=16 and addr changed")["count"] == 1
    named = [v(0, {"state": "IDLE"}), v(10, {"state": "0x7"})]            # 名前の無い列挙値は 16 進の文字で来る
    assert count_condition(named, "state=7 and state changed")["count"] == 1
    assert count_condition([v(0, {"state": "IDLE"}), v(10, {"state": "RUN"})], "state=RUN and state changed")["count"] == 1


def test_rose_and_fell_treat_any_zero_spelling_as_off():
    items = [v(0, {"s": "0x0"}), v(10, {"s": "RUN"}), v(20, {"s": "0x0"})]
    assert count_condition(items, "s rose")["count"] == 1
    assert count_condition(items, "s fell")["count"] == 1


def test_hidden_changes_of_a_snapshot_are_counted_in_lost_and_reported_separately():
    items = [v(0, {"s": 1}), {"kind": "hidden", "time_us": 5, "group": 0, "lost": 2}, v(10, {"s": 2})]
    out = count_condition(items, "s changed")
    assert out["lost"] == 2 and out["hidden"] == 1 and out["gaps"] == 0


def test_the_span_is_the_real_first_and_last_time_even_if_the_list_is_not_in_time_order():
    items = [v(500, {"s": 1}), v(100, {"s": 2}), v(300, {"s": 3})]
    out = count_condition(items, "s changed")
    assert (out["t0_us"], out["t1_us"], out["span_s"]) == (100, 500, pytest.approx(400e-6))


def test_two_pairs_with_the_same_target_time_do_not_raise():
    # 同じ対象の時刻の組が 2 つ（傾きが決まらない）: 例外にせず、申告の比に落とす（無ければ直線を作らない）
    assert fit_sync([(100.0, 5.0), (100.0, 6.0)], nominal_ratio=2.0)[0].ratio == 2.0
    assert fit_sync([(100.0, 5.0), (100.0, 6.0)]) == []
    with pytest.raises(ValueError):
        fit_line([(100.0, 5.0), (100.0, 6.0)])


def test_the_slope_of_a_previous_chain_is_forgotten_when_the_clock_source_changes():
    d = ObsDecoder(nominal_ratio=1.0)
    d.feed([group_record(0), value(0, 1), mark(100, 1), value(50, 2), mark(150, 2), sync(1, 1000), sync(2, 1400)])
    assert d._ratio is not None
    d.reset_clock(nominal_ratio=3.0, sync_delay=0.0, sample_delay=0.0)             # FPGA を書き直した / 開き直した
    assert d._ratio is None and d.nominal_ratio == 3.0


def test_marks_across_the_48_bit_wrap_keep_a_straight_line():
    wrap = 1 << 48
    d = ObsDecoder(nominal_ratio=1.0)
    out = d.feed([group_record(0), value(0, 1), mark(100, 1), value(50, 2), mark(150, 2), value(10, 3), mark(30, 3),
                  sync(1, wrap - 100), sync(2, 100), sync(3, 140)])        # 2 つ目の印から、48 ビットの値が折り返した後（生の値は小さくなる）
    times = [s.time_fprobe for s in out if s.kind == "value" and s.time_fprobe is not None]
    assert times == sorted(times) and len(times) == 3
    assert times[-1] - times[0] < 1000                                              # 折り返しを数で飛ばさない
    assert d._ratio == pytest.approx(1.0, abs=0.05)                                   # 直線の傾き（対象 1 周期 = fprobe 1 刻み）


# --- FpgaObs.open の後始末・待ち時間（エンジンのレビュー指摘） ---------------------------------------------------------
from test_fpga_obs import FakeDevice  # noqa: E402

from fprobe import generated as g  # noqa: E402
from fprobe.fpga_obs import FpgaObs  # noqa: E402


class SilentDevice(FakeDevice):
    """ID が揃わず、stuck にもならない（エンジンが答えを出さない）デバイス。"""

    def write32(self, address, v):
        super().write32(address, v)
        if address == g.OBS_CONTROL and v & 1:
            self.regs[g.OBS_STATUS] = 1 << g.OBS_STATUS_RUNNING[0]
            self.regs[g.OBS_ID] = 0


def test_open_that_times_out_stops_the_engine_so_it_does_not_keep_driving_the_lines():
    dev = SilentDevice()
    with pytest.raises(TimeoutError):
        FpgaObs(dev).open(chain={}, user_ir=2, half_period=2, words=1, timeout_s=0.05)
    controls = [v for a, v in dev.writes if a == g.OBS_CONTROL]
    assert controls[-1] == 0, f"open が例外で抜けたのに CONTROL が {controls[-1]} のまま（線を駆動し続ける）"


def test_open_that_fails_to_read_the_declaration_stops_the_engine():
    class Dying(FakeDevice):
        def read32(self, address):
            if address == g.OBS_READ_VALUE and any(a == g.OBS_CMD_PUSH for a, _ in self.writes):
                raise OSError("link lost")
            return super().read32(address)
    dev = Dying()
    with pytest.raises(OSError):
        FpgaObs(dev).open(chain={}, user_ir=2, half_period=5)
    assert [v for a, v in dev.writes if a == g.OBS_CONTROL][-1] == 0


def test_the_wait_for_the_id_is_long_enough_for_the_scans_the_caller_asked_for(monkeypatch):
    # id_scans を大きく渡したのに、ホストの待ち（40 スキャン）が先に切れて、エンジンが走ったままになっていた
    seen = {}
    obs = FpgaObs(FakeDevice())
    real = obs._wait

    def spy(done, timeout_s, what):
        seen.setdefault(what, timeout_s)
        return real(done, timeout_s, what)
    monkeypatch.setattr(obs, "_wait", spy)
    obs.open(chain={}, user_ir=2, half_period=1000, words=64, id_scans=200)
    assert seen["ID を読めない"] >= (200 + 8) * obs.scan_s


def test_close_estimates_the_scan_length_from_the_engine_settings_not_from_this_object():
    # デーモンは close のたびに新しい FpgaObs（scan_s = 0）を作る。遅い TCK・大きい words の 1 スキャンが 1 秒を超えても待てること
    dev = FakeDevice()
    dev.regs[g.OBS_HALF_PERIOD] = 65535
    dev.regs[g.OBS_WORDS] = 256
    obs = FpgaObs(dev)
    assert obs.scan_s == 0
    assert obs.scan_seconds() > 1.0


def test_reopening_during_a_recording_resets_the_collector_decoder_clock(tmp_path, monkeypatch):
    # 記録中に観測を開き直す（FPGA を書き直した等）: 取り込みの復号器は、前の回路の傾き・覚えている印を捨てて、新しい申告で数え直す
    from types import SimpleNamespace

    from fprobe.mcp_server import Probe
    monkeypatch.setenv("FPROBE_HOME", str(tmp_path))
    dev = FakeDevice()
    dev.caps = Capabilities(fpga_obs=True, timebase_hz=100_000_000)
    probe = Probe("unused")
    probe._session = SimpleNamespace(device=dev)
    decoder = ObsDecoder(nominal_ratio=9.0)
    decoder._ratio = 7.0
    probe._collector = SimpleNamespace(obs=decoder)
    probe._open_obs({"chain": {}, "user_ir": 2, "half_period": 5, "words": 8, "sync_period": 10})
    assert decoder._ratio is None
    assert decoder.nominal_ratio == pytest.approx(100_000_000 / 50_000_000)        # FakeDevice の CLOCK_HZ = 50 MHz
    assert (decoder.sync_delay, decoder.sample_delay) == (6.5, 1.0)


from fprobe.caps import Capabilities  # noqa: E402


@pytest.mark.parametrize("ir_len", [0, 33, 63])
def test_ir_len_outside_1_to_32_is_refused_instead_of_being_clamped_silently(ir_len):
    # エンジンは 0 を 1、33〜63 を 32 に黙って丸める（対象の IR が正しく書かれない）。ホストで断る
    with pytest.raises(ValueError, match="ir_len"):
        FpgaObs(FakeDevice()).open(chain={"ir_len": ir_len}, user_ir=2, half_period=5)


def test_a_new_chain_after_the_48_bit_wrap_continues_the_time():
    """別の鎖の最初の印が折り返しの後でも、前の鎖から続く時刻にする（鎖ごとに数え直すと時刻が戻る）"""
    wrap = 1 << 48
    d = ObsDecoder(nominal_ratio=1.0)
    out = d.feed([group_record(0), value(0, 1), mark(100, 1), sync(1, wrap - 100),
                  value(0, 2), mark(100, 2), sync(2, 100)])                   # 2 本目の鎖の印は折り返しの後
    times = [s.time_fprobe for s in out if s.kind == "value" and s.time_fprobe is not None]
    assert len(times) == 2 and times == sorted(times) and times[1] - times[0] < 1000


def test_snapshots_across_the_48_bit_wrap_keep_the_time_increasing():
    wrap = 1 << 48
    d = ObsDecoder()
    out = d.feed([snapshot(1, 0x11, wrap - 100), snapshot(1, 0x12, 50), snapshot(1, 0x13, 150)])
    times = [s.time_fprobe for s in out]
    assert times == sorted(times) and times[-1] - times[0] == 250
