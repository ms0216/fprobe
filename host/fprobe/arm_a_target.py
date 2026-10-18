"""Cortex-A（ARMv7-A）と ARM11 のコアを pyOCD のターゲットと同じ口で使う（U32）。pyOCD にはこれらのコアのデバッグが
無いので、JTAG で直接動かす（fprobe.arm_debug の手順、道は fprobe.armv7a_debug / fprobe.arm11_debug）。

- ArmATarget: fprobe.arm_debug.ArmDebugCore を、fprobe.debug.Debugger と Probe の道具が使う pyOCD の Target の口
  （get_state・halt・resume・step・レジスタ・メモリ・ブレークポイント・停止理由）に合わせる。ブレークポイントは
  ハードウェアの組だけ。走っている間に置く・外すときは一時的に止めて書き、再開する。
- ArmAConnection: Probe の _debug（pyOCD のセッションの代わり）。open_arm_a で線を起こし、コアを見つける。
- 部品の表（ARM_A_PARTS）: target_connect の target 名 → 種類（ARMv7-A / ARM11）・JTAG の IDCODE・コアのデバッグの
  番地の控え（ROM テーブルが読めないとき）。core はコアの番号。

チェーンの中の装置は jtag_scan と同じ走査で見つける（IDCODE）。jtag_chain を与えればその IR 長の列を使う。
リセット・ベクタキャッチ・SWO・Flash の書き込み・RTOS 等の Cortex-M の道具は使えない（明示して断る）。止めるのは
選んだコアだけ（ほかのコアは走り続ける。コアをまたぐ停止は CTI が要る: U31）。
"""

import contextlib
from dataclasses import dataclass

from pyocd.core.target import Target

from .arm11_debug import Arm11Port
from .arm_debug import REGISTER_NAMES, ArmDebugCore, ArmDebugError
from .armv7a_debug import V7DebugPort, find_apb_ap, find_debug_bases
from .jtag_chain import dap_link_wire, scan_chain
from .raw_dap import DpPort, RawDap

__all__ = ["ARM_A_PARTS", "ArmAConnection", "ArmAPart", "ArmATarget", "arm_a_part", "open_arm_a"]


@dataclass(frozen=True)
class ArmAPart:
    name: str                               # target_connect の target
    kind: str                               # "v7"（JTAG-DP → APB-AP の CoreSight）/ "arm11"（TAP で直接）
    idcode: int                             # チェーンで探す装置の IDCODE（版の 4 ビットは見ない）
    debug_bases: tuple[int, ...] = ()       # v7: ROM テーブルが読めないときのコアのデバッグの番地
    description: str = ""


ARM_A_PARTS = (
    ArmAPart("cortex_a", "v7", 0x0BA00477, description="ARMv7-A（JTAG-DP。コアは ROM テーブルから）"),
    # BCM2836: 4 コアのデバッグ 0x80010000 + 0x2000 × n（OpenOCD の target/bcm2836.cfg と同じ。実機で未確認）
    ArmAPart("bcm2836", "v7", 0x0BA00477, (0x80010000, 0x80012000, 0x80014000, 0x80016000),
             "Raspberry Pi 2 Model B（BCM2836、Cortex-A7 × 4）"),
    ArmAPart("arm11", "arm11", 0x07B7617F, description="ARM1176JZF-S（ARM11 の TAP）"),
    ArmAPart("bcm2835", "arm11", 0x07B7617F, description="Raspberry Pi Zero / 1（BCM2835、ARM1176JZF-S）"),
)


def arm_a_part(target: str | None) -> ArmAPart | None:
    name = (target or "").strip().lower()
    return next((p for p in ARM_A_PARTS if p.name == name), None)


_REASONS = {"breakpoint": Target.HaltReason.BREAKPOINT, "bkpt": Target.HaltReason.BREAKPOINT,
            "watchpoint": Target.HaltReason.WATCHPOINT, "step": Target.HaltReason.DEBUG,
            "halt_request": Target.HaltReason.USER, "external": Target.HaltReason.EXTERNAL,
            "vector_catch": Target.HaltReason.VECTOR_CATCH}
_WATCH = {Target.WatchpointType.READ: "read", Target.WatchpointType.WRITE: "write",
          Target.WatchpointType.READ_WRITE: "access"}


class ArmATarget:
    """pyOCD の Target の口の Cortex-A / ARM11 のコア（fprobe.arm_debug.ArmDebugCore の上）。"""

    default_register_names = REGISTER_NAMES
    memory_map = None
    supported_reset_types: list = []

    def __init__(self, core: ArmDebugCore, index: int = 0):
        self.core = core
        self.index = index
        self.debug_architecture = "armv7-a" if core.v7 else "armv6"   # Cortex-M だけの見張りはこれを見て外す
        self.breakpoints: dict[int, int] = {}                          # 番地 → 組の番号
        self.watchpoints: dict[tuple[int, int, object], int] = {}
        self.dp = None                                                 # ArmAConnection が DP の口を入れる

    # --- 状態・実行 ---------------------------------------------------------------------------------

    def get_state(self):
        return Target.State.HALTED if self.core.state() == "halted" else Target.State.RUNNING

    def halt(self) -> None:
        self.core.halt()

    def resume(self) -> None:
        self.core.resume()

    def step(self, disable_interrupts: bool = True, start: int = 0, end: int = 0, hook_cb=None) -> None:
        self.core.step(mask_interrupts=disable_interrupts)

    def get_halt_reason(self):
        return _REASONS.get(self.core.halt_cause(), Target.HaltReason.USER)

    @contextlib.contextmanager
    def _halted(self):
        """走っていれば一時的に止める（ARM11 のスキャンチェーン 7 は止まっている間に書く。ARMv7 も有効な組の書き替えは
        止めて行う）。"""
        running = self.core.state() != "halted"
        if running:
            self.core.halt()
        try:
            yield
        finally:
            if running:
                self.core.resume()

    def reset(self, reset_type=None) -> None:
        raise RuntimeError("Cortex-A・ARM11 の接続ではリセットは未対応（target_reset は使えない。Pi は電源か RUN の"
                           "ピンで）")

    def reset_and_halt(self, reset_type=None) -> None:
        self.reset(reset_type)

    def set_vector_catch(self, mask) -> None:
        raise RuntimeError("Cortex-A・ARM11 のベクタキャッチは未対応（Cortex-M の DEMCR の意味では置けない）")

    def get_vector_catch(self):
        raise RuntimeError("Cortex-A・ARM11 のベクタキャッチは未対応")

    # --- レジスタ ----------------------------------------------------------------------------------

    def read_core_register(self, name: str) -> int:
        return self.core.read_register(name)

    read_core_register_raw = read_core_register

    def read_core_registers_raw(self, names: list[str]) -> list[int]:
        return [self.read_core_register(n) for n in names]

    def write_core_register(self, name: str, value: int) -> None:
        self.core.write_register(name, value)

    write_core_register_raw = write_core_register

    # --- メモリ（コアを通す。番地はコアの見る番地） -----------------------------------------------------

    def flush(self) -> None:
        pass

    def read32(self, address: int) -> int:
        return self.core.read_memory(address, 1)[0]

    def write32(self, address: int, value: int) -> None:
        self.core.write_memory(address, [value])

    def read_memory(self, address: int, transfer_size: int = 32, now: bool = True) -> int:
        return self.core.read_memory(address, 1, transfer_size // 8)[0]

    def write_memory(self, address: int, value: int, transfer_size: int = 32) -> None:
        self.core.write_memory(address, [value], transfer_size // 8)

    def read_memory_block32(self, address: int, words: int) -> list[int]:
        return self.core.read_memory(address, words)

    def write_memory_block32(self, address: int, data) -> None:
        self.core.write_memory(address, list(data))

    def read_memory_block8(self, address: int, length: int) -> list[int]:
        start, end = address & ~3, (address + length + 3) & ~3
        data = b"".join(w.to_bytes(4, "little") for w in self.core.read_memory(start, (end - start) // 4))
        return list(data[address - start:address - start + length])

    def write_memory_block8(self, address: int, data) -> None:
        data = bytes(data)
        head = min(len(data), (-address) & 3)                        # 4 の倍数の番地まではバイトで
        self.core.write_memory(address, list(data[:head]), 1)
        middle = (len(data) - head) & ~3
        words = [int.from_bytes(data[i:i + 4], "little") for i in range(head, head + middle, 4)]
        self.core.write_memory(address + head, words)
        self.core.write_memory(address + head + middle, list(data[head + middle:]), 1)

    # --- ブレークポイント・ウォッチポイント ----------------------------------------------------------

    def set_breakpoint(self, address: int, type=Target.BreakpointType.AUTO) -> bool:
        if type == Target.BreakpointType.SW:
            return False                                            # ハードウェアの組だけ
        if address in self.breakpoints:
            return True
        used = set(self.breakpoints.values())
        index = next((i for i in range(self.core.breakpoint_count - 1) if i not in used), None)
        if index is None:
            return False
        with self._halted():
            self.core.set_breakpoint(index, address)
        self.breakpoints[address] = index
        return True

    def remove_breakpoint(self, address: int) -> None:
        index = self.breakpoints.pop(address, None)
        if index is not None:
            with self._halted():
                self.core.clear_breakpoint(index)

    def set_watchpoint(self, address: int, size: int, type) -> bool:
        key = (address, size, type)
        if key in self.watchpoints:
            return True
        used = set(self.watchpoints.values())
        index = next((i for i in range(self.core.watchpoint_count) if i not in used), None)
        if index is None:
            return False
        with self._halted():
            if not self.core.set_watchpoint(index, address, size, _WATCH[type]):
                return False
        self.watchpoints[key] = index
        return True

    def remove_watchpoint(self, address: int, size: int, type) -> None:
        index = self.watchpoints.pop((address, size, type), None)
        if index is not None:
            with self._halted():
                self.core.clear_watchpoint(index)


class _NoProbe:
    """pyOCD のプローブの口（SWO・ピン等）は Cortex-A・ARM11 の接続には無い。"""

    def __getattr__(self, name):
        raise RuntimeError(f"Cortex-A・ARM11 の接続ではプローブの {name} は使えない（target_disconnect してから）")


class _NoDp:
    """ARM11 は DP を持たない（TAP の命令で直接）。"""

    def __getattr__(self, name):
        raise RuntimeError("ARM11 には DP が無い（JTAG の TAP を直接使う）: dp_read / ap_read は使えない")


class ArmAConnection:
    """Probe の _debug（pyOCD のセッションの代わり）: target・probe・close。"""

    def __init__(self, link, dap: RawDap, target: ArmATarget, part: ArmAPart, details: dict):
        self.link, self.dap, self.target, self.part, self.details = link, dap, target, part, details
        self.probe = _NoProbe()

    def open(self) -> None:
        pass

    def close(self) -> None:
        try:
            self.dap.stop()
        finally:
            self.link.close()

    def info(self) -> dict:
        return {"arch": self.target.debug_architecture, "core": self.target.index, **self.target.core.info(),
                **self.details}


def _find_tap(wire, part: ArmAPart, jtag_chain: list[int] | None) -> tuple[list[int], int]:
    """チェーンを走査して part の装置（IDCODE）の位置と IR 長の列を返す。"""
    scan = scan_chain(wire)
    index = next((d.index for d in scan.devices
                  if d.idcode is not None and d.idcode & 0x0FFFFFFF == part.idcode & 0x0FFFFFFF), None)
    if index is None:
        seen = ", ".join(f"0x{d.idcode:08X}" if d.idcode is not None else "BYPASS" for d in scan.devices) or "なし"
        name = "ARM1176" if part.kind == "arm11" else "Arm の JTAG-DP"
        raise ArmDebugError(f"{part.name}: チェーンに {name}（IDCODE 0x{part.idcode:08X}）が見えない（見えた装置: "
                            f"{seen}。{' / '.join(scan.notes)}）。配線・ターゲットの JTAG の有効化"
                            f"（Pi は config.txt の enable_jtag_gpio=1）・TRST を確かめる")
    chain = list(jtag_chain) if jtag_chain is not None else scan.chain
    if chain is None or len(chain) != len(scan.devices):
        raise ArmDebugError(f"{part.name}: チェーンの IR 長が分からない（jtag_chain で与える）: {scan.as_dict()}")
    return chain, index


def _open_v7(dap: RawDap, wire, part: ArmAPart, core: int, frequency_hz: int, jtag_chain) -> tuple:
    chain, index = _find_tap(wire, part, jtag_chain)
    dap.start("jtag", frequency_hz, chain, dap_index=index)
    dap.read_dpidr()
    if not dap.power_up()["debug_power"]:
        raise ArmDebugError(f"{part.name}: DP のデバッグ電源が上がらない")
    ap = find_apb_ap(dap)
    if ap is None:
        raise ArmDebugError(f"{part.name}: APB-AP が無い（コアのデバッグレジスタに届かない）")
    bases, discovery = find_debug_bases(dap, ap), "rom_table"
    if not bases:
        if not part.debug_bases:
            raise ArmDebugError(f"{part.name}: APB-AP の ROM テーブルからコアのデバッグが見つからない（部品の名前で"
                                "番地の表のあるもの、例えば bcm2836 を選ぶ）")
        bases, discovery = list(part.debug_bases), "part_table"
    if not 0 <= core < len(bases):
        raise ValueError(f"{part.name} の core は 0〜{len(bases) - 1}")
    port = V7DebugPort(dap, ap, bases[core])
    port.unlock()
    details = {"cores": len(bases), "debug_base": bases[core], "apb_ap": ap, "discovery": discovery,
               "jtag_chain": chain, "tap": index}
    return port, details


def open_arm_a(link, part: ArmAPart, core: int, frequency_hz: int, connect_mode: str = "halt",
               jtag_chain: list[int] | None = None) -> ArmAConnection:
    """link（開いた CMSIS-DAP の層）で JTAG を起こし、チェーンから part の装置を見つけ、停止デバッグを有効にする。
    connect_mode: halt（止める）/ attach（止めない）。"""
    if connect_mode not in ("halt", "attach"):
        link.close()
        raise ValueError(f"Cortex-A・ARM11 の connect_mode は halt / attach（{connect_mode!r} は使えない）")
    dap = RawDap(link)
    try:
        dap.start("jtag", frequency_hz, jtag_chain or [4])
        dap.switch_to_jtag()
        wire = dap_link_wire(link)
        if part.kind == "v7":
            port, details = _open_v7(dap, wire, part, core, frequency_hz, jtag_chain)
        else:
            if core != 0:
                raise ValueError(f"{part.name} はコアが 1 つ（core は 0）")
            chain, index = _find_tap(wire, part, jtag_chain)
            port, details = Arm11Port(wire, chain, index), {"cores": 1, "jtag_chain": chain, "tap": index}
        debug = ArmDebugCore(port)
        debug.enable()
        target = ArmATarget(debug, core)
        target.dp = DpPort(dap) if part.kind == "v7" else _NoDp()
        if connect_mode == "halt":
            debug.halt()
        return ArmAConnection(link, dap, target, part, details)
    except BaseException:
        with contextlib.suppress(Exception):
            dap.stop()
        link.close()
        raise
