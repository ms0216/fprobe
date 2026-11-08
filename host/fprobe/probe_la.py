"""ロジックアナライザ（la_*）のサービス: 内蔵の捕捉（la_arm / la_collect / la_capture）・深いバッファ（la_deep_*）・流し込み（la_stream）・
捕捉の保持と波形（la_view）・トリガの状態。

Probe（fprobe.probe）が API の名前を委ねるサービス（fprobe.probe_service。ホストから使う名前は NEEDS）。Probe の _lock・session・_caps・_la・_la_period・_capture* と、
WatchService の _la_idle / _la_record 等を使う。部品は fprobe.la（LogicAnalyzer・DeepAnalyzer・波形）。
"""

import threading
from pathlib import Path

from . import generated as g
from .api import NoData
from .collector import add_capture
from .la import DeepAnalyzer, LogicAnalyzer, Transitions, Waveform, channel_names, external_pattern, write_vcd
from .la_raw import RawCapture, raw_to_vcd
from .timing import la_channel_timing, la_trigger_facts, la_xtrig_source, skew_of
from .probe_service import HostView


def _skew_ns(skews, ch: int, timebase_hz: float | None) -> float:
    """ch の時刻のずれ（ns。fprobe.timing。分からない ch は 0 = ずらさない。結果の skew_unknown と channel_timing に分からないと出す）。"""
    return skew_of(skews, ch) * 1e9 / timebase_hz if timebase_hz else 0.0


def _transition_summary(tr, timebase_hz: float | None = None) -> dict:
    period_ns = tr.sample_period_s * 1e9
    out = {}
    for ch in range(tr.channels):
        edges = tr.edges(ch)
        initial = tr.values[0] >> ch & 1 if len(tr.values) else 0
        if not edges and not initial:
            continue
        skew = _skew_ns(getattr(tr, "channel_skew", ()), ch, timebase_hz)
        out[ch] = {"initial": initial, "edges": len(edges),
                   "first_edges_ns": [round((i - tr.trigger) * period_ns + skew) for i in edges[:16]]}
    return out


def _skew_unknown(tr, timebase_hz: float | None) -> list[int]:
    """first_edges_ns に ch のずれを足さなかった ch（ずれが分からない。足すのは timebase_hz があるときだけ）。"""
    return tr.skew_unknown() if timebase_hz else []


FIRST_EDGES = 16                       # la_collect の要約で時刻を並べる先頭の変化の数（切ったかは結果に出す）


def _channel_summary(cap, timebase_hz: float | None = None) -> dict:
    """{channels: {ch: {initial, edges, first_edges_ns（トリガのピンの時刻 = 0 ns からの先頭 FIRST_EDGES 個。ch ごとの時刻の
    ずれ channel_timing を足したもの）, first_edges_truncated}}, first_edges_limit, constant_low（常に 0 で省いたチャンネル）,
    skew_unknown（ずれが分からず足さなかった ch）}。"""
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
            "first_edges_ns": [round((i - cap.trigger) * period_ns + _skew_ns(getattr(cap, "channel_skew", ()), ch, timebase_hz))
                               for i in edges[:FIRST_EDGES]],
            "first_edges_truncated": len(edges) > FIRST_EDGES,
        }
    return {"channels": out, "first_edges_limit": FIRST_EDGES, "constant_low": low,
            "skew_unknown": _skew_unknown(cap, timebase_hz)}


class LogicAnalyzerService:
    """Probe が委ねるロジックアナライザの道具。"""

    API = ("la_arm", "la_capture", "la_collect", "la_deep_arm", "la_deep_capture", "la_deep_collect", "la_raw_to_vcd",
           "la_stream", "la_trigger_status", "la_view")
    # ほかの領域（Probe の委譲）・試験に出す名前
    SHARED = ("_capture", "_capture_id", "_capture_info", "_capture_lock", "_capture_names", "_capture_wave", "_deep",
              "_keep_capture", "_la", "_la_period", "_wave")
    # ホスト（Probe の核と、ほかの領域）から使う名前
    NEEDS = ("_collector", "_la_idle", "_lock", "_recorder", "_require_halt_budget", "session")

    def __init__(self, host):
        self._host = HostView(host, self.NEEDS, type(self).__name__)
        self._capture_id = 0                                    # 最後の捕捉（画面の波形 la_view 用）
        self._capture: Transitions | None = None
        self._capture_lock = threading.Lock()             # 捕捉・情報・変化点の表をそろえて読み書きする（読みのスレッド）
        self._capture_info: dict | None = None
        self._capture_names: dict[int, str] = {}                # 最後の捕捉の ch の既定の名前（書き出し用）
        self._wave: Waveform | None = None                     # 変化点の表は画面が最初に求めたときに作る
        self._la: LogicAnalyzer | None = None
        self._la_period = 0.0
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
        with self._host._lock:
            self._host._la_idle()
            self._la = LogicAnalyzer(self._host.session.device)
            self._la_period = self._la.start(rate_hz=rate_hz, pretrigger=pretrigger, mask=mask, value=value,
                                             rise=rise, fall=fall, filter_channels=filter_channels,
                                             filter_ns=filter_ns, pulse=pulse, external_only=external_only)
            return {"sample_period_ns": self._la_period * 1e9, "depth": self._la.depth, "pretrigger": pretrigger,
                    **self._la.front_info}

    def la_collect(self, timeout_s: float = 5.0, vcd_path: str | None = None) -> dict:
        """完了を待って読み出し、チャンネルごとの要約（トリガ = 0 ns）を返す。vcd_path があれば VCD も書く。"""
        self._host._require_halt_budget("la.collect", timeout_s)
        with self._host._lock:
            if self._la is None:
                raise RuntimeError("la_arm を先に呼ぶ")
            la, self._la = self._la, None
            la.wait(timeout_s)
            cap = la.read(self._la_period)
            caps = self._host.session.device.capabilities()
            names = channel_names(caps)
            self._keep_capture(Transitions.from_capture(cap), "la.capture", names)
            if vcd_path:
                write_vcd(cap, Path(vcd_path), names, caps.timebase_hz)
            coarse = {ch: cap.channel_period_s(ch) * 1e9 for ch in range(cap.channels)
                      if cap.channel_period_s(ch) != cap.sample_period_s}
            return {"trigger_index": cap.trigger, "trigger_time": cap.trigger_time,
                    "trigger_time_span": caps.la_phase_span,
                    "trigger_fine_sample": cap.trigger_cap_cycles,
                    # このトリガをクロストリガの源（bit0）にしたときの源のパルスの周期（TLV TIMING。分からなければ None）
                    "xtrig_source_time": la_xtrig_source(caps, la._deep is not None, cap.trigger_time, cap.trigger_cap_cycles),
                    "sample_period_ns": cap.sample_period_s * 1e9, "samples": len(cap.samples),
                    "channel_period_ns": coarse,
                    **_channel_summary(cap, caps.timebase_hz),
                    "channel_timing": la_channel_timing(caps, deep=la._deep is not None, front_info=la.front_info),
                    "vcd": vcd_path}

    def _keep_capture(self, tr: Transitions, source: str, names: dict[int, str] | None = None) -> None:
        """最後の捕捉を画面の波形（la_view）のために持つ。変化点の表はここでは作らない（画面を使わない操作を遅くしない）。
        names は ch の既定の名前（捕捉した機器の申告 LA_LAYOUT か、読み込んだファイルの名前。書き出しに使う）。"""
        if self._host._recorder is not None:
            add_capture(self._host._recorder, tr)                       # 記録中なら共通の時間軸の記録にも入れる
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
            # 長い捕捉では時間がかかるので、ロックの外で作る。ch ごとの時刻のずれ（channel_skew）で ch を置き直す
            wave = (Waveform(tr, tr.sample_period_s * self._host.session.device.capabilities().timebase_hz)
                    if tr.channel_skew else Waveform(tr))
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
        外部メモリがあれば）。trigger_time はトリガした後だけ（それまで None）。トリガの回数を数えるレジスタは無い。
        trigger_time はトリガのサンプルのピンの時間軸の時刻（la_collect の trigger_time と同じ意味。変化は trigger_time 以上
        trigger_time_hi 未満）、trigger_time_raw は CSR の生の値（回路がトリガを受けた周期。段数を引いていない）、
        trigger_fine_sample は取り込みの領域・1 周期に複数のサンプルの構成の細かいサンプルの番号、xtrig_source_time はこのトリガを
        クロストリガの源にしたときの源のパルスの周期（fprobe.timing.la_trigger_facts。申告が無ければ None で trigger_time_unknown に
        名前）。rate_hz は取り込みの速さ（1 周期に複数のサンプルの内蔵バッファは rate を使わない: rate_used = false、rate_raw は
        レジスタの値）。filter・pulse の ns は取り込みのクロック（la_sample_hz）で数えた値。"""
        with self._host._lock:
            dev = self._host.session.device
            caps = dev.capabilities()
            if not caps.la_channels:
                raise RuntimeError("このデバイスにはロジックアナライザが無い")
            rate, pretrigger, mask, value, rise, fall = dev.read(g.LA_RATE, 6)
            rate &= 0xFFFF
            sys_hz = caps.sys_clock_hz
            lanes = (caps.la_sample_hz // sys_hz if caps.la_fine is not None and sys_hz
                     and caps.la_sample_hz % sys_hz == 0 else 1)
            deep_control = dev.read32(g.LA_DEEP_CONTROL) if caps.mem_bytes else 0
            if lanes > 1 and caps.la_depth:
                # 1 周期に複数のサンプルの内蔵バッファ（la_lane_front）は rate を使わない（la_sample_hz 固定）
                rate_hz, rate_used = caps.la_sample_hz, False
            elif lanes > 1:
                # 深いバッファの N 並列（la_lane_encoder）: 間隔 = 2^step × (rate + 1) 細かいサンプル（la.lane_divisor の逆）
                step = deep_control >> g.LA_DEEP_CONTROL_STEP[0] & (1 << g.LA_DEEP_CONTROL_STEP[1]) - 1
                rate_hz, rate_used = caps.la_sample_hz / ((1 << step) * (rate + 1)), True
            else:
                rate_hz, rate_used = caps.la_sample_hz / (rate + 1), True
            out: dict = {"mask": mask, "value": value, "rise": rise, "fall": fall,
                         "external_only": (mask, value, rise, fall) == external_pattern(True, 0, 0, 0, 0, None)
                         and not (caps.la_pulse and dev.read32(g.LA_PULSE) & 3),
                         "rate_hz": rate_hz, "rate_raw": rate, "rate_used": rate_used, "pretrigger": pretrigger}
            # フィルタ・パルス幅の周期は取り込みのクロック（la.Front と同じ: 取り込みの領域の構成は cap_clk。時刻の正しさ S1b の A8。
            # それまでは sys で ns に直していて、取り込みの領域の構成で 3 倍長く出ていた）
            clock = caps.la_sample_hz
            if caps.la_filter:
                word = dev.read32(g.LA_FILTER)
                cycles = word >> g.LA_FILTER_CYCLES[0] & (1 << g.LA_FILTER_CYCLES[1]) - 1
                out["filter"] = {"channels": word >> g.LA_FILTER_CHANNELS[0] & (1 << g.LA_FILTER_CHANNELS[1]) - 1,
                                 "cycles": cycles, "min_width_ns": cycles / clock * 1e9 if cycles > 1 else None}
            if caps.la_pulse:
                word, low, high = dev.read(g.LA_PULSE, 3)
                mode = ("none", "width", "timeout", "none")[word >> g.LA_PULSE_MODE[0] & 3]
                out["pulse"] = {"mode": mode} if mode == "none" else {
                    "mode": mode, "level": word >> g.LA_PULSE_LEVEL[0] & 1,
                    "channel": word >> g.LA_PULSE_CHANNEL[0] & (1 << g.LA_PULSE_CHANNEL[1]) - 1,
                    "min_ns": low / clock * 1e9, "max_ns": None if high == 0xFFFF_FFFF else high / clock * 1e9}

            def state(status_addr, fields, time_addr, buffer: str) -> dict:
                status = dev.read32(status_addr)
                lo, hi = dev.read(time_addr, 2)
                bits = {k: bool(status >> f[0] & 1) for k, f in fields.items()}
                if not bits["triggered"]:
                    return {**bits, "trigger_time": None, "trigger_time_raw": None, "trigger_time_hi": None,
                            "trigger_fine_sample": None, "xtrig_source_time": None, "trigger_time_from": None,
                            "trigger_time_unknown": []}
                raw = hi << 32 | lo
                kw: dict = {"rate": rate}
                if buffer == "internal" and caps.la_sample_hz != sys_hz and caps.la_sample_hz % sys_hz == 0:
                    info = dev.read32(g.LA_CAP_INFO)
                    kw.update(phase=info >> g.LA_CAP_INFO_TRIGGER_PHASE[0] & 0xF, latency=info >> g.LA_CAP_INFO_LATENCY[0] & 0xFF)
                if buffer == "deep":
                    kw["pack"] = deep_control >> g.LA_DEEP_CONTROL_PACK[0] & (1 << g.LA_DEEP_CONTROL_PACK[1]) - 1
                f = la_trigger_facts(caps, buffer, raw, **kw)
                # trigger_time = ピンの時間軸の時刻（la_collect の trigger_time と同じ意味）、trigger_time_raw = CSR の生の値（回路が
                # トリガを受けた周期。段数を引いていない。時刻の正しさ S1b の A8）
                return {**bits, "trigger_time": f["pin_time"], "trigger_time_raw": raw, "trigger_time_hi": f["pin_time_hi"],
                        "trigger_fine_sample": f["fine_sample"], "xtrig_source_time": f["xtrig_source_time"],
                        "trigger_time_from": f["from"], "trigger_time_unknown": f["unknown"]}
            if caps.la_depth:
                out["internal"] = state(g.LA_STATUS, {"capturing": g.LA_STATUS_CAPTURING, "triggered": g.LA_STATUS_TRIGGERED,
                                                      "done": g.LA_STATUS_DONE}, g.LA_TRIGGER_TIME_LO, "internal")
            if caps.mem_bytes:
                out["deep"] = state(g.LA_DEEP_STATUS, {"capturing": g.LA_DEEP_STATUS_CAPTURING,
                                                       "triggered": g.LA_DEEP_STATUS_TRIGGERED,
                                                       "done": g.LA_DEEP_STATUS_DONE}, g.LA_DEEP_TRIGGER_TIME_LO, "deep")
            return out

    def la_capture(self, timeout_s: float = 5.0, vcd_path: str | None = None, **trigger) -> dict:
        self._host._require_halt_budget("la.capture", timeout_s)
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
        self._host._require_halt_budget("la.deep.capture", timeout_s)
        armed = self.la_deep_arm(rate_hz, mask, value, rise, fall, rle, after_beats, filter_channels, filter_ns, pulse,
                                 external_only)
        result = self.la_deep_collect(timeout_s, before_beats, vcd_path)
        return {**result, **{k: armed[k] for k in ("filter", "pulse", "after_beats") if k in armed}}

    def la_deep_arm(self, rate_hz: float | None = None, mask: int = 0, value: int = 0, rise: int = 0, fall: int = 0,
                    rle: bool = True, after_beats: int = 4096, filter_channels: int = 0, filter_ns: float = 0.0,
                    pulse: dict | None = None, external_only: bool = False) -> dict:
        """深いバッファの捕捉を仕掛ける（すぐ戻る）。filter_channels・filter_ns・pulse・external_only は la_arm と同じ。"""
        with self._host._lock:
            self._host._la_idle()
            self._deep = DeepAnalyzer(self._host.session.device)
            if self._host._collector is not None:
                self._host._collector.pause_events(True)                # 深いバッファの捕捉中はイベント記録を読めない
            try:
                period = self._deep.start(rate_hz=rate_hz, mask=mask, value=value, rise=rise, fall=fall, rle=rle,
                                          post_beats=after_beats, filter_channels=filter_channels, filter_ns=filter_ns,
                                          pulse=pulse, external_only=external_only)
            except BaseException:                                 # 仕掛けられなかったら、止めたイベント記録の読みを必ず戻す（BUG-LA-01）
                self._deep = None
                if self._host._collector is not None:
                    self._host._collector.pause_events(False)
                raise
            return {"sample_period_ns": period * 1e9, "capacity_beats": self._deep.beats, "after_beats": after_beats,
                    **self._deep.front_info}

    def la_deep_collect(self, timeout_s: float = 10.0, before_beats: int = 4096, vcd_path: str | None = None) -> dict:
        """深いバッファの完了を待って、トリガ前 before_beats 拍から後ろを読み、要約を返す。"""
        self._host._require_halt_budget("la.deep.collect", timeout_s)
        with self._host._lock:
            if self._deep is None:
                raise RuntimeError("la_deep_arm を先に呼ぶ")
            deep, self._deep = self._deep, None
            try:
                deep.wait(timeout_s)
                tr = deep.read_transitions(before_beats=before_beats)    # 長い記録も展開しない
            finally:
                if self._host._collector is not None:
                    self._host._collector.pause_events(False)
            caps = self._host.session.device.capabilities()
            names = channel_names(caps)
            self._keep_capture(tr, "la.deep.capture", names)
            if vcd_path:
                write_vcd(tr, Path(vcd_path), names, caps.timebase_hz)
            return {"trigger_index": tr.trigger, "trigger_time": tr.trigger_time,
                    "trigger_time_span": caps.la_phase_span,
                    "xtrig_source_time": la_xtrig_source(caps, True, tr.trigger_time),
                    "sample_period_ns": tr.sample_period_s * 1e9, "samples": tr.length,
                    "duration_s": tr.length * tr.sample_period_s, "before_beats": before_beats,
                    "channels": _transition_summary(tr, caps.timebase_hz),
                    "skew_unknown": _skew_unknown(tr, caps.timebase_hz),
                    "channel_timing": la_channel_timing(caps, deep=True, front_info=deep.front_info),
                    "vcd": vcd_path}

    def la_stream(self, vcd_path: str, duration_s: float = 1.0, rate_hz: float | None = None,
                  rle: bool = True, pack_channels: int | None = None, max_bytes_per_s: float | None = None,
                  usage_interval_s: float | None = None, first_channel: int = 0, gaps: bool = False,
                  gap_hold_bytes: int | None = None, control_read_retries: int = 0, raw_path: str | None = None,
                  rle_format: str | None = None, tick_us: float | None = None,
                  rle_repeat: bool | None = None, max_changes: int | None = None) -> dict:
        """ロジアナを止めずに duration_s 秒流し続け、VCD に書く（トリガなし。記録長は保存先で決まる）。
        RAW の速さは、ボードの受け取りの速さまで（PYNQ の PS 直読み: 50 MB/s = 16 ch で 25 MS/s。USB・シリアルの Primer: 25 MB/s 程度
        = 12.5 MS/s）。pack_channels（8・4・2。rle = False のときだけ。ケイパビリティ la_pack）は、使うチャンネル（ch 0〜n − 1）だけを
        詰めて流し、データ量が 1/2・1/4・1/8 になる（8 ch で 50 MS/s、4 ch で 100 MS/s が 50 MB/s）。選んだチャンネルは欠けず、
        選ばなかった上位のチャンネルは VCD に出ない。rle = True（変化点圧縮）は遅い信号ならそれ以上。
        overrun = True はホストが遅れて途中で止まったこと（それまでの分は正しい）。
        1 周期に複数のサンプルの構成（fprobe_pynq_deep_fast、800 MS/s）: rate_hz は 800 MHz の 1・2・4 分の 1 か 8 の倍数分の 1、
        pack_channels は 1 も使え、first_channel で詰める最初の ch を選べる（例: 10 = PMOD JB の 1 番。VCD の信号 i = ch first_channel + i）。RAW は ch 数 / 8 × レートの量が max_bytes_per_s（None = デバイスが申告する持続できると測った速さ
        la_stream_bytes_per_s）を超える組を始める前に断る（RLE は断らない）。usage は、usage_interval_s 秒ごとと終わりの、
        リング（DDR）の未読・最大と、符号・受け取りのバイト数・毎秒の量（事実。止めるかの判断はしない）。
        link_buffer: PC が受け取ってまだ読んでいない量（TCP の道。capacity_bytes = 溜める上限、peak_bytes = この流れの最大、
        full_count・full_s = 上限で受け取りを止めた回数と時間。止めた分は中継の送りが止まり、溢れれば overrun になる）。USB では None。
        gaps = True（ケイパビリティ la_stream_gaps。PYNQ の深いバッファ）: 溢れても止めず、回路が捨てた拍の区間（欠け）を記録して続ける
        （docs/superpowers/specs/2026-11-01-la-deep-fast-design.md §12）。gaps = 欠けの区間 [{start, samples, beat}]（流れのサンプルの位置
        start から samples サンプルは値が無い。埋めない。VCD では全信号が x）、gap_samples = その和、samples は値のあるサンプルの数、
        duration_s は欠けを含む流れの長さ、unplaced_beats = 欠けの記録が届かず位置が決まらないので書かなかった拍（ふつう 0）。
        False（既定）は今までどおり溢れたら止まる（gaps は空）。gap_hold_bytes（gaps = True のとき必ず指定。既定は無い）= 欠けの記録が
        届くまで位置の決まらない拍を PC に持つ上限（バイト）。超えたら推定で置かずに止める（StreamHoldExceeded）。gap_held_peak_bytes =
        持った量の最大（事実）。link_buffer の dropped_frames・lost_frames = この流れの間にリンクが捨てた・落とした数（UDP・UART の道。
        欠けたら流れは StreamLinkLoss で止まる）。
        control_read_retries（既定 0）: 流している間の見回りの CSR の READ（読みに副作用が無い）の応答が時間切れになったとき、
        送り直してよい回数（使う側が明示する。0 なら送り直さず TimeoutError で止まる）。control = この流れの間の
        {read_retries: 送り直した READ, stale_responses: 読み捨てた遅れた応答}（事実。
        docs/superpowers/specs/2026-11-03-host-link-correctness-design.md §4）。
        raw_path: 受け取った拍をそのまま保存する生の保存ファイル（.fplaraw。fprobe.la_raw）の置き場所。指定するとそこに残り、結果の raw に
        返る（あとから la_raw_to_vcd で VCD にできる）。None = VCD の隣の一時ファイルに取り込み、VCD にした後に消す（raw は None）。
        受け取りは VCD の書き出しの速さに律速されない（取り込みと書き出しを分けた。
        docs/superpowers/specs/2026-11-03-la-raw-capture-design.md）。途中で誤りが出ても、取り込めた分は VCD になってから誤りが出る。
        relay: 出し切った後の PYNQ の中継の状態（zc_state = ゼロコピーの道の状態、tcp_path = 使った送りの道、copied 等の数。
        docs/superpowers/specs/2026-11-01-bandwidth-observation-design.md 1.7.9）。中継が無い・返事が無ければ None。
        rle_format（rle = True のときの RLE の形式）: "records"（1 レコード 4 バイト。None と同じ = 今までどおり）か "tokens"（トークン形式:
        変化 1 回がふつう 1 バイト。ケイパビリティ la_rle_tokens のある構成だけ。詰める（pack_channels）とは
        一緒に使えない。gaps とは使える（欠けの後は回路が値を再提示する）。docs/superpowers/specs/2026-11-07-la-rle2-design.md）。rle = False で指定すると断る。結果の rle_format = 実際に使った形式
        （"raw" | "records" | "tokens"）、wire_bytes = 受け取ったロジアナの拍のバイト数、changes = VCD に書いた変化点の数、
        bytes_per_change = wire_bytes / changes（割っただけの事実）、pad_bytes = そのうち NOP で埋めたバイト数（トークン形式。ほかは 0）。
        max_changes（既定 null = 上限なし。使う側が明示する。例: 100000000）= VCD に書く変化点の数の上限。VCD は変化点ごとに行が要るので、
        rle_repeat の周期的な信号は、受け取り（wire_bytes）が小さくても書き出すと保存先が変化点の数に比例して膨れる（100 MS/s の方形波は 10 秒で
        約 240 KB の受け取りが 10 億の変化点 = 数十 GB の VCD）。メモリは膨れない（変化点は断片ごとに復号して書く）。上限を超えるときは、
        上限ちょうどの変化点までを書いて VCD を閉じ、理由（上限・書いた数・超えた最初の変化点のサンプル）つきの誤りで断る。raw_path を
        指定していれば生の保存ファイルは残るので、la.raw_to_vcd で上限を変えて書き直せる。null なら従来どおり全部を書く。
        トークン形式の samples は最後の出来事の時刻 + 1（それ以降は次の出来事が届くまで分からない）。"""
        self._host._require_halt_budget("la.stream", duration_s)
        with self._host._lock:
            self._host._la_idle()
            s = DeepAnalyzer(self._host.session.device).stream_to_vcd(Path(vcd_path), duration_s, rate_hz=rate_hz, rle=rle,
                                                                pack_channels=pack_channels, max_bytes_per_s=max_bytes_per_s,
                                                                usage_interval_s=usage_interval_s, first_channel=first_channel,
                                                                gaps=gaps, gap_hold_bytes=gap_hold_bytes,
                                                                control_read_retries=control_read_retries, raw_path=raw_path,
                                                                rle_format=rle_format, tick_us=tick_us, rle_repeat=rle_repeat,
                                                                max_changes=max_changes)
            return {"samples": s.samples, "duration_s": s.duration_s, "sample_period_ns": s.sample_period_s * 1e9,
                    "elapsed_s": s.elapsed_s, "mb_per_s": s.mb_per_s, "overrun": s.overrun,
                    "link_buffer": s.link_buffer_dict, "vcd": vcd_path, "raw": raw_path,
                    "usage": list(s.usage), "first_channel": first_channel,
                    "gaps": [gap.as_dict() for gap in s.gaps], "gap_samples": s.gap_samples,
                    "unplaced_beats": s.unplaced_beats, "gap_held_peak_bytes": s.gap_held_peak_bytes,
                    "control": s.control, "rle_format": s.rle_format, "wire_bytes": s.wire_bytes, "changes": s.changes,
                    "bytes_per_change": s.bytes_per_change, "pad_bytes": s.pad_bytes,
                    "relay": s.relay}       # PYNQ の中継の状態（ゼロコピーの道か・copied 等。eth_link.decode_relay_status）。中継が無ければ None

    def la_raw_to_vcd(self, raw_path: str, vcd_path: str, names: dict[str, str] | None = None,
                      max_changes: int | None = None) -> dict:
        """ロジアナの生の保存ファイル（la_stream の raw_path で残したもの）を VCD に書き出す。機器には触れない（取り込みとは別に、
        あとから行う）。VCD のバイト列は、la_stream が直接書いたものと同じ。names（{"5": "rx"}）は保存した名前に重ねる。
        complete = False は、取り込みの途中で止まった（索引の無い）ファイルで、読めた所までを書いたこと（最後の切れたかたまりは捨てる）。
        max_changes（既定 null = 上限なし。使う側が明示する。例: 100000000）= VCD に書く変化点の数の上限（la_stream と同じ）。トークン形式の REPEAT は
        小さなファイルが何億もの変化点になり、VCD は変化点ごとに行が要るので保存先が膨れる。メモリは膨れない。超えるときは上限ちょうどまでを書いて
        VCD を閉じ、理由つきの誤りで断る。"""
        raw = RawCapture(raw_path)
        raw_to_vcd(raw, Path(vcd_path), None if names is None else {int(ch): name for ch, name in names.items()},
                   max_changes=max_changes)
        m = raw.meta
        return {"raw": raw_path, "vcd": vcd_path, "complete": raw.complete, "chunks": len(raw), "samples": raw.samples,
                "sample_period_ns": m.sample_period_s * 1e9, "channels": m.channels, "first_channel": m.first_channel,
                "rle": m.rle, "pack": m.pack, "rle_format": m.rle_format if m.rle else "raw"}
