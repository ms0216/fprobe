"""試験用: 命令の意味を辞書で与える小さな Cortex-M（pyOCD の CortexM の、ステップ・実行・ブレークポイント・
レジスタ・メモリ・DFSR の口）と、行単位のステップの試験に使う偽の行表。

命令（番地 → タプル）:
    ("op",)                 何もしない 2 バイトの命令        ("op", 4) は 4 バイト
    ("call", target)        BL（4 バイト）: lr = 次の番地 | 1、pc = target
    ("ret",)                BX LR（lr が EXC_RETURN なら例外からの戻り）
    ("push",)               PUSH {lr}: sp -= 4、[sp] = lr
    ("pop_pc",)             POP {pc}（EXC_RETURN なら例外からの戻り）
    ("jump", target)        B
    ("loop", target, n)     最初の n 回は target へ跳び、その後は次へ（ループの後ろ向きの分岐）
    ("do", fn)              fn(cpu) を実行して次へ（2 バイト）。戻り値・メモリの書き込み等
    ("bkpt",)               BKPT（止まる。PC は進まない）
    ("fault",)              HardFault を起こす（例外 3 に入る）
    ("spin",)               自分へ跳び続ける（終わらない処理）
"""

from dataclasses import dataclass
from types import SimpleNamespace

from pyocd.core.target import Target

DFSR, DEMCR, VTOR = 0xE000ED30, 0xE000EDFC, 0xE000ED08
HALTED, BKPT, DWTTRAP, VCATCH, EXTERNAL = 1, 2, 4, 8, 16
VC_HARDERR = 1 << 10
EXC_RETURN_THREAD_MSP = 0xFFFFFFF9
XPSR_T = 1 << 24
RUN_LIMIT = 100_000                         # resume 1 回で実行する命令の上限（越えたら走ったまま）

CORE = [f"r{i}" for i in range(13)] + ["sp", "lr", "pc", "xpsr", "msp", "psp", "primask", "basepri", "faultmask",
                                       "control"]
FPU = [f"s{i}" for i in range(32)] + ["fpscr"]


class FakeCpu:
    def __init__(self, program: dict[int, tuple], pc: int, sp: int = 0x20001000, lr: int = 0xFFFFFFFF,
                 handlers: dict[int, int] | None = None, fpu: bool = True):
        self.program = program
        names = CORE + (FPU if fpu else [])
        self.regs = {n: 0 for n in names}
        self.regs.update(pc=pc, sp=sp, lr=lr, xpsr=XPSR_T)
        self.memory: dict[int, int] = {}
        self.handlers = dict(handlers or {})
        self.breakpoints: set[int] = set()
        self.state = Target.State.HALTED
        self.dfsr = HALTED
        self.pending: int | None = None          # 保留中の割り込み（例外番号）
        self.counters: dict[int, int] = {}
        self.steps = 0
        self.step_masks: list[bool] = []
        self.resumes = 0
        self.breakpoint_limit = 6
        self.core_registers = SimpleNamespace(by_name={n: None for n in names})
        self.supported_reset_types = [Target.ResetType.HARDWARE, Target.ResetType.SYSRESETREQ,
                                      Target.ResetType.VECTRESET, Target.ResetType.EMULATED]
        self.resets: list[tuple] = []

    # --- 実行 ----------------------------------------------------------------------------------

    @property
    def pc(self) -> int:
        return self.regs["pc"]

    @property
    def ipsr(self) -> int:
        return self.regs["xpsr"] & 0x1FF

    def _enter_exception(self, number: int, return_pc: int) -> None:
        sp = self.regs["sp"] - 32
        frame = [self.regs["r0"], self.regs["r1"], self.regs["r2"], self.regs["r3"], self.regs["r12"],
                 self.regs["lr"], return_pc, self.regs["xpsr"]]
        for i, word in enumerate(frame):
            self.write32(sp + 4 * i, word)
        self.regs.update(sp=sp, lr=EXC_RETURN_THREAD_MSP, pc=self.handlers[number],
                         xpsr=(self.regs["xpsr"] & ~0x1FF) | number)

    def _return_from_exception(self) -> None:
        sp = self.regs["sp"]
        r0, r1, r2, r3, r12, lr, pc, xpsr = (self.read32(sp + 4 * i) for i in range(8))
        self.regs.update(r0=r0, r1=r1, r2=r2, r3=r3, r12=r12, lr=lr, pc=pc, xpsr=xpsr, sp=sp + 32)

    def _branch(self, target: int) -> None:
        if target >> 24 == 0xFF:
            self._return_from_exception()
        else:
            self.regs["pc"] = target & ~1

    def execute(self, interrupts: bool) -> int:
        """1 命令（か例外の入口）。止まる理由（DFSR のビット。0 = 普通に進んだ）を返す。"""
        if interrupts and self.pending is not None and not self.regs["primask"]:
            number, self.pending = self.pending, None
            self._enter_exception(number, self.pc)
            return 0
        pc = self.pc
        op = self.program.get(pc)
        if op is None:
            raise AssertionError(f"0x{pc:08X} に命令が無い")
        kind = op[0]
        if kind == "op":
            self.regs["pc"] = pc + (op[1] if len(op) > 1 else 2)
        elif kind == "call":
            self.regs.update(lr=(pc + 4) | 1, pc=op[1])
        elif kind == "ret":
            self._branch(self.regs["lr"])
        elif kind == "push":
            self.regs["sp"] -= 4
            self.write32(self.regs["sp"], self.regs["lr"])
            self.regs["pc"] = pc + 2
        elif kind == "pop_pc":
            value = self.read32(self.regs["sp"])
            self.regs["sp"] += 4
            self._branch(value)
        elif kind == "jump":
            self.regs["pc"] = op[1]
        elif kind == "loop":
            count = self.counters.get(pc, 0)
            if count < op[2]:
                self.counters[pc] = count + 1
                self.regs["pc"] = op[1]
            else:
                self.counters[pc] = 0
                self.regs["pc"] = pc + 2
        elif kind == "do":
            op[1](self)
            self.regs["pc"] = pc + 2
        elif kind == "bkpt":
            return BKPT
        elif kind == "fault":
            self._enter_exception(3, pc)
            if self.read32(DEMCR) & VC_HARDERR:
                return VCATCH
        elif kind == "spin":
            pass
        else:
            raise AssertionError(f"知らない命令 {op}")
        return 0

    def step(self, disable_interrupts: bool = True, start: int = 0, end: int = 0, hook_cb=None) -> None:
        assert self.state == Target.State.HALTED
        assert self.pc not in self.breakpoints, "ブレークポイントの上で 1 命令進めると、実機では止まり直す"
        self.steps += 1
        self.step_masks.append(disable_interrupts)
        self.dfsr = self.execute(not disable_interrupts) or HALTED

    def resume(self) -> None:
        assert self.state == Target.State.HALTED
        self.resumes += 1
        self.dfsr = 0
        self.state = Target.State.RUNNING
        for _ in range(RUN_LIMIT):
            if self.pc in self.breakpoints:
                return self._stop(BKPT)
            reason = self.execute(True)
            if reason:
                return self._stop(reason)

    def _stop(self, reason: int) -> None:
        self.state, self.dfsr = Target.State.HALTED, reason

    def halt(self) -> None:
        if self.state == Target.State.RUNNING:
            self._stop(HALTED)

    def get_state(self):
        return self.state

    def get_halt_reason(self):
        return {HALTED: Target.HaltReason.DEBUG, BKPT: Target.HaltReason.BREAKPOINT,
                VCATCH: Target.HaltReason.VECTOR_CATCH}.get(self.dfsr)

    # --- ブレークポイント -------------------------------------------------------------------------

    def set_breakpoint(self, address: int, kind=None) -> bool:
        if address not in self.breakpoints and len(self.breakpoints) >= self.breakpoint_limit:
            return False
        self.breakpoints.add(address)
        return True

    def remove_breakpoint(self, address: int) -> None:
        self.breakpoints.discard(address)

    # --- レジスタ ----------------------------------------------------------------------------------

    def _name(self, name: str) -> str:
        return {"msp": "sp"}.get(name, name)

    def read_core_register(self, name: str) -> int:
        return self.regs[self._name(name)]

    read_core_register_raw = read_core_register

    def read_core_registers_raw(self, names) -> list[int]:
        return [self.read_core_register(n) for n in names]

    def write_core_register(self, name: str, value: int) -> None:
        self.regs[self._name(name)] = value

    write_core_register_raw = write_core_register

    def write_core_registers_raw(self, names, values) -> None:
        for n, v in zip(names, values):
            self.write_core_register(n, v)

    # --- メモリ -------------------------------------------------------------------------------------

    def read32(self, address: int) -> int:
        if address == DFSR:
            return self.dfsr
        return int.from_bytes(bytes(self.memory.get(address + i, 0) for i in range(4)), "little")

    def write32(self, address: int, value: int) -> None:
        if address == DFSR:
            self.dfsr &= ~value
            return
        for i, b in enumerate((value & 0xFFFFFFFF).to_bytes(4, "little")):
            self.memory[address + i] = b

    def read_memory_block8(self, address: int, length: int) -> list[int]:
        return [self.memory.get(address + i, 0) for i in range(length)]

    def write_memory_block8(self, address: int, data) -> None:
        for i, b in enumerate(data):
            self.memory[address + i] = b

    def read_memory_block32(self, address: int, count: int) -> list[int]:
        return [self.read32(address + 4 * i) for i in range(count)]

    def read_memory(self, address: int, transfer_size: int = 32) -> int:
        return self.read32(address)

    def write_memory(self, address: int, value: int, transfer_size: int = 32) -> None:
        self.write32(address, value)

    def flush(self) -> None:
        pass

    # --- リセット・ベクタキャッチ（pyOCD の口） -------------------------------------------------------

    def reset(self, reset_type=None) -> None:
        self.resets.append(("reset", reset_type))
        self.state = Target.State.RUNNING

    def reset_and_halt(self, reset_type=None) -> None:
        self.resets.append(("reset_and_halt", reset_type))
        self.state = Target.State.HALTED

    def set_vector_catch(self, mask: int) -> None:
        self.vector_catch = mask

    def get_vector_catch(self) -> int:
        return getattr(self, "vector_catch", 0)


@dataclass(frozen=True)
class FakeFunction:
    name: str
    address: int
    size: int
    kind: str = "function"


@dataclass(frozen=True)
class FakeEntry:
    address: int
    file: str
    line: int


class FakeLines:
    """Symbols の、行単位のステップが使うところだけ（関数の範囲・行表・前置きの後・場所の名前）。
    functions: [(名前, 先頭, 大きさ, 前置きの後)]、rows: [(番地, 行)]（ファイルはすべて "demo.c"）。"""

    path = None
    entry = 0

    def __init__(self, functions, rows):
        self.functions = [FakeFunction(n, a, s) for n, a, s, _ in functions]
        self.bodies = {a: b for _, a, _, b in functions}
        self.rows = sorted(rows)

    def function_at(self, address):
        return next((f for f in self.functions if f.address <= address < f.address + f.size), None)

    def line_entry(self, address):
        function = self.function_at(address)
        if function is None:
            return None
        rows = [r for r in self.rows if function.address <= r[0] <= address]
        return FakeEntry(rows[-1][0], "demo.c", rows[-1][1]) if rows else None

    def after_prologue(self, address):
        return self.bodies.get(address, address)

    def resolve(self, location):
        if isinstance(location, int):
            return location
        if ":" in location:
            line = int(location.rpartition(":")[2])
            return min(a for a, n in self.rows if n == line)
        return next(f.address for f in self.functions if f.name == location)

    def describe(self, address):
        from fprobe.symbols import Location
        function = self.function_at(address)
        entry = self.line_entry(address)
        return Location(address, function.name if function else None,
                        address - function.address if function else None,
                        entry.file if entry else None, entry.line if entry else None)
