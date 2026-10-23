"""fprobe.diagnose: 接続の診断（偽の CMSIS-DAP の層・JTAG の模型で。機器は使わない）。"""

from coresight_fakes import ARM, add_component, add_rom_table, stm32f401_words
from dap_fakes import FakeDapLink, FakeMemAp, FakeTarget
from jtag_fakes import FakeChain, Tap

from fprobe.diagnose import diagnose
from fprobe.raw_dap import RawDap

GOOD_VOLTS = {"volts": 3.29, "over_range": False, "live": True, "samples": 10, "errors": 0}


def run(target, volts=GOOD_VOLTS, pins=0b1000_0011, **kw):
    link = FakeDapLink(target, pins=pins)
    return diagnose(RawDap(link), voltage=lambda: dict(volts), pins=lambda: link.pin_access(0, 0), **kw)


def step(d, name):
    return next(s for s in d.steps if s.name == name)


def test_a_healthy_stm32_is_identified_and_a_connect_is_suggested():
    d = run(FakeTarget(aps={0: FakeMemAp(words=stm32f401_words())}))
    assert d.ok and d.protocol == "swd" and d.frequency_hz == 1_000_000
    assert d.part.name == "STM32F401xD/E" and d.core["core"] == "Cortex-M4" and d.core["halted"]
    assert d.connect == {"target": "stm32f401xe", "frequency_hz": 1_000_000}
    assert [s.status for s in d.steps if s.status != "ok"] == []
    out = d.as_dict()
    assert out["verdict"].startswith("接続できる（STM32F401xD/E") and out["aps"][0]["kind"] == "mem-ap"
    assert out["aps"][0]["rom_table"]["children"][0]["name"] == "SCS (Cortex-M4)"


def test_voltage_is_reported_as_measured_without_a_threshold_verdict():
    # 「1.0 V 未満なら電源が入っていない」と判定しない（計測の配線が外れていても 0 V になる。2026-10-18 の実機）。
    # 測った値と fprobe の入出力の電圧を事実として出し、結論には測った値を添える
    off = dict(GOOD_VOLTS, volts=0.02)
    d = run(FakeTarget(powered=False), volts=off, pins=0b0000_0001)
    v = step(d, "voltage")
    assert not d.ok and v.status == "ok" and "0.02 V" in v.message and "3.3 V" in v.message
    assert "電源が入っていない" not in v.message
    assert step(d, "pins").status == "warn"
    assert step(d, "pins").message == "nRESET = Low / SWDIO = Low"         # 原因の候補（プルアップ・短絡等）は並べない
    assert step(d, "swd").status == "fail" and step(d, "jtag").status == "fail"
    assert d.verdict().startswith("接続できない: ") and "Vtarget = 0.02 V" in d.verdict()
    for volts in (1.2, 5.0):
        assert step(run(FakeTarget(), volts=dict(GOOD_VOLTS, volts=volts)), "voltage").status == "ok"


def test_a_slow_link_is_found_by_lowering_the_speed():
    d = run(FakeTarget(max_hz=500_000, aps={0: FakeMemAp(words=stm32f401_words())}), frequency_hz=4_000_000)
    assert d.ok and d.frequency_hz == 400_000 and step(d, "swd").status == "warn"
    msg = step(d, "swd").message                                   # 事実（頼んだ速さで応答しなかった）だけ。原因は推定しない
    assert "4,000,000 Hz では応答せず" in msg and "信号品質" not in msg and d.connect["frequency_hz"] == 400_000


def test_rp2040_is_reached_through_targetsel():
    words = {0x40000000: 0x10002927, 0xE000ED00: 0x410CC601}
    add_rom_table(words, 0xE00FF000, [0xE000E000], part=0x4C0)
    add_component(words, 0xE000E000, 0xE, ARM, 0x008)
    target = FakeTarget(dpidr=0x0BC12477, targetid=0x01002927, multidrop=True,
                        aps={0: FakeMemAp(words=words, readable=((0xE0000000, 0xE0100000),))})
    d = run(target)
    attempts = step(d, "swd").data["attempts"]
    assert d.ok and "targetsel 0x01002927" in str(attempts[-1]["method"])
    # 複数ドロップの部品は TARGETSEL を先に試す: TARGETSEL の無い線では DP が全部同時に応答し、書き込みが救済の DP にも
    # 届く（2026-10-22 の実機。チップがリセットされたまま止まった）。素の JTAG→SWD より前に TARGETSEL で選ぶ
    assert attempts[0]["method"].startswith("targetsel")
    assert d.dp["targetid"]["designer"] == "Raspberry Pi"
    assert d.part.name == "RP2040" and d.connect["target"] == "rp2040" and d.core["core"] == "Cortex-M0+"


def test_jtag_only_targets_are_found_on_the_chain():
    target = FakeTarget(swd=False, jtag_chain=FakeChain([Tap(4, 0x4BA00477), Tap(5, 0x06433041)]),
                        jtag_dp_index=0, aps={0: FakeMemAp(words=stm32f401_words())})
    d = run(target)
    assert step(d, "swd").status == "skip" and step(d, "jtag").status == "ok"
    assert d.ok and d.protocol == "jtag" and d.connect["jtag_chain"] == [4, 5]
    assert d.jtag.devices[1].name == "STM32 boundary scan"


def test_a_chain_without_a_jtag_dp_is_explained():
    target = FakeTarget(swd=False, jtag_chain=FakeChain([Tap(8, 0x0000081B)]))
    d = run(target)
    assert not d.ok and "Gowin" in step(d, "jtag").message and "JTAG-DP が無い" in step(d, "jtag").message


def test_lockup_suggests_connecting_under_reset():
    words = stm32f401_words()
    words[0xE000EDF0] = 1 << 19 | 1
    d = run(FakeTarget(aps={0: FakeMemAp(words=words)}))
    assert step(d, "core").status == "warn" and d.connect["connect_mode"] == "under-reset"


def test_debug_power_that_never_comes_up():
    d = run(FakeTarget(power_ack=False))
    assert not d.ok and step(d, "power").status == "fail" and "CDBGPWRUPACK" in step(d, "power").message


def test_nordic_approtect_and_a_disabled_mem_ap():
    ctrl_ap = FakeMemAp(idr=0x02880000, extra={0x0C: 0})
    locked = FakeMemAp(csw=0x03000000, words={})
    d = run(FakeTarget(aps={0: locked, 1: ctrl_ap}))
    assert "APPROTECT" in step(d, "protection").message
    assert step(d, "ap_disabled").status == "warn"


def test_adiv6_aps_are_found_from_baseptr():
    ap_words = {0x40000000: 0x00004927, 0xE000ED00: 0x410FD210}
    add_rom_table(ap_words, 0xE00FF000, [0xE000E000], cls=1, part=0x4C9)
    add_component(ap_words, 0xE000E000, 0xE, ARM, 0x00C)
    root: dict[int, int] = {}
    add_rom_table(root, 0x0000, [0x2000], cls=9)
    window: dict[int, int] = {}
    add_component(window, 0, 9, ARM, 0x9E3, devarch=0x47700A17)
    mem_ap = FakeMemAp(idr=0x34770008, base=0xE00FF003, words=ap_words, extra=window)
    target = FakeTarget(dpidr=0x4C013477, adi_v6=True, baseptr=0x00000001, aps={0x2000: mem_ap}, apb_words=root)
    d = run(target)
    assert d.ok, [s.as_dict() for s in d.steps]
    assert d.aps[0]["ap"] == 0x2000 and d.part.name == "RP2350" and d.core["core"] == "Cortex-M33"


def test_protection_hook_results_become_a_step():
    seen = []

    def protection(part, read):
        seen.append(read(0x40023C14))
        return {"status": "warn", "message": "RDP Level 1", "level": 1}
    d = run(FakeTarget(aps={0: FakeMemAp(words=stm32f401_words())}), protection=protection)
    assert seen == [0x0FFFAAED]
    assert step(d, "protection").message == "RDP Level 1" and step(d, "protection").data == {"level": 1}
    assert "ただし: RDP Level 1" in d.verdict()


def test_without_voltage_or_pins_those_steps_are_skipped():
    d = diagnose(RawDap(FakeDapLink(FakeTarget(aps={0: FakeMemAp(words=stm32f401_words())}))))
    assert step(d, "voltage").status == "skip" and step(d, "pins").status == "skip" and d.ok


def test_a_responding_target_with_zero_volts_is_both_facts():
    """ターゲットが SWD に応答し、電圧の計測は約 0 V（実機で補助の Pico の計測の線が外れていた。2026-10-18）。
    どちらも事実として並べ、どちらが正しいかは推定しない。"""
    d = run(FakeTarget(), volts=dict(GOOD_VOLTS, volts=0.08))
    step = next(s for s in d.steps if s.name == "voltage")
    assert step.status == "ok" and "0.08 V" in step.message, step
    assert d.ok, [s.as_dict() for s in d.steps if s.status == "fail"]


def test_a_sleeping_core_is_attached_without_reset():
    """WFI で眠っているのは RTOS のアイドルのふつうの姿で、そのまま接続できる。リセットを勧めると、止めると壊れる
    ターゲット（無線）を壊す（2026-10-18、XIAO nRF52840 の Zephyr で under-reset を勧めていた）。"""
    words = stm32f401_words()
    words[0xE000EDF0] = 1 << 18 | 1
    d = run(FakeTarget(aps={0: FakeMemAp(words=words)}))
    assert step(d, "core").status == "ok" and "眠っている" in step(d, "core").message
    assert "connect_mode" not in d.connect


def test_verdict_does_not_say_connectable_when_a_step_failed():
    # 実機（2026-10-22、RP2040 に TARGETSEL なし）: DP は応答したが AP が見つからず ok = false なのに、結論が
    # 「接続できる」だった。結論は ok と食い違わない（失敗した段があれば「接続できない」から書く）
    d = run(FakeTarget(aps={}))
    assert not d.ok and step(d, "aps").status == "fail"
    assert d.verdict().startswith("接続できない: AP が見つからない")


def test_swd_failure_messages_are_the_acks_without_guessed_causes():
    # 「配線・GND・信号品質・低消費電力」等の原因の候補を並べない。どの ACK だったかの事実だけ
    d = run(FakeTarget(powered=False), volts=dict(GOOD_VOLTS, volts=0.02), pins=0b0000_0001)
    msg = step(d, "swd").message
    assert msg.startswith("SWD に応答が無い") and not any(w in msg for w in ("配線", "GND", "電源", "信号品質"))
    over = run(FakeTarget(), volts=dict(GOOD_VOLTS, over_range=True))
    assert step(over, "voltage").message == "ターゲットの電圧が測れる上限を超えている"


def test_a_rescue_dp_that_answers_without_targetsel_is_never_powered_up():
    # 実機（2026-10-22、Shrike の RP2040）: TARGETSEL なしの JTAG→SWD で救済の DP（TARGETID 0x01002927・TINSTANCE 0xF）が
    # 応答し、診断がその CTRL/STAT に CDBGPWRUPREQ を書いたため、チップがリセットされたまま止まった。DPv2 なら
    # TARGETID・DLPIDR で応答した DP を確かめ、複数ドロップの部品の救済の DP なら電源を要求しない
    rescue = FakeTarget(dpidr=0x10212927, targetid=0x01002927, dlpidr=0xF0000001, aps={})
    link = FakeDapLink(rescue)
    d = diagnose(RawDap(link), voltage=lambda: dict(GOOD_VOLTS), pins=lambda: link.pin_access(0, 0))
    assert not link.ctrl_stat & (1 << 28), "救済の DP に CDBGPWRUPREQ を書いた"
    swd = step(d, "swd")
    assert swd.status == "fail" and "救済の DP" in swd.message and "0xF1002927" in swd.message
    assert not d.ok


def test_a_dp_that_ignores_targetsel_is_not_taken_for_a_multidrop_core():
    # 複数ドロップでない DP（STM32 の DPv1）は TARGETSEL を無視して応答する。TARGETID・DLPIDR が選んだ値と合わなければ
    # 複数ドロップのコアとはみなさず、素の JTAG→SWD で続ける
    d = run(FakeTarget(aps={0: FakeMemAp(words=stm32f401_words())}))
    attempts = step(d, "swd").data["attempts"]
    assert d.ok and attempts[-1]["method"] == "jtag_to_swd" and "targetsel" not in d.connect
