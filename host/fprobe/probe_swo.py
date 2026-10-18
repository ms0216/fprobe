"""Probe の道具: SWO の DWT・ITM の設定（U11）。ITM のポート・TPIU・例外トレース・イベントカウンタ・DWT の比較器による
データトレース（変数へのアクセスを値・PC・時刻付きで、止めずに）・値一致のウォッチポイント。

Probe（fprobe.mcp_server）に混ぜて使う mixin。Probe の _lock・target・session・_symbols・_recorder・_swo_baud・
swo_start・_profile_run と、WatchTools（式 → 番地と大きさと型: `_watch_target`）、InspectTools（SVD: `_device`）、
WindowTools（出来事の窓・write_registers）を使う。機器に触るのは必ず _lock の中。

- 書く順（2026-10-18 に実機で分かった注意）: パケットを出さない設定（DEMCR.TRCENA・ITM の鍵・TPIU・SWO のピン）
  → SWO の受信を始めて窓を開く → ITM → DWT_CTRL → 比較器（COMP・MASK の後に FUNCTION）。逆だと受信がパケットの
  途中から始まり、次の同期パケットまで解読がずれる。ITM が前から有効だった（ファームが使っている）ときは、窓の解読を
  最初の同期パケットから始める（同期パケットは CYCCNT[24] ごとに出す）。
- パケットは SWO の受信の口（swo_read / swo_decode）からでなく、窓（記録中なら記録の stream "swo"、そうでなければ
  イベント記録）のデバイスの時刻付きのバイトから読む。時刻は記録の共通の時間軸と同じ（ほかの道具と二重に読まない）。
- 比較器は pyOCD のウォッチポイントと取り合わないよう、上の番号から取り、pyOCD の DWT の帳簿に「使用中」と書く
  （pyOCD は最初のウォッチポイントで DWT を初期化して全比較器を消すので、その初期化を先に済ませる）。
- 記録中なら、組み立てたアクセスを記録の stream "swo"（kind data_access。a = 番地、b = 値）に、イベントカウンタの
  溢れを kind counter_overflow に入れる（swo_trace・swo_unconfigure のたびに増分を）。
"""

from dataclasses import dataclass, field

from .dwarf.typesys import type_name
from .dwarf.values import decode
from .itm import DecodeError, ExceptionTrace, Overflow, Timestamper
from .probe_window import EventWindow, write_registers
from .profile.dwt import DWT_CTRL, ITM_TCR, RegisterWrite, plan_swo
from .profile.exceptions import exception_name
from .swo_trace import (CPUID, LNK1ENA, NOPRFCNT, NOTRCPKT, CoreInfo, CounterTotals, DataAccess, DataTrace,
                        DataTraceAssembler, DwtInfo, TimedItmStream, TracePlan, TraceRequest, ValueWatch, core_info,
                        dwt_function, dwt_mask, plan_trace)

__all__ = ["ACCESS_LIMIT", "SwoTools"]

ACCESS_LIMIT = 1 << 20                  # ためるアクセスの上限（超えた分は数えて捨てる）
EXCEPTION_LIMIT = 1 << 16               # ためる例外トレースの上限
_DATAVMATCH = 1 << 8
_TRACE_PIN_REGISTERS = ("DBG.DBGMCU_CR", "DBGMCU.CR", "DBGMCU.DBGMCU_CR")


class _PyocdDwt:
    """pyOCD の DWT（pyocd.coresight.dwt.DWT）の帳簿。fprobe が使う比較器を pyOCD のウォッチポイントが上書きしないように
    使用中にする。pyOCD の内部に触るのはここだけ（ターゲットが pyOCD でなければ何もしない）。"""

    _RESERVED = 7                        # 使用中の印（pyOCD の FUNCTION の値。addr=None なので pyOCD の検索には当たらない）

    def __init__(self, target):
        core = getattr(target, "selected_core", None) or target
        dwt = getattr(core, "dwt", None)
        self.dwt = dwt if dwt is not None and hasattr(dwt, "watchpoints") else None

    def prepare(self) -> None:
        """pyOCD は最初のウォッチポイントで DWT を初期化し、全比較器の FUNCTION と DWT_CTRL を消す: 先に済ませる。"""
        if self.dwt is not None and not self.dwt.dwt_configured:
            self.dwt.init()

    def busy(self) -> set[int]:
        return set() if self.dwt is None else {n for n, w in enumerate(self.dwt.watchpoints) if w.func}

    def reserve(self, index: int) -> None:
        if self.dwt is not None and index < len(self.dwt.watchpoints):
            w = self.dwt.watchpoints[index]
            w.func, w.addr, w.size = self._RESERVED, None, 0
            self.dwt.watchpoint_used += 1

    def release(self, index: int) -> None:
        if self.dwt is not None and index < len(self.dwt.watchpoints):
            w = self.dwt.watchpoints[index]
            if w.addr is None and w.func:
                w.func = 0
                self.dwt.watchpoint_used -= 1


@dataclass
class _SwoRun:
    request: TraceRequest
    plan: TracePlan
    core: CoreInfo
    targets: dict                                         # 名前 → WatchTools._Watched（型で値を解釈する）
    restore: list[RegisterWrite]                          # 止めるときに書く（mask の部分だけ元の値に）
    reserved: list[int]
    baud: float | None
    window: EventWindow | None
    synced: bool
    trace_pin: str | None
    stream: TimedItmStream
    stamper: Timestamper | None                           # ローカルのタイムスタンプを付ける（有効なとき）
    assembler: DataTraceAssembler
    counters: CounterTotals = field(default_factory=CounterTotals)
    accesses: list[DataAccess] = field(default_factory=list)
    dropped: int = 0
    exceptions: list[tuple[int, int, str]] = field(default_factory=list)
    exception_counts: dict[str, int] = field(default_factory=dict)
    overflows: int = 0
    errors: int = 0
    last_t: int = -1                                      # 処理した最後のバイトの時刻
    recorded: int = 0                                     # 記録に書いたアクセスの数
    recorded_overflows: dict[str, int] = field(default_factory=dict)
    times: dict[int, int] = field(default_factory=dict)   # タイムスタンプ待ちのパケット（id）→ デバイスの時刻
    timebase_hz: float = 0.0
    running: bool = True


class SwoTools:
    """SWO の DWT・ITM の設定（Probe の mixin）。"""

    def _init_swo(self) -> None:
        self._swo_run: _SwoRun | None = None

    # --- 設定 ------------------------------------------------------------------------------------

    def swo_configure(self, ports: list[int] | None = None, timestamps: bool = False, timestamp_prescale: int = 1,
                      exception_trace: bool = False, counters: list[str] | None = None,
                      data_trace: list[dict] | None = None, value_watch: list[dict] | None = None,
                      swo_baud: int | None = None, core_clock_hz: float | None = None, trace_pin: bool = True) -> dict:
        """ITM・DWT の出力を止めずに設定する（前の swo_configure の設定は戻してから）。ports: ITM のスティミュラス・
        ポート。timestamps: ローカルのタイムスタンプ（サイクルの精度。timestamp_prescale 1/4/16/64）。exception_trace:
        例外の入る・出る・戻る。counters: イベントカウンタ（cpi・exc・sleep・lsu・fold）の溢れ。data_trace: 変数への
        アクセス（{"expression", "access": read|write|rw, "pc", "value", "offset"}）。value_watch: 値一致
        （{"expression"（省略で番地を問わない）, "value", "size", "access", "action": halt|trace}）。swo_baud を渡すと
        受信も始め、core_clock_hz も渡すと TPIU（SWO の速度）も設定する。trace_pin: SVD に DBGMCU_CR.TRACE_IOEN が
        あれば SWO のピンを有効にする（STM32）。計画（比較器・書いたレジスタ・帯域の見積もり・警告）を返す。"""
        traces = [self._data_trace_spec(d) for d in data_trace or []]
        watches = [self._value_watch_spec(w) for w in value_watch or []]
        request = TraceRequest(tuple(ports or ()), timestamps, timestamp_prescale, exception_trace,
                               tuple(counters or ()), tuple(t for t, _ in traces), tuple(w for w, _ in watches))
        targets = {t.name: w for t, w in traces + watches if w is not None}
        with self._lock:
            if getattr(self, "_profile_run", None) is not None:
                raise RuntimeError("プロファイルが DWT・ITM を使っている（profile_stop を先に）")
            if self._swo_run is not None and self._swo_run.running:
                self._swo_finish()                                   # 前の設定を戻してから（戻した後なら何もしない）
            target = self.target
            core = core_info(target.read_memory(CPUID))
            pyocd = _PyocdDwt(target)
            pyocd.prepare()
            info = self._dwt_info(target, core, pyocd)
            baud = swo_baud
            tpiu: list[RegisterWrite] = []
            warnings: list[str] = []
            if swo_baud is not None and core_clock_hz is not None:
                swo = plan_swo(core_clock_hz, swo_baud)
                tpiu, baud = list(swo.writes), swo.baud
                warnings += swo.warnings
            plan = plan_trace(request, info, core_clock_hz=core_clock_hz,
                              swo_baud=baud if baud is not None else self._swo_baud)
            plan.warnings[:0] = warnings
            pin_writes, pin = self._trace_pin(plan) if trace_pin and plan.uses_swo else ([], None)
            itm_was_on = bool(target.read_memory(ITM_TCR) & 1) if plan.uses_swo else False
            before = write_registers(target, plan.setup + tpiu + pin_writes)
            window = None
            if plan.uses_swo:
                if swo_baud is not None:
                    self.swo_start(round(baud))
                elif self._swo_baud is None:
                    raise RuntimeError("SWO の受信が始まっていない（swo_baud か swo_start）")
                window = self._open_window()
            before.update({a: v for a, v in write_registers(target, plan.enable).items() if a not in before})
            for u in plan.comparators:
                pyocd.reserve(u.index)
            restore = [RegisterWrite(w.address, before.get(w.address, 0), w.mask, "戻す") for w in plan.enable
                       if w.mask != 0xFFFFFFFF]
            restore[:0] = [RegisterWrite(dwt_function(u.index), 0, note="比較器を外す") for u in plan.comparators]
            run = _SwoRun(request, plan, core, targets, restore, [u.index for u in plan.comparators],
                          baud if plan.uses_swo else None, window, not itm_was_on, pin, TimedItmStream(not itm_was_on),
                          Timestamper() if timestamps else None, DataTraceAssembler(plan.comparators),
                          timebase_hz=self.session.device.capabilities().timebase_hz)
            self._swo_run = run
        return {"running": True, "core": {"name": core.name, "arch": core.arch, "cpuid": core.cpuid},
                "plan": plan.as_dict(), "swo_baud": run.baud, "synced": run.synced, "trace_pin": pin,
                "t0": window.t0 if window else None,
                "notes": [] if run.synced else ["ITM は前から有効だった: 最初の同期パケット（CYCCNT[24] ごと）までの"
                                                 "パケットは捨てる"]}

    def _data_trace_spec(self, d: dict) -> tuple[DataTrace, object]:
        unknown = set(d) - {"expression", "access", "pc", "value", "offset"}
        if unknown or "expression" not in d:
            raise ValueError(f"data_trace の項目は expression・access・pc・value・offset: {sorted(unknown) or d}")
        w = self._watch_target(d["expression"])
        return DataTrace(d["expression"], w.address, w.size, d.get("access", "write"), d.get("pc", True),
                         d.get("value", True), d.get("offset", False)), w

    def _value_watch_spec(self, d: dict) -> tuple[ValueWatch, object]:
        unknown = set(d) - {"expression", "value", "size", "access", "action", "pc"}
        if unknown or "value" not in d:
            raise ValueError(f"value_watch の項目は expression・value・size・access・action・pc: {sorted(unknown) or d}")
        expression = d.get("expression")
        w = self._watch_target(expression) if expression else None
        size = d.get("size") or (w.size if w is not None else 4)
        name = expression or f"value=={d['value']:#x}"
        return ValueWatch(name, int(d["value"]), size, w.address if w is not None else None, d.get("access"),
                          d.get("action", "halt"), d.get("pc", True)), w

    def _dwt_info(self, target, core: CoreInfo, pyocd: _PyocdDwt) -> DwtInfo:
        """比較器の数・使用中・値の比較のできるもの（DATAVMATCH を書いて読み返す）・最大の MASK を調べる。
        調べるために書くのは空いている比較器だけ（FUNCTION = 0 のまま。一致は起きない）。"""
        ctrl = target.read_memory(DWT_CTRL)
        numcomp = ctrl >> 28
        busy = pyocd.busy() | {n for n in range(numcomp) if target.read_memory(dwt_function(n)) & 0xF}
        free = [n for n in range(numcomp) if n not in busy]
        value_match, linkable, max_mask = set(), set(), 0
        if core.arch == "v7m" and free:
            for n in free:
                target.write_memory(dwt_function(n), _DATAVMATCH)
                f = target.read_memory(dwt_function(n))
                target.write_memory(dwt_function(n), 0)
                if f & _DATAVMATCH:
                    value_match.add(n)
                    if f & LNK1ENA:
                        linkable.add(n)
            n = free[0]
            old = target.read_memory(dwt_mask(n))
            target.write_memory(dwt_mask(n), 0x1F)
            max_mask = target.read_memory(dwt_mask(n)) & 0x1F
            target.write_memory(dwt_mask(n), old)
        return DwtInfo(numcomp, core.arch, not ctrl & NOTRCPKT, not ctrl & NOPRFCNT, frozenset(busy),
                       frozenset(value_match), frozenset(linkable), max_mask)

    def _trace_pin(self, plan: TracePlan) -> tuple[list[RegisterWrite], str | None]:
        """SVD に DBGMCU_CR.TRACE_IOEN があれば、SWO のピンを有効に（TRACE_MODE = 0: 非同期）。無ければ何もしない。"""
        try:
            device = self._device()
        except Exception as e:                                   # SVD が無い: ピンはファームか target_peripheral_write で
            plan.warnings.append(f"SWO のピンの設定を確かめられない（{e}）: STM32 等はファームか "
                                 "target_peripheral_write で DBGMCU_CR.TRACE_IOEN を 1 に")
            return [], None
        for path in _TRACE_PIN_REGISTERS:
            try:
                reg = device.register(path)
                ioen = reg.field("TRACE_IOEN")
            except KeyError:
                continue
            mask, value = ioen.mask, ioen.insert(0, 1)
            try:
                mask |= reg.field("TRACE_MODE").mask
            except KeyError:
                pass
            return [RegisterWrite(reg.address, value, mask, f"{reg.path}.TRACE_IOEN: SWO のピンを有効に")], reg.path
        return [], None

    def swo_unconfigure(self) -> dict:
        """swo_configure の設定を戻す（比較器を外し、DWT_CTRL・ITM の変えたビットを元に）。SWO の受信・TPIU は残す
        （ファームの printf 等も使うため）。届いている分まで組み立てた結果（swo_trace と同じ形）を返す。"""
        with self._lock:
            if self._swo_run is None or not self._swo_run.running:
                raise RuntimeError("swo_configure で設定していない")
            run = self._swo_finish()
        return self._trace_dict(run, 0, 0, None)

    def _swo_finish(self) -> _SwoRun:
        """_lock の中で: 届いている分を組み立て、設定を戻し、窓を閉じる。"""
        run = self._swo_run
        try:
            if run.window is not None:
                self._swo_pull(run)
            if self._debug is not None:
                write_registers(self.target, run.restore)
        finally:
            pyocd = _PyocdDwt(self.target) if self._debug is not None else None
            for index in run.reserved:
                if pyocd is not None:
                    pyocd.release(index)
            if run.window is not None:
                self._close_window(run.window)
            run.running = False
            self._swo_finish_assembly(run)
        return run

    def _swo_finish_assembly(self, run: _SwoRun) -> None:
        tail = run.stamper.flush() if run.stamper is not None else []
        self._swo_take(run, [(run.times.pop(id(p.packet), 0), p.local_time, p.packet) for p in tail])
        self._swo_keep(run, run.assembler.flush())
        self._swo_record(run)

    # --- 読む ------------------------------------------------------------------------------------

    def swo_trace(self, offset: int = 0, limit: int = 500, name: str | None = None) -> dict:
        """swo_configure の結果（設定中ならいままでの分、戻した後なら最後の結果）: データトレース・値一致（trace）の
        アクセス（時刻・変数・値・読み書き・PC とその関数と行）の offset から limit 件（name で変数を絞る）、
        イベントカウンタの合計（溢れ × 256）、例外の回数。記録中なら記録の stream "swo" にも入れる。"""
        run = self._swo_run
        if run is None:
            raise RuntimeError("swo_configure で設定していない")
        if run.running:
            with self._lock:
                if run.window is not None:
                    self._swo_pull(run)
                self._swo_record(run)
        return self._trace_dict(run, offset, limit, name)

    def _swo_pull(self, run: _SwoRun) -> None:
        """窓の新しいバイトを解読して組み立てる（_lock の中で呼んでよい: 窓の読みは RLock を取り直すだけ）。"""
        new = [(t, b) for t, _, b in self._window_events(run.window, "swo") if t > run.last_t]
        if not new:
            return
        run.last_t = new[-1][0]
        packets = run.stream.feed(new)
        if run.stamper is None:
            items = [(t, None, p) for t, p in packets]
        else:
            run.times.update((id(p), t) for t, p in packets)      # ローカルのタイムスタンプが来るまで待つパケット
            timed = run.stamper.feed([p for _, p in packets])
            items = [(run.times.pop(id(tp.packet), 0), tp.local_time, tp.packet) for tp in timed]
        self._swo_take(run, items)

    def _swo_take(self, run: _SwoRun, items: list) -> None:
        for t, _, p in items:
            if isinstance(p, ExceptionTrace):
                name = exception_name(p.number)
                if p.action.name == "ENTER":
                    run.exception_counts[name] = run.exception_counts.get(name, 0) + 1
                if len(run.exceptions) < EXCEPTION_LIMIT:
                    run.exceptions.append((t, p.number, p.action.name.lower()))
            run.overflows += isinstance(p, Overflow)
            run.errors += isinstance(p, DecodeError)
        run.counters.feed(p for _, _, p in items)
        self._swo_keep(run, run.assembler.feed(items))

    @staticmethod
    def _swo_keep(run: _SwoRun, accesses: list[DataAccess]) -> None:
        room = max(ACCESS_LIMIT - len(run.accesses), 0)
        run.accesses += accesses[:room]
        run.dropped += max(len(accesses) - room, 0)

    def _swo_record(self, run: _SwoRun) -> None:
        rec = self._recorder
        if rec is None or (run.window is not None and not run.window.recorded):
            run.recorded = len(run.accesses)
            return
        rows = []
        for a in run.accesses[run.recorded:]:
            body = {k: v for k, v in self._access_dict(run, a).items() if k not in ("t", "time_us")}
            rows.append(("swo", "data_access", a.t, a.address or 0, a.value or 0, body))
        run.recorded = len(run.accesses)
        for counter, n in run.counters.overflows.items():
            if n > run.recorded_overflows.get(counter, 0):
                rows.append(("swo", "counter_overflow", run.last_t, 0, 256 * n, {"counter": counter, "total": 256 * n}))
                run.recorded_overflows[counter] = n
        if rows:
            rec.add_many(rows)

    def _access_dict(self, run: _SwoRun, a: DataAccess) -> dict:
        out = {"t": a.t, "time_us": (a.t - run.window.t0) * 1e6 / run.timebase_hz if run.window else None,
               "name": a.name, "role": a.role, "comparator": a.comparator, "write": a.write,
               "address": a.address, "size": a.size, "raw": a.value, "value": a.value, "complete": a.complete}
        if a.local_time is not None:
            out["local_time"] = a.local_time
        w = run.targets.get(a.name)
        if w is not None and a.value is not None and a.size == w.size and w.type is not None and w.bit_size is None:
            try:
                out["value"] = decode(w.type, a.value.to_bytes(a.size, "little"))
                out["type"] = type_name(w.type)
            except Exception:                                    # 解釈できない型: 生の値のまま
                pass
        if a.pc is not None:
            out["pc"] = a.pc
            if self._symbols is not None:
                out.update({k: v for k, v in self._place_of(a.pc).items()})
        return out

    def _trace_dict(self, run: _SwoRun, offset: int, limit: int, name: str | None) -> dict:
        accesses = [a for a in run.accesses if name is None or a.name == name]
        out = {"running": run.running, "core": run.core.name, "total": len(accesses),
               "accesses": [self._access_dict(run, a) for a in accesses[offset:offset + limit]],
               "counters": run.counters.as_dict() if run.request.counters else {},
               "exceptions": {"counts": dict(run.exception_counts), "events": len(run.exceptions)},
               "lost": run.assembler.lost, "outside": run.assembler.outside, "unknown": run.assembler.unknown,
               "overflows": run.overflows, "decode_errors": run.errors, "dropped": run.dropped,
               "comparators": [c.as_dict() for c in run.plan.comparators], "warnings": list(run.plan.warnings)}
        if run.window is not None:
            out["window"] = self._window_info(run.window, run.timebase_hz)
        return out

    def _swo_counter_overflows(self) -> dict[str, int] | None:
        """いまのイベントカウンタの溢れの数（swo_configure の counters で数えているもの。無ければ None）。
        サイクルの計測（probe_cycles）が 8 ビットのカウンタの折り返しを補うのに使う。_lock の中で呼ぶ。"""
        run = self._swo_run
        if run is None or not run.running or not run.request.counters or run.window is None:
            return None
        self._swo_pull(run)
        return {c: run.counters.overflows[c] for c in run.request.counters}
