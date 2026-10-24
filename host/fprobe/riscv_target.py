"""RISC-V のコアを pyOCD のターゲットと同じ口で使う（U33）。RP2350 の Hazard3 は SWD の DP（ADIv6）の APB-AP の
向こうにある RISC-V の Debug Module で見る（pyOCD には RISC-V が無い）。

- ApbDmi: Arm の MEM-AP（APB-AP）の上の DMI。DM のレジスタ n は AP の番地 n × 4。
- RiscvTarget: fprobe.riscv_dm.DebugModule を、fprobe.debug.Debugger と Probe の道具が使う pyOCD の Target の口
  （get_state・halt・resume・step・レジスタ・メモリ・ブレークポイント・停止理由）に合わせる。ブレークポイントは
  トリガ（ハードウェア）だけ。走っている間に置く・外すときは一時的に止めて書き、再開する。
- RiscvConnection: Probe の _debug（pyOCD のセッションの代わり）。open_riscv で線を起こして DM を有効にする。
- 部品の表（RISCV_PARTS）: target_connect の target 名 → DM の AP の番地。core はハートの番号。

リセット・ベクタキャッチ・SWO・Flash の書き込み・RTOS 等の Cortex-M の道具は使えない（明示して断る）。
"""

import contextlib
from dataclasses import dataclass

from pyocd.core.target import Target

from .raw_dap import DapError, DpPort, RawDap
from .riscv_dm import CSR, DCSR_EBREAKM, DCSR_EBREAKU, GPR_NAMES, MC_EXECUTE, MC_LOAD, MC_STORE, DebugModule, DmError

__all__ = ["ApbDmi", "RISCV_PARTS", "RiscvConnection", "RiscvPart", "RiscvTarget", "open_riscv", "riscv_part"]


@dataclass(frozen=True)
class RiscvPart:
    name: str                     # target_connect の target
    dm_ap: int                    # Debug Module の APB-AP（ADIv6 の AP の番地）


# RP2350: DP は ADIv6。Cortex-M33 の AP は 0x2000・0x4000、RISC-V の DM の APB-AP は 0xA000（OpenOCD の rp2350 と同じ）
RISCV_PARTS = (RiscvPart("rp2350_riscv", 0xA000),)


def riscv_part(target: str | None) -> RiscvPart | None:
    name = (target or "").strip().lower()
    return next((p for p in RISCV_PARTS if p.name == name), None)


class ApbDmi:
    """MEM-AP（APB-AP）の上の DMI（DM のレジスタ n = AP の番地 n × 4）。"""

    def __init__(self, dap: RawDap, ap: int):
        self.dap, self.ap = dap, ap

    def read(self, index: int) -> int:
        return self.dap.mem_read32(self.ap, index * 4)

    def write(self, index: int, value: int) -> None:
        self.dap.mem_write32(self.ap, index * 4, value)


_GPR = {**{f"x{i}": i for i in range(32)}, **{name: i for i, name in enumerate(GPR_NAMES)}, "fp": 8}
_REASONS = {"ebreak": Target.HaltReason.BREAKPOINT, "haltreq": Target.HaltReason.USER,
            "step": Target.HaltReason.DEBUG, "resethaltreq": Target.HaltReason.USER}
_WATCH = {Target.WatchpointType.READ: MC_LOAD, Target.WatchpointType.WRITE: MC_STORE,
          Target.WatchpointType.READ_WRITE: MC_LOAD | MC_STORE}


class RiscvTarget:
    """pyOCD の Target の口の RISC-V のハート（fprobe.riscv_dm.DebugModule の上）。"""

    debug_architecture = "riscv"              # Cortex-M だけの見張り（fprobe.probe_safety）はこれを見て外す
    default_register_names = ["pc", *GPR_NAMES[1:]]
    memory_map = None
    supported_reset_types: list = []

    def __init__(self, dm: DebugModule):
        self.dm = dm
        self.breakpoints: dict[int, int] = {}                      # 番地 → トリガの番号
        self.watchpoints: dict[tuple[int, int, object], int] = {}  # (番地, 大きさ, 種類) → トリガの番号
        self.dp = None                                             # RiscvConnection が DP の口を入れる

    # --- 状態・実行 ---------------------------------------------------------------------------------

    def get_state(self):
        state = self.dm.state()
        if state == "halted":
            return Target.State.HALTED
        if state == "running":
            return Target.State.RUNNING
        if state == "unavailable":
            return Target.State.RESET                             # リセット中・電源なし（Arm で起動した RP2350 等）
        raise DmError(f"ハート {self.dm.hart} が無い")

    def halt(self) -> None:
        self.dm.halt()

    def resume(self) -> None:
        self.dm.resume()

    def step(self, disable_interrupts: bool = True, start: int = 0, end: int = 0, hook_cb=None) -> None:
        self.dm.step()                                            # dcsr.stepie = 0（既定）: 割り込みは入らない

    def get_halt_reason(self):
        cause = self.dm.halt_cause()
        if cause == "trigger":
            pc = self.dm.read_csr(CSR["dpc"])
            return Target.HaltReason.BREAKPOINT if pc in self.breakpoints else Target.HaltReason.WATCHPOINT
        return _REASONS.get(cause)                                # dcsr.cause が表に無ければ None（分からない）

    def enable_ebreak(self) -> None:
        """M・U モードの ebreak でデバッグモードに入る（Arm の BKPT と同じく、ソフトのブレーク命令で止まる）。"""
        dcsr = self.dm.read_csr(CSR["dcsr"])
        self.dm.write_csr(CSR["dcsr"], dcsr | DCSR_EBREAKM | DCSR_EBREAKU)

    @contextlib.contextmanager
    def _halted(self):
        """走っていれば一時的に止める（トリガ・CSR はハートが止まっている間だけ書ける）。"""
        running = self.dm.state() != "halted"
        if running:
            self.dm.halt()
        try:
            yield
        finally:
            if running:
                self.dm.resume()

    def reset(self, reset_type=None) -> None:
        raise RuntimeError("RISC-V の接続ではリセットは未対応（target_reset は使えない）")

    def reset_and_halt(self, reset_type=None) -> None:
        self.reset(reset_type)

    def set_vector_catch(self, mask) -> None:
        raise RuntimeError("RISC-V にベクタキャッチは無い")

    def get_vector_catch(self):
        raise RuntimeError("RISC-V にベクタキャッチは無い")

    # --- レジスタ ----------------------------------------------------------------------------------

    @staticmethod
    def _register(name: str) -> tuple[str, int]:
        key = name.strip().lower()
        if key == "pc":
            return "csr", CSR["dpc"]
        if key in _GPR:
            return "gpr", _GPR[key]
        if key in CSR:
            return "csr", CSR[key]
        raise ValueError(f"レジスタ {name!r} を知らない（pc、x0〜x31・ABI の名前、CSR {', '.join(CSR)}）")

    def read_core_register(self, name: str) -> int:
        kind, number = self._register(name)
        return self.dm.read_gpr(number) if kind == "gpr" else self.dm.read_csr(number)

    read_core_register_raw = read_core_register

    def read_core_registers_raw(self, names: list[str]) -> list[int]:
        return [self.read_core_register(n) for n in names]

    def write_core_register(self, name: str, value: int) -> None:
        kind, number = self._register(name)
        if kind == "gpr":
            if number:
                self.dm.write_gpr(number, value)
        else:
            self.dm.write_csr(number, value)

    write_core_register_raw = write_core_register

    # --- メモリ ----------------------------------------------------------------------------------

    def flush(self) -> None:
        pass

    def read32(self, address: int) -> int:
        return self.dm.read_memory(address, 1)[0]

    def write32(self, address: int, value: int) -> None:
        self.dm.write_memory(address, [value])

    def read_memory(self, address: int, transfer_size: int = 32, now: bool = True) -> int:
        return self.dm.read_memory(address, 1, transfer_size // 8)[0]

    def write_memory(self, address: int, value: int, transfer_size: int = 32) -> None:
        self.dm.write_memory(address, [value], transfer_size // 8)

    def read_memory_block32(self, address: int, words: int) -> list[int]:
        return self.dm.read_memory(address, words)

    def write_memory_block32(self, address: int, data) -> None:
        self.dm.write_memory(address, list(data))

    def read_memory_block8(self, address: int, length: int) -> list[int]:
        start, end = address & ~3, (address + length + 3) & ~3
        data = b"".join(w.to_bytes(4, "little") for w in self.dm.read_memory(start, (end - start) // 4))
        return list(data[address - start:address - start + length])

    def write_memory_block8(self, address: int, data) -> None:
        data = bytes(data)
        head = min(len(data), (-address) & 3)                        # 4 の倍数の番地まではバイトで
        self.dm.write_memory(address, list(data[:head]), 1)
        middle = (len(data) - head) & ~3
        words = [int.from_bytes(data[i:i + 4], "little") for i in range(head, head + middle, 4)]
        self.dm.write_memory(address + head, words)
        self.dm.write_memory(address + head + middle, list(data[head + middle:]), 1)

    # --- ブレークポイント・ウォッチポイント ----------------------------------------------------------

    def _free_trigger(self) -> int | None:
        used = set(self.breakpoints.values()) | set(self.watchpoints.values())
        return next((i for i in range(self.dm.trigger_count()) if i not in used), None)

    def set_breakpoint(self, address: int, type=Target.BreakpointType.AUTO) -> bool:
        if type == Target.BreakpointType.SW:
            return False                                            # ハードウェア（トリガ）だけ
        if address in self.breakpoints:
            return True
        with self._halted():
            index = self._free_trigger()
            if index is None or not self.dm.set_trigger(index, address, MC_EXECUTE):
                return False
            self.breakpoints[address] = index
        return True

    def remove_breakpoint(self, address: int) -> None:
        index = self.breakpoints.pop(address, None)
        if index is not None:
            with self._halted():
                self.dm.clear_trigger(index)

    def set_watchpoint(self, address: int, size: int, type) -> bool:
        key = (address, size, type)
        if key in self.watchpoints:
            return True
        with self._halted():
            index = self._free_trigger()
            if index is None or not self.dm.set_trigger(index, address, _WATCH[type]):
                return False
            self.watchpoints[key] = index
        return True

    def remove_watchpoint(self, address: int, size: int, type) -> None:
        index = self.watchpoints.pop((address, size, type), None)
        if index is not None:
            with self._halted():
                self.dm.clear_trigger(index)


class _NoProbe:
    """pyOCD のプローブの口（SWO・ピン等）は RISC-V の接続には無い。"""

    def __getattr__(self, name):
        raise RuntimeError(f"RISC-V の接続ではプローブの {name} は使えない（target_disconnect してから）")


class RiscvConnection:
    """Probe の _debug（pyOCD のセッションの代わり）: target・probe・close。"""

    def __init__(self, link, dap: RawDap, target: RiscvTarget, part: RiscvPart):
        self.link, self.dap, self.target, self.part = link, dap, target, part
        self.probe = _NoProbe()

    def open(self) -> None:
        pass

    def close(self) -> None:
        try:
            self.dap.stop()
        finally:
            self.link.close()

    def info(self) -> dict:
        return {"arch": "riscv", "hart": self.target.dm.hart, "debug_module": self.target.dm.info.as_dict()}


def open_riscv(link, part: RiscvPart, hart: int, frequency_hz: int, connect_mode: str = "halt") -> RiscvConnection:
    """link（開いた CMSIS-DAP の層）で SWD を起こし（休眠からの起こしを先に）、DP の電源を上げ、DM を有効にする。
    connect_mode: halt（止めて ebreak でも止まるようにする）/ attach（止めない）。"""
    if connect_mode not in ("halt", "attach"):
        link.close()
        raise ValueError(f"RISC-V の connect_mode は halt / attach（{connect_mode!r} は使えない）")
    dap = RawDap(link)
    try:
        dap.start("swd", frequency_hz)
        try:
            dap.switch_to_swd(dormant=True)
            dap.read_dpidr()
        except DapError:
            dap.switch_to_swd()
            dap.read_dpidr()
        if not dap.adi_v6:
            raise DmError(f"{part.name}: DP が ADIv6（DPv3）でない（部品の選び違い）")
        if not dap.power_up()["debug_power"]:
            raise DmError(f"{part.name}: DP のデバッグ電源が上がらない")
        dm = DebugModule(ApbDmi(dap, part.dm_ap), hart)
        dm.activate()
        dm.ack_reset()
        target = RiscvTarget(dm)
        target.dp = DpPort(dap)
        if connect_mode == "halt":
            if dm.state() == "unavailable":
                raise DmError(f"ハート {hart} が使えない（unavailable: リセット中か、Arm のコアで起動している）")
            dm.halt()
            target.enable_ebreak()
        return RiscvConnection(link, dap, target, part)
    except BaseException:
        with contextlib.suppress(Exception):
            dap.stop()
        link.close()
        raise
