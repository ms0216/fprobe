"""ほかの試験が呼ばない API のメソッド（変数の読み書き・ウォッチポイントの解除・RAM への読み込み）を偽の CPU で呼び、
応答を仕様の result と照合する（照合は tests/api_result_check.py が呼ぶたびにする）。"""

from test_probe_breakpoints import BreakCpu, setup  # noqa: F401
from test_probe_observe import RTT_DEMO, probe  # noqa: F401（probe は fixture）

from fprobe.symbols import Symbols


class LoadCpu(BreakCpu):
    """RAM への読み込み（Debugger.load_to_ram）とウォッチポイントの口を足した偽の CPU。"""

    def __init__(self):
        super().__init__(hits=0)
        self.watches = {}

    def reset_and_halt(self, reset_type=None):
        pass

    def write32(self, address, value):
        if 0x20000000 <= address < 0x20008000:
            self.write_memory_block8(address, list(value.to_bytes(4, "little")))

    def set_watchpoint(self, address, size, kind):
        self.watches[address] = (size, kind)
        return True

    def remove_watchpoint(self, address, size, kind):
        self.watches.pop(address, None)


def _connect(probe):
    from test_probe_observe import connect
    cpu = LoadCpu()
    connect(probe, cpu)
    probe._symbols = Symbols(RTT_DEMO)
    probe.semihosting_configure(enabled=False)
    return cpu


def test_a_variable_is_written_and_read_back_by_name(probe):
    _connect(probe)
    assert probe.variable_write("ticks", 0x1234) is None
    assert probe.variable_read("ticks") == {"name": "ticks", "value": 0x1234}
    assert probe.variable_read("up1_buffer")["value"] == "00" * 256          # 1・2・4・8 バイトでなければ 16 進


def test_clearing_a_watchpoint_returns_the_remaining_breakpoints_and_watchpoints(probe):
    cpu = _connect(probe)
    probe.watchpoint_set("ticks", kind="write")
    assert cpu.watches
    out = probe.watchpoint_clear("ticks")
    assert out["watchpoints"] == [] and not cpu.watches


def test_program_ram_loads_the_elf_and_says_where_it_starts(probe):
    cpu = _connect(probe)
    out = probe.program_ram(str(RTT_DEMO))
    entry = Symbols(RTT_DEMO).entry
    assert out["start"] == {"from": "entry", "entry": entry, "vector_table": None}
    assert out["pc"] == cpu.regs["pc"] == entry & ~1 and out["reason"] == "user"
