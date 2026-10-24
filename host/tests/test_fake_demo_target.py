"""デモ（fake_demo_target.TargetDemo）: Fault Analyzer・Flash・Breakpoints の値が本物の Probe と同じ形で返る。"""

import base64
import time

import pytest

from fake_demo import DemoProbe
from fprobe.probe_elf_files import DEMO_ELF
from value_kinds import vint


@pytest.fixture
def demo(tmp_path, monkeypatch):
    monkeypatch.setenv("FPROBE_HOME", str(tmp_path / "home"))
    d = DemoProbe()
    d.notices = []
    d.attach_publisher(lambda topic, data: d.notices.append((topic, data)))
    yield d
    d.state = "HALTED"                                   # 背景の模擬を止める


def wait_for(cond, timeout=3.0):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if cond():
            return
        time.sleep(0.02)
    raise AssertionError("時間切れ")


def test_not_faulted_while_halted_in_the_program(demo):
    report = demo.target_fault()
    assert report["faulted"] is False and report["exception"] is None and report["causes"] == []
    assert [r["bit"] for r in report["halt_reasons"]] == ["HALTED", "BKPT"]


def test_vector_catch_then_continue_stops_in_the_fault_handler_with_a_real_shaped_report(demo):
    out = demo.vector_catch(["hard_fault", "bus_fault", "secure_fault"])
    assert out["enabled"] == ["bus_fault", "hard_fault"] and out["unsupported"] == ["secure_fault"]
    assert demo.vector_catch()["enabled"] == ["bus_fault", "hard_fault"] and "unsupported" not in demo.vector_catch()
    demo.target_resume()
    wait_for(lambda: demo.state == "HALTED")
    status = demo.target_status()
    assert status["function"] == "default_handler" and status["line"] == 32
    report = demo.target_fault()
    assert report["faulted"] and report["exception"] == {"number": 3, "name": "HardFault"}
    assert [c["bit"] for c in report["causes"]] == ["FORCED", "PRECISERR", "BFARVALID"]
    assert report["registers"]["BFAR"] == 0x20018000 and report["fault_address"]["address"] == 0x20018000
    frame = report["frame"]
    assert frame["stack"] == "MSP" and frame["registers"]["r1"] == 0x20018000
    assert "reached_milestone" in frame["pc"]["where"] and "sram_demo.c:13" in frame["pc"]["where"]
    assert "reset_handler" in frame["lr"]["where"]
    assert [r["bit"] for r in report["halt_reasons"]] == ["VCATCH"]
    bt = demo.backtrace()
    assert [f.get("function") for f in bt] == ["default_handler", "reached_milestone", "reset_handler"]
    assert bt[1]["note"] == "exception"
    demo.target_reset(halt=True)
    assert demo.target_fault()["faulted"] is False


def test_continuing_from_the_fault_handler_keeps_running_in_it_and_is_reported_as_fatal(demo):
    clock = [5000.0]
    demo._clock = lambda: clock[0]
    demo.vector_catch(["hard_fault"])
    demo.target_resume()
    wait_for(lambda: demo.state == "HALTED")
    demo.target_resume()                                  # ハンドラの無限ループを回り続ける
    assert "fatal" not in demo.target_status()            # 名前の表を持たない: 渡されなければ知らせない
    demo.liveness_set(functions=["default_handler"], pc_samples=4)      # 利用者が明示する
    status = demo.target_status()
    assert status["state"] == "RUNNING" and status["fatal"]["kind"] == "pc_in"   # 待たずに事実として
    clock[0] += 2
    fatal = demo.target_status()["fatal"]
    assert fatal["kind"] == "pc_in" and fatal["function"] == "default_handler" and fatal["since"] == 5000.0
    demo.target_halt()
    assert "fatal" not in demo.target_status()


def test_fatal_demo_is_risky_to_halt_and_crashes_while_running(tmp_path, monkeypatch):
    monkeypatch.setenv("FPROBE_HOME", str(tmp_path / "home"))
    d = DemoProbe(fatal_after_s=0.05)
    try:
        risk = d.halt_risk()
        assert risk["matched"] and [r["query"] for r in risk["matches"]] == ["RADIO_IRQHandler", "_kernel"]
        clock = [100.0]
        d._clock = lambda: clock[0]
        assert d.state == "RUNNING"
        wait_for(lambda: d._fatal_pc is not None)
        d.target_status()
        clock[0] += 2
        assert d.target_status()["fatal"]["kind"] == "pc_in"
    finally:
        d.state = "HALTED"


def test_the_plain_demo_is_not_risky(demo):
    assert demo.halt_risk()["matched"] is False


def test_breakpoint_actions_are_kept_and_listed(demo):
    listed = demo.breakpoint_list()["breakpoints"]
    assert [b["location"] for b in listed] == ["sram_demo.c:13"]
    assert listed[0]["action"]["log"] == ["value", "counter"] and listed[0]["kind"] == "hw"
    demo.breakpoint_set("sram_demo.c:18", condition="counter > 0x300", ignore_count=2)
    b = next(b for b in demo.breakpoint_list()["breakpoints"] if b["location"] == "sram_demo.c:18")
    assert b["action"] == {"condition": "counter > 0x300", "ignore_count": 2, "log": None, "message": None,
                           "hits": 0, "last_error": None}
    demo.breakpoint_set("sram_demo.c:18")                       # 動作なしで置き直すと外れる
    assert "action" not in next(b for b in demo.breakpoint_list()["breakpoints"] if b["location"] == "sram_demo.c:18")
    demo.breakpoint_clear()
    assert demo.breakpoint_list()["breakpoints"] == [] and demo._bp_actions == {}


def test_the_log_has_recorded_hits_and_grows_while_running(demo):
    log = demo.breakpoint_log()
    assert len(log["entries"]) == 7 and all(0 < e["t"] < 2 * 10_000_000 for e in log["entries"])
    first = log["entries"][0]
    assert first["function"] == "reached_milestone" and first["line"] == 13 and first["message"] == "milestone {value} (counter={counter})"
    assert first["values"] == {"value": vint(256), "counter": vint(256)}
    demo.target_resume()
    wait_for(lambda: len(demo.breakpoint_log(since=log["next"])["entries"]) >= 2)
    new = demo.breakpoint_log(since=log["next"])["entries"]
    assert "t" not in new[0] and new[0]["values"]["value"] == new[0]["values"]["counter"]
    assert new[0]["values"]["value"]["value"] > 0
    assert demo.state == "RUNNING"                               # ログでは止まらない


def test_a_conditional_breakpoint_stops_when_true(demo):
    demo.counter = 0
    demo.breakpoint_set("sram_demo.c:18", condition="counter >= 0x300")
    demo.target_resume()
    wait_for(lambda: demo.state == "HALTED")
    assert demo.line == 18 and demo.counter >= 0x300
    b = next(b for b in demo.breakpoint_list()["breakpoints"] if b["location"] == "sram_demo.c:18")
    assert b["action"]["hits"] == 3


def test_flash_writes_reports_stages_and_progress(demo, tmp_path):
    image = tmp_path / "app.bin"
    image.write_bytes(bytes(range(256)) * 64)                      # 16 KB
    out = demo.flash(str(image), address=0x08004000, reset=False)
    assert out["image"]["format"] == "bin" and out["image"]["bytes"] == 0x4000
    program = out["program"]
    assert program["erase"]["sectors"] == 1 and program["programmed_bytes"] == 0x4000
    assert program["program"]["bytes_per_s"] > 0 and out["verify"]["match"] is True
    assert out["state"] == "HALTED"
    phases = [(d["operation"], d["phase"]) for t, d in demo.notices if t == "flash.progress"]
    assert ("program", "erase") in phases and ("program", "program") in phases and ("program", "verify") in phases
    again = demo.flash(str(image), address=0x08004000, reset=False)
    assert again["program"]["programmed_bytes"] == 0 and again["program"]["skipped_bytes"] == 0x4000   # smart
    assert demo.flash_verify(str(image), address=0x08004000)["match"] is True
    back = demo.flash_read(str(tmp_path / "back.hex"), address=0x08004000, length=0x4000)
    assert back["format"] == "hex" and back["bytes"] == 0x4000 and (tmp_path / "back.hex").exists()
    erased = demo.flash_erase("range", 0x08004000, 1)
    assert erased["sectors"] == 1 and erased["start"] == 0x08004000 and erased["end"] == 0x08008000
    bad = demo.flash_verify(str(image), address=0x08004000)
    assert bad["match"] is False and bad["differing_bytes"] > 0 and bad["differences"]


def test_flash_of_the_demo_elf_writes_ram_directly_and_loads_symbols(demo):
    out = demo.flash(str(DEMO_ELF))
    assert out["program"]["ram_bytes"] == out["image"]["bytes"] and out["program"]["programmed_bytes"] == 0
    assert out["state"] == "RUNNING"


def test_regions_benchmark_packs_and_upload(demo, tmp_path):
    regions = demo.flash_regions()["regions"]
    assert regions[0]["start"] == 0x08000000 and sum(s["count"] for s in regions[0]["sectors"]) == 8
    bench = demo.flash_benchmark(program_address=0x08060000)
    assert bench["frequency_hz"] == 4_000_000 and bench["ram"]["write"]["bytes_per_s"] > 0
    assert bench["flash_program"]["length"] == 0x20000 and bench["flash_read"]["bytes"] == 0x10000
    assert [p["pack"] for p in demo.pack_list()["packs"]] == ["Keil.STM32F4xx_DFP"]
    found = demo.pack_find("nrf52840")["parts"]
    assert found[0]["installed"] is False
    assert demo.pack_install("nrf52840")["targets"] == ["nrf52840_xxaa"]
    assert demo.pack_find("nrf52840")["parts"][0]["installed"] is True
    loaded = demo.pack_load("C:/packs/RaspberryPi.RP2040_DFP.1.0.0.pack")
    assert loaded["targets"] == ["rp2040"]
    with pytest.raises(ValueError):
        demo.pack_load("C:/packs/unknown.pack")
    added = demo.flash_algo_add("C:/flm/W25Q128.FLM", address=0x90000000, size=0x1000000)
    assert added["name"] == "W25Q128" and added["algorithm"] == "flm:C:/flm/W25Q128.FLM"
    listed = demo.flash_regions()
    assert [r["name"] for r in listed["regions"]] == ["flash", "W25Q128"]
    assert listed["algorithms"][0] == {"name": "W25Q128", "flm": "C:/flm/W25Q128.FLM", "address": 0x90000000,
                                       "size": 0x1000000, "ram_address": None, "ram_size": None}
    with pytest.raises(ValueError):
        demo.flash_algo_add("C:/flm/W25Q128.FLM")
    assert demo.flash_algo_remove("W25Q128") == {"removed": "W25Q128"}
    assert demo.flash_regions()["algorithms"] == []
    up = demo.flash_upload("app.bin", base64.b64encode(b"\x01\x02").decode())
    assert up["format"] == "bin" and up["bytes"] == 2
    fbp = demo.flash_breakpoints()
    assert fbp["available"] and fbp["hardware"] == sorted(demo.breakpoints) and fbp["flash"] == []
