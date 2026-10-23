"""Probe の道具: サイクルの計測（U12）。CYCCNT による 2 つのブレークポイントの間のサイクル、DWT のイベントカウンタ
（CPI・EXC・SLEEP・LSU・FOLD）と命令数、DAP サンプラで CYCCNT を時刻付きで読む CPU クロックの実測。

Probe（fprobe.mcp_server）に混ぜて使う mixin。Probe の _lock・target・session・dbg・_run_until_halt・_place_of と、
WindowTools（DAP サンプラの持ち主・出来事の窓・write_registers）、SwoTools（イベントカウンタの溢れの数）を使う。
機器に触るのは必ず _lock の中（止まるのを待つ間は放す）。

- 区間: 始めと終わりにブレークポイントを置き（無ければ。終えたら外す）、始めで止まったら CYCCNT と 8 ビットの
  カウンタを読み、終わりで止まったらもう一度読んで差を取る。CYCCNT はコアが止まっている間は進まないので、
  止まる・再開の手間は区間に入らない（始めの命令は数に入り、終わりの命令は入らない）。
- 8 ビットのカウンタは 256 で折り返す。swo_configure の counters で溢れを数えていればその分を足す（正確）。
  数えていなければ 256 未満とみなす（counters_exact = false）。
- CPU クロック: サンプラが CYCCNT を周期的に読み、デバイスのタイムベースの時刻との直線の傾き（最小二乗）。
  サンプラが無い・ほかの道具が使っているときは、ホストから 2 回読んでその前後のタイムベースの時刻で挟む。
"""

import time

from pyocd.core.target import Target

from .probe_window import write_registers
from .profile.cycles import (clock_from_two_reads, counter_delta, estimate_clock, instructions, interval_summary)
from .profile.dwt import DEMCR, DEMCR_TRCENA, DWT_CTRL, RegisterWrite
from .sampler import Sampler
from .swo_trace import COUNTERS, DWT_CYCCNT, NOPRFCNT

__all__ = ["CyclesTools"]

INTERVAL_LIMIT = 10_000
_CYCCNTENA = 1
_NOCYCCNT = 1 << 25                                     # DWT_CTRL（読み出し専用）: CYCCNT が無い（ARMv7-M）
_ENABLE = [RegisterWrite(DEMCR, DEMCR_TRCENA, DEMCR_TRCENA, "DEMCR.TRCENA: DWT を使う"),
           RegisterWrite(DWT_CTRL, _CYCCNTENA, _CYCCNTENA, "DWT_CTRL.CYCCNTENA: サイクルを数える")]


class CyclesTools:
    """サイクルの計測（Probe の mixin）。"""

    def cycles_read(self) -> dict:
        """CYCCNT（32 ビット）と 8 ビットのイベントカウンタのいまの値と、読んだときのタイムベースの時刻。変えない。"""
        with self._lock:
            target = self.target
            ctrl = target.read_memory(DWT_CTRL)
            out = {"cyccnt": target.read_memory(DWT_CYCCNT), "cyccnt_enabled": bool(ctrl & _CYCCNTENA),
                   "time": self.session.device.time()}
            if not ctrl & NOPRFCNT:
                out["counters"] = self._read_counters(target)
            return out

    @staticmethod
    def _read_counters(target) -> dict[str, int]:
        return {name: target.read_memory(address) & 0xFF for name, (_, address, _) in COUNTERS.items()}

    # --- 区間 ------------------------------------------------------------------------------------

    def cycles_measure(self, start: str, end: str, count: int = 1, timeout_s: float = 5.0,
                       core_clock_hz: float | None = None, counters: bool = True) -> dict:
        """start（関数名・"ファイル:行"・番地）から end までのサイクルを count 回測る（ターゲットを走らせ、止まるたびに
        CYCCNT を読む）。counters なら CPI・EXC・SLEEP・LSU・FOLD の増分と命令数も。core_clock_hz があれば µs も。
        終えるとターゲットは end で止まっている。区間の一覧・統計・止まった場所を返す。"""
        if count < 1 or count > INTERVAL_LIMIT:
            raise ValueError(f"count は 1〜{INTERVAL_LIMIT}")
        with self._lock:
            dbg, target = self.dbg, self.target
            ctrl = target.read_memory(DWT_CTRL)
            if counters and ctrl & NOPRFCNT:
                raise RuntimeError("このコアの DWT にはイベントカウンタが無い（counters=false で CYCCNT だけ）")
            if ctrl & _NOCYCCNT:
                raise RuntimeError("このコアには CYCCNT（サイクルのカウンタ）が無い（DWT_CTRL.NOCYCCNT）")
            write_registers(target, _ENABLE)
            if not target.read_memory(DWT_CTRL) & _CYCCNTENA:    # ARMv6-M（Cortex-M0/M0+）は書いても立たず、読むと 0
                raise RuntimeError("このコアには CYCCNT（サイクルのカウンタ）が無い（DWT_CTRL.CYCCNTENA を書いても立たない。"
                                   "Cortex-M0/M0+ 等）")
            locations = {"start": dbg.breakpoint_address(start), "end": dbg.breakpoint_address(end)}
            added = []
            for location, address in ((start, locations["start"]), (end, locations["end"])):
                if address not in dbg.breakpoints and address not in added:
                    dbg.breakpoint(location)
                    added.append(address)
        intervals: list[int] = []
        deltas: list[dict] = []
        notes: list[str] = []
        exact = True
        restarts = 0                                             # 終わりの前にまた始めに来た回（その回は測り直す）
        deadline = time.monotonic() + timeout_s
        try:
            while len(intervals) < count:
                if not self._run_to(locations["start"], deadline):
                    notes.append(f"timeout: 始めの場所に {timeout_s} 秒以内に着かなかった（{len(intervals)} 回で終えた）")
                    break
                with self._lock:
                    c0 = self.target.read_memory(DWT_CYCCNT)
                    k0 = self._read_counters(self.target) if counters else None
                    o0 = self._swo_counter_overflows() if counters else None
                    t0 = time.monotonic()
                stop = self._run_to_either(locations, deadline)
                if stop is None:
                    notes.append(f"timeout: 終わりの場所に {timeout_s} 秒以内に着かなかった（{len(intervals)} 回で終えた）")
                    break
                if stop == "start":                              # 終わりの前にまた始めに来た: そこから測り直す
                    restarts += 1
                    continue
                with self._lock:
                    c1 = self.target.read_memory(DWT_CYCCNT)
                    k1 = self._read_counters(self.target) if counters else None
                    o1 = self._swo_counter_overflows() if counters else None
                cycles = (c1 - c0) % (1 << 32)
                if core_clock_hz and (time.monotonic() - t0) * core_clock_hz >= 1 << 32:
                    notes.append("区間が CYCCNT の 1 周（2^32 サイクル）を超えたかもしれない: その回の値は 2^32 の倍数ずれる")
                intervals.append(cycles)
                if counters:
                    delta = {name: counter_delta(k0[name], k1[name], None if o0 is None or name not in o0
                                                 else o1[name] - o0[name]) for name in COUNTERS}
                    exact &= o0 is not None and all(name in o0 for name in COUNTERS)
                    delta["cycles"] = cycles
                    delta["instructions"] = instructions(delta)
                    delta["ipc"] = delta["instructions"] / cycles if cycles else None
                    deltas.append(delta)
        finally:
            if restarts:
                notes.append(f"終わりの前に始めの場所に戻った回が {restarts} 回あった（その回は始めから測り直した）")
            with self._lock:
                if self._debug is not None:
                    for address in added:
                        self.dbg.remove_breakpoint(address)
        if not intervals:
            raise RuntimeError(f"区間を 1 回も測れなかった（{'; '.join(notes)}）: 始めと終わりの場所・timeout_s を確かめる")
        out = {"intervals": intervals, "summary": interval_summary(intervals, core_clock_hz),
               "start": {"address": locations["start"], **self._place_of(locations["start"])},
               "end": {"address": locations["end"], **self._place_of(locations["end"])}, "notes": notes}
        if counters:
            out["counters"] = deltas
            out["counters_exact"] = exact
            if not exact:
                notes.append("8 ビットのイベントカウンタは 256 で折り返す: 溢れを数えていない（swo_configure の counters）"
                             "ので、各カウンタの増分は 256 未満とみなした")
        return out

    def _run_to(self, address: int, deadline: float) -> bool:
        """address で止まるまで走らせる（ほかの場所で止まったらまた走らせる）。時間切れなら False。"""
        while True:
            stop = self._run_until_halt(max(0.0, deadline - time.monotonic()))
            if stop["reason"] == "timeout":
                return False
            if stop.get("pc") == address:
                return True
            if time.monotonic() >= deadline:
                return False

    def _run_to_either(self, locations: dict[str, int], deadline: float) -> str | None:
        """終わりか始めで止まるまで走らせ、どちらかを返す（ほかの場所はまた走らせる）。時間切れなら None。"""
        while True:
            stop = self._run_until_halt(max(0.0, deadline - time.monotonic()))
            if stop["reason"] == "timeout":
                return None
            for key in ("end", "start"):
                if stop.get("pc") == locations[key]:
                    return key
            if time.monotonic() >= deadline:
                return None

    # --- CPU クロック -----------------------------------------------------------------------------

    def cycles_clock(self, duration_s: float = 1.0, rate_hz: float = 100.0, ap: int = 0) -> dict:
        """CPU クロックを実測する（コアが走っている間に、CYCCNT の進みをデバイスのタイムベースと比べる）。DAP サンプラが
        使えれば rate_hz で duration_s の間読んで直線の傾き、使えなければ前後 2 回の読み。CYCCNT を有効にする。"""
        if duration_s <= 0 or rate_hz <= 0:
            raise ValueError("duration_s と rate_hz は正")
        warnings: list[str] = []
        with self._lock:
            target = self.target
            if target.get_state() != Target.State.RUNNING:
                raise RuntimeError("コアが走っていない（CYCCNT は止まっている間は進まない）: target_resume してから測る")
            write_registers(target, _ENABLE)
            device = self.session.device
            hz = device.capabilities().timebase_hz
            window = None
            try:
                self._claim_sampler("clock")
            except RuntimeError as e:
                warnings.append(f"{e}: 2 回の読みで測った（誤差は USB の往復の幅）")
            else:
                try:
                    sampler = Sampler(device)
                    sampler.configure([(DWT_CYCCNT, 4)], 1.0 / rate_hz, ap)
                    window = self._open_window()
                    sampler.start()
                except Exception as e:
                    self._release_sampler("clock")
                    warnings.append(f"DAP サンプラが使えない（{e}）: 2 回の読みで測った")
                    window = None
            if window is None:
                first = self._timed_read(target, device)
        if window is None:
            time.sleep(duration_s)
            with self._lock:
                second = self._timed_read(self.target, self.session.device)
            est = clock_from_two_reads(first, second, hz)
            est.warnings[:0] = warnings
            return {"method": "two_reads", **est.as_dict()}
        try:
            time.sleep(duration_s)
        finally:
            with self._lock:
                Sampler(self.session.device).stop()
                self._close_window(window)
                self._release_sampler("clock")
        samples = [(t, v) for t, a, v in self._window_events(window, "sample") if a == DWT_CYCCNT]
        est = estimate_clock(samples, hz)
        est.warnings[:0] = warnings
        if window.lost:
            est.warnings.append(f"イベント記録の {window.lost} 件を読めなかった: duration_s か rate_hz を下げる")
        return {"method": "sampler", "rate_hz": rate_hz, **est.as_dict()}

    @staticmethod
    def _timed_read(target, device) -> tuple[int, int, int]:
        before = device.time()
        value = target.read_memory(DWT_CYCCNT)
        return before, device.time(), value
