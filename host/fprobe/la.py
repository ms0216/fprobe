"""ロジックアナライザ: 内蔵バッファの LogicAnalyzer と、公開 API の入口（部品を再公開する）。

設定の部品は fprobe.la_config、深いバッファ（トリガ取り込み・連続ストリーミング）の DeepAnalyzer は fprobe.la_deep。

値の表現と復号は fprobe.la_wave、VCD 出力は fprobe.la_vcd、見出し付きストリームの分割は fprobe.la_segments、
連続ストリームの受け取りは fprobe.la_stream（いずれもここから再公開する。使う側は fprobe.la から読んでよい）。

仕様: docs/superpowers/specs/2026-10-16-m7-logic-analyzer-design.md
      docs/superpowers/specs/2026-10-16-m8c-la-streaming-design.md（連続ストリーミング）
"""

import time

from . import generated as g
from .device import Device, select_stream_path  # noqa: F401  (再公開)
from .events import EventLog, read_counts  # noqa: F401  (再公開)
from .timing import la_channel_skew
from .la_raw import RawCapture, RawCaptureWriter, RawMeta, raw_to_vcd  # noqa: F401  (再公開)
from .la_config import (Front, Pulse, channel_layout, channel_names, check_trigger_channels, external_channels,  # noqa: F401  (再公開)
                        external_pattern, lane_divisor, pin_time, raw_stream_bytes_per_s,
                        _FILTER_MAX_CYCLES, _PACK_CODES, _PULSE_UNBOUNDED, _RATE_DIVIDER_MAX, _pack_code)
from .la_deep import DeepAnalyzer  # noqa: F401  (再公開)
from .la_segments import (SegmentSplitter, StreamFormatError, StreamHoldExceeded, StreamLinkLoss,  # noqa: F401  (再公開)
                          StreamSegment, split_segments)
from .la_stream import (STREAM_BUFFER_BYTES, GapPlacer, LaStream, StreamChunk, StreamCursor, StreamGap,  # noqa: F401  (再公開)
                        StreamStats, _expand_words, _stream_buffer)
from .la_vcd import DEFAULT_NAMES, VcdWriter, vcd_blocks, vcd_blocks_skewed, write_vcd  # noqa: F401  (再公開)
from .la_wave import (BEAT_BYTES, Capture, Transitions, Waveform, decode_beats, decode_transitions,  # noqa: F401  (再公開)
                      stream_transitions, trigger_sample)
from .la_wave import _array_of, _extent, _np, _samples_of  # noqa: F401  (la_wave に移した内部。ここからも読める)

__all__ = ["Capture", "DeepAnalyzer", "Front", "channel_layout", "channel_names", "check_trigger_channels",
           "external_channels", "GapPlacer", "LaStream", "STREAM_BUFFER_BYTES", "LogicAnalyzer", "Pulse", "SegmentSplitter", "StreamChunk",
           "StreamCursor", "StreamFormatError", "StreamGap", "StreamHoldExceeded", "StreamLinkLoss", "StreamSegment", "StreamStats", "Transitions", "VcdWriter", "Waveform", "decode_beats",
           "decode_transitions", "split_segments", "stream_transitions", "vcd_blocks", "vcd_blocks_skewed", "write_vcd"]

class LogicAnalyzer:
    """ロジアナ（DEPTH サンプルの窓、プリトリガ指定）。内蔵バッファが無いボード（ケイパビリティの LA_DEPTH = 0、
    外部メモリあり）では、深いバッファを RAW で使って同じ結果（8192 サンプルの窓）を返す。"""

    WINDOW = 8192                                   # 深いバッファを使うときの窓（サンプル）

    def __init__(self, dev: Device):
        caps = dev.capabilities()
        if not caps.la_channels:
            raise RuntimeError("このデバイスにはロジックアナライザが無い")
        self.dev, self.caps = dev, caps
        self.channels, self.depth, self.clock_hz = caps.la_channels, caps.la_depth, caps.la_sample_hz
        self.front_info: dict = {}                      # 最後の start のグリッチフィルタ・パルス幅（Front.info）
        self._deep = None
        if not self.depth:
            if not caps.mem_bytes:
                raise RuntimeError("ロジアナのバッファが無い")
            self._deep = DeepAnalyzer(dev)
            self.depth = self.WINDOW
        self._pretrigger = 0
        # 取り込みのクロックが sys の整数倍（内蔵バッファの構成だけ）。1 なら sys と同じ
        self.cap_ratio = (caps.la_sample_hz // caps.sys_clock_hz
                          if self.depth and caps.sys_clock_hz and caps.la_sample_hz % caps.sys_clock_hz == 0 else 1)
        # 1 語（sys の 1 周期）のサンプルの数（端子の直並列変換の構成。ケイパビリティ LA_FINE を申告する構成だけ。
        # docs/superpowers/specs/2026-11-01-la-resolution-design.md）。数える単位（pretrigger・位置）が語になり、バッファは la_lanes_mem
        self.lanes = self.cap_ratio if self._deep is None and caps.la_fine is not None else 1

    def testgen(self, *, period: int, high: int, edge: int = 0) -> float:
        """試験用の方形波を、ピン（PYNQ: RPi ヘッダ 32 番）に出す（CSR la_testgen。ケイパビリティ la_testgen のある構成だけ）。
        period = 周期（方形波を刻むクロックの周期の数。2〜4095）、high = 1 の周期の数（1〜period − 1）、
        edge = 0 なら刻むクロックの立ち上がりで、1 なら立ち下がり（半周期遅れる）で変える。出力の周波数（Hz）を返す。
        範囲外の値は、回路が黙って丸めるので、ここで断る。止めるのは testgen_off。"""
        self._require_testgen()
        if not 2 <= period <= 0xFFF:
            raise ValueError(f"period は 2〜4095（方形波を刻むクロックの周期の数。回路は 12 ビット）: {period}")
        if not 1 <= high <= period - 1:
            raise ValueError(f"high は 1〜period − 1（period = {period}）: {high}")
        if edge not in (0, 1):
            raise ValueError(f"edge は 0（立ち上がり）か 1（立ち下がり）: {edge}")
        self.dev.write32(g.LA_TESTGEN_PERIOD, period)
        self.dev.write32(g.LA_TESTGEN_HIGH, high)
        self.dev.write32(g.LA_TESTGEN_CONTROL, 1 | edge << g.LA_TESTGEN_CONTROL_EDGE[0])      # 周期・high を書いてから有効にする
        return self.testgen_clock_hz / period

    def testgen_off(self) -> None:
        """試験用の方形波をやめる（ピンを駆動しない = Hi-Z）。"""
        self._require_testgen()
        self.dev.write32(g.LA_TESTGEN_CONTROL, 0)

    @property
    def testgen_clock_hz(self) -> int:
        """試験用の方形波を刻むクロックの周波数（Hz）。デバイスの申告（ケイパビリティ LA_TESTGEN_HZ）。申告の無い古いデバイスは、
        取り込みが sys の整数倍の構成（cap_ratio > 1）なら取り込みのクロック、そうでなければ sys（その頃のコアの選びと同じ規則）。"""
        declared = self.caps.la_testgen_hz
        if declared:
            return declared
        return self.clock_hz if self.cap_ratio > 1 else self.caps.sys_clock_hz

    def _require_testgen(self) -> None:
        if not self.caps.la_testgen:
            raise RuntimeError("この構成には試験用の方形波が無い（ケイパビリティ la_testgen が無い。"
                               "fprobe_pynq_cap か fprobe_pynq_deep のような構成だけ）")

    def start(self, *, rate_hz: float | None = None, pretrigger: int = 0, mask: int = 0, value: int = 0,
              rise: int = 0, fall: int = 0, filter_channels: int = 0, filter_ns: float = 0.0,
              pulse: "Pulse | dict | None" = None, external_only: bool = False) -> float:
        """捕捉を始める。実際のサンプル周期（秒）を返す。filter_channels（外の信号の ch のビット）は filter_ns より短い変化を
        捨てる（グリッチフィルタ）。pulse はパルス幅のトリガ（Pulse）。どちらもケイパビリティに無ければ RuntimeError。
        external_only: クロストリガ（xtrig.route の to_la・FPGA の条件）でだけ捕捉する（パターンは使わない）。"""
        mask, value, rise, fall = external_pattern(external_only, mask, value, rise, fall, pulse)
        if not 0 <= pretrigger < self.depth:
            raise ValueError(f"pretrigger は 0〜{self.depth - 1}")
        check_trigger_channels(self.caps, mask=mask, rise=rise, fall=fall)
        front = Front.of(self.caps, filter_channels, filter_ns, pulse)
        self.front_info = front.info if front is not None else {}
        self._pretrigger = pretrigger
        if self._deep is not None:
            post_beats = -(-(self.depth - pretrigger) // 8)
            return self._deep.start(rate_hz=rate_hz, mask=mask, value=value, rise=rise, fall=fall, rle=False,
                                    post_beats=post_beats, pretrigger=pretrigger, filter_channels=filter_channels,
                                    filter_ns=filter_ns, pulse=pulse)
        divider = 1 if rate_hz is None else max(1, min(_RATE_DIVIDER_MAX, round(self.clock_hz / rate_hz)))
        if self.lanes > 1 and divider != 1:
            raise ValueError(f"この構成（{self.caps.design}）の内蔵バッファは間引かない（1 周期に {self.lanes} "
                             f"サンプル、{self.clock_hz / 1e6:g} MS/s 固定）。rate_hz は指定しない")
        # 1 周期に複数のサンプルの構成: プリトリガは語（sys の周期）で数える。指定のサンプル数以上を残す語の数
        pre_words = -(-pretrigger // self.lanes)
        if pre_words >= self.depth // self.lanes:
            raise ValueError(f"pretrigger は 0〜{(self.depth // self.lanes - 1) * self.lanes}（{self.lanes} サンプルの語の単位）")
        self._pretrigger = pre_words * self.lanes
        dev = self.dev
        dev.write32(g.LA_CONTROL, 0)
        dev.write(g.LA_RATE, [divider - 1, pre_words, mask, value, rise, fall])
        if front is not None:
            front.write(dev)
        dev.write32(g.LA_CONTROL, 1)
        return divider / self.clock_hz

    def wait(self, timeout_s: float = 5.0) -> None:
        if self._deep is not None:
            self._deep.wait(timeout_s)
            return
        deadline = time.monotonic() + timeout_s
        while not self.dev.read32(g.LA_STATUS) >> g.LA_STATUS_DONE[0] & 1:
            if time.monotonic() > deadline:
                self.dev.write32(g.LA_CONTROL, 0)
                raise TimeoutError("トリガが成立しない")
            time.sleep(0.001)

    def read(self, sample_period_s: float) -> Capture:
        if self._deep is not None:
            cap = self._deep.read(before_beats=-(-self._pretrigger // 8),
                                  after_beats=-(-(self.depth - self._pretrigger) // 8))
            first = max(0, cap.trigger - self._pretrigger)
            samples = cap.samples[first:first + self.depth]
            self.dev.write32(g.LA_DEEP_CONTROL, 0)
            return Capture(samples, cap.trigger - first, cap.trigger_time, cap.sample_period_s, self.channels,
                           channel_skew=cap.channel_skew)
        start, trigger, time_lo, time_hi = self.dev.read(g.LA_START_INDEX, 4)
        info = self.dev.read32(g.LA_CAP_INFO) if self.cap_ratio > 1 else 0
        if self.cap_ratio > 1:
            lanes = info >> g.LA_CAP_INFO_LANES[0] & 0xF or 1       # 0 = この欄の無い古い回路（1 周期 1 サンプル）
            if info >> g.LA_CAP_INFO_RATIO[0] & 0xF != self.cap_ratio or lanes != self.lanes:
                self.dev.write32(g.LA_CONTROL, 0)
                raise RuntimeError(f"取り込みの比が合わない（申告 比 {self.cap_ratio}・レーン {self.lanes}、デバイス 比 "
                                   f"{info >> g.LA_CAP_INFO_RATIO[0] & 0xF}・レーン {lanes}）")
        if self.lanes > 1:
            samples, trigger_pos = self._read_lanes(start, trigger, info >> g.LA_CAP_INFO_TRIGGER_PHASE[0] & 0xF)
        else:
            raw = self.dev.read(g.LA_MEM_BASE, self.depth)
            samples, trigger_pos = raw[start:] + raw[:start], (trigger - start) % self.depth
        self.dev.write32(g.LA_CONTROL, 0)
        raw_time, cap_cycles = time_hi << 32 | time_lo, None
        if self.cap_ratio > 1:
            # ピンが変わった細かいサンプルの番号 = タイムベース × 比 + 位相（レーン）− 固定の遅れ
            # （rtl/la/la_cap_domain.vhd・la_lane_capture.vhd）
            cap_cycles = (raw_time * self.cap_ratio + (info >> g.LA_CAP_INFO_TRIGGER_PHASE[0] & 0xF)
                          - (info >> g.LA_CAP_INFO_LATENCY[0] & 0xFF))
            # n はピンが変わった周期の下限そのもの（la_cap_info の latency に前段の段数が入っている）。sys の経路の
            # 段数 LA_DELAY は引かない（二重になる）
            pin = cap_cycles // self.cap_ratio
        else:
            pin = pin_time(raw_time, self.caps.la_delay_internal, self.caps.la_phase_span)
        fine = self.caps.la_fine
        return Capture(samples, trigger_pos, pin, sample_period_s, self.channels,
                       trigger_cap_cycles=cap_cycles, fine_channels=fine,
                       coarse_period_s=None if fine is None else 1 / self.caps.sys_clock_hz,
                       channel_skew=la_channel_skew(self.caps, deep=False, front_info=self.front_info))

    def _read_lanes(self, start: int, trigger: int, lane: int) -> tuple[list[int], int]:
        """la_lanes_mem から時間順のサンプルと、トリガのサンプルの位置。語 w のレーン 2j・2j + 1 は、ワード j × 語の数 + w の
        下位・上位 16 ビット（fprobe_map の la_lanes_mem）。"""
        words = self.depth // self.lanes
        slices = [self.dev.read(g.LA_LANES_MEM_BASE + 4 * j * words, words) for j in range(self.lanes // 2)]
        samples = []
        for w in range(words):
            word = (start + w) % words
            for part in slices:
                samples += [part[word] & 0xFFFF, part[word] >> 16]
        return samples, (trigger - start) % words * self.lanes + lane

    def capture(self, *, timeout_s: float = 5.0, **trigger) -> Capture:
        period = self.start(**trigger)
        self.wait(timeout_s)
        return self.read(period)
