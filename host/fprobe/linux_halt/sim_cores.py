"""テスト用の模擬コア（`CoreAccess` を満たす）。実機の挙動の模型ではなく、上の層を試すための決まった振る舞い。"""
from __future__ import annotations

from dataclasses import replace

from .core_access import MOE_BKPT_INSN, MOE_HALT, CoreState, StopReport


class SimCores:
    def __init__(self, n_cores: int, simultaneous: bool, skew_upper_bound_ns: int | None,
                 fail_halt_on: frozenset[int] = frozenset(), halt_moe: int = MOE_HALT,
                 park_after_first: bool = False, raise_after_halt: Exception | None = None,
                 fail_resume: Exception | None = None, kernel_base: int = 0):
        self.n_cores = n_cores
        self._simultaneous = simultaneous
        self._bound = skew_upper_bound_ns
        self._fail = fail_halt_on
        self._halted = [False] * n_cores
        self.reg_values: dict[tuple[int, str], int] = {}
        self.reg_lists: dict[int, list[int]] = {}
        self.cp15: dict[tuple[int, str], int] = {}
        self.mem = bytearray(0x100000)
        self.hw_bps: dict[tuple[int, int], int] = {}
        self.hw_watches: dict[tuple[int, int], tuple[int, int, str]] = {}
        self.events: list[tuple[tuple[int, ...], int]] = []
        self.sync_count = 0
        self.synced_cores: list[tuple[int, ...]] = []
        self.steps: list[int] = []
        self.states: dict[int, CoreState] = {c: CoreState(False) for c in range(n_cores)}   # テストが書く
        self.resumed: list[tuple[int, ...]] = []
        self.fetches: dict[tuple[int, int], int] = {}
        self.on_step = None                  # on_step(core) -> None: テストが 1 命令ぶん状態を進める
        self.parked: set[int] = set()
        self.empty_waits = 0
        self._halt_moe = halt_moe                  # MOE of a core stopped by halt_all (a CTI request enters with MOE_EXT)
        self._park_after_first = park_after_first   # resume() resumes one core and parks the rest (like a backend whose first
        #                                             resumed core stopped by itself)
        self._raise_after_halt = raise_after_halt
        self._fail_resume = fail_resume
        self._kernel_base = kernel_base            # read_kernel(va) = read_phys(va - kernel_base)
        self.memory_needs_halt = False             # True: 実機（ArmACores）と同じく、コア 0 が止まっていないとメモリに触れない

    def halt_all(self) -> StopReport:
        for c in range(self.n_cores):
            if c not in self._fail:
                self._halted[c] = True
        for c in range(self.n_cores):
            if self._halted[c] and c not in self._fail and self.states[c].moe == MOE_HALT:
                self.states[c] = replace(self.states[c], moe=self._halt_moe)
        if self._raise_after_halt is not None:
            raise self._raise_after_halt
        done = tuple(c for c in range(self.n_cores) if self._halted[c])
        return StopReport("simultaneous" if self._simultaneous else "sequential", done, self._bound,
                          "simulated")

    def resume_all(self) -> None:
        self._halted = [False] * self.n_cores

    def halted(self) -> tuple[bool, ...]:
        return tuple(self._halted)

    def queue_event(self, *cores: int, moe: int = MOE_BKPT_INSN) -> None:
        """The next wait_event returns these cores as halted by themselves, with debug-entry reason `moe`."""
        self.events.append((tuple(cores), moe))

    def wait_event(self, timeout_s: float) -> tuple[int, ...]:
        if not self.events:
            self.empty_waits += 1
            if self.empty_waits > 200:           # the test scripted fewer events than the code under test waits for
                raise RuntimeError("SimCores: nothing more to wait for (no event was scripted)")
            return ()
        self.empty_waits = 0
        cores, moe = self.events.pop(0)
        for c in cores:
            self._halted[c] = True
            self.states[c] = replace(self.states[c], moe=moe)
        return cores

    def step(self, core: int) -> None:
        self.steps.append(core)
        self._forget_entry_reason(core)          # a stepped core re-enters debug state by the step, not by its old event
        if self.on_step is not None:
            self.on_step(core)

    def read_reg(self, core: int, name: str) -> int:
        return self.reg_values.get((core, name), 0)

    def write_reg(self, core: int, name: str, value: int) -> None:
        self.reg_values[(core, name)] = value

    def read_cp15(self, core: int, name: str) -> int:
        return self.cp15.get((core, name), 0)

    def read_kernel(self, va: int, length: int) -> bytes:
        return self.read_phys(va - self._kernel_base, length)

    def write_kernel(self, va: int, data: bytes) -> None:
        self.write_phys(va - self._kernel_base, data)

    def read_phys(self, addr: int, length: int) -> bytes:
        self._require_halt()
        return bytes(self.mem[addr:addr + length])

    def write_phys(self, addr: int, data: bytes) -> None:
        self._require_halt()
        self.mem[addr:addr + len(data)] = data

    def _require_halt(self) -> None:
        if self.memory_needs_halt and not self._halted[0]:
            raise RuntimeError("SimCores: core 0 is running (memory is read and written through a halted core)")

    def set_hw_bp(self, core: int, slot: int, addr: int, width: int = 4) -> None:
        self.hw_bps[(core, slot)] = addr

    def clear_hw_bp(self, core: int, slot: int) -> None:
        self.hw_bps.pop((core, slot), None)

    def set_hw_watch(self, core: int, slot: int, addr: int, length: int, kind: str) -> None:
        self.hw_watches[(core, slot)] = (addr, length, kind)

    def clear_hw_watch(self, core: int, slot: int) -> None:
        self.hw_watches.pop((core, slot), None)

    def sync_caches(self, cores=None) -> None:
        self.sync_count += 1
        self.synced_cores.append(tuple(range(self.n_cores)) if cores is None else tuple(sorted(cores)))

    def resume(self, cores) -> None:
        cores = tuple(cores)
        if self._fail_resume is not None:
            raise self._fail_resume
        self.parked = set()
        if self._park_after_first and len(cores) > 1:
            self.parked = set(cores[1:])
            cores = cores[:1]
        self.resumed.append(cores)
        for c in cores:
            self._halted[c] = False
            self._forget_entry_reason(c)         # the next stop of a resumed core is a requested halt unless scripted again

    def _forget_entry_reason(self, core: int) -> None:
        self.states[core] = replace(self.states[core], moe=MOE_HALT)

    def snapshot(self, cores=None):
        cores = range(self.n_cores) if cores is None else cores
        return {c: replace(self.states[c], halted=self._halted[c]) for c in cores}

    def regs(self, core: int):
        return list(self.reg_lists.get(core, [0] * 17))

    def set_regs(self, core: int, vals) -> None:
        self.reg_lists[core] = list(vals)

    def fetch(self, core: int, va: int, width: int) -> int:
        return self.fetches.get((core, va), 0)
