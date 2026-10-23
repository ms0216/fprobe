"""fprobe.halt_risk: 止めると壊れうるターゲットの見分け（ELF のシンボルだけ。機器固有の名前は使わない）。"""

from pathlib import Path

from fprobe.halt_risk import assess
from fprobe.symbols import Symbol, Symbols

DEMO = Path(__file__).resolve().parents[1] / "firmware" / "sram_demo" / "sram_demo.elf"


def f(name):
    return Symbol(name, 0x1000, 4, "function")


def v(name):
    return Symbol(name, 0x20000000, 4, "object")


def kinds(result):
    return [r["kind"] for r in result["matches"]]


def test_plain_bare_metal_matches_nothing():
    r = assess([f("main"), f("tick"), v("counter")])
    assert r == {"matched": False, "matches": [], "monitor_in_elf": False}


def test_radio_interrupt_and_bluetooth_controller_are_radio():
    r = assess([f("main"), f("RADIO_IRQHandler"), f("ll_adv_enable"), f("lll_conn_isr_rx"), f("hci_driver_open")])
    assert r["matched"] and kinds(r) == ["radio"]
    assert set(r["matches"][0]["symbols"]) >= {"RADIO_IRQHandler", "ll_adv_enable"}


def test_rtos_kernels_are_recognized_from_their_globals():
    for name in ("pxCurrentTCB", "_kernel", "_tx_thread_current_ptr", "osRtxInfo"):
        r = assess([f("main"), v(name)])
        assert kinds(r) == ["rtos"], name


def test_usb_and_watchdog_are_reasons_too():
    r = assess([f("tud_task"), f("wdt_feed")])
    assert kinds(r) == ["usb", "watchdog"]


def test_matches_come_in_a_fixed_order_and_evidence_is_short():
    names = [f(f"ll_func_{i}") for i in range(40)] + [v("pxCurrentTCB"), f("USBD_IRQHandler")]
    r = assess(names)
    assert kinds(r) == ["radio", "usb", "rtos"]
    assert len(r["matches"][0]["symbols"]) <= 5 and r["matches"][0]["count"] == 40


def test_monitor_in_elf_is_reported():
    r = assess([f("fprobe_monitor_init"), f("DebugMon_Handler"), v("pxCurrentTCB")])
    assert r["monitor_in_elf"] is True


def test_unrelated_names_that_merely_contain_the_words_are_not_matched():
    r = assess([f("fill_buffer"), f("all_done"), v("radiometer_gain"), f("usb_cable_table_lookup_x")])
    assert r["matched"] is False


def test_demo_elf_matches_nothing():
    r = assess(Symbols(DEMO).names())
    assert r["matched"] is False
