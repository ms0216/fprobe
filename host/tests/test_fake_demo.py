"""fake_demo.DemoProbe: 機器なしで画面の各パネルに出るデモの値（fake_daemon.py --demo）。"""

import pytest

from fake_demo import DemoProbe
from value_kinds import vint


@pytest.fixture(scope="module")
def demo():
    return DemoProbe()


def test_starts_connected_and_halted_in_the_demo_source(demo):
    s = demo.target_status()
    assert s["state"] == "HALTED" and s["file"] == "sram_demo.c" and s["line"] == 28
    assert "sram_demo.c" in {f["name"] for f in demo.source_list()["files"]}
    assert demo._elf.la_view(0, None, 10)["capture"]["length"] == 8192        # Timeline の捕捉もある


def test_steps_follow_the_source(demo):
    demo.target_connect("cortex_m", frequency_hz=4_000_000)
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
    assert sensors["type"] == "struct Sensor[3]"
    assert sensors["value"]["items"][0]["members"]["name"]["string"] == "accel"
    assert demo.evaluate("active")["value"]["symbol"] == "sensors+32"
    assert demo.evaluate("active->next->name")["value"]["string"] == "temp"
    log = demo.evaluate("log_buffer", max_elements=64)["value"]
    assert log["length"] == 100 and len(log["items"]) == 64                                # 多い配列は先頭だけ
    assert demo.evaluate("config.label")["value"]["string"] == "fprobe demo v1.0"
    assert [m["name"] for m in demo.type_info("struct Sensor")["members"]][:3] == ["id", "mode", "gain"]
    assert demo.write_expression("sensors[0].mode", "MODE_FAULT")["value"]["name"] == "MODE_FAULT"
    assert demo.write_expression("sensors[2].channel", 5)["value"] == vint(5)              # ビットフィールド
    demo.write_expression("counter", 300)
    assert demo.counter == 300


def test_live_watch_reads_globals_while_running_and_checks_expressions_from_the_elf():
    demo = DemoProbe()
    demo.state = "RUNNING"
    assert demo.evaluate("counter", live=True)["value"] == vint(demo.counter)
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
    assert demo.evaluate("sensors[0].id")["value"] == vint(9)
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


def test_demo_system_status_has_the_real_shape(demo):
    """画面のシステム解析が読む system_status を、デモも本物と同じ形で返す（FPGA の観測は無い）"""
    out = demo.system_status(addresses=[0x20000000])
    assert set(out) == {"time_us", "mcu", "fpga"} and out["fpga"] == {"available": False}
    assert out["mcu"]["connected"] is True and "0x20000000" in out["mcu"]["memory"]


# --- 偽の機器が本物の Probe の引数・メソッドに追いつく（使いやすさの評価 2026-11-03で MCP の課題 4・7 が落ちた原因） ---

def _valid(method, result):
    from fprobe.api.daemon import jsonable
    from fprobe.api.spec import load_spec
    load_spec().validate_result(method, jsonable(result))


def test_run_until_halt_stops_at_a_breakpoint_like_the_real_probe():
    d = DemoProbe()
    d.breakpoint_set("sram_demo.c:19")
    stop = d.run_until_halt(timeout_s=3.0)
    _valid("debug.run_until_halt", stop)
    assert stop["reason"] == "breakpoint" and (stop["function"], stop["line"]) == ("tick", 19)
    assert (d.target_status()["function"], d.target_status()["line"]) == ("tick", 19)


def test_run_until_halt_times_out_and_halts_when_nothing_stops_it():
    d = DemoProbe()
    stop = d.run_until_halt(timeout_s=0.05)
    _valid("debug.run_until_halt", stop)
    assert stop["reason"] == "timeout" and d.target_status()["state"] == "HALTED"


def test_peripheral_read_skip_leaves_the_named_registers_out(demo):
    out = demo.peripheral_read("GPIOA", skip=["GPIOA.MODER"])
    _valid("target.peripheral.read", out)
    assert "GPIOA.MODER" not in {r["path"] for r in out["registers"]} and [x["path"] for x in out["skipped"]] == ["GPIOA.MODER"]


def test_the_demo_answers_the_methods_the_evaluation_called(demo):
    _valid("debug.semihosting.status", demo.semihosting_status())
    _valid("probe.pins.read", demo.pins_read())
    _valid("debug.disasm.text", demo.disasm_text())
    _valid("debug.disasm.around", demo.disasm_around(before=1, after=1, data_ranges=[], sync_limit=1024))
    _valid("debug.backtrace", demo.backtrace(assume_leaf=False))


def test_an_argument_the_demo_cannot_honour_is_refused_not_ignored(demo):
    from fake_probe_contract import NotInDemo
    with pytest.raises(NotInDemo, match="core"):
        demo.target_resume(core=1)
    with pytest.raises(NotInDemo, match="from_target"):
        demo.svd_load(from_target=True)


def test_terminal_read_with_terminals_returns_the_switch_list_for_rtt_only(demo):
    # 画面の Terminal は terminals=True で読む。偽の機器が受けないと、画面は黙って読みに失敗して空のままだった
    # （使いやすさの評価 2026-11-03 の課題 8: 画面の担当が RTT の出力に辿り着けなかった一因）
    rtt = demo.terminal_read("rtt0", terminals=True)
    _valid("terminal.read", rtt)
    assert rtt["terminals"] == [] and "tick counter" in rtt["data"]
    assert "terminals" not in demo.terminal_read("uart", terminals=True)


def test_step_over_the_call_that_returns_to_the_same_line_still_shows_progress():
    # 3 行ステップが同じ行（28）に戻る: 位置は同じでも、API は steps・runs（測った事実）で進んだことを返す。counter も増える
    d = DemoProbe()
    d._go("reset_handler", 28)
    before = d.counter
    out = d.step_line("over")
    _valid("debug.step", out)
    assert (out["function"], out["line"]) == ("reset_handler", 28)
    assert out["steps"] == 1 and out["runs"] == 1 and d.counter == before + 1


# --- 本物の機器に合わせる: 止まっている間は端末の出力を増やさない・同じ番地は同じ値（使いやすさの評価 2026-11-03） ---

def test_terminal_output_grows_only_while_the_target_runs():
    # 止まったターゲットは出力を出さない。偽の機器が止まっていても時刻で足していたため、画面の担当は「最後の 1 行が決まらない」と
    # あきらめた。走っている時間（止めている間は進まない）で足す
    from fake_demo_io import DemoStreams
    now = [0.0]
    streams = DemoStreams(lambda: 42, clock=lambda: now[0])
    start = streams.size("rtt0")
    now[0] = 0.4
    assert streams.size("rtt0") == start                           # 周期（0.5 s）に満たない
    now[0] = 2.0
    grown = streams.size("rtt0")
    assert grown > start
    assert streams.size("rtt0") == grown                           # 時計が進まなければ増えない（止まっている間）
    now[0] = 4.0
    assert streams.size("rtt0") > grown


def test_a_halted_demo_target_does_not_add_output_and_a_running_one_does():
    import time
    d = DemoProbe()
    d.target_halt()
    before = d.terminal_read("rtt0")["next"]
    time.sleep(0.7)
    assert d.terminal_read("rtt0")["next"] == before               # 止まっている間は増えない
    d.target_resume()
    time.sleep(0.7)
    assert d.terminal_read("rtt0")["next"] > before               # 走り出したら増える
    d.target_halt()


def _word(d, address):
    return int.from_bytes(bytes.fromhex(d.read_memory(address, 4)), "little")


def test_a_peripheral_register_reads_the_same_through_the_peripheral_and_memory_paths():
    # 同じ番地は同じ値: 周辺レジスタの読みとメモリの読みが別の値（メモリは 0）だと、画面の担当は MCU 欄の 0 を答えにした
    d = DemoProbe()
    reg = d.peripheral_read("GPIOA.MODER")["registers"][0]
    assert reg["value"] != 0
    assert _word(d, reg["address"]) == reg["value"]
    row = d.memory_view(reg["address"], 16)["rows"][0]
    assert row["address"] == reg["address"] and row["values"][:4] == list(reg["value"].to_bytes(4, "little"))
    d.peripheral_write("GPIOA.MODER", value=0x12345678)
    assert _word(d, reg["address"]) == 0x12345678                   # 周辺の書きがメモリの読みに出る
    d.write_memory(reg["address"], (0xA5A5A5A5).to_bytes(4, "little").hex())
    assert d.peripheral_read("GPIOA.MODER")["registers"][0]["value"] == 0xA5A5A5A5     # メモリの書きが周辺の読みに出る


def test_memory_at_an_address_with_no_register_is_zero_and_ram_is_unchanged():
    d = DemoProbe()
    assert d.read_memory(0x5FFFFFF0, 4) == "00000000"                       # どのレジスタにも当たらない周辺の番地は 0
    assert d.read_memory(0x20000000, 4) == d._mem.read(0x20000000, 4).hex()
