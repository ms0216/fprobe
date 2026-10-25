"""Probe の道具: スナップショット（止めた状態の保存・プローブ無しでの調査・書き戻し・ELF のコアファイル）。

Probe（fprobe.probe。共通の部品の型は fprobe.probe_host.ProbeHost）に混ぜて使う mixin。Probe の _lock・_debug・target・_symbols・_target_type・_recorder・
_session と、InspectTools（SVD・周辺の一括読みの守り・target_fault）、RtosTools（_kernel）を使う。
機器に触るのは必ず _lock の中。

- 保存（snapshot_save）: コアレジスタ（浮動小数点も）・RAM（ターゲットのメモリマップか ranges）・Flash（任意）・
  システム制御空間のうち読んでも状態が変わらない所（SCB・NVIC・SysTick・CPACR・MPU の窓・FPU の設定。DHCSR 等の
  デバッグのレジスタは読むと固定のビットが消えるので読まない）・SVD の周辺レジスタ（readAction とデータ・FIFO の口は
  読まない。peripheral_read の一括読みと同じ守り）・停止の理由と場所・RTOS のタスクの一覧・故障の解析・記録の参照
  （記録中なら記録の名前とデバイスの時刻、記録にも stream "snapshot" の出来事）・ELF の指紋（既定で ELF も埋め込む）。
- 開く（snapshot_open）: 未接続のとき、Probe の target を読むだけのスナップショット（SnapshotTarget）に差し替える。
  呼び出し履歴・C の式・RTOS・周辺レジスタ・故障の解析・メモリの表示が、そのままスナップショットを読む。
  書き込み・実行は SnapshotReadOnly。target_connect で閉じる。
- 書き戻し（snapshot_restore）: 接続中のターゲットを止め、ELF のコード（スナップショットの RAM の外）が同じかを
  確かめてから、RAM・システムのレジスタ（VTOR・優先度・割り込みの許可と保留・SysTick・CPACR・FPU）・名前を挙げた
  周辺・コアレジスタの順に書く。resume で再開。
"""

import re
import struct
from datetime import datetime
from pathlib import Path

from pyocd.core import exceptions as pyocd_errors
from pyocd.core.target import Target

from .home import fprobe_home
from . import fault
from .api import NotConnected
from .debug import _REASONS
from .dwarf.access import AccessError
from .rtos import detect
from .snapshot import SUFFIX, ElfPrint, Region, Snapshot, SnapshotAccess, merge_regions, write_core
from .symbols import Symbols, is_network_path

__all__ = ["SnapshotReadOnly", "SnapshotTarget", "SnapshotTools"]

BASE_REGISTERS = [f"r{i}" for i in range(13)] + ["sp", "lr", "pc", "xpsr", "msp", "psp", "control", "primask"]
OPTIONAL_REGISTERS = ["basepri", "faultmask", "fpscr"] + [f"s{i}" for i in range(32)]
# 書き戻しの順: 特別なレジスタ（control で sp の指す先が決まる）→ 汎用 → pc・xpsr → 浮動小数点。sp は msp / psp で決まる
RESTORE_ORDER = (["control", "msp", "psp", "primask", "basepri", "faultmask"] + [f"r{i}" for i in range(13)]
                 + ["lr", "pc", "xpsr", "fpscr"] + [f"s{i}" for i in range(32)])
READ_CHUNK = 0x4000

# システム制御空間（読んでも状態が変わらない所）
ICTR, SYST_CSR, SYST_RVR, SYST_CVR = 0xE000E004, 0xE000E010, 0xE000E014, 0xE000E018
NVIC_ISER, NVIC_ICER, NVIC_ISPR, NVIC_ICPR, NVIC_IABR, NVIC_IPR = (0xE000E100, 0xE000E180, 0xE000E200, 0xE000E280,
                                                                    0xE000E300, 0xE000E400)
SCB, VTOR, AIRCR, SCR, CCR, SHPR1, SHPR2, SHPR3, SHCSR = (0xE000ED00, 0xE000ED08, 0xE000ED0C, 0xE000ED10, 0xE000ED14,
                                                         0xE000ED18, 0xE000ED1C, 0xE000ED20, 0xE000ED24)
MPU_TYPE, DEMCR, FPCCR, FPDSCR = 0xE000ED90, 0xE000EDFC, 0xE000EF34, 0xE000EF3C
AIRCR_VECTKEY, AIRCR_PRIGROUP = 0x05FA0000, 0x700
SHCSR_ENABLES = 0x000F0000                         # MEMFAULTENA・BUSFAULTENA・USGFAULTENA・SECUREFAULTENA
_V6M_PARTNOS = {0xC20, 0xC60, 0xD20}


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


class SnapshotTools:
    """スナップショットの道具（Probe の mixin）。"""

    def _init_snapshot(self) -> None:
        self._snapshot_target: SnapshotTarget | None = None
        self._snapshot_path: Path | None = None
        self._snapshot_elf: Path | None = None

    def _offline_target(self):
        """未接続でスナップショットを開いていれば、その読み手（Probe.target が使う）。"""
        return self._snapshot_target if self._debug is None else None

    def _close_snapshot(self) -> None:
        self._snapshot_target = self._snapshot_path = self._snapshot_elf = None
        self._dbg = None

    def _live(self):
        """接続中のターゲット（スナップショットではない）。"""
        if self._debug is None:
            raise NotConnected("ターゲット未接続（target_connect を先に呼ぶ）")
        return self._debug.target

    @staticmethod
    def _snapshots_dir() -> Path:
        return fprobe_home() / "snapshots"

    # --- 保存 -------------------------------------------------------------------------------

    def snapshot_save(self, name: str = "", path: str | None = None, halt: bool = False,
                      ranges: list[list[int]] | None = None, flash: bool = False,
                      peripherals: list[str] | None = None, embed_elf: bool = True, note: str = "",
                      skip_registers: list[str] | None = None) -> dict:
        """止まったターゲットの状態を 1 つのファイル（.fsnap）に保存する。halt なら動いていても止めてから。
        ranges（[[番地, 長さ], …]）を省くとメモリマップの RAM すべて。flash で Flash も。peripherals は SVD の
        周辺の名前（省略で SVD が分かればすべて、[] で読まない）。読むと状態が変わる周辺レジスタ（SVD の
        readAction）は読まない。readAction の無いレジスタは名前から推さずに読む: ほかに読まないものは skip_registers
        （"周辺.レジスタ" の glob。target_peripheral_read の skip と同じ）で渡す。"""
        with self._lock:
            target = self._live()
            if target.get_state() != Target.State.HALTED:
                if not halt:
                    raise RuntimeError("ターゲットが動いている: 止めてから（target_halt）か halt=True")
                target.halt()
                self._watch_stops(False)
            ticks = self._device_time()
            registers = self._capture_registers(target)
            skipped: list[dict] = []
            regions = self._capture_memory(target, ranges, flash, skipped)
            regions += self._capture_system(target, "fpscr" in registers, regions, skipped)
            regions += self._capture_peripherals(target, peripherals, regions, skipped, tuple(skip_registers or ()))
            info = {"target": self._target_type, "halt": self._halt_info(target, registers["pc"]),
                    "elf_path": str(self._symbols.path) if self._symbols else None,
                    "svd": self._svd_info, "rtos": self._capture_rtos(), "fault": self._capture_fault(),
                    "recording": None, "note": note, "skipped": skipped}
            if self._recorder is not None:
                info["recording"] = {"name": self._recorder.directory.name, "time": ticks}
            elf = ElfPrint.of(self._symbols.path) if self._symbols else None
            snap = Snapshot(registers, regions, info, elf,
                            self._symbols.path.read_bytes() if self._symbols and embed_elf else None)
            out_path = Path(path) if path else self._snapshots_dir() / self._file_name(name)
            if is_network_path(out_path):
                raise ValueError(f"外の共有には書かない: {out_path}")
            snap.save(out_path)
            if self._recorder is not None and ticks is not None:
                self._recorder.add("snapshot", "save", ticks, body={"path": str(out_path), "pc": registers["pc"]})
            return {"path": str(out_path), **snap.summary(), "skipped": skipped}

    @staticmethod
    def _file_name(name: str) -> str:
        stamp = datetime.now().strftime("%Y%m%d-%H%M%S-%f")
        safe = re.sub(r"[^\w\-]+", "_", name).strip("_")
        return f"{stamp}-{safe}{SUFFIX}" if safe else f"{stamp}{SUFFIX}"

    def _device_time(self) -> int | None:
        try:
            return int(self._session.device.time()) if self._session is not None else None
        except Exception:                                         # タイムベースの無い構成
            return None

    @staticmethod
    def _capture_registers(target) -> dict[str, int]:
        registers = dict(zip(BASE_REGISTERS, target.read_core_registers_raw(BASE_REGISTERS)))
        known = getattr(getattr(target, "core_registers", None), "by_name", None)
        for name in OPTIONAL_REGISTERS:
            if known is not None and name not in known:
                continue
            try:
                registers[name] = target.read_core_register_raw(name)
            except (pyocd_errors.Error, KeyError, ValueError):
                pass                                              # その核に無いレジスタ（FPU 無し・ARMv6-M）
        return registers

    def _halt_info(self, target, pc: int) -> dict:
        reason = _REASONS.get(target.get_halt_reason(), "unknown")
        return {"reason": reason, "pc": pc, **self._place_of(pc)}

    @staticmethod
    def _read_region(target, address: int, length: int, kind: str, name: str, skipped: list) -> list[Region]:
        out = []
        for start in range(address, address + length, READ_CHUNK):
            size = min(READ_CHUNK, address + length - start)
            try:
                out.append(Region(start, bytes(target.read_memory_block8(start, size)), kind, name))
            except pyocd_errors.Error as e:
                skipped.append({"path": f"{name}@0x{start:08X}", "reason": f"read failed: {type(e).__name__}: {e}"})
        return merge_regions(out)

    def _capture_memory(self, target, ranges, flash: bool, skipped: list) -> list[Region]:
        if ranges is not None:
            wanted = [(int(a), int(n), "ram", f"0x{int(a):08X}") for a, n in ranges]
        else:
            memory_map = getattr(target, "memory_map", None) or []
            wanted = [(r.start, r.length, "ram", getattr(r, "name", "") or "RAM") for r in memory_map
                      if getattr(r, "is_ram", False)]
            if not wanted:
                raise RuntimeError("RAM の範囲が分からない（ranges = [[番地, 長さ], …] を指定する）")
        if flash:
            memory_map = getattr(target, "memory_map", None) or []
            wanted += [(r.start, r.length, "flash", getattr(r, "name", "") or "Flash") for r in memory_map
                       if getattr(r, "is_flash", False)]
        regions: list[Region] = []
        for address, length, kind, name in wanted:
            regions += self._read_region(target, address, length, kind, name, skipped)
        return regions

    def _capture_system(self, target, fpu: bool, taken: list[Region], skipped: list) -> list[Region]:
        """システム制御空間のうち、読んでも状態が変わらない所（DHCSR・DFSR 以外のデバッグのレジスタは読まない）。"""
        partno = target.read32(fault.CPUID) >> 4 & 0xFFF
        if partno in _V6M_PARTNOS:                                # ARMv6-M: 割り込みは 32 本、SCB は少ない
            wanted = [(SYST_CSR, 16, "SysTick"), (NVIC_ISER, 4, "NVIC.ISER"), (NVIC_ISPR, 4, "NVIC.ISPR"),
                      (NVIC_IPR, 32, "NVIC.IPR"), (SCB, 0x18, "SCB"), (SHPR2, 12, "SCB.SHPR"),
                      (fault.DFSR, 4, "SCB.DFSR"), (DEMCR, 4, "DEMCR")]
        else:
            lines = 32 * ((target.read32(ICTR) & 0xF) + 1)
            wanted = [(ICTR, 4, "ICTR"), (SYST_CSR, 16, "SysTick"), (NVIC_ISER, lines // 8, "NVIC.ISER"),
                      (NVIC_ISPR, lines // 8, "NVIC.ISPR"), (NVIC_IABR, lines // 8, "NVIC.IABR"),
                      (NVIC_IPR, lines, "NVIC.IPR"), (SCB, 0x40, "SCB"), (fault.CPACR, 4, "SCB.CPACR"),
                      (MPU_TYPE, 0x14, "MPU"), (DEMCR, 4, "DEMCR")]
            if fpu:
                wanted.append((FPCCR, 12, "FPU"))
        regions = []
        for address, length, name in wanted:
            if not _overlaps(address, length, taken):
                regions += self._read_region(target, address, length, "system", name, skipped)
        return regions

    def _capture_peripherals(self, target, names: list[str] | None, taken: list[Region],
                             skipped: list, skip: tuple[str, ...] = ()) -> list[Region]:
        if names is not None and not names:
            return []
        try:
            device = self._device()
        except Exception:
            if names:
                raise                                             # 名前を挙げたのに SVD が無い
            return []                                             # SVD が分からない: 周辺は取らない
        peripherals = device.peripherals if names is None else [device.peripheral(n) for n in names]
        regions: list[Region] = []
        for p in peripherals:
            for r in p.registers:
                reason = self._bulk_skip_reason(r, False, skip)
                size = max(1, r.size // 8)
                if reason is None and _overlaps(r.address, size, taken + regions):
                    continue                                      # SVD の SCB・NVIC 等（システムとして取った）
                if reason is None:
                    try:
                        value = target.read_memory(r.address, transfer_size=r.size)
                        regions.append(Region(r.address, (value & (1 << r.size) - 1).to_bytes(size, "little"),
                                              "peripheral", r.path))
                        continue
                    except Exception as e:                        # 時計の止まった周辺のバスエラー等: 他は読む
                        reason = f"read failed: {type(e).__name__}: {e}"
                skipped.append({"path": r.path, "reason": reason})
        return regions

    def _capture_rtos(self) -> dict | None:
        if self._symbols is None:
            return None
        try:
            info = self._rtos_debug_info()
            if detect(info) is None:
                return None
            return self._kernel().snapshot()
        except Exception as e:                                    # DWARF が無い・リストが壊れている
            return {"error": f"{type(e).__name__}: {e}"}

    def _capture_fault(self) -> dict | None:
        try:
            return self.target_fault()
        except Exception as e:
            return {"error": f"{type(e).__name__}: {e}"}

    # --- 開く --------------------------------------------------------------------------------

    def snapshot_open(self, path: str, elf: str | None = None) -> dict:
        """スナップショットを開く（未接続のとき）。以後、呼び出し履歴・C の式・RTOS・周辺レジスタ・故障の解析・メモリの
        読みがスナップショットに対して働く（書き込み・実行はできない）。ELF は elf、無ければ埋め込んだもの、それも無ければ
        保存した時の場所（指紋が合うときだけ）。target_connect で閉じる。"""
        with self._lock:
            if self._debug is not None:
                raise RuntimeError("接続中は開けない（target_disconnect してから。接続中の状態は snapshot_save で保存）")
            snap = Snapshot.load(path)
            elf_path, warnings = self._snapshot_elf_path(snap, elf)
            self._close_snapshot()
            self._snapshot_target = SnapshotTarget(snap)
            self._snapshot_path, self._snapshot_elf = Path(path), elf_path
            self._symbols = Symbols(elf_path) if elf_path is not None else None
            self._forget_elf()
            self._target_type = snap.info.get("target") or self._target_type
            self._restore_svd(snap.info.get("svd"), warnings)
            return {"path": str(path), **snap.summary(), "elf_path": str(elf_path) if elf_path else None,
                    "warnings": warnings}

    def _snapshot_elf_path(self, snap: Snapshot, elf: str | None) -> tuple[Path | None, list[str]]:
        warnings: list[str] = []
        if snap.elf is None:
            if elf is not None:
                warnings.append("スナップショットに ELF の指紋が無い: 指定の ELF をそのまま使う")
                return Path(elf), warnings
            return None, ["ELF の無いスナップショット: 番地で読む"]
        if elf is not None:
            if not snap.elf.matches(elf):
                raise ValueError(f"{elf} はスナップショットの ELF（{snap.elf.name}、SHA-256 {snap.elf.sha256[:12]}…）と違う")
            return Path(elf), warnings
        if snap.elf_data is not None:
            cached = self._snapshots_dir() / "elf" / snap.elf.sha256 / snap.elf.name
            if not snap.elf.matches(cached):
                cached.parent.mkdir(parents=True, exist_ok=True)
                cached.write_bytes(snap.elf_data)
            return cached, warnings
        original = snap.info.get("elf_path")
        if original and not is_network_path(original) and snap.elf.matches(original):
            return Path(original), warnings
        return None, [f"ELF（{snap.elf.name}）が見つからない・違うビルド: elf を指定する。それまでは番地で読む"]

    def _restore_svd(self, svd: dict | None, warnings: list[str]) -> None:
        if svd and svd.get("origin") == "file" and not is_network_path(svd["source"]) and Path(svd["source"]).is_file():
            try:
                self.svd_load(path=svd["source"])
                return
            except Exception as e:
                warnings.append(f"SVD（{svd['source']}）が読めない: {e}")
        self._svd = self._svd_info = None                         # ターゲット名から探し直す

    def snapshot_close(self) -> dict:
        """開いたスナップショットを閉じる（Probe は未接続に戻る）。"""
        with self._lock:
            was = self._snapshot_target is not None
            if was:
                self._close_snapshot()
            return {"closed": was}

    def snapshot_info(self, path: str | None = None) -> dict:
        """スナップショットの中身の説明（path 省略で開いているもの）: 作った時刻・停止の理由と場所・区画・ELF の指紋・
        記録の参照と、保存した時の RTOS のタスクの一覧と故障の解析。"""
        with self._lock:
            if path is None:
                if self._snapshot_target is None:
                    return {"open": False}
                snap, path = self._snapshot_target.snapshot, self._snapshot_path
            else:
                snap = Snapshot.load(path)
            return {"open": self._snapshot_target is not None and Path(path) == self._snapshot_path, "path": str(path),
                    **snap.summary(), "rtos": snap.info.get("rtos"), "fault": snap.info.get("fault"),
                    "skipped": snap.info.get("skipped", [])}

    def snapshot_list(self) -> dict:
        """保存したスナップショット（既定の場所）の一覧（新しい順）。"""
        root = self._snapshots_dir()
        items = []
        for p in sorted(root.glob(f"*{SUFFIX}"), key=lambda p: p.stat().st_mtime, reverse=True) if root.is_dir() else []:
            try:
                s = Snapshot.load(p)
            except Exception as e:
                items.append({"name": p.name, "path": str(p), "error": str(e)})
                continue
            halt = s.info.get("halt") or {}
            items.append({"name": p.name, "path": str(p), "bytes": p.stat().st_size, "created": s.created,
                          "pc": s.registers.get("pc"), "where": halt.get("where"), "reason": halt.get("reason"),
                          "elf": s.elf.name if s.elf else None, "note": s.info.get("note", "")})
        return {"directory": str(root), "snapshots": items}

    def snapshot_export_core(self, path: str | None = None, out: str | None = None) -> dict:
        """スナップショット（path 省略で開いているもの）を ELF のコアファイルに書き出す（GDB の core-file で読める。
        out 省略で同じ場所に拡張子 .core）。"""
        with self._lock:
            if path is None:
                if self._snapshot_target is None:
                    raise RuntimeError("スナップショットを開いていない（path を指定する）")
                snap, path = self._snapshot_target.snapshot, self._snapshot_path
            else:
                snap = Snapshot.load(path)
            target = Path(out) if out else Path(path).with_suffix(".core")
            if is_network_path(target):
                raise ValueError(f"外の共有には書かない: {target}")
            write_core(snap, target)
            elf = snap.elf.name if snap.elf else "firmware.elf"
            return {"path": str(target), "bytes": target.stat().st_size,
                    "gdb": f'arm-none-eabi-gdb {elf} -ex "core-file {target.as_posix()}"'}

    # --- 書き戻し ----------------------------------------------------------------------------

    def snapshot_restore(self, path: str, resume: bool = False, peripherals: list[str] | None = None,
                         force: bool = False) -> dict:
        """接続中のターゲットをスナップショットの状態に戻す: 止めて、コード（スナップショットの RAM の外にある ELF の
        区画）が同じかを確かめ（force で省く）、RAM・システムのレジスタ・peripherals に挙げた周辺・コアレジスタを書く。
        resume なら再開する。Flash は書かない（違えば先に target_flash で同じ ELF を書く）。"""
        snap = Snapshot.load(path)
        with self._lock:
            target = self._live()
            if target.get_state() != Target.State.HALTED:
                target.halt()
            self._watch_stops(False)
            code_checked = self._check_code(target, snap, force)
            ram = [r for r in snap.regions if r.kind == "ram"]
            for r in ram:
                for start in range(0, len(r.data), READ_CHUNK):
                    target.write_memory_block8(r.address + start, list(r.data[start:start + READ_CHUNK]))
            target.flush()
            system = _restore_system(target, snap.access())
            written = self._restore_peripherals(target, snap, peripherals or [])
            restored = []
            for name in RESTORE_ORDER:
                if name in snap.registers:
                    target.write_core_register_raw(name, snap.registers[name])
                    restored.append(name)
            target.flush()
            self._dbg = None
            out = {"path": str(path), "ram_bytes": sum(len(r.data) for r in ram), "system": system,
                   "peripherals": written, "registers": len(restored), "code_checked": code_checked}
            if resume:
                out.update(self.target_resume())
            else:
                out.update(self.target_status())
            return out

    def _check_code(self, target, snap: Snapshot, force: bool) -> bool:
        """スナップショットの RAM の外にある ELF の区画（コード・定数）が、ターゲットのものと同じか。"""
        if force:
            return False
        elf_path, _ = self._snapshot_elf_path(snap, None)
        if elf_path is None:
            raise RuntimeError("スナップショットの ELF が無い: コードが同じか確かめられない（force で省く）")
        for segment in Symbols(elf_path).segments:
            for offset in range(0, len(segment.data), READ_CHUNK):
                address, chunk = segment.address + offset, segment.data[offset:offset + READ_CHUNK]
                if _covered(address, len(chunk), snap.regions, "ram"):
                    continue                                      # RAM ごと書き戻す
                try:
                    actual = bytes(target.read_memory_block8(address, len(chunk)))
                except pyocd_errors.Error as e:
                    raise RuntimeError(f"0x{address:08X} が読めない（{e}）: ELF のコードを確かめられない") from None
                if actual != chunk:
                    first = next(i for i in range(len(chunk)) if actual[i] != chunk[i])
                    raise RuntimeError(f"0x{address + first:08X} がスナップショットの ELF（{snap.elf.name}）と違う: "
                                       "同じ ELF を書いてから（target_flash）。確かめずに書くなら force")
        return True

    def _restore_peripherals(self, target, snap: Snapshot, names: list[str]) -> list[str]:
        if not names:
            return []
        device = self._device()
        written = []
        for name in names:
            p = device.peripheral(name)
            for r in p.registers:
                if r.access != "read-write":
                    continue
                region = next((x for x in snap.regions if x.kind == "peripheral" and x.name == r.path), None)
                if region is None:
                    continue
                target.write_memory(r.address, int.from_bytes(region.data, "little"), transfer_size=r.size)
                written.append(r.path)
        target.flush()
        return written


def _overlaps(address: int, length: int, regions: list[Region]) -> bool:
    return any(address < r.end and r.address < address + length for r in regions)


def _covered(address: int, length: int, regions: list[Region], kind: str) -> bool:
    """[address, address + length) がすべて kind の区画の中か。"""
    at = address
    for r in sorted((r for r in regions if r.kind == kind), key=lambda r: r.address):
        if r.address <= at < r.end:
            at = r.end
        if at >= address + length:
            return True
    return False


def _restore_system(target, access: SnapshotAccess) -> list[str]:
    """システム制御空間を書き戻す（取ってあるものだけ）。割り込みは全部消してから許可と保留を書き、SysTick は
    再読み込み値と現在値を書いてから有効にする。MPU の領域は窓（RNR）の 1 つしか取っていないので戻さない。"""
    written: list[str] = []

    def saved(address: int) -> int | None:
        try:
            return int.from_bytes(access.read_memory(address, 4), "little")
        except AccessError:
            return None

    def put(address: int, value: int, name: str) -> None:
        target.write_memory(address, value & 0xFFFFFFFF)
        written.append(name)

    for address, name in ((VTOR, "VTOR"), (SCR, "SCR"), (CCR, "CCR"), (SHPR1, "SHPR1"), (SHPR2, "SHPR2"),
                          (SHPR3, "SHPR3"), (fault.CPACR, "CPACR"), (FPCCR, "FPCCR"), (FPDSCR, "FPDSCR")):
        if (value := saved(address)) is not None:
            put(address, value, name)
    if (value := saved(AIRCR)) is not None:
        put(AIRCR, AIRCR_VECTKEY | value & AIRCR_PRIGROUP, "AIRCR.PRIGROUP")
    if (value := saved(SHCSR)) is not None:
        current = target.read32(SHCSR)
        put(SHCSR, current & ~SHCSR_ENABLES | value & SHCSR_ENABLES, "SHCSR")
    for i in range(0, 496, 4):
        if (value := saved(NVIC_IPR + i)) is None:
            break
        put(NVIC_IPR + i, value, f"NVIC.IPR{i // 4}")
    for n in range(16):
        enabled, pending = saved(NVIC_ISER + 4 * n), saved(NVIC_ISPR + 4 * n)
        if enabled is None and pending is None:
            break
        if enabled is not None:
            put(NVIC_ICER + 4 * n, 0xFFFFFFFF, f"NVIC.ICER{n}")
            put(NVIC_ISER + 4 * n, enabled, f"NVIC.ISER{n}")
        if pending is not None:
            put(NVIC_ICPR + 4 * n, 0xFFFFFFFF, f"NVIC.ICPR{n}")
            put(NVIC_ISPR + 4 * n, pending, f"NVIC.ISPR{n}")
    control = saved(SYST_CSR)
    if (value := saved(SYST_RVR)) is not None:
        put(SYST_RVR, value, "SYST_RVR")
        put(SYST_CVR, 0, "SYST_CVR")                              # 現在値は書けない（書くと 0 になる）
    if control is not None:
        put(SYST_CSR, control & 0x7, "SYST_CSR")
    target.flush()
    return written
