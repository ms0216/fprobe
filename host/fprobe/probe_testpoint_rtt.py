"""テストポイントの RTT での受け取り（TestpointService に混ぜ込む部品）。

時刻はターゲットの CYCCNT（core_clock_hz）。タイムベースへの写しは、最初に受けた かたまりの最後の記録をホストの時計で受けた
時刻に合わせる。Probe の session・_rtt と、ObserveService の RTT（rtt_start・rtt_stop・_rtt_listen・_rtt_unlisten）を使う。
"""

import time

from .probe_testpoint_run import _Run
from .testpoint import Hit, RttDecoder, TimeMap

__all__ = ["TestpointRttMixin"]


class TestpointRttMixin:
    """method rtt の始め方・止め方・受信の取り込み。"""

    def _tp_start_rtt(self, table, table_path, options, rtt_channel, core_clock_hz) -> _Run:
        if core_clock_hz is None or core_clock_hz <= 0:
            raise ValueError("method rtt には core_clock_hz（CYCCNT の速さ = コアのクロック）が要る")
        started_rtt = self._host._rtt is None
        if started_rtt:
            self._host.rtt_start()
        try:
            up = {c.index for c in self._host._rtt.control_block.up}
            if rtt_channel not in up:
                raise ValueError(f"RTT の上りのチャンネル {rtt_channel} が無い（{sorted(up)}）")
            device = self._host.session.device
            h0 = time.monotonic()
            ticks = device.time()
            h1 = time.monotonic()
            hz_tb = device.capabilities().timebase_hz
        except Exception:
            if started_rtt:
                self._host.rtt_stop()
            raise
        run = _Run("rtt", None, self._tp_analysis(table, core_clock_hz, options), str(table_path), RttDecoder(),
                   {"rtt_channel": rtt_channel, "core_clock_hz": core_clock_hz}, time.monotonic(),
                   rtt_channel=rtt_channel, started_rtt=started_rtt,
                   host_clock=((h0 + h1) / 2, ticks, (h1 - h0) / 2 * hz_tb))
        run.warnings.append("rtt: 時刻はターゲットの CYCCNT。タイムベースへの写しはホストの時計で受けた時刻に合わせる"
                            "（不確かさ付き。ターゲットとプローブのクロックのずれは補正しない）")

        def listener(channel, data: bytes, host_s: float, run=run) -> None:
            if channel == run.rtt_channel and run.running:
                self._tp_rtt_received(run, data, host_s)
        run.rtt_listener = listener
        self._host._rtt_listen(listener)
        return run

    def _tp_rtt_received(self, run: _Run, data: bytes, host_s: float) -> None:
        """RTT の受信（ObserveService の背景のスレッドから、_lock の中で）。"""
        items = run.decoder.feed(data)
        if not run.synced:                                        # 受け始めの途中の記録は取りこぼしではない
            first = next((k for k, i in enumerate(items) if isinstance(i, Hit)), None)
            if first is None:
                run.skipped_before_sync += len(items)
                return
            run.skipped_before_sync += first
            items, run.synced = items[first:], True
        if run.live.time_map is None:
            # かたまりの最後の記録は、この読みより前に書かれた: 読んだ PC の時刻は上限（どれだけ前かは分からない。
            # 設定の読みの周期 poll_s を幅として足さない: 実際の間隔は周期と限らない）。幅は PC の時計の写しの分だけ
            last = max(i.t for i in items if isinstance(i, Hit))
            hz_tb = self._tp_timebase_hz()
            ticks, uncertainty = self._tp_host_ticks(run, host_s)
            run.live.anchor(TimeMap(run.live.hz, hz_tb, ticks - round(last * hz_tb / run.live.hz),
                                    uncertainty, bound="upper"))
        run.live.feed(items)

    def _tp_timebase_hz(self) -> float:
        return self._host.session.device.capabilities().timebase_hz

    def _tp_host_ticks(self, run: _Run, host_s: float) -> tuple[int, float]:
        """ホストの単調時計 → タイムベース（記録中なら記録の時計の対応付け、そうでなければ始めたときの 1 点と公称の速さ。
        1 点では速さが分からないので、不確かさに点からの経過時間をそのまま足す: fprobe.recording.ClockMap と同じ）。"""
        rec = run.live.recorder
        if rec is not None:
            try:
                return rec.clock.to_ticks(host_s)
            except RuntimeError:
                pass                                              # 記録を始めた直後で対応付けの点が無い
        h, ticks, uncertainty = run.host_clock
        hz = self._tp_timebase_hz()
        return ticks + round((host_s - h) * hz), uncertainty + abs(host_s - h) * hz

    def _tp_stop_rtt(self, run: _Run) -> None:
        self._host._rtt_unlisten(run.rtt_listener)
        with self._host._lock:
            run.stopped = time.monotonic()                        # 背景の受信が以後は渡さない
            if self._host._rtt is not None and run.started_rtt:
                self._host.rtt_stop()
        items = run.decoder.flush()
        if run.synced:
            run.live.feed(items)
