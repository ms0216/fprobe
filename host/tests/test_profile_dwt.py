"""fprobe.profile.dwt: DWT / ITM / TPIU の設定値と SWO の帯域の見積もり（手計算の例）。"""

import pytest

from fprobe.profile import dwt
from fprobe.profile.dwt import plan_itm_sampling, plan_pcsr_sampling


def writes_to(plan, address):
    return [w for w in plan.writes if w.address == address]


def test_16mhz_10khz_2mbaud():
    # 目標の間隔 1600 サイクル → 64 × 16 = 1024 では足りず 1024 × 2 = 2048（目標より速くしない）
    plan = plan_itm_sampling(16e6, 10_000, 2_000_000)
    assert (plan.cyctap, plan.postpreset, plan.interval_cycles) == (1, 1, 2048)
    assert plan.rate_hz == pytest.approx(7812.5)
    assert (plan.acpr, plan.swo_baud, plan.baud_error) == (7, 2_000_000, 0)
    assert plan.bytes_per_s == pytest.approx(5 * 7812.5)
    assert plan.utilization == pytest.approx(5 * 7812.5 * 10 / 2e6)
    assert plan.fits and not plan.fitted and plan.warnings == []
    ctrl = writes_to(plan, dwt.DWT_CTRL)
    assert [w.value for w in ctrl] == [0xE23, 0x1E23]                 # PCSAMPLENA は 2 回目で立てる
    assert all(w.mask == 0x007FFFFF for w in ctrl)
    assert writes_to(plan, dwt.ITM_TCR)[0].value == 0x1000D
    assert writes_to(plan, dwt.TPIU_ACPR)[0].value == 7
    assert writes_to(plan, dwt.TPIU_SPPR)[0].value == 2
    assert writes_to(plan, dwt.ITM_LAR)[0].value == 0xC5ACCE55
    demcr = writes_to(plan, dwt.DEMCR)[0]
    assert (demcr.value, demcr.mask) == (1 << 24, 1 << 24)            # 読み・変え・書き
    assert plan.writes[0].address == dwt.DEMCR and plan.writes[-1].address == dwt.DWT_CTRL


def test_exception_trace_and_timestamps_cost_bandwidth():
    plan = plan_itm_sampling(16e6, 10_000, 2_000_000, exception_trace=True, exception_rate_hz=1000, timestamps=True)
    # PC 5 B × 7812.5 + 例外 3 B × 3 × 1000 + タイムスタンプ（差 2048 → 12 ビット → 1 + 2 = 3 B）× パケット数
    packets = 7812.5 + 3 * 1000
    assert plan.bytes_per_s == pytest.approx(5 * 7812.5 + 9 * 1000 + 3 * packets)
    assert writes_to(plan, dwt.DWT_CTRL)[1].value == 0x1E23 | 1 << 16
    assert writes_to(plan, dwt.ITM_TCR)[0].value == 0x1000F


def test_bandwidth_overflow_lengthens_interval():
    plan = plan_itm_sampling(168e6, 100_000, 2_000_000)
    # 2048 サイクル（82 kHz）では 205% → 0.75 に収まる最短の 1024 × 6 = 6144 サイクル
    assert plan.interval_cycles == 6144 and plan.fitted and plan.fits
    assert plan.utilization == pytest.approx(5 * 168e6 / 6144 * 10 / 2e6)
    assert any("帯域" in w for w in plan.warnings)


def test_bandwidth_cannot_fit():
    plan = plan_itm_sampling(168e6, 100_000, 9600)
    assert not plan.fits and plan.interval_cycles == 16384
    assert plan.utilization > 0.75


def test_rate_out_of_range_is_clamped():
    fast = plan_itm_sampling(16e6, 1e6, 16e6, headroom=1.0)            # 帯域では延ばさない条件で
    assert fast.interval_cycles == 64 and any("速すぎ" in w for w in fast.warnings)
    slow = plan_itm_sampling(16e6, 10, 2e6)
    assert slow.interval_cycles == 16384 and any("遅すぎ" in w for w in slow.warnings)


def test_baud_error_warning():
    plan = plan_itm_sampling(16e6, 1000, 3_000_000)                   # 16 / 3 = 5.33 → ACPR 4 → 3.2 Mbaud
    assert plan.acpr == 4 and plan.swo_baud == pytest.approx(3.2e6)
    assert plan.baud_error == pytest.approx(0.0667, abs=1e-3)
    assert any("SWO の速度" in w for w in plan.warnings)


def test_separate_trace_clock():
    plan = plan_itm_sampling(64e6, 1000, 2_000_000, traceclk_hz=16e6)
    assert plan.acpr == 7


def test_as_dict_is_json_ready():
    import json
    d = plan_itm_sampling(16e6, 10_000, 2_000_000).as_dict()
    json.dumps(d)
    assert d["method"] == "itm" and d["writes"][0]["address"] == dwt.DEMCR


def test_pcsr_plan():
    plan = plan_pcsr_sampling(5000)
    assert plan.method == "pcsr"
    assert plan.sampler_ranges == [(dwt.DWT_PCSR, 4)]
    assert plan.period_s == pytest.approx(1 / 5000)
    assert [w.address for w in plan.writes] == [dwt.DEMCR]
    with pytest.raises(ValueError):
        plan_pcsr_sampling(0)


def test_swo_plan_alone_is_the_tpiu_part_of_the_itm_plan():
    swo = dwt.plan_swo(16e6, 2_000_000)
    assert (swo.acpr, swo.baud, swo.baud_error, swo.warnings) == (7, 2_000_000, 0, ())
    itm = plan_itm_sampling(16e6, 10_000, 2_000_000)
    assert [w for w in itm.writes if w.address in (dwt.TPIU_CSPSR, dwt.TPIU_ACPR, dwt.TPIU_SPPR, dwt.TPIU_FFCR)]         == list(swo.writes)
    with pytest.raises(ValueError):
        dwt.plan_swo(16e6, 0)


def test_itm_port_writes_only_touch_the_port_and_enable_bits():
    writes = {w.address: w for w in dwt.itm_port_writes(31)}
    assert writes[dwt.ITM_TER].value == writes[dwt.ITM_TER].mask == 1 << 31            # ほかのポートは残す
    assert writes[dwt.ITM_TCR].mask == 0b101                                           # ITMENA・SYNCENA だけ
    assert writes[dwt.DEMCR].mask == dwt.DEMCR_TRCENA
    with pytest.raises(ValueError):
        dwt.itm_port_writes(32)
