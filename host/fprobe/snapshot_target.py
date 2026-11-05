"""開いたスナップショットを pyOCD の CortexM の読みの口に見せる（fprobe.probe_snapshot の snapshot_open が使う）。"""

import struct

from pyocd.core import exceptions as pyocd_errors
from pyocd.core.target import Target

from .debug import _REASONS
from .dwarf.access import AccessError
from .snapshot import Snapshot, SnapshotAccess

__all__ = ["SnapshotReadOnly", "SnapshotTarget"]


class SnapshotReadOnly(RuntimeError):
    """開いたスナップショットに書こうとした・実行しようとした。"""


def _read_only(*_args, **_kwargs):
    raise SnapshotReadOnly("スナップショットは読むだけ（書き込み・実行は target_connect で実物に）")


class SnapshotTarget:
    """pyOCD の CortexM の読みの口を、スナップショットで満たす（Debugger・PyocdAccess・fault・TargetMemory が使う所）。
    状態はいつも HALTED、止まった理由は保存した時のもの。書き込み・実行は SnapshotReadOnly。"""

    memory_map: list = []

    def __init__(self, snapshot: Snapshot):
        self.snapshot = snapshot
        self.access = SnapshotAccess(snapshot)

    # 状態
    def get_state(self):
        return Target.State.HALTED

    def get_halt_reason(self):
        name = (self.snapshot.info.get("halt") or {}).get("reason")
        return {v: k for k, v in _REASONS.items()}.get(name)      # 保存した理由が "unknown" 等なら None

    # メモリ（読めない番地は pyOCD と同じくバスエラー）
    def read_memory_block8(self, address, length):
        try:
            return list(self.access.read_memory(address, length))
        except AccessError as e:
            raise pyocd_errors.TransferFaultError(str(e)) from None

    def read_memory_block32(self, address, count):
        return list(struct.unpack(f"<{count}I", bytes(self.read_memory_block8(address, 4 * count))))

    def read_memory(self, address, transfer_size=32, now=True):
        return int.from_bytes(bytes(self.read_memory_block8(address, transfer_size // 8)), "little")

    def read32(self, address):
        return self.read_memory(address, 32)

    def read16(self, address):
        return self.read_memory(address, 16)

    def read8(self, address):
        return self.read_memory(address, 8)

    # コアレジスタ
    def read_core_register_raw(self, name):
        try:
            return self.access.read_register(name)
        except AccessError as e:
            raise pyocd_errors.CoreRegisterAccessError(str(e)) from None

    read_core_register = read_core_register_raw

    def read_core_registers_raw(self, names):
        return [self.read_core_register_raw(n) for n in names]

    def flush(self):
        pass

    write_memory = write_memory_block8 = write_memory_block32 = write32 = write16 = write8 = _read_only
    write_core_register = write_core_register_raw = write_core_registers_raw = _read_only
    resume = halt = step = reset = reset_and_halt = _read_only
    set_breakpoint = remove_breakpoint = set_watchpoint = remove_watchpoint = _read_only
