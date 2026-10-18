"""ブレークポイントの動作（fprobe.probe_breakpoints）: 条件・無視する回数・ログ。止まった後の処理は
run_until_halt の中と、target_resume の後の背景の見張りの両方。機器は使わない（偽の CPU）。"""

import pytest
from test_probe_observe import RTT_DEMO, FakeCpu, connect, probe, wait_for  # noqa: F401（probe は fixture）

from fprobe.symbols import Symbols

BP = 0x20000300


class BreakCpu(FakeCpu):
    """FPB のブレークポイントと 1 命令の実行（何もしない）を足した偽の CPU。"""

    def __init__(self, hits):
        super().__init__(stops=[(BP, 0, 0)] * hits, image=RTT_DEMO)
        self.breakpoints = set()

    def set_breakpoint(self, address, kind=None):
        self.breakpoints.add(address)
        return True

    def remove_breakpoint(self, address):
        self.breakpoints.discard(address)

    def step(self):
        pass

    def read_core_register_raw(self, name):
        return self.regs.get(name, 0x20007000 if name in ("sp", "msp") else 0)

    def read_core_registers_raw(self, names):
        return [self.read_core_register_raw(n) for n in names]

    def read_memory_block8(self, address, length):
        if not 0x20000000 <= address < 0x20008000:
            return [0] * length                                # 巻き戻しが SRAM の外を覗く（ベクタ表等）
        return super().read_memory_block8(address, length)


def setup(probe, hits=5):
    cpu = BreakCpu(hits)
    connect(probe, cpu)
    probe._symbols = Symbols(RTT_DEMO)
    probe.semihosting_configure(enabled=False)                 # 止まった場所は BKPT 0xAB ではない
    return cpu


def test_log_breakpoint_records_values_and_does_not_stop(probe):
    cpu = setup(probe)
    probe.breakpoint_set(hex(BP), log=["2 + 3", "1 / 0"], message="通った")
    stop = probe.run_until_halt(timeout_s=0.3)
    assert stop["reason"] == "timeout" and cpu.resumes == 6                    # 5 回当たって、どれでも止まらない
    log = probe.breakpoint_log()
    assert [e["hit"] for e in log["entries"]] == [1, 2, 3, 4, 5] and log["next"] == 5
    first = log["entries"][0]
    assert first["values"]["2 + 3"] == 5 and "error" in first["values"]["1 / 0"] and first["message"] == "通った"
    assert probe.breakpoint_log(since=4)["entries"][0]["hit"] == 5
    assert probe.breakpoint_list()["breakpoints"][0]["action"]["hits"] == 5


def test_ignore_count_and_condition(probe):
    setup(probe)
    probe.breakpoint_set(hex(BP), ignore_count=2)
    assert probe.run_until_halt(timeout_s=1)["reason"] == "breakpoint"
    assert probe.breakpoint_list()["breakpoints"][0]["action"]["hits"] == 3     # 3 回目で止まった
    probe.breakpoint_set(hex(BP), condition="1 == 0")
    assert probe.run_until_halt(timeout_s=0.3)["reason"] == "timeout"         # 残りの 2 回は条件が偽
    probe.breakpoint_clear(hex(BP))
    assert probe._actions == {}


def test_a_condition_that_cannot_be_evaluated_stops_and_reports(probe):
    setup(probe)
    probe.breakpoint_set(hex(BP), condition="no_such_variable > 1")
    assert probe.run_until_halt(timeout_s=1)["reason"] == "breakpoint"
    assert "no_such_variable" in probe.breakpoint_list()["breakpoints"][0]["action"]["last_error"]


def test_log_breakpoints_are_serviced_in_the_background_after_resume(probe):
    cpu = setup(probe, hits=3)
    probe.breakpoint_set(hex(BP), log=["7"])
    probe.target_resume()
    wait_for(lambda: len(probe.breakpoint_log()["entries"]) == 3)
    wait_for(lambda: cpu.state.name == "RUNNING")


class _Recorder:
    """記録の代わり: add_host が写した時刻を返す。"""

    def __init__(self):
        self.added = []

    def add_host(self, stream, kind, host_s, **fields):
        self.added.append((stream, kind))
        return 1000 + len(self.added)


def test_log_entries_carry_the_recording_time_while_recording(probe):
    """記録中は、ログの各項に記録のタイムベースの時刻 t（画面が時間軸のその時刻へ飛ぶ）。記録していなければ付けない。"""
    setup(probe, hits=3)
    probe.breakpoint_set(hex(BP), log=["1"])
    probe._recorder = recorder = _Recorder()
    probe.run_until_halt(timeout_s=0.3)
    probe._recorder = None
    entries = probe.breakpoint_log()["entries"]
    assert [e["t"] for e in entries] == [1001, 1002, 1003]
    assert recorder.added == [("debug", "log")] * 3


def test_an_empty_log_list_logs_the_message_only(probe):
    """log = []（式なし）でもログ・ブレークポイント（VS Code の Log Message に式が無いとき）。None なら動作なし。"""
    setup(probe, hits=2)
    probe.breakpoint_set(hex(BP), log=[], message="here")
    assert probe.breakpoint_list()["breakpoints"][0]["action"]["log"] == []
    assert probe.run_until_halt(timeout_s=0.3)["reason"] == "timeout"
    assert [(e["message"], e["values"]) for e in probe.breakpoint_log()["entries"]] == [("here", {})] * 2
    probe.breakpoint_set(hex(BP))
    assert "action" not in probe.breakpoint_list()["breakpoints"][0]


def test_log_entries_have_no_time_without_a_recording(probe):
    setup(probe, hits=1)
    probe.breakpoint_set(hex(BP), log=["1"])
    probe.run_until_halt(timeout_s=0.3)
    assert "t" not in probe.breakpoint_log()["entries"][0]
