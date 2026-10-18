"""Probe の道具: GDB サーバ（fprobe.gdb）の粒度の操作。レジスタ・メモリ・ブレークポイント・実行と停止の判定・
RTOS のタスク（GDB のスレッド）・Flash の書き込み。

Probe（fprobe.mcp_server）に混ぜて使う mixin。Probe の _lock・target・dbg・_debug・_symbols・target_connect・
target_halt・target_reset・target_fault・_kernel（RtosTools）・_watch_stops・_service_in_background・_sh_watch・
_sh_exit・_logs（ObserveTools）を使う。機器に触るのは必ず _lock の中。API（JSON-RPC・MCP）には出さない
（呼ぶのはデーモンの GDB サーバだけ。調停と記録はデーモンが行う）。

- ブレークポイント・ウォッチポイントは Debugger の表（debug.breakpoint.list で見える）に入れる。GDB が置いた
  ブレークポイントの場所の名前は "gdb:0x…"。持ち主（接続）ごとに覚え、GDB が外すのは自分が置いたものだけ
  （AI・画面が同じ番地に置いたものは外さない）。トレースポイント（probe_gdb_trace、"gdb-trace:0x…"）と同じ番地なら
  共有し、外すときはもう一方に残す（_gdb_trace_wants・_gdb_trace_adopt・_gdb_trace_release を使う）。
- 停止の判定（gdb_poll）は背景の見張り（セミホスティング・動作のあるブレークポイント）と同じ処理を _lock の中で
  先に行う（見張りより先に止まった様子を読んでも、続けるべき停止を GDB に返さない）。

仕様: docs/superpowers/specs/2026-10-18-u04-gdb-server-design.md
"""

import time

from pyocd.core.target import Target
from pyocd.flash.loader import MemoryLoader

from .debug import _REASONS
from .gdb.description import Region
from .probe_observe import SEMIHOSTING

__all__ = ["BASE_REGISTERS", "GDB_MARK", "GdbTools", "TRACE_MARK"]

BASE_REGISTERS = frozenset([f"r{i}" for i in range(13)] + ["sp", "lr", "pc", "xpsr", "msp", "psp", "primask", "control"])
GDB_MARK = "gdb:"                       # GDB が置いたブレークポイントの場所の名前の頭
TRACE_MARK = "gdb-trace:"               # GDB のトレースポイントが置いたもの（probe_gdb_trace）
DWT_CTRL = 0xE000_1000
DWT_COMP0 = 0xE000_1020
DWT_FUNCTION0 = 0xE000_1028
DWT_MATCHED = 1 << 24
_KINDS = {"break": None, "hbreak": None, "write": "write", "read": "read", "access": "access"}
_MEMORY_KINDS = {"RAM": "ram", "ROM": "rom", "FLASH": "flash"}


def _raw_names(names: list[str]) -> list[str]:
    """GDB の名前 → pyOCD の名前（d*n* は s*2n*・s*2n+1*）。"""
    out = []
    for name in names:
        if name.startswith("d") and name[1:].isdigit():
            n = int(name[1:])
            out += [f"s{2 * n}", f"s{2 * n + 1}"]
        else:
            out.append(name)
    return out


def _combine(names: list[str], raw: dict[str, int | None]) -> list[int | None]:
    values = []
    for name in names:
        if name.startswith("d") and name[1:].isdigit():
            n = int(name[1:])
            lo, hi = raw.get(f"s{2 * n}"), raw.get(f"s{2 * n + 1}")
            values.append(None if lo is None or hi is None else lo | hi << 32)
        else:
            values.append(raw.get(name))
    return values


class GdbTools:
    """GDB サーバの操作（Probe の mixin）。"""

    def _init_gdb(self) -> None:
        self._gdb_breaks: dict[int, set[str]] = {}      # 番地 → GDB の持ち主（接続）
        self._gdb_watches: dict[int, set[str]] = {}
        self._gdb_exit_seen = None                       # 最後に GDB へ返したセミホスティングの終了（_sh_exit）

    # --- 接続 ----------------------------------------------------------------------------------

    def gdb_attach(self, target: str = "cortex_m", frequency_hz: int = 4_000_000) -> dict:
        """GDB がつながった: 未接続なら接続して止め、動いていれば止める（all-stop の GDB は止まった状態から始まる）。
        GDB に見せるレジスタ（コアにある pyOCD の名前）とメモリの区画を返す。"""
        with self._lock:
            if self._debug is None:
                self.target_connect(target, frequency_hz, "halt")
            elif self.target.get_state() != Target.State.HALTED:
                self.target_halt()
            return {"registers": sorted(self._gdb_core_names()), "regions": self._gdb_regions()}

    def _gdb_core_names(self) -> set[str]:
        index = getattr(self.target, "core_registers", None)
        return set(index.by_name) if index is not None else set(BASE_REGISTERS)

    def _gdb_regions(self) -> list[Region]:
        regions = []
        for r in getattr(self.target, "memory_map", None) or []:
            kind = _MEMORY_KINDS.get(r.type.name)
            if kind is None or not r.length:
                continue                                       # 周辺（DEVICE）等は書かない: GDB は区画の外も読める
            # attributes は既定値を関数で持つ（pyOCD）。属性で引くと解決される
            regions.append(Region(kind, r.start, r.length, getattr(r, "blocksize", None) if kind == "flash" else None))
        return regions

    def gdb_release(self, owner: str) -> None:
        """その接続が置いたブレークポイント・ウォッチポイントを外す（detach・切断）。その接続が始めたトレースは、
        切断しても続ける設定（QTDisconnected:1）でなければ止める。"""
        with self._lock:
            self._gdb_trace_release(owner)
            for address in [a for a, owners in self._gdb_breaks.items() if owner in owners]:
                self._gdb_unbreak(owner, address)
            for address in [a for a, owners in self._gdb_watches.items() if owner in owners]:
                self._gdb_unwatch(owner, address)

    # --- レジスタ・メモリ ---------------------------------------------------------------------------

    def gdb_read_registers(self, names: list[str], thread: int | None = None) -> list[int | None]:
        """レジスタ（GDB の名前）を読む。読めないものは None。thread（TCB の番地）が実行中のタスクでなければ、
        そのタスクの保存された文脈から。"""
        with self._lock:
            context = self._gdb_context(thread)
            if context is not None:
                values = []
                for name in names:
                    try:
                        values.append(context.read_register(name))
                    except Exception:                          # 文脈に無いレジスタ（msp・FP の文脈の無いタスクの d 等）
                        values.append(None)
                return values
            return self._gdb_read_core(names)

    def _gdb_read_core(self, names: list[str]) -> list[int | None]:
        """コアのレジスタ（GDB の名前）を読む（_lock の中で）。読めないものは None。"""
        raw = _raw_names(names)
        try:
            read = dict(zip(raw, self.target.read_core_registers_raw(raw)))
        except Exception:                                      # どれかが読めない: 1 つずつ
            read = {}
            for name in raw:
                try:
                    read[name] = self.target.read_core_register_raw(name)
                except Exception:
                    read[name] = None
        return _combine(names, read)

    def gdb_write_registers(self, values: dict[str, int], thread: int | None = None) -> None:
        with self._lock:
            if self._gdb_context(thread) is not None:
                raise RuntimeError("止まっているタスクのレジスタ（保存された文脈）は書けない。実行中のタスクを選ぶ")
            names, words = [], []
            for name, value in values.items():
                if name.startswith("d") and name[1:].isdigit():
                    n = int(name[1:])
                    names += [f"s{2 * n}", f"s{2 * n + 1}"]
                    words += [value & 0xFFFF_FFFF, value >> 32 & 0xFFFF_FFFF]
                else:
                    names.append(name)
                    words.append(value)
            self.target.write_core_registers_raw(names, words)
            self.target.flush()

    def gdb_read_memory(self, address: int, length: int) -> bytes:
        with self._lock:
            return bytes(self.target.read_memory_block8(address, length))

    def gdb_write_memory(self, address: int, data: bytes) -> None:
        with self._lock:
            self.target.write_memory_block8(address, list(data))
            self.target.flush()

    # --- ブレークポイント・ウォッチポイント ----------------------------------------------------------

    def gdb_breakpoint(self, owner: str, insert: bool, kind: str, address: int, length: int) -> None:
        """kind: break（Z0）/ hbreak（Z1）/ write（Z2）/ read（Z3）/ access（Z4）。"""
        if kind not in _KINDS:
            raise ValueError(f"kind {kind!r}")
        with self._lock:
            if kind in ("break", "hbreak"):
                if insert:
                    self._gdb_break(owner, address, kind == "hbreak")
                else:
                    self._gdb_unbreak(owner, address)
            elif insert:
                self._gdb_watch(owner, address, length, _KINDS[kind])
            else:
                self._gdb_unwatch(owner, address)

    def _gdb_break(self, owner: str, address: int, hardware: bool) -> None:
        dbg = self.dbg
        if address not in dbg.breakpoints:
            if hardware:
                if not self.target.set_breakpoint(address, Target.BreakpointType.HW):
                    raise RuntimeError(f"0x{address:08X} にハードのブレークポイントを置けない（比較器が足りない）")
            else:
                dbg.breakpoint(address)
            dbg.breakpoints[address] = f"{GDB_MARK}0x{address:08X}"
        elif not dbg.breakpoints[address].startswith((GDB_MARK, TRACE_MARK)):
            return                                             # AI・画面が置いたもの: 共有する（持ち主にならない）
        self._gdb_breaks.setdefault(address, set()).add(owner)

    def _gdb_unbreak(self, owner: str, address: int) -> None:
        owners = self._gdb_breaks.get(address)
        if owners is None or owner not in owners:
            return
        owners.discard(owner)
        if owners:
            return
        del self._gdb_breaks[address]
        dbg = self.dbg
        if dbg.breakpoints.get(address, "").startswith(GDB_MARK):   # 後から AI・画面が置き直していれば残す
            if self._gdb_trace_wants(address):
                self._gdb_trace_adopt(address)                 # トレースポイントが使い続ける
            else:
                dbg.remove_breakpoint(address)

    def _gdb_watch(self, owner: str, address: int, length: int, kind: str) -> None:
        dbg = self.dbg
        if address not in dbg.watches:
            dbg.watchpoint(address, length, kind)
            self._gdb_watches.setdefault(address, set()).add(owner)
        elif address in self._gdb_watches:
            self._gdb_watches[address].add(owner)

    def _gdb_unwatch(self, owner: str, address: int) -> None:
        owners = self._gdb_watches.get(address)
        if owners is None or owner not in owners:
            return
        owners.discard(owner)
        if not owners:
            del self._gdb_watches[address]
            self.dbg.remove_watchpoint(address)

    # --- 実行 ------------------------------------------------------------------------------------

    def gdb_resume(self) -> None:
        """再開する（いまの PC のブレークポイントは 1 命令踏み越える。セミホスティング・動作のあるブレークポイントは
        背景の見張りと gdb_poll が処理する）。"""
        with self._lock:
            self._gdb_exit_seen = self._sh_exit
            self.dbg.resume()
            self._watch_stops(True)

    def gdb_step(self) -> dict:
        """1 命令進める（割り込みは止めたまま。いまの PC のブレークポイントは外して進め、置き直す）。止まった様子を返す。"""
        with self._lock:
            self._watch_stops(False)
            self._gdb_step_over()
            return self._gdb_stop()

    def _gdb_step_over(self) -> None:
        """1 命令進める（_lock の中で）。いまの PC のブレークポイントは外して進め、置き直す。"""
        target, dbg = self.target, self.dbg
        pc = target.read_core_register("pc")
        if pc in dbg.breakpoints:
            bp = target.find_breakpoint(pc)
            kind = bp.type if bp is not None else Target.BreakpointType.AUTO      # Z1（ハード）はハードのまま
            target.remove_breakpoint(pc)
            try:
                target.step()
            finally:
                target.set_breakpoint(pc, kind)
        else:
            target.step()

    def gdb_halt(self) -> None:
        with self._lock:
            self.target.halt()
            self._watch_stops(False)

    def gdb_reset(self, halt: bool) -> dict:
        with self._lock:
            return self.target_reset(halt)

    def gdb_poll(self, console_from: int = 0) -> dict:
        """実行中の見張り: {"stop": 止まっていれば様子（gdb_stop と同じ）/ None, "console": console_from より後の
        セミホスティングの出力, "console_next": 次に渡す位置}。"""
        with self._lock:
            target = self.target
            stop = None
            if target.get_state() == Target.State.HALTED:
                if self._sh_watch:
                    self._service_in_background()              # 続けるべき停止なら再開される
                if target.get_state() == Target.State.HALTED:
                    stop = self._gdb_stop()
                    if self._sh_exit is not None and self._sh_exit is not self._gdb_exit_seen:
                        stop["exit"] = self._sh_exit["code"] or 0
                        self._gdb_exit_seen = self._sh_exit
        with self._observe:
            log = self._logs.get(SEMIHOSTING)
            data, start, _ = log.read(console_from) if log is not None else (b"", console_from, 0)
        return {"stop": stop, "console": data, "console_next": start + len(data)}

    def gdb_stop(self) -> dict:
        """いま止まっている様子（GDB の '?'）。"""
        with self._lock:
            return self._gdb_stop()

    def _gdb_stop(self) -> dict:
        target = self.target
        pc, sp = target.read_core_registers_raw(["pc", "sp"])
        reason = _REASONS.get(target.get_halt_reason(), "unknown")
        stop = {"reason": reason, "pc": pc, "sp": sp, "thread": self._gdb_running_task()}
        if reason == "watchpoint":
            watch = self._gdb_hit_watch()
            if watch is not None:
                stop["watch"] = {"address": watch.address, "kind": watch.kind}
        elif reason == "breakpoint":
            kind = self._gdb_break_kind(pc)
            if kind is not None:
                stop["breakpoint"] = kind
        elif reason != "step":
            try:
                report = self.target_fault()
            except Exception:                                  # 故障レジスタが読めない: 原因を添えないだけ
                report = {"faulted": False}
            if report.get("faulted"):
                stop["fault"] = {"causes": [c["bit"] for c in report["causes"]],
                                 "at": (report.get("frame") or {}).get("pc")}
        return stop

    def _gdb_break_kind(self, address: int) -> str | None:
        """その番地のデバッガのブレークポイントの種類: sw（BKPT 命令を書いた）/ hw（比較器）/ None（無い。
        プログラムの中の BKPT 命令等）。"""
        bp = self.target.find_breakpoint(address)
        if bp is None:
            return None
        return "sw" if bp.type == Target.BreakpointType.SW else "hw"

    def _gdb_hit_watch(self):
        """どのウォッチポイントが成立したか（DWT の FUNCTION の MATCHED と COMP）。分からなければ、1 つだけならそれ。"""
        watches = self.dbg.watches
        if not watches:
            return None
        try:
            for n in range(self.target.read32(DWT_CTRL) >> 28):
                if self.target.read32(DWT_FUNCTION0 + 16 * n) & DWT_MATCHED:
                    comp = self.target.read32(DWT_COMP0 + 16 * n)
                    for w in watches.values():
                        if w.address <= comp < w.address + w.size:
                            return w
        except Exception:
            pass
        return next(iter(watches.values())) if len(watches) == 1 else None

    # --- RTOS のタスク（GDB のスレッド） -------------------------------------------------------------

    def gdb_threads(self) -> dict:
        """止まっているターゲットのタスクの一覧 {"threads": [{"id"（TCB の番地）, "name", "state", "priority",
        "current"}], "note"}。RTOS が無い・ELF が無い・読めないなら threads は空で、note に理由。"""
        with self._lock:
            if self._symbols is None:
                return {"threads": [], "note": "ELF が読み込まれていない（monitor elf <path>）"}
            try:
                kernel = self._kernel()
                current = kernel.current()
                tasks = kernel.tasks()
            except Exception as e:                             # RTOS が無い・スケジューラの前・壊れたリスト
                return {"threads": [], "note": str(e)}
            return {"threads": [{"id": t.handle, "name": t.name, "state": t.state.value, "priority": t.priority,
                                 "current": t.handle == current} for t in tasks], "note": None}

    def _gdb_running_task(self) -> int | None:
        if self._symbols is None:
            return None
        try:
            return self._kernel().current() or None
        except Exception:
            return None

    def _gdb_context(self, thread: int | None):
        """thread が実行中でないタスクなら、その保存された文脈の読み手。実レジスタで読むなら None。"""
        if thread is None:
            return None
        kernel = self._kernel()
        if kernel.current() == thread:
            return None
        return kernel.task_access(thread)

    # --- Flash -----------------------------------------------------------------------------------

    def gdb_flash(self, blocks: list[tuple[int, bytes]]) -> dict:
        """GDB の load（vFlashWrite で集めた番地とデータ）を Flash に書く。消去は書く区画（セクタ）ごとで、書かない
        部分は消えたまま（GDB の vFlashErase と同じ結果）。リセットしない（GDB が PC を入口に置く）。"""
        with self._lock:
            started = time.monotonic()
            self.target                                        # 未接続ならここで失敗する
            self._require_halt_mode("Flash の書き込み")
            loader = MemoryLoader(self._debug, keep_unwritten=False)
            for address, data in blocks:
                loader.add_data(address, list(data))
            loader.commit()
            return {"bytes": sum(len(d) for _, d in blocks), "seconds": round(time.monotonic() - started, 3)}
