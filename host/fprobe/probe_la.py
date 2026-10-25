"""ロジックアナライザ（la_*）の mixin: 内蔵の捕捉（la_arm / la_collect / la_capture）・深いバッファ（la_deep_*）・流し込み（la_stream）・
捕捉の保持と波形（la_view）・トリガの状態。

Probe（fprobe.probe。共通の部品の型は fprobe.probe_host.ProbeHost）に混ぜて使う。Probe の _lock・session・_caps・_la・_la_period・_capture* と、
WatchTools の _la_idle / _la_record 等を使う。部品は fprobe.la（LogicAnalyzer・DeepAnalyzer・波形）。
"""

from pathlib import Path

from . import generated as g
from .api import NoData
from .collector import add_capture
from .la import DeepAnalyzer, LogicAnalyzer, Transitions, Waveform, channel_names, external_pattern, write_vcd


def _transition_summary(tr) -> dict:
    period_ns = tr.sample_period_s * 1e9
    out = {}
    for ch in range(tr.channels):
        edges = tr.edges(ch)
        initial = tr.values[0] >> ch & 1 if len(tr.values) else 0
        if not edges and not initial:
            continue
        out[ch] = {"initial": initial, "edges": len(edges),
                   "first_edges_ns": [round((i - tr.trigger) * period_ns) for i in edges[:16]]}
    return out


FIRST_EDGES = 16                       # la_collect の要約で時刻を並べる先頭の変化の数（切ったかは結果に出す）


def _channel_summary(cap) -> dict:
    """{channels: {ch: {initial, edges, first_edges_ns（トリガ = 0 ns からの先頭 FIRST_EDGES 個）, first_edges_truncated}},
    first_edges_limit, constant_low（常に 0 で省いたチャンネル）}。"""
    period_ns = cap.sample_period_s * 1e9
    out, low = {}, []
    for ch in range(cap.channels):
        bits = cap.channel(ch)
        edges = [i for i in range(1, len(bits)) if bits[i] != bits[i - 1]]
        if not edges and not bits[0]:
            low.append(ch)                                 # 常に 0 のチャンネルは省く（どれを省いたかは返す）
            continue
        out[ch] = {
            "initial": bits[0],
            "edges": len(edges),
            "first_edges_ns": [round((i - cap.trigger) * period_ns) for i in edges[:FIRST_EDGES]],
            "first_edges_truncated": len(edges) > FIRST_EDGES,
        }
    return {"channels": out, "first_edges_limit": FIRST_EDGES, "constant_low": low}


class LogicAnalyzerTools:
    """Probe に混ぜるロジックアナライザの道具。"""

    def _init_la(self) -> None:
        self._deep: DeepAnalyzer | None = None             # 仕掛けた深いバッファの捕捉（la_deep_arm → la_deep_collect）

    def la_arm(self, rate_hz: float | None = None, pretrigger: int = 1024, mask: int = 0, value: int = 0,
               rise: int = 0, fall: int = 0, filter_channels: int = 0, filter_ns: float = 0.0,
               pulse: dict | None = None, external_only: bool = False) -> dict:
        """捕捉を仕掛ける（すぐ戻る）。条件が何もなければ即トリガ。external_only = true ならクロストリガ（xtrig_route の
        to_la・fpga_obs_trigger_set の la）でだけ捕捉する（パターンとは一緒に指定できない）。
        filter_channels（外の信号の ch = 拡張ヘッダのピンと専用の入力のビット）は filter_ns より短い変化を捨てる
        （グリッチフィルタ。フィルタした ch は filter.delay_ns 遅れて記録される）。pulse はパルス幅のトリガ {"channel", "level"（既定 1 = High の脈）,
        "min_ns", "max_ns"（省略で上限なし）, "timeout"（true なら level が min_ns を超えて続いた時点で成立）}。
        パルス幅はパターン（mask・rise・fall）と両方満たしたときに成立。ケイパビリティ la_filter / la_pulse が要る。"""
        with self._lock:
            self._la_idle()
            self._la = LogicAnalyzer(self.session.device)
            self._la_period = self._la.start(rate_hz=rate_hz, pretrigger=pretrigger, mask=mask, value=value,
                                             rise=rise, fall=fall, filter_channels=filter_channels,
                                             filter_ns=filter_ns, pulse=pulse, external_only=external_only)
            return {"sample_period_ns": self._la_period * 1e9, "depth": self._la.depth, "pretrigger": pretrigger,
                    **self._la.front_info}

    def la_collect(self, timeout_s: float = 5.0, vcd_path: str | None = None) -> dict:
        """完了を待って読み出し、チャンネルごとの要約（トリガ = 0 ns）を返す。vcd_path があれば VCD も書く。"""
        with self._lock:
            if self._la is None:
                raise RuntimeError("la_arm を先に呼ぶ")
            la, self._la = self._la, None
            la.wait(timeout_s)
            cap = la.read(self._la_period)
            names = channel_names(self.session.device.capabilities())
            self._keep_capture(Transitions.from_capture(cap), "la.capture", names)
            if vcd_path:
                write_vcd(cap, Path(vcd_path), names)
            return {"trigger_index": cap.trigger, "trigger_time": cap.trigger_time,
                    "trigger_time_span": self.session.device.capabilities().la_phase_span,
                    "sample_period_ns": cap.sample_period_s * 1e9, "samples": len(cap.samples),
                    **_channel_summary(cap), "vcd": vcd_path}

    def _keep_capture(self, tr: Transitions, source: str, names: dict[int, str] | None = None) -> None:
        """最後の捕捉を画面の波形（la_view）のために持つ。変化点の表はここでは作らない（画面を使わない操作を遅くしない）。
        names は ch の既定の名前（捕捉した機器の申告 LA_LAYOUT か、読み込んだファイルの名前。書き出しに使う）。"""
        if self._recorder is not None:
            add_capture(self._recorder, tr)                       # 記録中なら共通の時間軸の記録にも入れる
        with self._capture_lock:                                  # 捕捉・情報・表をそろえて替える（_capture_wave）
            self._capture_id += 1
            self._capture, self._wave = tr, None
            self._capture_info = {"id": self._capture_id, "length": tr.length,
                                  "sample_period_ns": tr.sample_period_s * 1e9, "trigger_index": tr.trigger,
                                  "channels": tr.channels, "source": source}
            self._capture_names = dict(names or {})

    def _capture_wave(self) -> tuple[Waveform, dict]:
        """最後の捕捉の変化点の表と情報（画面の波形・タイル用。表は最初に求められたときに作る）。読みのスレッドから
        呼ばれるので、機器の操作のロック（_lock。la_collect 等が長く持つ）ではなく捕捉の小さなロックで読む。"""
        with self._capture_lock:
            if self._capture is None:
                raise NoData("捕捉が無い（la_capture / la_deep_capture を先に）")
            tr, info, wave = self._capture, self._capture_info, self._wave
        if wave is None:
            wave = Waveform(tr)                                       # 長い捕捉では時間がかかるので、ロックの外で作る
            with self._capture_lock:
                if self._capture is tr:
                    self._wave = wave
        return wave, info

    def la_view(self, start: int = 0, end: int | None = None, width: int = 1000) -> dict:
        """最後の捕捉を画面の幅に縮めた波形。levels[ch][i] は画素 i（サンプル [start + i·(end−start)/width, …)）が
        '0' / '1' / 'x'（区間内で変化）。"""
        wave, info = self._capture_wave()
        end = info["length"] if end is None else end
        levels = wave.view(start, end, width)
        return {"capture": info, "start": max(0, start), "end": min(info["length"], end), "width": width, "levels": levels}

    def la_trigger_status(self) -> dict:
        """ロジアナのトリガの条件と状態（la ブロックのレジスタの読み戻し。la_arm・la_deep_arm が書いたもの）:
        mask・value・rise・fall、external_only（パターンが決して成り立たない形 = クロストリガでだけ捕捉。la_arm の
        external_only が書く形そのもの）、rate_hz・pretrigger、filter（ケイパビリティ la_filter）・pulse（la_pulse）、
        internal（内蔵バッファの捕捉の状態: capturing・triggered・done・trigger_time）・deep（深いバッファの同じもの。
        外部メモリがあれば）。trigger_time はトリガした後だけ（それまで None）。トリガの回数を数えるレジスタは無い。"""
        with self._lock:
            dev = self.session.device
            caps = dev.capabilities()
            if not getattr(caps, "la_channels", 0):
                raise RuntimeError("このデバイスにはロジックアナライザが無い")
            rate, pretrigger, mask, value, rise, fall = dev.read(g.LA_RATE, 6)
            out: dict = {"mask": mask, "value": value, "rise": rise, "fall": fall,
                         "external_only": (mask, value, rise, fall) == external_pattern(True, 0, 0, 0, 0, None)
                         and not (getattr(caps, "la_pulse", False) and dev.read32(g.LA_PULSE) & 3),
                         "rate_hz": caps.la_sample_hz / ((rate & 0xFFFF) + 1), "pretrigger": pretrigger}
            clock = caps.sys_clock_hz
            if getattr(caps, "la_filter", False):
                word = dev.read32(g.LA_FILTER)
                cycles = word >> g.LA_FILTER_CYCLES[0] & (1 << g.LA_FILTER_CYCLES[1]) - 1
                out["filter"] = {"channels": word >> g.LA_FILTER_CHANNELS[0] & (1 << g.LA_FILTER_CHANNELS[1]) - 1,
                                 "cycles": cycles, "min_width_ns": cycles / clock * 1e9 if cycles > 1 else None}
            if getattr(caps, "la_pulse", False):
                word, low, high = dev.read(g.LA_PULSE, 3)
                mode = ("none", "width", "timeout", "none")[word >> g.LA_PULSE_MODE[0] & 3]
                out["pulse"] = {"mode": mode} if mode == "none" else {
                    "mode": mode, "level": word >> g.LA_PULSE_LEVEL[0] & 1,
                    "channel": word >> g.LA_PULSE_CHANNEL[0] & (1 << g.LA_PULSE_CHANNEL[1]) - 1,
                    "min_ns": low / clock * 1e9, "max_ns": None if high == 0xFFFF_FFFF else high / clock * 1e9}

            def state(status_addr, fields, time_addr) -> dict:
                status = dev.read32(status_addr)
                lo, hi = dev.read(time_addr, 2)
                bits = {k: bool(status >> f[0] & 1) for k, f in fields.items()}
                return {**bits, "trigger_time": hi << 32 | lo if bits["triggered"] else None}
            if getattr(caps, "la_depth", 0):
                out["internal"] = state(g.LA_STATUS, {"capturing": g.LA_STATUS_CAPTURING, "triggered": g.LA_STATUS_TRIGGERED,
                                                      "done": g.LA_STATUS_DONE}, g.LA_TRIGGER_TIME_LO)
            if getattr(caps, "mem_bytes", 0):
                out["deep"] = state(g.LA_DEEP_STATUS, {"capturing": g.LA_DEEP_STATUS_CAPTURING,
                                                       "triggered": g.LA_DEEP_STATUS_TRIGGERED,
                                                       "done": g.LA_DEEP_STATUS_DONE}, g.LA_DEEP_TRIGGER_TIME_LO)
            return out

    def la_capture(self, timeout_s: float = 5.0, vcd_path: str | None = None, **trigger) -> dict:
        armed = self.la_arm(**trigger)
        return {**self.la_collect(timeout_s, vcd_path), **{k: armed[k] for k in ("filter", "pulse", "pretrigger")
                                                            if k in armed}}

    def la_deep_capture(self, rate_hz: float | None = None, mask: int = 0, value: int = 0, rise: int = 0,
                        fall: int = 0, rle: bool = True, before_beats: int = 4096, after_beats: int = 4096,
                        timeout_s: float = 10.0, vcd_path: str | None = None, filter_channels: int = 0,
                        filter_ns: float = 0.0, pulse: dict | None = None, external_only: bool = False) -> dict:
        """深いバッファ（外部メモリ 128 MB）で捕捉して要約を返す。トリガ条件（グリッチフィルタ・パルス幅を含む）は
        la_capture と同じ。rle = True は変化点圧縮（遅い信号なら数秒〜数十秒分）、False は RAW（100 MS/s で最大 0.67 秒）。
        読むのはトリガの前 before_beats 拍・後 after_beats 拍（1 拍 = RAW 8 サンプル / RLE 4 変化点）。
        ターゲット操作と同時に使うときは la_deep_arm → 操作 → la_deep_collect。"""
        armed = self.la_deep_arm(rate_hz, mask, value, rise, fall, rle, after_beats, filter_channels, filter_ns, pulse,
                                 external_only)
        result = self.la_deep_collect(timeout_s, before_beats, vcd_path)
        return {**result, **{k: armed[k] for k in ("filter", "pulse", "after_beats") if k in armed}}

    def la_deep_arm(self, rate_hz: float | None = None, mask: int = 0, value: int = 0, rise: int = 0, fall: int = 0,
                    rle: bool = True, after_beats: int = 4096, filter_channels: int = 0, filter_ns: float = 0.0,
                    pulse: dict | None = None, external_only: bool = False) -> dict:
        """深いバッファの捕捉を仕掛ける（すぐ戻る）。filter_channels・filter_ns・pulse・external_only は la_arm と同じ。"""
        with self._lock:
            self._la_idle()
            self._deep = DeepAnalyzer(self.session.device)
            if self._collector is not None:
                self._collector.pause_events(True)                # 深いバッファの捕捉中はイベント記録を読めない
            try:
                period = self._deep.start(rate_hz=rate_hz, mask=mask, value=value, rise=rise, fall=fall, rle=rle,
                                          post_beats=after_beats, filter_channels=filter_channels, filter_ns=filter_ns,
                                          pulse=pulse, external_only=external_only)
            except BaseException:                                 # 仕掛けられなかったら、止めたイベント記録の読みを必ず戻す（BUG-LA-01）
                self._deep = None
                if self._collector is not None:
                    self._collector.pause_events(False)
                raise
            return {"sample_period_ns": period * 1e9, "capacity_beats": self._deep.beats, "after_beats": after_beats,
                    **self._deep.front_info}

    def la_deep_collect(self, timeout_s: float = 10.0, before_beats: int = 4096, vcd_path: str | None = None) -> dict:
        """深いバッファの完了を待って、トリガ前 before_beats 拍から後ろを読み、要約を返す。"""
        with self._lock:
            if self._deep is None:
                raise RuntimeError("la_deep_arm を先に呼ぶ")
            deep, self._deep = self._deep, None
            try:
                deep.wait(timeout_s)
                tr = deep.read_transitions(before_beats=before_beats)    # 長い記録も展開しない
            finally:
                if self._collector is not None:
                    self._collector.pause_events(False)
            names = channel_names(self.session.device.capabilities())
            self._keep_capture(tr, "la.deep.capture", names)
            if vcd_path:
                write_vcd(tr, Path(vcd_path), names)
            return {"trigger_index": tr.trigger, "trigger_time": tr.trigger_time,
                    "trigger_time_span": self.session.device.capabilities().la_phase_span,
                    "sample_period_ns": tr.sample_period_s * 1e9, "samples": tr.length,
                    "duration_s": tr.length * tr.sample_period_s, "before_beats": before_beats,
                    "channels": _transition_summary(tr), "vcd": vcd_path}

    def la_stream(self, vcd_path: str, duration_s: float = 1.0, rate_hz: float | None = None,
                  rle: bool = True) -> dict:
        """ロジアナを止めずに duration_s 秒流し続け、VCD に書く（トリガなし。記録長は保存先で決まる）。
        RAW は 12.5 MS/s（25 MB/s）程度まで、rle = True（変化点圧縮）は遅い信号ならそれ以上。
        overrun = True はホストが遅れて途中で止まったこと（それまでの分は正しい）。"""
        with self._lock:
            self._la_idle()
            s = DeepAnalyzer(self.session.device).stream_to_vcd(Path(vcd_path), duration_s, rate_hz=rate_hz, rle=rle)
            return {"samples": s.samples, "duration_s": s.duration_s, "sample_period_ns": s.sample_period_s * 1e9,
                    "elapsed_s": s.elapsed_s, "mb_per_s": s.mb_per_s, "overrun": s.overrun, "vcd": vcd_path}
