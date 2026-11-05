"""深いバッファ（外部メモリ）のロジアナ DeepAnalyzer: トリガ取り込み・連続ストリーム・RAW/VCD への保存。

設定の部品は fprobe.la_config、波形は fprobe.la_wave、ストリームの受け取りは fprobe.la_stream。fprobe.la が再公開する。
"""

import contextlib
import dataclasses
import time
from pathlib import Path

from . import generated as g
from .device import Device, select_stream_path
from .events import EventLog, read_counts
from .timing import la_channel_skew
from .la_config import (_RATE_DIVIDER_MAX, Front, Pulse, _pack_code, channel_names, check_trigger_channels, external_pattern,
                        lane_divisor, pin_time, raw_stream_bytes_per_s)
from .la_raw import RawCapture, RawCaptureWriter, RawMeta, raw_to_vcd
from .la_stream import LaStream, StreamStats, _stream_buffer
from .la_wave import BEAT_BYTES, Capture, Transitions, decode_beats, decode_transitions, trigger_sample


class DeepAnalyzer:
    """深いバッファ（外部メモリ）のロジアナ。前段の設定（rate・トリガ）は LogicAnalyzer と同じ。

    読み出しは USB の拡張ストリーム。RAW は 1 拍 = 8 サンプル、RLE（変化点）は 1 拍 = 4 レコード。

    1 周期に複数のサンプルの構成（lanes > 1。fprobe_pynq_deep_fast。docs/superpowers/specs/2026-11-01-la-deep-fast-design.md）:
    la_sample_hz（800 MS/s）の細かさのまま垂れ流す（stream だけ。トリガ取り込みは無い）。RAW のサンプルの間隔は細かいサンプルの
    1・2・4 個か 8 の倍数（回路の選び方）、pack_channels は 1 も使える。
    """

    def __init__(self, dev: Device):
        caps = dev.capabilities()
        if not caps.mem_bytes or not caps.la_channels:
            raise RuntimeError("このデバイスには深いバッファが無い")
        self.dev, self.caps = dev, caps
        self.front_info: dict = {}
        self.beats = dev.read32(g.LA_DEEP_RING_BEATS)               # ロジアナのリング（残りはイベント用）
        self.channels, self.clock_hz = caps.la_channels, caps.la_sample_hz
        # 符号器の 1 語のサンプルの数（細かいサンプル / sys の周期）: LA_FINE を申告する 1 周期に複数のサンプルの構成だけ > 1
        sys_hz = caps.sys_clock_hz or 0
        self.lanes = (caps.la_sample_hz // sys_hz if caps.la_fine is not None and sys_hz
                      and caps.la_sample_hz % sys_hz == 0 else 1)
        self._rle = False
        self._period = 0.0
        self._step = 0                                               # 1 周期に複数のサンプル: 細かいサンプルの 2^step 個ごと
        self._divisor = 1                                            # サンプルの間隔（細かいサンプルの数）

    def start(self, *, rate_hz: float | None = None, mask: int = 0, value: int = 0, rise: int = 0, fall: int = 0,
              rle: bool = False, post_beats: int | None = None, pretrigger: int = 0, filter_channels: int = 0,
              filter_ns: float = 0.0, pulse: "Pulse | dict | None" = None, external_only: bool = False) -> float:
        """捕捉を始める（トリガ後 post_beats 拍。既定は容量の 1/2。開始から pretrigger サンプルまではトリガを受け付けない）。
        filter_channels・filter_ns・pulse・external_only は LogicAnalyzer.start と同じ。実際のサンプル周期（秒）を返す。"""
        if self.lanes > 1:
            raise RuntimeError(f"この構成（{self.caps.design}）の深いバッファは垂れ流しだけ（1 周期に {self.lanes} "
                               "サンプルの符号器にトリガ取り込みは無い。stream を使う）")
        mask, value, rise, fall = external_pattern(external_only, mask, value, rise, fall, pulse)
        post = self.beats // 2 if post_beats is None else post_beats
        if not 0 <= post < self.beats:
            raise ValueError(f"post_beats は 0〜{self.beats - 1}")
        self._configure(rate_hz, mask, value, rise, fall, pretrigger, filter_channels, filter_ns, pulse)
        self.dev.write32(g.LA_DEEP_POST_BEATS, post)
        self.dev.write32(g.LA_DEEP_CONTROL, 1 | int(rle) << g.LA_DEEP_CONTROL_RLE[0])
        self._rle = rle
        return self._period

    def _configure(self, rate_hz: float | None, mask: int = 0, value: int = 0, rise: int = 0, fall: int = 0,
                   pretrigger: int = 0, filter_channels: int = 0, filter_ns: float = 0.0,
                   pulse: "Pulse | dict | None" = None) -> None:
        """止めて前段（la ブロック）を設定する。pretrigger: 開始からこのサンプル数まではトリガを受け付けない。
        グリッチフィルタ・パルス幅は指定が無ければ切る（前の捕捉の設定を持ち越さない）。"""
        check_trigger_channels(self.caps, mask=mask, rise=rise, fall=fall)
        if not self.dev.read32(g.MEM_STATUS) >> g.MEM_STATUS_READY[0] & 1:
            # 準備のできていない外部メモリで始めると、書き手が止まって捕捉が終わらない（理由が PC に見えない）
            raise RuntimeError("外部メモリの準備ができていない（PYNQ-Z2 では PS の中継が DDR の領域 MEM_BASE を書くまで"
                               "使えない: /etc/fprobe/mem と起動の mem= の予約を確かめる）")
        front = Front.of(self.caps, filter_channels, filter_ns, pulse)
        if self.lanes > 1:
            self._divisor, self._step, rate_field = lane_divisor(self.clock_hz, self.lanes, rate_hz)
        else:
            divider = 1 if rate_hz is None else max(1, min(_RATE_DIVIDER_MAX, round(self.clock_hz / rate_hz)))
            self._divisor, self._step, rate_field = divider, 0, divider - 1
        dev = self.dev
        dev.write32(g.LA_DEEP_CONTROL, 0)
        dev.write32(g.LA_CONTROL, 0)
        dev.write(g.LA_RATE, [rate_field, pretrigger, mask, value, rise, fall])
        if front is not None:
            front.write(dev)
        self.front_info = front.info if front is not None else {}
        self._period = self._divisor / self.clock_hz

    def stream(self, *, rate_hz: float | None = None, rle: bool = False, filter_channels: int = 0,
               filter_ns: float = 0.0, events_from: int | None = None, ps_read: bool | None = None,
               pack_channels: int | None = None, max_bytes_per_s: float | None = None,
               first_channel: int = 0, gaps: bool = False, gap_hold_bytes: int | None = None,
               control_read_retries: int = 0) -> "LaStream":
        """連続ストリーミングを始める（トリガは使わない）。受け取りは返した LaStream の chunks で行う。
        イベントも流す構成（ケイパビリティ la_stream_events。R1）では、イベント記録の events_from 件目（events.written で
        数える。既定: 今の総数 = これから後の出来事）からのイベントも同じ流れで届く（StreamChunk.events）。

        ps_read: PS が DDR のリングを直接読んで流す形（ケイパビリティ la_stream_ps。PYNQ の深いバッファ。窓の語ごとの
        読みを通らないので速い。docs/superpowers/specs/2026-10-30-la-stream-ps-direct-design.md）。None = ケイパビリティが
        あれば使う。True でケイパビリティが無ければ RuntimeError。流れの形（見出し付きのかたまり）は同じ。

        pack_channels: 使うチャンネルだけ詰めて流す（ケイパビリティ la_pack。CSR la_deep.control.pack）。8 = ch 0〜7、4 = ch 0〜3、
        2 = ch 0〜1 だけを流し、データ量が 1/2・1/4・1/8 になる（そのぶん、同じ速さの受け取りで速いサンプルレートを流せる）。選んだ
        チャンネルは 1 サンプルも欠けない。選ばなかったチャンネル（上位）は記録されない。None = 詰めない（16 ch）。rle とは同時に使えない。
        1 周期に複数のサンプルの構成（lanes > 1）では 1 = ch 0 だけ も使え、first_channel で詰める最初の ch を選べる
        （ch first_channel〜first_channel + pack_channels − 1。例: PMOD JB の 1 番 = ch 10 を 1 ch で。0 = ch 0 から）。

        max_bytes_per_s: RAW の流れの量の上限（B/s）。RAW の量は始める前に決まる（ch 数 / 8 × サンプルレート）ので、上限を超える組は
        始める前に ValueError で断る（黙って捨てない）。None = デバイスが申告する持続できると測った速さ（ケイパビリティ
        la_stream_bytes_per_s。申告が無ければ断らない）。RLE は断らない（量は信号の変化の頻度で決まり、始める前には分からない。
        溢れたら overrun として事実を返す）。流している間のリングと帯域の使用量は LaStream.usage()。

        gaps: 溢れても止めず、回路が捨てた拍の区間（欠け）を記録して続ける（ケイパビリティ la_stream_gaps。CSR la_deep.control.gaps。
        docs/superpowers/specs/2026-11-01-la-deep-fast-design.md §12）。False（既定）= 今までの形: 溢れたら書き込みが止まり、届いた分は
        欠けの無い先頭の部分 + overrun。True: 欠けの区間は StreamChunk.gap（流れのサンプルの位置 start から samples サンプルは値が無い。
        埋めない・補わない）と LaStream.gaps で返し、流れは続く（overrun は、欠けを記録できなかった・回路の符号器そのものが溢れた等、
        それでも止まったときだけ）。欠けの記録はイベントのリングを通るので、イベントの記録をしていなければ始める（EventLog.ensure_started。
        記録中ならそのまま続ける）。

        gap_hold_bytes: gaps = True のとき必ず明示する（既定は持たない）。欠けの記録が届くまで位置の決まらない拍を PC に持つ上限
        （バイト）。超えたら推定で置かずに StreamHoldExceeded で止める（GapPlacer の説明）。

        control_read_retries: 流している間の見回りの READ（la_deep.status〜stream_beats。読みに副作用が無い）の応答が時間切れに
        なったとき、送り直してよい回数（既定 0 = 送り直さず TimeoutError で止まる）。使う側が明示する。送り直した回数と読み捨てた
        遅れた応答は StreamStats.control（docs/superpowers/specs/2026-11-03-host-link-correctness-design.md §4）。"""
        if control_read_retries < 0:
            raise ValueError(f"control_read_retries は 0 以上: {control_read_retries}")
        if gaps and not self.caps.la_stream_gaps:
            raise RuntimeError("この構成は欠けの区間を記録して続けられない（ケイパビリティ la_stream_gaps が無い。溢れたら止まる形だけ）")
        if gaps and (gap_hold_bytes is None or gap_hold_bytes <= 0):
            raise ValueError(f"gaps = True は gap_hold_bytes（位置の決まらない拍を PC に持つ上限、バイト。正の値）を明示する: {gap_hold_bytes}")
        pack = _pack_code(pack_channels, rle, self.caps, self.lanes)
        if first_channel:
            if self.lanes <= 1:
                raise ValueError("first_channel はこの構成では 0 だけ（詰める ch を選べるのは 1 周期に複数のサンプルの構成）")
            if pack_channels is None or not 0 < first_channel <= self.channels - pack_channels:
                raise ValueError(f"first_channel は pack_channels と一緒に、ch {first_channel}〜{first_channel + (pack_channels or 16) - 1} が "
                                 f"0〜{self.channels - 1} に収まる値（{first_channel} は収まらない）")
        if self.lanes > 1 and rle and rate_hz is not None and lane_divisor(self.clock_hz, self.lanes, rate_hz)[0] != 1:
            raise ValueError("1 周期に複数のサンプルの RLE はサンプルの間隔を変えない（rate_hz は指定しない。RLE の量は変化の数で決まる）")
        can_ps = bool(self.caps.la_stream_ps)
        if ps_read and not can_ps:
            raise RuntimeError("この構成は PS の直接読みでストリームを流せない（ケイパビリティ la_stream_ps が無い）")
        use_ps = can_ps if ps_read is None else ps_read
        events = None
        if gaps:
            EventLog(self.dev).ensure_started()                 # 欠けの記録はイベントのリングに書かれる
        if self.caps.la_stream_events:
            capacity = self.dev.read32(g.EVENTS_CAPACITY)
            start = read_counts(self.dev)[0] if events_from is None else events_from
            events = (start, capacity)
        elif events_from is not None:
            raise RuntimeError("この構成はストリームにイベントを流せない（ケイパビリティ la_stream_events が無い）")
        self._configure(rate_hz, filter_channels=filter_channels, filter_ns=filter_ns)
        if not rle:
            limit = max_bytes_per_s if max_bytes_per_s is not None else self.caps.la_stream_bytes_per_s or None
            need = raw_stream_bytes_per_s(pack_channels or self.channels, 1 / self._period)
            if limit is not None and need > limit:
                source = "指定の max_bytes_per_s" if max_bytes_per_s is not None else "デバイスが申告する持続できると測った速さ"
                raise ValueError(f"RAW の流れ {need / 1e6:.1f} MB/s（{pack_channels or self.channels} ch × {1e-6 / self._period:.1f} "
                                 f"MS/s）が上限 {limit / 1e6:.1f} MB/s（{source}）を超える。ch を減らす（pack_channels）・サンプル"
                                 "レートを下げる・RLE にする・max_bytes_per_s で上限を明示する")
        self._discard_stream()
        select_stream_path(self.dev, use_ps)
        # 欠けの数えの基準は開始の書きの前に取る（後で取ると、開始から基準までに落ちたフレームが基準に入って数えから漏れ、
        # 見出しの無い流れがずれたサンプルを返す。レビュー 2026-11-03）
        buffer_start = _stream_buffer(self.dev, reset_peak=True)
        if events is not None:
            self.dev.write32(g.LA_DEEP_POST_BEATS, events[0] % events[1])   # このストリームでは最初のイベントの位置
        self.dev.write32(g.LA_DEEP_CONTROL, 1 | int(rle) << g.LA_DEEP_CONTROL_RLE[0]
                         | 1 << g.LA_DEEP_CONTROL_STREAM[0] | int(use_ps) << g.LA_DEEP_CONTROL_PS_READ[0]
                         | pack << g.LA_DEEP_CONTROL_PACK[0] | self._step << g.LA_DEEP_CONTROL_STEP[0]
                         | first_channel << g.LA_DEEP_CONTROL_FIRST[0] | int(gaps) << g.LA_DEEP_CONTROL_GAPS[0])
        timing = None
        if self.lanes > 1:
            info = self.dev.read32(g.LA_DEEP_INFO)
            if info >> g.LA_DEEP_INFO_LANES[0] & 0xF != self.lanes:
                self.dev.write32(g.LA_DEEP_CONTROL, 0)
                raise RuntimeError(f"符号器の 1 語のサンプルの数が合わない（申告 {self.lanes}、デバイス "
                                   f"{info >> g.LA_DEEP_INFO_LANES[0] & 0xF}）")
            timing = {"lanes": self.lanes, "divisor": self._divisor, "latency": info >> g.LA_DEEP_INFO_LATENCY[0] & 0xFF}
        return LaStream(self.dev, rle, self._period, pack_channels or self.channels, events=events, pack=pack,
                        fine_timing=timing, first_channel=first_channel, gaps=gaps, gap_hold_bytes=gap_hold_bytes,
                        control_read_retries=control_read_retries, buffer_start=buffer_start)

    def _discard_stream(self, timeout_s: float = 5.0) -> None:
        """止めた後（_configure の後）に、前の流れの残りを読み捨てる。止めると機器は未読を出し切るが、受け取り手が
        いなければ（例外で抜けた・デーモンが落ちた）残りが受け取り口（USB の EP・リンクの ch 4 の受け取り済み）に残り、
        次の流れの先頭として読まれる（見出しの無い所から読み始めて StreamFormatError）。出し切り（reading = 0）、
        50 ms 何も届かなくなるまで読む。"""
        deadline = time.monotonic() + timeout_s
        while True:
            reading = self.status() >> g.LA_DEEP_STATUS_READING[0] & 1
            try:
                self.dev.read_stream_some(LaStream.READ_MAX, timeout_ms=50)
            except TimeoutError:
                if not reading:
                    return
            if time.monotonic() > deadline:
                raise RuntimeError("前のストリームの残りが出し切れない（機器の status.reading が 0 にならない）")

    def stream_to_raw(self, raw_path: str | Path, duration_s: float, *, rate_hz: float | None = None, rle: bool = False,
                      names: dict[int, str] | None = None, pack_channels: int | None = None,
                      max_bytes_per_s: float | None = None, usage_interval_s: float | None = None,
                      first_channel: int = 0, gaps: bool = False, gap_hold_bytes: int | None = None,
                      control_read_retries: int = 0) -> "StreamStats":
        """duration_s 秒ストリーミングして、受け取った拍をそのまま生の保存ファイル（fprobe.la_raw）に追記する（取り込みだけ。
        変化点の取り出しも VCD の整形もしないので、受け取りが書き出しの速さに律速されない）。後から raw_to_vcd（または
        stream_to_vcd の raw_path）で VCD にする。引数と返す StreamStats は stream_to_vcd と同じ。
        仕様: docs/superpowers/specs/2026-11-03-la-raw-capture-design.md"""
        stream, meta = self._raw_stream(rate_hz=rate_hz, rle=rle, names=names, pack_channels=pack_channels,
                                        max_bytes_per_s=max_bytes_per_s, first_channel=first_channel, gaps=gaps,
                                        gap_hold_bytes=gap_hold_bytes, control_read_retries=control_read_retries)
        return self._capture(stream, meta, raw_path, duration_s, usage_interval_s)

    def stream_to_vcd(self, path: str | Path, duration_s: float, *, rate_hz: float | None = None, rle: bool = False,
                      names: dict[int, str] | None = None, pack_channels: int | None = None,
                      max_bytes_per_s: float | None = None, usage_interval_s: float | None = None,
                      first_channel: int = 0, gaps: bool = False, gap_hold_bytes: int | None = None,
                      control_read_retries: int = 0, raw_path: str | Path | None = None) -> "StreamStats":
        """duration_s 秒ストリーミングして VCD に書く（記録の長さはメモリに依らない）。pack_channels・max_bytes_per_s は stream と
        同じ（使うチャンネルだけ詰めて流す・RAW の量の上限）。VCD には、選んだチャンネル（ch 0〜pack_channels − 1）だけを並べる。
        usage_interval_s: 流している間、この秒数ごとに LaStream.usage を読む（None = 読まない）。終わった後にも 1 回読む
        （ケイパビリティ la_stream_usage のある構成）。読んだ結果は返す StreamStats.usage に古い順。
        gaps・gap_hold_bytes: stream と同じ（溢れても止めず欠けの区間を記録して続ける）。VCD では欠けの区間の全信号を x（値が分からない）にする。

        取り込み（stream_to_raw）と書き出し（raw_to_vcd）を続けて行う: 受け取った拍をまず生の保存ファイルに書き、終わってから VCD にする。
        raw_path = 生の保存ファイルの置き場所（残る。None = VCD の隣の一時ファイルで、書き出しの後に消す）。途中で例外（リンクの
        喪失・保持の上限等）が出ても、それまでに取り込んだ分を VCD にしてから、その例外をそのまま出す（隠さない）。
        VCD のバイト列は、取り込みと書き出しを分ける前と同じ。"""
        stream, meta = self._raw_stream(rate_hz=rate_hz, rle=rle, names=names, pack_channels=pack_channels,
                                        max_bytes_per_s=max_bytes_per_s, first_channel=first_channel, gaps=gaps,
                                        gap_hold_bytes=gap_hold_bytes, control_read_retries=control_read_retries)
        raw = Path(raw_path) if raw_path is not None else Path(f"{path}.fplaraw.tmp")
        try:
            try:
                stats = self._capture(stream, meta, raw, duration_s, usage_interval_s)
            finally:
                raw_to_vcd(RawCapture(raw), path)                  # 取り込めた分は、途中の例外でも VCD にする
        finally:
            if raw_path is None:
                with contextlib.suppress(FileNotFoundError):
                    raw.unlink()
        return stats

    def _raw_stream(self, *, rate_hz, rle, names, pack_channels, max_bytes_per_s, first_channel, gaps, gap_hold_bytes,
                    control_read_retries) -> "tuple[LaStream, RawMeta]":
        """ストリームを始め、生の保存ファイルのヘッダ（流れの性質。信号 i = ch first_channel + i、名前もその ch のもの）を作る。"""
        stream = self.stream(rate_hz=rate_hz, rle=rle, pack_channels=pack_channels, max_bytes_per_s=max_bytes_per_s,
                             first_channel=first_channel, gaps=gaps, gap_hold_bytes=gap_hold_bytes,
                             control_read_retries=control_read_retries)
        count = pack_channels or self.channels
        names = {ch - first_channel: name for ch, name in {**channel_names(self.caps), **(names or {})}.items()
                 if first_channel <= ch < first_channel + count}
        # ch ごとの時刻のずれ（fprobe.timing）。分からない ch はずらさず、VCD の $comment に書く
        timebase_hz = self.caps.timebase_hz
        skew = tuple(self.channel_skew[first_channel:first_channel + count]) if timebase_hz else ()
        return stream, RawMeta(rle=rle, pack=stream.pack, channels=count, first_channel=first_channel,
                               sample_period_s=stream.sample_period_s, names=names, skew=skew,
                               timebase_hz=timebase_hz or None)

    def _capture(self, stream: "LaStream", meta: "RawMeta", raw_path: str | Path, duration_s: float,
                 usage_interval_s: float | None) -> "StreamStats":
        """受け取りのループ: 受け取ったかたまりを生の保存ファイルにそのまま追記するだけ（復号しない）。"""
        last = time.monotonic()
        with RawCaptureWriter(raw_path, meta) as raw:
            for chunk in stream.chunks(duration_s):
                raw.write(chunk)
                if usage_interval_s is not None and time.monotonic() - last >= usage_interval_s:
                    stream.usage()
                    last = time.monotonic()
        if self.caps.la_stream_usage:
            stream.usage()
        return dataclasses.replace(stream.stats(), usage=tuple(stream.usage_history))

    def status(self) -> int:
        return self.dev.read32(g.LA_DEEP_STATUS)

    def wait(self, timeout_s: float = 10.0) -> None:
        deadline = time.monotonic() + timeout_s
        while not (status := self.status()) >> g.LA_DEEP_STATUS_DONE[0] & 1:
            if time.monotonic() > deadline:
                self.dev.write32(g.LA_DEEP_CONTROL, 0)
                if status >> g.LA_DEEP_STATUS_TRIGGERED[0] & 1:
                    raise TimeoutError("トリガは成立したが、トリガ後の拍がまだ埋まらない（RLE は信号の変化が少ないと"
                                       "遅い: after_beats を減らす・rle=False・timeout_s を延ばす）")
                raise TimeoutError("トリガが成立しない")
            time.sleep(0.001)

    def read(self, before_beats: int | None = None, after_beats: int | None = None) -> Capture:
        """トリガの前 before_beats 拍（既定: 残っている全部）から後 after_beats 拍（既定: 全部）までを読み、
        毎サンプルに展開する（RLE の長い記録は read_transitions を使う）。"""
        data, trigger, trigger_time = self._read_raw(before_beats, after_beats)
        return Capture(decode_beats(data, self._rle), trigger, trigger_time, self._period, self.channels,
                       channel_skew=self.channel_skew)

    def read_transitions(self, before_beats: int | None = None, after_beats: int | None = None) -> Transitions:
        """read と同じ範囲を、毎サンプルに展開せず変化点として読む。"""
        data, trigger, trigger_time = self._read_raw(before_beats, after_beats)
        values, starts, length = decode_transitions(data, self._rle)
        return Transitions(values, starts, length, trigger, trigger_time, self._period, self.channels,
                           self.channel_skew)

    @property
    def channel_skew(self) -> tuple[float | None, ...]:
        """ch ごとの時刻のずれ（最後に設定した前段で。fprobe.timing.la_channel_skew）。"""
        return la_channel_skew(self.caps, deep=True, front_info=self.front_info)

    def _read_command(self) -> int:
        """読み出しの命令。PS が DDR から直接読んで TCP で送る形（read_ps）は、デバイスにその能力（ケイパビリティ la_stream_ps）があり、
        TCP のストリームが開いているときだけ（UDP で 31 MB を一気に送ると、欠けても気づけない）。書き込みが止まった取り込みの読み出し専用
        （書き込み中のイベントのリングは、応答の来ていない書きを PS が読む恐れがあるので、今まで通り）。"""
        ps = bool(self.caps.la_stream_ps) and bool(getattr(self.dev, "stream_tcp_open", False))
        return 1 << (g.LA_DEEP_COMMAND_READ_PS[0] if ps else g.LA_DEEP_COMMAND_READ[0])

    def _read_raw(self, before_beats: int | None, after_beats: int | None) -> tuple[bytes, int, int]:
        status = self.status()
        if status >> g.LA_DEEP_STATUS_OVERRUN[0] & 1:
            raise RuntimeError("書き込みが追いつかず拍を捨てた（OVERRUN）。サンプルレートを下げるか RAW にする")
        trig_beat, written, time_lo, time_hi = self.dev.read(g.LA_DEEP_TRIGGER_BEAT, 4)
        post = self.dev.read32(g.LA_DEEP_POST_BEATS)
        slot = status >> g.LA_DEEP_STATUS_TRIGGER_SLOT[0] & 7
        available = written - post - 1                                    # トリガの前に残っている拍
        before = available if before_beats is None else min(before_beats, available)
        first = (trig_beat - before) % self.beats
        count = before + 1 + (post if after_beats is None else min(after_beats, post))
        self.dev.write32(g.LA_DEEP_READ_REGION, 0)
        self.dev.write(g.LA_DEEP_READ_FIRST, [first, count])
        command = self._read_command()
        select_stream_path(self.dev, command == 1 << g.LA_DEEP_COMMAND_READ_PS[0])
        self.dev.write32(g.LA_DEEP_COMMAND, command)
        data = self.dev.read_stream(count * BEAT_BYTES, timeout_ms=5000)
        return data, trigger_sample(data, self._rle, before, slot), pin_time(time_hi << 32 | time_lo,
                                                                             self.caps.la_delay_deep,
                                                                             self.caps.la_phase_span)

    def capture(self, *, timeout_s: float = 10.0, before_beats: int | None = None, after_beats: int | None = None,
                **trigger) -> Capture:
        self.start(**trigger)
        self.wait(timeout_s)
        return self.read(before_beats, after_beats)


# --- 連続ストリーミング（仕様: docs/superpowers/specs/2026-10-16-m8c-la-streaming-design.md） ---------------
