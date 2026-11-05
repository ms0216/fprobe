"""テストポイントの SWO（ITM）での受け取り（TestpointService に混ぜ込む部品）。

受信を始めてから出力（ITM_TER）を有効にし、止めると ITM_TER を元に戻す。窓（記録、またはイベント記録）の新しい SWO のバイトを
解析へ渡す。背景の周期の読みの本体（_tp_itm_puller）は probe_testpoint.py（周期 ITM_PULL_S をそこで持つ）。
Probe の _lock・session・target・_debug・_swo_baud・swo_start と、WindowService（_open_window・_close_window・_window_events）を使う。
"""

import threading
import time

from . import probe_window
from .probe_window import EventWindow, write_registers
from .probe_testpoint_run import _Run
from .profile.dwt import ITM_TER, RegisterWrite, itm_port_writes, plan_swo
from .testpoint import ItmDecoder, TimedSwo, TimeMap

__all__ = ["TestpointItmMixin"]


class TestpointItmMixin:
    """method itm の始め方・止め方・窓からの取り込み。"""

    def _tp_start_itm(self, table, table_path, options, port, swo_baud, core_clock_hz) -> _Run:
        target = self._host.target
        if not 0 <= port < 32:
            raise ValueError("port は 0〜31")
        if swo_baud is None and self._host._swo_baud is None:
            raise RuntimeError("SWO の受信の速度が分からない（swo_baud を渡すか swo_start を先に）")
        warnings: list[str] = []
        enable = itm_port_writes(port)                           # DEMCR・LAR・TCR・TER（port のビット）
        setup = enable[:-1]
        baud = swo_baud
        if swo_baud is not None and core_clock_hz is not None:
            swo = plan_swo(core_clock_hz, swo_baud)
            setup += list(swo.writes)
            warnings += swo.warnings
            baud = round(swo.baud)
        # 受信を始めてから出力を有効にする: ITM の全ポートを止めて（他のポートの printf もパケットの途中で窓が
        # 始まらないように）受信と窓を始め、元のポートと port を有効にする
        before = write_registers(target, setup + [RegisterWrite(ITM_TER, 0, note="ITM: 全ポートを止める")])
        try:
            if baud is not None:
                self._host.swo_start(baud)
            baud = baud or self._host._swo_baud
            window = self._host._open_window()
            seen = 0 if window.recorded else window.seen
            target.write_memory(ITM_TER, before[ITM_TER] | 1 << port)
        except Exception:
            target.write_memory(ITM_TER, before[ITM_TER])        # 止めたポートを戻す
            raise
        hz = self._host.session.device.capabilities().timebase_hz
        run = _Run("itm", None, self._tp_analysis(table, hz, options), str(table_path), ItmDecoder(port),
                   {"port": port, "swo_baud": baud, "core_clock_hz": core_clock_hz}, time.monotonic(),
                   warnings=warnings, window=window, swo=TimedSwo(), restore={ITM_TER: before[ITM_TER]},
                   ring_seen=seen)
        # 時刻は記録の先頭のバイトを FPGA が受けた時刻（出来事の上限）。端子の辺からの遅れは SWO の方式・分周で決まり
        # （UART 方式と Manchester で違う。8N1 の 10 ビットを仮定しない）、対象の FIFO の遅れは分からない: 幅は None
        run.live.anchor(TimeMap(hz, hz, 0, None, bound="upper"))
        run.pump = threading.Thread(target=self._tp_itm_puller, args=(run,), name="fprobe-testpoint-itm",
                                    daemon=True)
        return run

    def _tp_stop_itm(self, run: _Run) -> None:
        run.halt.set()
        run.pump.join()
        with self._host._lock:
            if self._host._debug is not None:
                for address, value in run.restore.items():
                    self._host.target.write_memory(address, value)
            self._host._close_window(run.window)
        self._tp_pull_itm(run)
        run.live.feed(run.decoder.flush())

    # --- ITM: 窓から届いた分を解析へ --------------------------------------------------------------

    def _tp_pull_itm(self, run: _Run) -> None:
        """窓の SWO のバイトのうち、まだ解析に渡していないものを渡す（_lock の外で。背景と道具の呼び出しの両方から）。
        記録からは前回の最後の時刻から後を、イベント記録からは前回までに読んだ数より後を読む（同じ時刻のバイトは
        渡した数で見分ける）。"""
        with run.pulling:
            events = self._tp_new_swo(run)
            fresh = []
            seen_at_last = 0
            for t, b in events:
                if run.last_t is not None and t < run.last_t:
                    continue
                if run.last_t is not None and t == run.last_t:
                    seen_at_last += 1
                    if seen_at_last <= run.at_last:
                        continue
                fresh.append((t, b))
            if not fresh:
                return
            last = fresh[-1][0]
            run.at_last = sum(1 for t, _ in fresh if t == last) + (run.at_last if last == run.last_t else 0)
            run.last_t = last
            run.live.feed(run.decoder.feed(run.swo.feed(fresh)))

    def _tp_new_swo(self, run: _Run) -> list[tuple[int, int]]:
        window = run.window
        if window.recorded:
            if run.last_t is not None:
                window = EventWindow(run.last_t, True, t1=run.window.t1)
            return [(t, b) for t, _, b in self._host._window_events(window, "swo")]
        with self._host._lock:
            written = probe_window.EventLog(self._host.session.device).counts()[0]
        part = EventWindow(window.t0, False, run.ring_seen, window.t1)
        events = self._host._window_events(part, "swo")
        run.ring_seen = written                                   # 間に増えた分は次も読む（時刻で重複を除く）
        window.lost += part.lost
        window.notes += [n for n in part.notes if n not in window.notes]
        return [(t, b) for t, _, b in events]
