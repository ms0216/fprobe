"""fake_demo.DemoProbe: 機器なしで画面の各パネルに出るデモの値（fake_daemon.py --demo）。"""

import pytest

from fake_demo import DemoProbe


@pytest.fixture(scope="module")
def demo():
    return DemoProbe()


def test_starts_connected_and_halted_in_the_demo_source(demo):
    s = demo.target_status()
    assert s["state"] == "HALTED" and s["file"] == "sram_demo.c" and s["line"] == 28
    assert "sram_demo.c" in {f["name"] for f in demo.source_list()["files"]}
    assert demo._elf.la_view(0, None, 10)["capture"]["length"] == 8192        # Timeline の捕捉もある


def test_steps_follow_the_source(demo):
    demo.target_connect()
    demo._go("reset_handler", 28)
    assert demo.step_line("into")["line"] == 17
    assert demo.step_line("over")["line"] == 18
    assert demo.step_line("out")["line"] == 28
    assert demo.step_line("until", "sram_demo.c:13")["function"] == "reached_milestone"
    assert [f["where"].split("+")[0] for f in demo.backtrace()] == ["reached_milestone", "tick", "reset_handler"]


def test_panels_have_values(demo):
    regs = demo.read_registers()
    assert regs["pc"] == demo.pc and "xpsr" in regs
    assert demo.frame_variables()["function"] == demo.function
    mem = demo.memory_view(0x20000000, 64)
    assert len(mem["rows"]) == 4 and mem["dump"]
    assert demo.disasm_around(before=2, after=2)["instructions"]
    assert any(p["name"] == "GPIOA" for p in demo.svd_peripherals()["peripherals"])
    gpio = demo.peripheral_read("GPIOA")["registers"]
    assert gpio and gpio[0]["fields"]
    assert [t["name"] for t in demo.rtos_tasks()["tasks"]][0] == "main"
    first = demo.rtt_read(since=0)
    assert first["data"].startswith("SEGGER RTT demo") and demo.rtt_read(since=first["next"])["start"] == first["next"]


def test_watch_and_locals_have_structs_arrays_and_pointers():
    # 画面の変数の木（構造体・配列・ポインタの展開）と書き込みを機器なしで試せる。式は本物の C の式の評価（cexpr）
    demo = DemoProbe()
    sensors = demo.evaluate("sensors")
    assert sensors["type"] == "struct Sensor[3]" and sensors["value"][0]["name"]["string"] == "accel"
    assert demo.evaluate("active")["value"]["symbol"] == "sensors+32"
    assert demo.evaluate("active->next->name")["value"]["string"] == "temp"
    assert demo.evaluate("log_buffer", max_elements=64)["value"]["length"] == 100         # 多い配列は先頭だけ
    assert demo.evaluate("config.label")["value"]["string"] == "fprobe demo v1.0"
    assert [m["name"] for m in demo.type_info("struct Sensor")["members"]][:3] == ["id", "mode", "gain"]
    assert demo.write_expression("sensors[0].mode", "MODE_FAULT")["value"]["enum"] == "MODE_FAULT"
    assert demo.write_expression("sensors[2].channel", 5)["value"] == 5                    # ビットフィールド
    demo.write_expression("counter", 300)
    assert demo.counter == 300


def test_live_watch_reads_globals_while_running_and_checks_expressions_from_the_elf():
    demo = DemoProbe()
    demo.state = "RUNNING"
    assert demo.evaluate("counter", live=True)["value"] == demo.counter
    assert demo.evaluate("active->next->name", live=True)["value"]["string"] == "temp"
    with pytest.raises(ValueError, match="止まっているとき"):
        demo.evaluate("p", live=True)                                    # ローカル変数
    assert demo.expression_check("sensors[1].id")["live"] is True
    with pytest.raises(ValueError):
        demo.expression_check("sensors[")


def test_frames_have_their_own_variables():
    demo = DemoProbe()
    demo._go("reset_handler", 28)
    demo.step_line("until", "sram_demo.c:13")
    frames = demo.backtrace()
    assert [f["function"] for f in frames] == ["reached_milestone", "tick", "reset_handler"]
    assert frames[1]["file"] == "sram_demo.c" and frames[1]["index"] == 1
    assert [v["name"] for v in demo.frame_variables(0)["arguments"]] == ["value"]
    assert [v["name"] for v in demo.frame_variables(1)["locals"]] == ["s", "mode"]
    assert [v["name"] for v in demo.frame_variables(2)["locals"]] == ["p"]
    assert demo.evaluate("p", frame=2)["value"]["symbol"] == "table+8"
    with pytest.raises(IndexError):
        demo.frame_variables(3)


def test_memory_is_shared_with_variables_and_writable():
    demo = DemoProbe()
    base = demo.evaluate("sensors")["address"]
    demo.write_memory(base, "09")
    assert demo.evaluate("sensors[0].id")["value"] == 9
    view = demo.memory_view(base, 16, compare=True)
    assert view["rows"][0]["values"][0] == 9 and {"address": base, "name": "sensors"} in view["names"]
    demo.write_expression("sensors[0].id", 7)
    assert demo.memory_view(base, 16, compare=True)["rows"][0]["changed"][:2] == [True, False]


def test_peripheral_fields_are_writable():
    demo = DemoProbe()
    defs = {r["name"]: r for r in demo.svd_registers("GPIOA")["registers"]}
    assert any(f["enums"] for f in defs["MODER"]["fields"])                      # 画面の選択肢（列挙）
    out = demo.peripheral_write("GPIOA.MODER", fields={"MODER5": "Output"})
    assert out["written"] & (3 << 10) == 1 << 10
    fields = {f["name"]: f for f in demo.peripheral_read("GPIOA.MODER")["registers"][0]["fields"]}
    assert fields["MODER5"]["value"] == 1 and fields["MODER5"]["enum"] == "Output"
    demo.peripheral_write("GPIOA.ODR", value=0x20)
    assert demo.peripheral_read("GPIOA")["registers"][[r["path"] for r in demo.peripheral_read("GPIOA")["registers"]].index("GPIOA.ODR")]["value"] == 0x20


def test_terminal_sources_stream_and_echo():
    demo = DemoProbe()
    ids = [s["id"] for s in demo.terminal_sources()["sources"]]
    assert ids == ["rtt0", "rtt1", "uart", "semihosting"]                # SWO は swo_start の後
    demo.swo_start(baud=2_000_000)
    ids = [s["id"] for s in demo.terminal_sources()["sources"]]
    assert ids == ["rtt0", "rtt1", "swo0", "swo1", "uart", "semihosting"]
    first = demo.terminal_read("uart")
    assert "loader" in first["data"]
    demo.uart_write("help\r")
    assert "help\r\nok" in demo.terminal_read("uart", since=first["next"])["data"]
    demo.rtt_write(0, text="ping\n")
    assert ">> ping" in demo.terminal_read("rtt0")["data"]
    assert demo.terminal_read("swo0")["data"].startswith("ITM printf")


def test_core_registers_are_writable():
    demo = DemoProbe()
    demo.write_register("r0", 0x1234)
    demo.write_register("pc", demo._line_address(17))
    regs = demo.read_registers()
    assert regs["r0"] == 0x1234 and regs["pc"] == demo._line_address(17)
    with pytest.raises(ValueError):
        demo.write_register("r99", 1)


def test_terminal_lines_have_times_and_the_recorded_uart_lines_jump_to_the_timeline(tmp_path):
    import fake_demo
    demo = DemoProbe()
    fake_demo.start(demo)                                                  # デモの記録を開く（UART の最初の行になる）
    r = demo.terminal_read("uart", times=True)
    assert r["data"].startswith("[  0.1] tick counter=")                  # 記録の UART の送信の行
    recorded = [m for m in r["times"] if m["t"] is not None]
    assert len(recorded) >= 15 and r["timebase_hz"] == 10_000_000
    first_tx = demo._elf.recording_find({"stream": "uart", "kind": "tx"})["events"][0]["t"]
    assert recorded[0]["t"] == first_tx and recorded[0]["at"] == 0
    assert all(a["wall"] <= b["wall"] for a, b in zip(r["times"], r["times"][1:]))
    assert r["times"][-1]["t"] is None and "loader" in r["data"]          # 記録の外の行（時間軸の時刻は無い）
    assert demo.uart_configure(baud=9600) == {"baud": 9600}
    assert next(s for s in demo.terminal_sources()["sources"] if s["id"] == "uart")["baud"] == 9600


def test_swo_trace_has_data_accesses_and_exceptions_on_the_recording_timebase():
    import fake_demo
    demo = DemoProbe()
    fake_demo.start(demo)
    demo.swo_configure(exception_trace=True, data_trace=[{"expression": "counter"}], swo_baud=2_000_000)
    r = demo.swo_trace(limit=5)
    assert r["running"] and r["total"] > 1000 and len(r["accesses"]) == 5
    a = r["accesses"][0]
    assert a["name"] == "counter" and a["write"] and a["file"] == "sram_demo.c" and a["line"] == 17
    assert r["exceptions"]["counts"]["SysTick"] > 100
    assert demo.swo_unconfigure()["running"] is False
