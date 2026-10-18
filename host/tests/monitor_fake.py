"""試験用: モニタ方式（fprobe.monitor）の偽のコア。pyOCD の CortexM の口（停止方式）と、ファームのモニタ
（host/firmware/monitor/fprobe_monitor.c）と同じ規則で動く DebugMon を持つ。

時間は口を 1 回使うごとに 1 歩進む（_tick）。1 歩で:
- 優先度の高い割り込み（SysTick の見立て）が fast_ticks を 1 増やす。止まっている間（モニタの中）も増える。停止方式で
  止まっていれば増えない。
- モニタの中なら、箱の命令を見る（GO / STEP）。
- 走っていれば、スレッドの命令を 1 つ進める（LOOP から 8 命令の輪。WRITER の命令が loop_count を書く）。8 命令ごとに
  優先度の低い割り込み（PendSV の見立て）が slow_ticks を 1 増やす。モニタの中・停止方式の停止中は増えない。
- デバッグの出来事（ブレークポイント・ウォッチポイント・MON_PEND・MON_STEP）は、C_DEBUGEN = 1 なら停止方式で止め、
  C_DEBUGEN = 0 かつ DEMCR.MON_EN なら DebugMon に入る（ファームの入口と同じく、例外の枠を積み、箱に写す）。どちらでも
  なければ HardFault に格上げ（hardfaults を数えるだけ）。
"""

from types import SimpleNamespace

from pyocd.core.target import Target

TABLE = 0x2000_0000            # ベクタ表（VTOR）
LOOP = 0x2000_0100             # スレッドの輪（2 バイトの命令 × 8）
WORK = LOOP + 4                # ブレークポイントの的
WRITER = LOOP + 8              # loop_count を書く命令
HANDLER = 0x2000_0400          # DebugMon_Handler（頭に印）
OTHER_HANDLER = 0x2000_0480    # モニタでないハンドラ
BOX = 0x2000_0600
FAST, SLOW, LOOPS = 0x2000_0800, 0x2000_0804, 0x2000_0808
MSP_TOP, PSP_TOP = 0x2000_7F00, 0x2000_6004

DHCSR, DFSR, DEMCR, VTOR = 0xE000EDF0, 0xE000ED30, 0xE000EDFC, 0xE000ED08
SHPR3, SHCSR, SYST_CSR, ICTR = 0xE000ED20, 0xE000ED24, 0xE000E010, 0xE000E004
NVIC_ISER, NVIC_IPR = 0xE000E100, 0xE000E400
DBGKEY = 0xA05F0000
MON_EN, MON_PEND, MON_STEP, TRCENA = 1 << 16, 1 << 17, 1 << 18, 1 << 24
HALTED, BKPT, DWTTRAP = 1, 2, 4
MAGIC = 0x4E4D5046
ALIGNED = 1 << 9

O_STATE, O_REASON, O_STOPS, O_SEQ_CMD, O_CMD, O_ARG, O_SEQ_ACK, O_RESULT = 0x10, 0x14, 0x18, 0x1C, 0x20, 0x24, 0x28, 0x2C
O_EXC, O_MSP, O_PSP, O_PRIMASK, O_BASEPRI, O_FAULTMASK, O_CONTROL, O_SP = 0x30, 0x34, 0x38, 0x3C, 0x40, 0x44, 0x48, 0x4C
O_FRAME, O_R4, O_S16, O_EXIT, O_STEP_BASEPRI, O_STEP_FORCED, O_PRIO_BITS, O_STACK = (
    0x50, 0xB8, 0xD8, 0x118, 0x11C, 0x120, 0x124, 0x128)

CORE = [f"r{i}" for i in range(13)] + ["sp", "lr", "pc", "xpsr", "msp", "psp", "primask", "basepri", "faultmask",
                                       "control"]
FPU = [f"s{i}" for i in range(32)] + ["fpscr"]


class MonitorChip:
    """installed: ベクタ表の DebugMon がモニタか。initialized: fprobe_monitor_init を呼んだか。fp: 割り込まれる処理が
    FPU を使っている（FP 付きの枠）。psp: スレッドが PSP で動く（RTOS のタスクの見立て）。odd_sp: SP が 8 の倍数でない
    （枠の整列の詰め物）。blocked: DebugMon に入れない区間に留まっている（優先度の高い処理・割り込み禁止）。"""

    def __init__(self, installed: bool = True, initialized: bool = True, fp: bool = False, psp: bool = False,
                 odd_sp: bool = False, priority: int = 0x80, prio_bits: int = 0xF0):
        self.words: dict[int, int] = {}
        self.regs = {n: 0 for n in CORE + FPU}
        for i in range(13):
            self.regs[f"r{i}"] = 0x1000 + i
        for i in range(32):
            self.regs[f"s{i}"] = 0x3F80_0000 + i
        self.regs["fpscr"] = 0x0300_0000
        self.psp = psp
        top = (PSP_TOP if psp else MSP_TOP) - (4 if odd_sp else 0)
        self.regs.update(sp=top, lr=0x2000_0201, pc=LOOP, xpsr=1 << 24, control=2 if psp else 0)
        self.other_sp = MSP_TOP if psp else PSP_TOP       # 使っていない方のスタックポインタ
        self.fp = fp
        self.installed, self.blocked = installed, False
        self.debugen, self.halted = True, True            # 接続（halt）の直後
        self.dfsr, self.demcr = HALTED, 0
        self.priority = priority & prio_bits
        self.prio_bits = prio_bits
        self.in_monitor = False
        self.step_armed = False
        self.step_args: list[int] = []
        self.breakpoints: dict[int, object] = {}
        self.watches: set[int] = set()
        self.executed = 0
        self.hardfaults = 0
        self.resets = 0
        self.time = 0
        self.memory_map = None
        self.core_registers = SimpleNamespace(by_name={n: None for n in CORE + FPU})
        self.supported_reset_types = [Target.ResetType.SYSRESETREQ, Target.ResetType.HARDWARE]
        self.vector_catch = 0
        # ベクタ表・ハンドラの頭（b.n 1f; nop; "FPMN"; 箱の番地）・モニタでないハンドラ
        self.words[TABLE + 4 * 12] = (HANDLER if installed else OTHER_HANDLER) | 1
        self._store32(HANDLER, 0xBF00E004)
        self._store32(HANDLER + 4, MAGIC)
        self._store32(HANDLER + 8, BOX)
        self._store32(OTHER_HANDLER, 0xE7FEE7FE)
        if initialized:
            self._init_box()
        # 止まっている間も動く割り込み: SysTick（優先度 0）、IRQ3（0x40）。動かない: SVCall・PendSV（0xF0）、IRQ5（0xC0）
        self.words[SYST_CSR] = 7
        self.words[SHPR3 - 4] = 0xF000_0000               # SHPR2: SVCall
        self.words[SHPR3] = 0x00F0_0000                   # SHPR3: SysTick 0x00、PendSV 0xF0
        self.words[NVIC_ISER] = (1 << 3) | (1 << 5)
        self.words[NVIC_IPR] = 0x4000_0000                # IRQ3 = 0x40
        self.words[NVIC_IPR + 4] = 0x0000_C000            # IRQ5 = 0xC0

    def _init_box(self) -> None:
        for offset, value in ((0, MAGIC), (4, 1), (8, 0x12C), (0xC, 1), (O_PRIO_BITS, self.prio_bits),
                              (O_STACK, 1024)):
            self._store32(BOX + offset, value)

    # --- メモリ ------------------------------------------------------------------------------------

    def _load32(self, address: int) -> int:
        if address == DHCSR:
            return int(self.debugen) | (int(self.halted) << 17) | (1 << 16)
        if address == DFSR:
            return self.dfsr
        if address == DEMCR:
            return self.demcr
        if address == VTOR:
            return TABLE
        if address == SHPR3:
            return self.words.get(SHPR3, 0) & 0xFFFFFF00 | self.priority
        return self.words.get(address, 0)

    def _store32(self, address: int, value: int) -> None:
        value &= 0xFFFFFFFF
        if address == DHCSR:
            assert value & 0xFFFF0000 == DBGKEY, "DHCSR は DBGKEY と一緒に書く"
            debugen = bool(value & 1)
            assert debugen or not self.halted, "止まったまま C_DEBUGEN を消した（結果は定まらない）"
            self.debugen = debugen
            if debugen and value & 2:
                if not self.halted:
                    self.dfsr |= HALTED
                self.halted = True
            elif self.halted:
                self.halted = False
        elif address == DFSR:
            self.dfsr &= ~value
        elif address == DEMCR:
            self.demcr = value
        elif address == SHPR3:
            self.priority = value & self.prio_bits & 0xFF
            self.words[SHPR3] = value & 0xFFFFFF00
        else:
            self.words[address] = value

    def _load8(self, address: int) -> int:
        return self._load32(address & ~3) >> 8 * (address & 3) & 0xFF

    def _store8(self, address: int, value: int) -> None:
        base = address & ~3
        shift = 8 * (address & 3)
        word = self._load32(base) & ~(0xFF << shift) | (value & 0xFF) << shift
        self._store32(base, word)

    def read32(self, address: int, now: bool = True) -> int:
        self._tick()
        return self._load32(address)

    def write32(self, address: int, value: int) -> None:
        self._tick()
        self._store32(address, value)

    def read_memory(self, address: int, transfer_size: int = 32, now: bool = True) -> int:
        assert transfer_size == 32
        return self.read32(address)

    def write_memory(self, address: int, value: int, transfer_size: int = 32) -> None:
        assert transfer_size == 32
        self.write32(address, value)

    def read_memory_block32(self, address: int, count: int) -> list[int]:
        self._tick()
        return [self._load32(address + 4 * i) for i in range(count)]

    def write_memory_block32(self, address: int, data) -> None:
        self._tick()
        for i, value in enumerate(data):
            self._store32(address + 4 * i, value)

    def read_memory_block8(self, address: int, length: int) -> list[int]:
        self._tick()
        return [self._load8(address + i) for i in range(length)]

    def write_memory_block8(self, address: int, data) -> None:
        self._tick()
        for i, value in enumerate(data):
            self._store8(address + i, value)

    def flush(self) -> None:
        pass

    def box(self, offset: int) -> int:
        return self._load32(BOX + offset)

    def var(self, address: int) -> int:
        return self._load32(address)

    # --- 停止方式の口 ------------------------------------------------------------------------------

    def get_state(self):
        self._tick()
        return Target.State.HALTED if self.halted else Target.State.RUNNING

    def halt(self) -> None:
        self._store32(DHCSR, DBGKEY | 3)

    def resume(self) -> None:
        assert self.debugen and self.halted
        self._store32(DHCSR, DBGKEY | 1)

    def step(self, disable_interrupts: bool = True, start: int = 0, end: int = 0, hook_cb=None) -> None:
        assert self.halted and self.debugen
        self._execute()
        self.dfsr |= HALTED

    def get_halt_reason(self):
        if self.dfsr & DWTTRAP:
            return Target.HaltReason.WATCHPOINT
        if self.dfsr & BKPT:
            return Target.HaltReason.BREAKPOINT
        return Target.HaltReason.DEBUG if self.dfsr & HALTED else Target.HaltReason.USER

    def _require_halted(self) -> None:
        if not self.halted:
            raise RuntimeError("コアが走っている（停止方式のレジスタの読み書きは止めてから）")

    def read_core_registers_raw(self, names) -> list[int]:
        self._require_halted()
        return [self.regs[n] for n in names]

    def read_core_register_raw(self, name) -> int:
        return self.read_core_registers_raw([name])[0]

    def read_core_register(self, name) -> int:
        return self.read_core_register_raw(name)

    def write_core_registers_raw(self, names, values) -> None:
        self._require_halted()
        for n, v in zip(names, values):
            self.regs[n] = v

    def write_core_register_raw(self, name, value) -> None:
        self.write_core_registers_raw([name], [value])

    def write_core_register(self, name, value) -> None:
        self.write_core_registers_raw([name], [value])

    def set_breakpoint(self, address: int, type=Target.BreakpointType.AUTO) -> bool:
        self.breakpoints[address] = type
        return True

    def remove_breakpoint(self, address: int) -> None:
        self.breakpoints.pop(address, None)

    def find_breakpoint(self, address: int):
        if address not in self.breakpoints:
            return None
        return SimpleNamespace(addr=address, type=self.breakpoints[address])

    def set_watchpoint(self, address: int, size: int, type) -> bool:
        self.watches.add(address)
        return True

    def remove_watchpoint(self, address: int, size: int, type) -> None:
        self.watches.discard(address)

    def set_vector_catch(self, mask: int) -> None:
        self.vector_catch = mask

    def get_vector_catch(self) -> int:
        return self.vector_catch

    def reset(self, reset_type=None) -> None:
        self.resets += 1
        self.in_monitor = False
        self.regs.update(pc=LOOP)

    def reset_and_halt(self, reset_type=None) -> None:
        self.reset(reset_type)
        self.halted = self.debugen = True

    # --- 時間 ---------------------------------------------------------------------------------------

    def _tick(self) -> None:
        self.time += 1
        if self.halted:
            return
        self.words[FAST] = self.words.get(FAST, 0) + 1
        if self.in_monitor:
            self._monitor_poll()
            return
        if self.demcr & MON_PEND and self.installed and not self.blocked:   # 保留の例外: C_DEBUGEN に依らない
            self._enter(0)
            return
        if self.regs["pc"] in self.breakpoints:
            self._event(BKPT)
            return
        writes = self._execute()
        if writes and LOOPS in self.watches:
            self._event(DWTTRAP)
        elif self.step_armed:
            self.step_armed = False
            self._enter(HALTED)

    def _execute(self) -> bool:
        """スレッドの命令を 1 つ進める。loop_count を書いたら True。"""
        pc = self.regs["pc"]
        self.executed += 1
        if self.executed % 8 == 0:
            self.words[SLOW] = self.words.get(SLOW, 0) + 1
        self.regs["pc"] = LOOP if pc >= LOOP + 14 else pc + 2
        if pc == WRITER:
            self.words[LOOPS] = self.words.get(LOOPS, 0) + 1
            return True
        return False

    def _can_enter_monitor(self) -> bool:
        return not self.debugen and bool(self.demcr & MON_EN) and self.installed and not self.blocked

    def _event(self, bits: int) -> None:
        if self.debugen:
            self.halted = True
            self.dfsr |= bits
        elif self._can_enter_monitor():
            self._enter(bits)
        else:
            self.hardfaults += 1                              # BKPT は HardFault に格上げ（ここでは数えるだけ）
            self.regs["pc"] = LOOP if self.regs["pc"] >= LOOP + 14 else self.regs["pc"] + 2

    # --- モニタ（fprobe_monitor.c と同じ規則） --------------------------------------------------------------

    def _frame_words(self) -> int:
        return 26 if self.fp else 8

    def _enter(self, bits: int) -> None:
        r = self.regs
        n = self._frame_words()
        frame = r["sp"] - 4 * n
        pad = frame & 4
        frame -= pad
        words = [r["r0"], r["r1"], r["r2"], r["r3"], r["r12"], r["lr"], r["pc"], r["xpsr"] | (ALIGNED if pad else 0)]
        if self.fp:
            words += [r[f"s{i}"] for i in range(16)] + [r["fpscr"], 0]
        for i, w in enumerate(words):
            self._store32(frame + 4 * i, w)
        exc_return = 0xFFFFFFE1 | 0x8 | (0x4 if self.psp else 0) | (0 if self.fp else 0x10)
        b = BOX
        self._store32(b + O_EXC, exc_return)
        self._store32(b + O_MSP, self.other_sp if self.psp else frame)
        self._store32(b + O_PSP, frame if self.psp else self.other_sp)
        for i in range(8):
            self._store32(b + O_R4 + 4 * i, r[f"r{4 + i}"])
        if self.fp:
            for i in range(16):
                self._store32(b + O_S16 + 4 * i, r[f"s{16 + i}"])
        # 本体
        dfsr = (self.dfsr | bits) & 0x1F
        self.dfsr = 0
        self.demcr &= ~(MON_STEP | MON_PEND)
        if self.box(O_STEP_FORCED):
            if r["basepri"] == self.box(O_STEP_FORCED):
                r["basepri"] = self.box(O_STEP_BASEPRI)
            self._store32(b + O_STEP_FORCED, 0)
        for i in range(n):
            self._store32(b + O_FRAME + 4 * i, self._load32(frame + 4 * i))
        self._store32(b + O_SP, frame + 4 * n + (4 if pad else 0))
        self._store32(b + O_FRAME + 28, self.box(O_FRAME + 28) & ~ALIGNED)
        self._store32(b + O_PRIMASK, r["primask"])
        self._store32(b + O_BASEPRI, r["basepri"])
        self._store32(b + O_FAULTMASK, r["faultmask"])
        self._store32(b + O_CONTROL, r["control"] & 1)
        self._store32(b + O_REASON, dfsr)
        self._store32(b + O_STOPS, self.box(O_STOPS) + 1)
        self._store32(b + O_STATE, 1)
        self.in_monitor = True

    def _monitor_poll(self) -> None:
        seq = self.box(O_SEQ_CMD)
        if seq == self.box(O_SEQ_ACK):
            return
        cmd = self.box(O_CMD)
        if cmd not in (1, 2):
            self._store32(BOX + O_RESULT, 1)
            self._store32(BOX + O_SEQ_ACK, seq)
            return
        self._exit(cmd, self.box(O_ARG), seq)

    def _exit(self, cmd: int, arg: int, seq: int) -> None:
        r = self.regs
        n = self._frame_words()
        at = (self.box(O_SP) & ~3) - 4 * n
        xpsr = self.box(O_FRAME + 28) & ~ALIGNED
        pad = at & 4
        if pad:
            at -= 4
            xpsr |= ALIGNED
        for i in range(n):
            self._store32(at + 4 * i, xpsr if i == 7 else self.box(O_FRAME + 4 * i))
        self._store32(BOX + O_EXIT, at)
        r["basepri"] = self.box(O_BASEPRI) & 0xFF
        if cmd == 2:
            self.step_args.append(arg)
            if arg & 1:
                level = self.priority + (self.prio_bits & -self.prio_bits)
                if level <= 0xFF and not (r["basepri"] and r["basepri"] <= level):
                    self._store32(BOX + O_STEP_BASEPRI, r["basepri"])
                    self._store32(BOX + O_STEP_FORCED, level)
                    r["basepri"] = level
            self.demcr |= MON_STEP
            self.step_armed = True
        self._store32(BOX + O_RESULT, 0)
        self._store32(BOX + O_STATE, 0)
        self._store32(BOX + O_SEQ_ACK, seq)
        r["primask"] = self.box(O_PRIMASK) & 1
        # 例外からの戻り（ハード）: 枠を下ろし、r4〜r11・s16〜s31 は出口のアセンブラが箱から
        words = [self._load32(at + 4 * i) for i in range(n)]
        for name, w in zip(["r0", "r1", "r2", "r3", "r12", "lr", "pc", "xpsr"], words):
            r[name] = w
        r["xpsr"] &= ~ALIGNED
        if self.fp:
            for i in range(16):
                r[f"s{i}"] = words[8 + i]
            r["fpscr"] = words[24]
            for i in range(16):
                r[f"s{16 + i}"] = self.box(O_S16 + 4 * i)
        r["sp"] = at + 4 * n + (4 if words[7] & ALIGNED else 0)
        for i in range(8):
            r[f"r{4 + i}"] = self.box(O_R4 + 4 * i)
        self.in_monitor = False
