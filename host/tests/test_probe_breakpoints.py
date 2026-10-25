"""ブレークポイントの動作（fprobe.probe_breakpoints）: 条件・無視する回数・ログ。止まった後の処理は
run_until_halt の中と、target_resume の後の背景の見張りの両方。機器は使わない（偽の CPU）。"""

import pytest
from test_probe_observe import RTT_DEMO, FakeCpu, connect, probe, wait_for  # noqa: F401（probe は fixture）
from value_kinds import ints, varray, vbool, venum, vfloat, vint, vna, vptr, vstruct

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

    def read32(self, address):                      # DFSR: 1 命令のステップは HALTED だけ立つ
        return 1

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
    assert first["values"]["2 + 3"] == vint(5) and first["message"] == "通った"
    assert first["values"]["1 / 0"]["kind"] == "unavailable" and first["values"]["1 / 0"]["reason"]   # 評価できない式
    assert probe.breakpoint_log(since=4)["entries"][0]["hit"] == 5
    assert log["lost"] == 0 and log["more"] is False and log["kept_limit"] == 10_000
    cut = probe.breakpoint_log(limit=2)
    assert cut["more"] is True and cut["next"] == 2                            # 続きがあることを返す
    assert probe.breakpoint_list()["breakpoints"][0]["action"]["hits"] == 5
    # いちばん新しい seq（last）を返す: since がそれより後なら、読み手の位置はこのログに無い（事実で分かる）
    assert log["last"] == 5 and {k: probe.breakpoint_log(since=99)[k] for k in ("entries", "next", "last")} == {"entries": [], "next": 99, "last": 5}


def test_the_log_says_how_many_entries_were_dropped(probe, monkeypatch):
    # 持つのは最新 LOG_LIMIT 件。since より後で捨てた分を lost で返す（黙って飛ばさない。?37）
    import collections
    setup(probe)
    probe._break_log = collections.deque(maxlen=3)
    for seq in range(1, 7):
        probe._break_log.append({"seq": seq})
    out = probe.breakpoint_log(since=1)
    assert [e["seq"] for e in out["entries"]] == [4, 5, 6] and out["lost"] == 2


def test_a_condition_without_a_truth_value_is_an_error_not_true():
    # 真偽は値の kind で決める。構造体・読めない値を真とみなさない: 評価できない条件として止めて知らせる（?38）
    import pytest

    from fprobe.probe_breakpoints import _truth
    assert _truth(vint(0)) is False and _truth(vint(3)) is True and _truth(vfloat("nan")) is True
    assert _truth(venum(None, 0)) is False and _truth(vbool(True)) is True
    assert _truth(vptr(0, "int")) is False and _truth(vptr(0x2000, "int")) is True
    assert _truth(varray(ints(120, 0), string="x")) is True and _truth(varray(ints(0), string="")) is False
    for value in (vstruct(enum=venum("A", 1), value=vint(1)), vna("optimized out"), varray(ints(1, 2))):
        with pytest.raises(ValueError):
            _truth(value)                       # 構造体のメンバー名が enum・value でも構造体（キーで推さない）


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


def test_disabled_breakpoint_is_off_the_target_and_keeps_its_place_and_action(probe):
    """無効にすると機器から外れ（当たらない）、一覧には enabled = false で場所と動作が残る。有効に戻すと置き直す"""
    cpu = setup(probe)
    probe.breakpoint_set(hex(BP), condition="1")
    listed = probe.breakpoint_enable(hex(BP), False)["breakpoints"]
    assert BP not in cpu.breakpoints
    assert [(b["address"], b["enabled"], b["action"]["condition"]) for b in listed] == [(BP, False, "1")]
    assert "kind" not in listed[0]
    probe.breakpoint_enable(hex(BP), True)
    assert BP in cpu.breakpoints
    again = probe.breakpoint_list()["breakpoints"][0]
    assert again["enabled"] is True and again["action"]["condition"] == "1"
    with pytest.raises(KeyError):
        probe.breakpoint_enable("0x20000400", False)
    probe.breakpoint_enable(hex(BP), False)
    assert probe.breakpoint_clear(hex(BP))["breakpoints"] == []                   # 無効のものも外せる
