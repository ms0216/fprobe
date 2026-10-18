"""試験用: GDB サーバの試験の偽の Cortex-M（pyOCD の CortexM の、GDB サーバが使う口）と、それを持つ Probe。"""

from types import SimpleNamespace

from probe_fakes import DictMemory, FakeDevice
from pyocd.core.memory_map import FlashRegion, RamRegion
from pyocd.core.target import Target

from fprobe.mcp_server import Probe

CORE = [f"r{i}" for i in range(13)] + ["sp", "lr", "pc", "xpsr", "msp", "psp", "primask", "basepri", "faultmask",
                                       "control"]
FPU = [f"s{i}" for i in range(32)] + ["fpscr"]
FLASH = (0x0800_0000, 0x8_0000, 0x4000)
RAM = (0x2000_0000, 0x1_8000)


class FakeCore:
    """状態・止まった理由・レジスタ・メモリ・ブレークポイント（AUTO は Flash なら HW、RAM なら SW）・ウォッチポイント。
    resume の後は試験が hit()（ブレークポイント等で止まる）か、ほかの利用者の halt で止める。"""

    def __init__(self, memory=None, fpu: bool = True, halted: bool = True):
        self.memory = memory if memory is not None else DictMemory()
        names = CORE + (FPU if fpu else [])
        self.core_registers = SimpleNamespace(by_name={n: None for n in names})
        self.registers = {n: 0 for n in names}
        self.registers.update(pc=0x0800_0100, sp=0x2000_1000, xpsr=0x0100_0000)
        self.state = Target.State.HALTED if halted else Target.State.RUNNING
        self.reason = Target.HaltReason.DEBUG
        self.breakpoints: dict[int, Target.BreakpointType] = {}
        self.watchpoints: dict[int, tuple[int, Target.WatchpointType]] = {}
        self.memory_map = [FlashRegion(start=FLASH[0], length=FLASH[1], blocksize=FLASH[2], is_boot_memory=True),
                           RamRegion(start=RAM[0], length=RAM[1])]
        self.resumes = 0
        self.steps = 0
        self.resets: list[bool] = []

    # 状態
    def get_state(self):
        return self.state

    def get_halt_reason(self):
        return self.reason

    def halt(self):
        if self.state != Target.State.HALTED:
            self.state, self.reason = Target.State.HALTED, Target.HaltReason.USER

    def resume(self):
        self.state = Target.State.RUNNING
        self.resumes += 1

    def step(self, disable_interrupts=True):
        assert self.registers["pc"] not in self.breakpoints, "ブレークポイントの上でステップした"
        self.registers["pc"] += 2
        self.reason = Target.HaltReason.DEBUG
        self.steps += 1

    def hit(self, pc: int, reason=Target.HaltReason.BREAKPOINT) -> None:
        """実行中のコアが pc で止まる。"""
        self.registers["pc"] = pc
        self.state, self.reason = Target.State.HALTED, reason

    def reset(self, reset_type=None):
        self.resets.append(False)
        self.state = Target.State.RUNNING

    def reset_and_halt(self, reset_type=None):
        self.resets.append(True)
        self.state, self.reason = Target.State.HALTED, Target.HaltReason.DEBUG

    # レジスタ
    def read_core_register_raw(self, name):
        if name not in self.registers:
            raise KeyError(name)
        return self.registers[name]

    read_core_register = read_core_register_raw

    def read_core_registers_raw(self, names):
        return [self.read_core_register_raw(n) for n in names]

    def write_core_register_raw(self, name, value):
        self.registers[name] = value

    write_core_register = write_core_register_raw

    def write_core_registers_raw(self, names, values):
        for n, v in zip(names, values):
            self.registers[n] = v

    # メモリ
    def read_memory_block8(self, address, length):
        return list(self.memory.read_memory(address, length))

    def write_memory_block8(self, address, data):
        self.memory.write_memory(address, bytes(data))

    def read32(self, address):
        return int.from_bytes(self.memory.read_memory(address, 4), "little")

    def write32(self, address, value):
        self.memory.write_memory(address, value.to_bytes(4, "little"))

    def read_memory_block32(self, address, count):
        return [self.read32(address + 4 * i) for i in range(count)]

    def flush(self):
        pass

    # ブレークポイント・ウォッチポイント
    def set_breakpoint(self, address, type=Target.BreakpointType.AUTO):
        if type == Target.BreakpointType.AUTO:
            type = Target.BreakpointType.HW if FLASH[0] <= address < FLASH[0] + FLASH[1] else Target.BreakpointType.SW
        self.breakpoints[address] = type
        return True

    def remove_breakpoint(self, address):
        self.breakpoints.pop(address, None)

    def find_breakpoint(self, address):
        type = self.breakpoints.get(address)
        return None if type is None else SimpleNamespace(type=type, addr=address)

    def set_watchpoint(self, address, size, type):
        if size not in (1, 2, 4):
            return False
        self.watchpoints[address] = (size, type)
        return True

    def remove_watchpoint(self, address, size, type):
        self.watchpoints.pop(address, None)


def make_probe(core: FakeCore, symbols=None) -> Probe:
    """偽のコアにつながった Probe（機器は開かない）。"""
    p = Probe("unused")
    p._session = SimpleNamespace(device=FakeDevice())
    p._symbols = symbols
    p._debug = SimpleNamespace(target=core, probe=None, close=lambda: None)
    return p
