"""Probe の道具: GDB のトレースポイント（U48）の機器の側。トレースの実験（fprobe.gdb.trace.Experiment）を持ち、
有効なトレースポイントの番地にブレークポイントを置き、当たったら止まった一瞬にレジスタ・メモリを集めて続ける。

Probe（fprobe.probe。共通の部品の型は fprobe.probe_host.ProbeHost）に混ぜて使う mixin。Probe の _lock・target・dbg・_symbols・_monitor・_record_host と、
BreakpointActions の _break_hooks（道具が置くブレークポイントの処理）・ObserveTools の _watch_stops（再開した後の
見張り）、GdbTools の _gdb_breaks・_gdb_read_core・_gdb_step_over を使う。機器に触るのは必ず _lock の中。
API（JSON-RPC・MCP）には出さない（呼ぶのはデーモンの GDB サーバだけ）。

止める時間: Cortex-M には「PC がそこを通ったこと」を止めずに知らせる仕組みが無い（DWT の比較器の PC 一致は停止か
ETM へのきっかけだけで、DWT のデータトレースは番地への読み書きにしか効かない。ETM は U34・U35 の後）。よって当たるたびにコアを止めて
集める（ログ・ブレークポイントと同じ。1 回に数 ms と、止まったことに気づくまでの見張りの周期）。止めずに値を追うなら
変数のライブ観測（watch.set、DAP サンプラ）。断るのは、使う側が明示したときだけ（monitor trace deny-halt。モニタ方式
= target.debug_mode.set ではコアを止めないので断らない）。ELF の名前（debug.halt_risk）の一致だけを根拠に既定で
断ることはしない（名前の一致は「止めると壊れる」の証拠ではない。CLAUDE.md）。

仕様: docs/superpowers/specs/2026-10-18-u04-gdb-server-design.md（トレースポイントの節）
"""

import time

from pyocd.core.target import Target

from .gdb.trace import Experiment
from .probe_gdb import GDB_MARK, TRACE_MARK

__all__ = ["GdbTraceTools", "TOOL"]

TOOL = "gdb.trace"                      # _break_hooks の道具の名前（debug.breakpoint.list の tool）


class _Core:
    """当たって止まっているコア（fprobe.gdb.trace.HitTarget）。レジスタは GDB の番号（接続の並び）。"""

    def __init__(self, probe, names: list[str]):
        self.probe, self.names = probe, names

    def read_register(self, number: int) -> int | None:
        if not 0 <= number < len(self.names):
            return None
        return self.probe._gdb_read_core([self.names[number]])[0]

    def read_memory(self, address: int, length: int) -> bytes:
        return bytes(self.probe.target.read_memory_block8(address, length))

    def step(self) -> None:
        self.probe._gdb_step_over()


class GdbTraceTools:
    """GDB のトレースポイント（Probe の mixin）。"""

    def _init_gdb_trace(self) -> None:
        self._gdb_trace: Experiment | None = None
        self._gdb_trace_owner: str | None = None
        self._gdb_trace_names: list[str] = []           # GDB のレジスタ番号 → 名前（始めた接続の並び）
        self._gdb_trace_placed: set[int] = set()        # トレースポイントのために置いたブレークポイント

    # --- 実験 ------------------------------------------------------------------------------------

    def gdb_trace_experiment(self) -> Experiment:
        """いまの実験（無ければ空のものを作る。GDB がつなぎ直しても同じものが見える）。"""
        with self._lock:
            if self._gdb_trace is None:
                self._gdb_trace = Experiment()
            return self._gdb_trace

    def gdb_trace_init(self, owner: str) -> None:
        """QTinit: 前の実験を止めて捨て、空の実験にする。"""
        with self._lock:
            if self._gdb_trace is not None:
                self._gdb_trace.stop()
            self._gdb_trace = Experiment()
            self._gdb_trace_owner = owner
            self._gdb_trace_sync()

    def gdb_trace_start(self, owner: str, registers: list[tuple[str, int]], deny_halt: bool = False) -> dict:
        """QTStart: 有効なトレースポイントにブレークポイントを置いて始める。registers: GDB の並び（名前, ビット数）。
        deny_halt（使う側が monitor trace deny-halt で明示した）なら、モニタ方式でなければ RuntimeError。"""
        with self._lock:
            exp = self.gdb_trace_experiment()
            self.target                                        # 未接続ならここで失敗する
            if deny_halt and self._monitor is None:
                raise RuntimeError("monitor trace deny-halt が指定されている: トレースポイントは当たるたびにコアを止めて集める"
                                   "（1 回に数 ms と、止まったことに気づくまでの見張りの周期）。許すなら monitor trace "
                                   "allow-halt、止めずに済ませるならモニタ方式（monitor target.debug_mode.set mode=monitor）"
                                   "か変数のライブ観測（monitor watch.set）")
            for address in exp.addresses():
                hook = self._break_hooks.get(address)
                if hook is not None and hook[0] != TOOL:
                    raise RuntimeError(f"0x{address:08X} は {hook[0]} がブレークポイントを使っている（止めてから）")
            names = [name for name, _ in registers]
            exp.start(pc_register=names.index("pc"), register_sizes={i: bits // 8 for i, (_, bits) in enumerate(registers)})
            self._gdb_trace_names, self._gdb_trace_owner = names, owner
            self._gdb_trace_sync()
            return {"tracepoints": sorted(exp.addresses())}

    def gdb_trace_stop(self) -> None:
        """QTStop: 止めてブレークポイントを外す（集めたフレームは残す）。"""
        with self._lock:
            if self._gdb_trace is not None:
                self._gdb_trace.stop()
            self._gdb_trace_sync()

    def gdb_trace_install(self) -> None:
        """実験の途中で足した・有効 / 無効を変えたトレースポイントのブレークポイントを合わせる（InstallInTrace・QTEnable）。"""
        with self._lock:
            self._gdb_trace_sync()

    def gdb_trace_summary(self) -> dict:
        """monitor trace の表示（実験の状態と、当たって止めた時間）。"""
        with self._lock:
            exp = self.gdb_trace_experiment()
            out = exp.summary()
            out["notes"] = ["当たるたびにコアを止めて集める（collect_ms は集めた時間。止まったことに気づくまでの見張りの"
                            "周期が足される）。止めずに値を追うなら watch.set（DAP サンプラ）"]
            return out

    def _gdb_trace_release(self, owner: str) -> None:
        """接続が切れた（_lock の中で。gdb_release から）: その接続が始めた実験は、QTDisconnected:1 でなければ止める。"""
        exp = self._gdb_trace
        if exp is not None and exp.running and owner == self._gdb_trace_owner and not exp.disconnected_tracing:
            exp.stop("tdisconnected")
            self._gdb_trace_sync()

    def _gdb_trace_detached(self) -> None:
        """すべてのブレークポイントが外された（breakpoint_clear）: 実験は誤りで止まる。"""
        exp = self._gdb_trace
        for address in [a for a, (tool, _) in self._break_hooks.items() if tool == TOOL]:
            self._break_hooks.pop(address, None)
        self._gdb_trace_placed.clear()
        if exp is not None and exp.running:
            exp.stop("terror", 0, "breakpoint_clear でブレークポイントが外された")

    # --- ブレークポイント -------------------------------------------------------------------------------

    def _gdb_trace_wants(self, address: int) -> bool:
        hook = self._break_hooks.get(address)
        return hook is not None and hook[0] == TOOL

    def _gdb_trace_adopt(self, address: int) -> None:
        """GDB が外したブレークポイントを、トレースポイントのものとして残す（_lock の中で）。"""
        self.dbg.breakpoints[address] = f"{TRACE_MARK}0x{address:08X}"
        self._gdb_trace_placed.add(address)

    def _gdb_trace_sync(self) -> None:
        """ブレークポイントを実験に合わせる（_lock の中で）: 動いていれば有効なトレースポイントの番地に置き、ほかは外す。"""
        exp = self._gdb_trace
        want = exp.addresses() if exp is not None and exp.running else set()
        hooked = {a for a, (tool, _) in self._break_hooks.items() if tool == TOOL}
        if not want and not hooked:
            return
        dbg = self.dbg
        for address in hooked - want:
            self._break_hooks.pop(address, None)
            if address in self._gdb_trace_placed:
                self._gdb_trace_placed.discard(address)
                if self._gdb_breaks.get(address):              # GDB のブレークポイントでもある: GDB のものにする
                    dbg.breakpoints[address] = f"{GDB_MARK}0x{address:08X}"
                elif dbg.breakpoints.get(address, "").startswith(TRACE_MARK):
                    dbg.remove_breakpoint(address)
        for address in want - hooked:
            if address not in dbg.breakpoints:
                dbg.breakpoint(address)
                self._gdb_trace_adopt(address)
            self._break_hooks[address] = (TOOL, self._gdb_trace_hit)
        if want:
            self._watch_stops(self.target.get_state() != Target.State.HALTED)

    # --- 当たったとき（_lock の中で。BreakpointActions._break_action から） ------------------------------------

    def _gdb_trace_hit(self, pc: int) -> bool | None:
        exp = self._gdb_trace
        if exp is None or not exp.running:
            return None
        started = time.monotonic()
        frames = exp.hit(pc, _Core(self, self._gdb_trace_names), started)
        exp.note_halt(time.monotonic() - started)
        for frame in frames:
            self._record_host("debug", "tracepoint", started, a=pc,
                              body={"tracepoint": frame.tracepoint, "frame": frame.number})
        if not exp.running:
            self._gdb_trace_sync()                             # パスカウント・満杯・誤り: ブレークポイントを外す
        if self._gdb_breaks.get(pc) or not self.dbg.breakpoints.get(pc, TRACE_MARK).startswith(TRACE_MARK):
            return None                                        # GDB・AI・画面のブレークポイントでもある: 止める
        return True
