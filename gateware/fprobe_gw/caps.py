"""ケイパビリティ（デバイスが申告する機能・構成）の定義と TLV 符号化。

仕様: docs/superpowers/specs/2026-10-15-m3-control-plane-design.md §6
"""

import struct
from dataclasses import dataclass

from .csr.fprobe_map import (MEM_BEAT_BYTES, FPROBE_MAP, LA_LANES_MAX, LA_LANES_MAX_WORDS, LA_WIDTH, UART_FLOW_PIN_CHOICES,
                             XOUT_ACTIONS, XOUT_PIN_CHOICES)
from .timing import (LA_DEEP_DELAY, LA_FILTER_DELAY, LA_INTERNAL_DELAY, LA_PHASE_SPAN_TICKS, SYNC_RX_LATENCY_TICKS,  # noqa: F401
                     SYNC_RX_PHASE_SPAN_TICKS, SYNC_TX_LATENCY_TICKS, TIMING_EXCLUDES, TIMING_POINTS, TIMING_SOURCES,
                     TIMING_UNITS, TIMING_UNKNOWN_DELAY, TIMING_UNKNOWN_SPAN, la_channel_timing, timing_bytes)
from . import timing as _timing

__all__ = ["Capabilities", "HOST_CONSTANTS", "encode", "la_fine_channels", "la_layout", "la_testgen_hz", "render_package"]

# TLV の種類（0 は終端）
TYPE_END = 0
TYPE_PROTOCOL_VERSION = 1    # u32
TYPE_BOARD = 2               # 文字列（UTF-8）
TYPE_DESIGN = 3              # 文字列
TYPE_SYS_CLOCK_HZ = 4        # u32
TYPE_TIMEBASE_HZ = 5         # u32
TYPE_HOST_LINK = 6           # u32（LINK_*）
TYPE_CHANNELS = 7            # u32 ビットマップ（CH_* 番目のビット）
TYPE_LA_CHANNELS = 8         # u32（ロジアナがある場合）
TYPE_LA_DEPTH = 9            # u32（サンプル数）
TYPE_LA_SAMPLE_HZ = 10       # u32（最大サンプルレート）
TYPE_MEM_BYTES = 11          # u32（外部メモリ = 深いサンプルバッファの容量。ある場合）
TYPE_MEM_TEST = 12           # u32 = 1（メモリ試験回路がある場合）
TYPE_FEATURES = 13           # u32 ビットマップ（FEATURE_*。小さい FPGA で外せる機能。無ければ全部ある）
FEATURE_XTRIG = 1 << 0       # クロストリガ（割り込み書き込み・番地一致）
FEATURE_TRIG_SEQ = 1 << 1    # 多段トリガ
FEATURE_AUX = 1 << 2         # 補助マイコン（Pico）の計測値（CSR aux・イベント 6）
FEATURE_SAMPLER = 1 << 3     # DAP サンプラ（F4: 変数のライブ観測。CSR sampler・イベント 7）
FEATURE_LA_FILTER = 1 << 4   # ロジアナのグリッチフィルタ（U21。CSR la.filter）
FEATURE_LA_PULSE = 1 << 5    # ロジアナのパルス幅のトリガ（U21。CSR la.pulse・pulse_min・pulse_max）
FEATURE_LA_STREAM_EVENTS = 1 << 6   # ロジアナのストリームにイベントも見出し付きで流す（R1。無ければ今の形式）
FEATURE_LA_STREAM_PS = 1 << 11   # 深いバッファのストリームを、PS が DDR のリングを直接読んで流せる（PYNQ。中継の窓の版 2。
                                  # docs/superpowers/specs/2026-10-30-la-stream-ps-direct-design.md。無ければ今の形式）
FEATURE_LA_PACK = 1 << 12    # 深いバッファのストリームで、使うチャンネルだけ詰めて流せる（CSR la_deep.control.pack。無ければ 16 ch 固定）
FEATURE_LA_TESTGEN = 1 << 13   # 試験用の方形波（CSR la_testgen。取り込みが速い構成 fprobe_pynq_cap と垂れ流しの fprobe_pynq_deep。無ければ窓が無い）
FEATURE_FPGA_OBS = 1 << 7   # 対象の FPGA の観測（CSR obs・イベント 8・9。大きい FPGA の構成だけ）
FEATURE_OBS_MATCH = 1 << 8   # 観測の値の一致をクロストリガの源 bit4 に（CSR obs.match。観測の口とクロストリガがある構成）
FEATURE_SAMPLER_MATCH = 1 << 10   # サンプラの値の一致をクロストリガの源 bit5 に（CSR sampler_match。R21）
FEATURE_SYNC = 1 << 9        # ボードの間の時刻の同期（CSR sync・イベント 10・端子 sync_in / sync_out）
FEATURE_EVENT_BYTE_PACK = 1 << 14   # UART・SWO のバイトのイベントを詰めた形（種類 13・14・15: 1 件に最大 4 バイトと各バイトの
                                    # 時刻）で出す。無ければ古い形（種類 3・4・5: 1 バイト 1 件）。
                                    # docs/superpowers/specs/2026-11-01-bandwidth-observation-design.md 3.5
FEATURE_LA_STREAM_USAGE = 1 << 15   # 深いバッファのリングと符号の数え（CSR la_deep.la_written・ring_unread・ring_peak・start_time・info）。
                                    # 深いバッファのある構成（メモリ試験の構成を除く）は全部持つ。無ければ古い回路（la_deep は 0x40 まで）。
                                    # docs/superpowers/specs/2026-11-01-la-deep-fast-design.md §6
FEATURE_DAP_LINE_TIME = 1 << 16    # DAP の時刻が線上の点（docs/superpowers/specs/2026-11-01-time-accuracy-design.md 段階 S2）:
                                    # ホストの転送（1・12）・割り込み書き込み（11）の時刻は転送の線上の点（SWD は ACK の後の立ち上がり、
                                    # JTAG は Update-DR を出る立ち上がり。fprobe_gw.timing の DAP_POINT_*）、サンプラの語（7）は読みに
                                    # 行った転送の点 + [119:115] = 値を返した転送の WAIT の回数、種類 16（PHY の設定）・17（値を返した
                                    # 転送の点）・18（誤りの区間）、CSR sampler.check（JTAG の K 語の確認）。無ければ古い意味
                                    # （完了 + 2 刻み・値が返った周期）
FEATURE_LA_STREAM_GAPS = 1 << 17    # 深いバッファのストリームで、溢れても止めず欠けの区間をイベント（種類 EVENT_KIND_LA_GAP）に記録して
                                    # 続けられる（CSR la_deep.control.gaps・gap_last_beat・gaps。Capabilities.la_stream_gaps）。
                                    # 無ければ溢れたら止まる形だけ。
                                    # docs/superpowers/specs/2026-11-01-la-deep-fast-design.md §12
FEATURE_WATCH_START = 1 << 18      # 割り込み書き込みの始動の列（CSR inject.start・start_status・start_*_time）が成功したら、
                                    # サンプラの値の一致の門（sampler_match.control.after_start・first_sample・started、fire.first）を
                                    # 開ける経路。値の一致と割り込み書き込みの両方がある構成。無ければそれらの CSR は無い。
                                    # docs/superpowers/specs/2026-11-03-halt-watch-fpga-restart-design.md
# 同期の段数（SYNC_*_TICKS）は時刻の表 fprobe_gw.timing が正（このモジュールは読み込んで同じ名前で出す）
TYPE_DEBUG = 14              # u32 ビットマップ（DEBUG_*。デバッグポートの構成。無い古いデバイスは全部ある）
DEBUG_JTAG = 1 << 0          # JTAG の PHY（無ければ DAP_Connect の JTAG は失敗）
DEBUG_INJECT = 1 << 1        # 故障注入 = 割り込み書き込み（swd_injector。xtrig の inject_*）
DEBUG_SWO_MANCHESTER = 1 << 2   # SWO の Manchester 方式（U51。DAP_Info の Capabilities bit3 と同じ）。古いデバイスは無い
DEBUG_JTAG_BIT_2HP = 1 << 3  # JTAG の 1 ビット = 2 × half_period 周期（jtag_phy。デバッグの口・観測の口とも。TCK = sys / (2 × half_period)）。
                             # 回路の版の事実なので常に申告する。無い古いデバイスは 2 × half_period + 1 周期（2026-11-01 より前）
DEBUG_HALF_PERIOD_1 = 1 << 4  # SWCLK / TCK の half_period の下限が 1（SWJ_Clock の最大 = sys / 2。swd_phy・jtag_phy・dap_cmd_config）、
                              # SWD の取り込みの遅れ（CSR debug.sample_delay.swd）が 0〜7。回路の版の事実なので常に申告する。
                              # 無い古いデバイスは下限 2（sys / 4）・遅れ 0〜3（2026-11-03 より前。
                              # docs/superpowers/specs/2026-11-03-dap-line-rate-design.md）
TYPE_OUTPUTS = 15            # クロストリガの動作「出力の端子」（CSR xtrig_out）: 先頭 1 バイト = 同時に動かせる動作の数、
                             # 続いて端子の候補 1 本に 1 バイト（OUTPUT_*。ctrl.pin の番号の順）。無ければ出力なし
OUTPUT_CHANNEL_MASK = 0x7F   # その端子のロジアナのチャンネル
OUTPUT_PULL_UP = 0x80        # その端子に（外せない）プルアップがある: 動いていない間も弱く High
TYPE_BUILD = 16              # ビルドの版（U49）: u32 × 5（fprobe_gw.build_info の語の並び）。ROM では build_info_pkg を
                             # 参照し、ビルドのたびに差し替わる。時刻 0 は版なし（シミュレーション・古い作り方）
TYPE_LA_LAYOUT = 18          # ロジアナの ch の並び（U22）: ch 1 本に 1 バイト = 種類 << 4 | 種類の中の番号（LA_CH_*）。
                             # 無い古いデバイスは ch 0〜7 = ピン、8・9 = 内部、残りは無し
LA_CH_NONE = 0               # 何もつながっていない（常に 0）
LA_CH_PIN = 1                # 拡張ヘッダ（デバッグコネクタ）のピン。番号 = コネクタの番号（デバッグの線と兼用）
LA_CH_INTERNAL = 2           # 内部の信号。0 = プローブが SWDIO を駆動中、1 = DAP の処理中
LA_CH_INPUT = 3              # 専用の入力（デバッグの線と兼用しない純粋な入力）。番号 = 入力の番号
LA_INTERNAL_PROBES = 2       # fprobe_core が外部のピンの後に置く内部の信号の数
TYPE_LA_DELAY = 20           # ロジアナの時刻（trigger_time）がピンの変化より遅い分: u16 × 3 = 内蔵・深いバッファの段数
                             # （刻み。回路で決まる）と、非同期のピンを取り込む位相の幅（刻み。ピンの変化は 時刻 − 段数 以上
                             # 時刻 − 段数 + 幅 未満。時刻の約束は timing の冒頭）。19 は旧い形（半刻みで平均 0.5 を足していた。読まない）。
                             # 無い古いデバイスは 0（直さない）
# ロジアナの段数（LA_FILTER_DELAY・LA_INTERNAL_DELAY・LA_DEEP_DELAY・LA_PHASE_SPAN_TICKS）は時刻の表 fprobe_gw.timing が正
TYPE_LA_CH_TIMING = 21       # ロジアナの ch ごとの時刻（時刻の表 timing.la_channel_timing）: ch 1 本に 3 バイト = int8 内蔵の
                             # バッファ・int8 深いバッファ（ch の変化 = 時間軸の時刻 + この刻み）・u8 位相の幅（刻み）。
                             # -128 / 255 = 回路で決まっていない・確かめていない。無い古いデバイスは ch を区別しない
LA_CH_TIMING_UNKNOWN_SKEW = -128
LA_CH_TIMING_UNKNOWN_SPAN = 0xFF
TYPE_LA_TESTGEN_HZ = 22      # 試験用の方形波（CSR la_testgen）を刻むクロックの周波数（u32、Hz）。la_testgen のある構成だけ。
                             # 無い古いデバイスは、取り込みが sys の整数倍の構成なら la_sample_hz、そうでなければ sys_clock_hz
TYPE_LA_FINE = 23            # 内蔵バッファで la_sample_hz の細かさで取る ch（u32 ビットマップ。ビット n = ch n）。ほかの ch は sys で取り、
                             # sys の 1 周期の la_sample_hz / sys_clock_hz 個のサンプルに同じ値が並ぶ（時刻の分解能は sys の周期）。
                             # 無ければ全部の ch が la_sample_hz（1 周期に複数のサンプルの構成 la_lanes > 1 だけが申告する）
TYPE_LA_STREAM_BYTES_PER_S = 24   # 深いバッファの垂れ流しを、この構成で持続できると測った速さ（u32、B/s。PC に届く符号のバイト数）。
                             # 測った事実（方法と条件は designs.py の注と設計書）で、ホストは RAW の流れ（始める前に量が決まる）が
                             # これを超える組を、始める前に断る（使う側が明示した上限があれば、そちらを使う）。RLE は断らない
                             # （量は信号の変化で決まる）。無い構成は断らない（申告が無い = 分からない）。
                             # docs/superpowers/specs/2026-11-01-la-deep-fast-design.md §5
TYPE_TIMING = 25             # 情報源ごとの時刻の点（時刻の表 timing.timing_points）: 1 件 8 バイト = u8 情報源・u8 点・u8 単位・
                             # u8 含まない項目（ビット）・i16 段数（時刻 = 点 + 段数）・u16 位相の幅（TIMING_* の符号）。
                             # 申告の無い情報源・無い古いデバイスは、ホストがその版の回路のいまの意味で読む。
                             # docs/superpowers/specs/2026-11-01-time-accuracy-design.md §4.2（段階 S1）。DAP の線上の点の
                             # 端子までの段（段階 S2）も情報源 dap_swd・dap_jtag としてここで申告する（別の TLV にしない。§12.4）
TYPE_UART_FLOW = 17          # UART の RTS/CTS（U52。CSR uart.flow）: 端子の候補 1 本に 1 バイト（ロジアナのチャンネル。
                             # flow.rts_pin・cts_pin の番号の順）。無ければフロー制御の回路が無い

LINK_USB_HS_ULPI = 1
LINK_SERIAL = 2              # SerialLink（UART、COBS フレーム）
LINK_ETH = 3                 # EthernetLink（UDP）

# 論理チャンネル（全体設計 §3.2）
CH_CONTROL, CH_DAP, CH_SWO, CH_UART0, CH_CAPTURE, CH_EVENT = range(6)

MAGIC = b"FCAP"


@dataclass(frozen=True)
class Capabilities:
    protocol_version: int
    board: str
    design: str
    sys_clock_hz: int
    timebase_hz: int
    host_link: int
    channels: tuple[int, ...]
    la_channels: int = 0         # 0 ならロジアナ無し（サンプルの幅。並びは la_layout）
    la_depth: int = 0
    la_sample_hz: int = 0
    mem_bytes: int = 0           # 0 なら外部メモリ無し
    mem_test: bool = False       # メモリ試験回路（基板立ち上げ用の構成だけが持つ。深いバッファの代わり）
    xtrig: bool = True           # クロストリガ
    trig_seq: bool = True        # 多段トリガ
    aux: bool = False            # 補助マイコンの計測値（ボードに補助マイコンをつないだ構成だけ）
    sampler: bool = True         # DAP サンプラ（変数のライブ観測）
    sampler_match: bool = True   # サンプラの値の一致 → クロストリガの源 bit5（R21。CSR sampler_match。サンプラとクロストリガが要る）
    jtag: bool = True            # JTAG の PHY（B22: 標準構成は外し、JTAG の構成が持つ）
    inject: bool = True          # 故障注入（割り込み書き込み）。クロストリガ（xtrig）の一部
    la_filter: bool = True       # ロジアナのグリッチフィルタ（ロジアナがある構成で）
    la_pulse: bool = True        # ロジアナのパルス幅のトリガ
    la_stream_events: bool = False   # ストリームにイベントも流す（R1。資源のため既定は無し、構成で有効にする）
    la_stream_ps: bool = False       # ストリームを PS が DDR のリングを直接読んで流せる（PYNQ の深いバッファの構成だけ）
    # ストリームで溢れても止めず、欠けの区間をイベントに記録して続けられる（la_deep_buffer の STREAM_GAPS。外部メモリがあり、
    # ストリームにイベントも流す構成だけ。docs/superpowers/specs/2026-11-01-la-deep-fast-design.md §12）
    la_stream_gaps: bool = False
    la_pack: bool = False            # ストリームで使うチャンネルだけ詰められる（外部メモリ = 深いバッファのある構成だけ）
    la_testgen: bool = False         # 試験用の方形波（ピンに出す。刻むクロックは cap_ratio > 1 なら cap_clk、そうでなければ sys）
    fpga_obs: bool = False           # 対象の FPGA の観測の口（obs_*。大きい FPGA の構成だけ）
    sync: bool = False               # ボードの間の時刻の同期（端子 sync_in / sync_out をつないだ構成だけ）
    # クロストリガの動作「出力の端子」: 端子の候補 j の (ロジアナのチャンネル, プルアップがある) と、同時に動かせる動作の数。
    # 候補が空なら回路が無い
    outputs: tuple[tuple[int, bool], ...] = ()
    output_actions: int = XOUT_ACTIONS
    # ロジアナの ch の並び（fprobe_core の LA_PIN_COUNT・LA_INPUT_COUNT と一致させる）: ch 0〜 = 拡張ヘッダのピン
    # la_pins 本、続いて内部の信号 2 本、続いて専用の入力 la_inputs 本（U22）
    la_pins: int = 8
    la_inputs: int = 0
    swo_manchester: bool = True  # SWO の Manchester 方式の受信（U51）
    # 内蔵バッファの 1 語（sys の 1 周期）のサンプルの数（端子の直並列変換。docs/superpowers/specs/2026-11-01-la-resolution-design.md）。
    # 1 = 1 周期 1 サンプル（cap_ratio の構成を含む）。2 以上 = la_sample_hz = sys × la_lanes、細かい ch は LA_FINE（専用の入力と、la_fine_pins ならピン）。
    # la_depth はサンプルの数（= 語の数 × la_lanes）。グリッチフィルタ・パルス幅のトリガは無い（la_filter・la_pulse = False）
    la_lanes: int = 1
    # la_lanes > 1 のとき、拡張ヘッダのピン（ch 0〜la_pins − 1）もボードの直並列変換で細かく取る（fprobe_core の LA_FINE_PINS。
    # LA_FINE に入る）。False ならピンは sys で 2 段同期化して全レーンに並べる
    la_fine_pins: bool = False
    # la_lanes > 1 のとき、ボードの直並列変換の 1 語の最も新しいレーンの遅れ（細かいサンプルの数。fprobe_core の LA_LANE_IN_AGE。
    # PYNQ-Z2 は designs.pynq_z2_iserdes_newest_age）。TLV には出さない（時刻の表の段数の計算と、回路の generic にだけ使う）
    la_lane_in_age: int = 0
    cap_ratio: int = 1          # 内蔵ロジアナの取り込みのクロック / sys（整数比。1 = sys と同じ。la_sample_hz = sys × 比。TLV には出さず、ホストは la_sample_hz / timebase_hz で知る）
    # UART の RTS/CTS（U52）: 端子の候補 j のロジアナのチャンネル（flow.rts_pin・cts_pin の番号の順）。空なら回路が無い
    uart_flow: tuple[int, ...] = ()
    # 外部メモリの無い構成のイベント記録の件数（2**event_ring_bits、0 = 無し）。ROM には入れない（PC は EVENTS_CAPACITY を
    # 読む）。回路の generic（生成パッケージの EVENT_RING_BITS）だけに使う
    event_ring_bits: int = 0
    # UART・SWO のバイトのイベントを詰めた形で出す（event_collector の BYTE_PACK。イベント記録のある構成だけ）
    event_byte_pack: bool = False
    # 深いバッファの垂れ流しを持続できると測った速さ（B/s。0 = 申告しない）。TLV LA_STREAM_BYTES_PER_S
    la_stream_bytes_per_s: int = 0
    # DAP サンプラの JTAG の確認の間隔 K の上限（dap_sampler の CHECK_MAX = 溜める語の数）。ROM には入れない（PC は CSR
    # sampler.check.max を読む）。回路の generic（生成パッケージの SAMPLER_CHECK_MAX）だけに使う
    sampler_check_max: int = 1

    def __post_init__(self):
        if self.la_channels and self.la_pins + LA_INTERNAL_PROBES + self.la_inputs > self.la_channels:
            raise ValueError(f"ロジアナの幅 {self.la_channels} に ch（ピン {self.la_pins}・内部 {LA_INTERNAL_PROBES}・"
                             f"専用の入力 {self.la_inputs}）が収まらない")
        if self.la_channels > LA_WIDTH or not 0 <= self.la_inputs <= 15 or not 0 < self.la_pins <= 15:
            raise ValueError("ロジアナの ch の数が範囲外")
        if self.la_inputs and not self.la_channels:
            raise ValueError("専用の入力（la_inputs）はロジアナのある構成だけ")
        if self.la_stream_events and not self.mem_bytes:
            raise ValueError("ストリームにイベントを流す（la_stream_events）のは外部メモリ（深いバッファ）のある構成だけ")
        if self.la_pack and not self.mem_bytes:
            raise ValueError("チャンネルを詰める（la_pack）のは外部メモリ（深いバッファ）のある構成だけ")
        if self.la_stream_ps and not (self.mem_bytes and self.la_stream_events):
            raise ValueError("PS の直接読み（la_stream_ps）は外部メモリがあり、ストリームにイベントも流す構成だけ")
        if self.la_stream_gaps and not (self.mem_bytes and self.la_stream_events and not self.mem_test):
            raise ValueError("欠けの区間の記録（la_stream_gaps）は外部メモリがあり、ストリームにイベントも流す構成だけ（欠けの記録はイベントの流れで届く）")
        if self.event_ring_bits and self.mem_bytes:
            raise ValueError("event_ring_bits は外部メモリの無い構成だけ（外部メモリのある構成は深いバッファに記録する）")
        if self.event_byte_pack and not (self.mem_bytes or self.event_ring_bits):
            raise ValueError("バイトを詰めたイベント（event_byte_pack）はイベント記録のある構成だけ（外部メモリか event_ring_bits）")
        if not 1 <= self.sampler_check_max <= 64:
            raise ValueError("sampler_check_max は 1〜64（CSR sampler.check.max の 7 ビット・溜める語の数）")
        if self.inject and not self.xtrig:
            raise ValueError("故障注入（inject）はクロストリガ（xtrig）の一部: xtrig=False なら inject=False")
        if len(self.outputs) > XOUT_PIN_CHOICES:
            raise ValueError(f"出力の端子の候補は {XOUT_PIN_CHOICES} 本まで")
        if not 1 <= self.output_actions <= XOUT_ACTIONS:
            raise ValueError(f"出力の動作は 1〜{XOUT_ACTIONS} 本")
        if any(not 0 <= ch <= OUTPUT_CHANNEL_MASK for ch, _ in self.outputs):
            raise ValueError("出力の端子のチャンネルが範囲外")
        if self.la_lanes > 1:
            # 深いバッファの構成（mem_bytes > 0）は内蔵バッファを持たない（la_depth = 0。垂れ流しを N 並列で符号にする）
            words = self.la_depth // self.la_lanes if not self.mem_bytes else 1
            if self.mem_bytes and self.la_depth:
                raise ValueError("1 周期に複数のサンプルの深いバッファの構成は内蔵バッファを持たない（la_depth = 0）")
            if (self.la_lanes % 2 or self.la_lanes > LA_LANES_MAX or self.la_depth % self.la_lanes or words & (words - 1)
                    or not 0 < words <= LA_LANES_MAX_WORDS):
                raise ValueError(f"1 周期に複数のサンプル: la_lanes は {LA_LANES_MAX} 以下の偶数、la_depth / la_lanes は "
                                 f"{LA_LANES_MAX_WORDS} 以下の 2 の累乗")
            if self.la_filter or self.la_pulse:
                raise ValueError("1 周期に複数のサンプルの構成にはグリッチフィルタ・パルス幅のトリガが無い（la_filter・la_pulse = False）")
            if self.la_sample_hz != self.sys_clock_hz * self.la_lanes:
                raise ValueError("la_sample_hz は sys_clock_hz × la_lanes")
        if self.la_fine_pins and self.la_lanes <= 1:
            raise ValueError("ピンを細かく取る（la_fine_pins）のは 1 周期に複数のサンプルの構成（la_lanes > 1）だけ")
        if self.la_lane_in_age and self.la_lanes <= 1:
            raise ValueError("la_lane_in_age は 1 周期に複数のサンプルの構成（la_lanes > 1）だけ")
        if self.la_pulse and not self.la_filter and self.la_channels and self.cap_ratio == 1:
            # sys の構成のパルス幅のトリガ（la_pulse）は同期化した値を見る前提で、同期化はグリッチフィルタ（la_filter）の段にある。
            # フィルタの無い構成では同期化していない値を 1 段で取り、メタ安定になる（docs/superpowers/specs/
            # 2026-11-01-time-accuracy-design.md §9.5 の N5）。取り込みの領域（cap_ratio > 1）は la_cap_domain が常に同期化する
            raise ValueError("パルス幅のトリガ（la_pulse）はグリッチフィルタ（la_filter）のある構成だけ（sys の構成で同期化はフィルタの段にある）")
        if self.la_stream_bytes_per_s and not self.mem_bytes:
            raise ValueError("垂れ流しの速さ（la_stream_bytes_per_s）は外部メモリ（深いバッファ）のある構成だけ")
        if len(self.uart_flow) > UART_FLOW_PIN_CHOICES or any(not 0 <= ch <= 0xFF for ch in self.uart_flow):
            raise ValueError(f"UART の RTS/CTS の端子の候補は {UART_FLOW_PIN_CHOICES} 本まで（チャンネル 0〜255）")


def la_layout(caps: Capabilities) -> bytes:
    """TLV LA_LAYOUT の中身（ch 1 本に 1 バイト）。"""
    kinds = ([LA_CH_PIN << 4 | i for i in range(caps.la_pins)]
             + [LA_CH_INTERNAL << 4 | i for i in range(LA_INTERNAL_PROBES)]
             + [LA_CH_INPUT << 4 | i for i in range(caps.la_inputs)])
    return bytes(kinds + [LA_CH_NONE << 4] * (caps.la_channels - len(kinds)))


def _tlv(kind: int, value: bytes) -> bytes:
    return struct.pack("<HH", kind, len(value)) + value + bytes(-len(value) % 4)


def la_delay_ticks(caps: Capabilities) -> tuple[int, int]:
    """ロジアナの時刻がピンの変化より遅い段数（内蔵, 深いバッファ。刻み）。位相の幅（LA_PHASE_SPAN_TICKS）は別に申告する。"""
    return _timing.la_delay_ticks(caps.la_filter)


def la_fine_channels(caps: Capabilities) -> int:
    """la_sample_hz の細かさで取る ch のビットマップ（1 周期に複数のサンプルの構成: 専用の入力と、la_fine_pins ならピン。
    内部の 2 本は sys の信号なので入らない）。"""
    if caps.la_lanes <= 1:
        return (1 << caps.la_channels) - 1
    first = caps.la_pins + LA_INTERNAL_PROBES
    pins = (1 << caps.la_pins) - 1 if caps.la_fine_pins else 0
    return ((1 << caps.la_inputs) - 1) << first | pins


def la_testgen_hz(caps: Capabilities) -> int:
    """試験用の方形波を刻むクロック（fprobe_core: CAP_RATIO > 1 なら cap_clk = sys × cap_ratio、そうでなければ sys）。"""
    return caps.sys_clock_hz * caps.cap_ratio


def la_channel_timing_bytes(caps: Capabilities) -> bytes:
    """TLV LA_CH_TIMING の中身（ch 1 本に 3 バイト）。"""
    out = bytearray()
    for row in la_channel_timing(caps):
        for skew in (row.internal, row.deep):
            out += struct.pack("<b", LA_CH_TIMING_UNKNOWN_SKEW if skew is None else skew)
        out.append(LA_CH_TIMING_UNKNOWN_SPAN if row.span is None else row.span)
    return bytes(out)


def encode(caps: Capabilities, build: list[int] | None = None) -> bytes:
    """先頭 "FCAP" + 総バイト数（u32）、続いて TLV（4 バイト境界まで 0 詰め）、種類 0 で終端。
    build はビルドの版の語（fprobe_gw.build_info.words。None なら版なし = 全部 0）。"""
    return _encode(caps, build)[0]


def _encode(caps: Capabilities, build: list[int] | None = None) -> tuple[bytes, int]:
    """符号化と、TLV BUILD の中身の先頭の語の位置。"""
    from .build_info import WORDS

    def u32(v: int) -> bytes:
        return struct.pack("<I", v)

    build = build if build is not None else [0] * WORDS
    if len(build) != WORDS:
        raise ValueError(f"版は {WORDS} 語")
    head = b"".join([
        _tlv(TYPE_PROTOCOL_VERSION, u32(caps.protocol_version)),
        _tlv(TYPE_BOARD, caps.board.encode()),
        _tlv(TYPE_DESIGN, caps.design.encode()),
        _tlv(TYPE_SYS_CLOCK_HZ, u32(caps.sys_clock_hz)),
        _tlv(TYPE_TIMEBASE_HZ, u32(caps.timebase_hz)),
        _tlv(TYPE_HOST_LINK, u32(caps.host_link)),
        _tlv(TYPE_CHANNELS, u32(sum(1 << ch for ch in caps.channels))),
        *([_tlv(TYPE_LA_CHANNELS, u32(caps.la_channels)), _tlv(TYPE_LA_DEPTH, u32(caps.la_depth)),
           _tlv(TYPE_LA_SAMPLE_HZ, u32(caps.la_sample_hz)), _tlv(TYPE_LA_LAYOUT, la_layout(caps))]
          if caps.la_channels else []),
        *([_tlv(TYPE_MEM_BYTES, u32(caps.mem_bytes))] if caps.mem_bytes else []),
        *([_tlv(TYPE_MEM_TEST, u32(1))] if caps.mem_test else []),
        _tlv(TYPE_FEATURES, u32(FEATURE_XTRIG * caps.xtrig | FEATURE_TRIG_SEQ * caps.trig_seq
                                | FEATURE_AUX * caps.aux | FEATURE_SAMPLER * caps.sampler
                                | FEATURE_LA_FILTER * caps.la_filter | FEATURE_LA_PULSE * caps.la_pulse
                                | FEATURE_LA_STREAM_EVENTS * caps.la_stream_events
                                | FEATURE_LA_STREAM_PS * caps.la_stream_ps
                                | FEATURE_LA_PACK * caps.la_pack
                                | FEATURE_LA_TESTGEN * caps.la_testgen
                                | FEATURE_FPGA_OBS * caps.fpga_obs
                                | FEATURE_OBS_MATCH * (caps.fpga_obs and caps.xtrig)
                                | FEATURE_SYNC * caps.sync
                                | FEATURE_EVENT_BYTE_PACK * caps.event_byte_pack
                                | FEATURE_LA_STREAM_USAGE * bool(caps.mem_bytes and not caps.mem_test)
                                | FEATURE_DAP_LINE_TIME
                                | FEATURE_LA_STREAM_GAPS * caps.la_stream_gaps
                                | FEATURE_SAMPLER_MATCH * (caps.sampler_match and caps.sampler and caps.xtrig)
                                | FEATURE_WATCH_START * (caps.sampler_match and caps.sampler and caps.xtrig and caps.inject))),
        _tlv(TYPE_DEBUG, u32(DEBUG_JTAG * caps.jtag | DEBUG_INJECT * caps.inject
                             | DEBUG_SWO_MANCHESTER * caps.swo_manchester | DEBUG_JTAG_BIT_2HP | DEBUG_HALF_PERIOD_1)),
        *([_tlv(TYPE_OUTPUTS, bytes([caps.output_actions,
                                     *(ch | OUTPUT_PULL_UP * pull_up for ch, pull_up in caps.outputs)]))]
          if caps.outputs else []),
        *([_tlv(TYPE_UART_FLOW, bytes(caps.uart_flow))] if caps.uart_flow else []),
        *([_tlv(TYPE_LA_DELAY, struct.pack("<HHH", *la_delay_ticks(caps), LA_PHASE_SPAN_TICKS)),
           _tlv(TYPE_LA_CH_TIMING, la_channel_timing_bytes(caps))]
          if caps.la_channels else []),
        *([_tlv(TYPE_LA_TESTGEN_HZ, u32(la_testgen_hz(caps)))] if caps.la_testgen else []),
        *([_tlv(TYPE_LA_FINE, u32(la_fine_channels(caps)))] if caps.la_lanes > 1 else []),
        *([_tlv(TYPE_LA_STREAM_BYTES_PER_S, u32(caps.la_stream_bytes_per_s))] if caps.la_stream_bytes_per_s else []),
        _tlv(TYPE_TIMING, timing_bytes(caps)),
    ])
    body = head + _tlv(TYPE_BUILD, b"".join(map(u32, build))) + _tlv(TYPE_END, b"")
    build_word = (8 + len(head) + 4) // 4                   # 先頭（magic・総バイト数）と TLV の頭の後
    return MAGIC + struct.pack("<I", 8 + len(body)) + body, build_word


def _mem_addr_bits(mem_bytes: int) -> int:
    """外部メモリの拍（MEM_BEAT_BYTES バイト）の番号のビット数（0 = 外部メモリ無し）。大きさは 2 の累乗の拍。"""
    if not mem_bytes:
        return 0
    beats = mem_bytes // MEM_BEAT_BYTES
    if beats * MEM_BEAT_BYTES != mem_bytes or beats & (beats - 1):
        raise ValueError(f"mem_bytes {mem_bytes} が 2 の累乗の拍（{MEM_BEAT_BYTES} バイト）でない")
    return beats.bit_length() - 1


def _la_lane_addr_bits(caps: Capabilities) -> int:
    """1 周期に複数のサンプルの内蔵バッファの語の番号のビット数（それ以外の構成は使わないので 1）。"""
    return max(1, (caps.la_depth // caps.la_lanes).bit_length() - 1) if caps.la_lanes > 1 else 1


def render_package(package: str, caps: Capabilities) -> str:
    """ROM の内容（32 ビットワード、リトルエンディアン）を定数 CAPS_ROM とする VHDL パッケージ。
    回路の有無を決める定数（MEM_TEST）も同じ定義から出す（申告と実装が食い違わない）。"""
    from .build_info import WORDS
    data, build_word = _encode(caps)
    window = FPROBE_MAP.block("caps").size
    if len(data) > window:
        raise ValueError(f"ケイパビリティ {len(data)} バイトが ROM の窓 {window} バイトに収まらない")
    words = [f'x"{int.from_bytes(data[i:i + 4], "little"):08X}"' for i in range(0, len(data), 4)]
    for k in range(WORDS):                                  # 版はビルドのたびに差し替わるパッケージから
        words[build_word + k] = f"BUILD_INFO({k})"
    rom = ",\n".join(f"    {w}" for w in words)
    return "\n".join([
        "-- 自動生成ファイル。直接編集しないこと（python -m fprobe_gw gen で再生成）。",
        "-- 生成元: gateware/fprobe_gw/caps.py とデザイン定義（fprobe_gw/designs.py）",
        f"-- board={caps.board!r} design={caps.design!r} sys_clock_hz={caps.sys_clock_hz}",
        "library ieee;",
        "use ieee.std_logic_1164.all;",
        "use work.csr_bus_pkg.all;",
        "use work.build_info_pkg.all;",
        "",
        f"package {package} is",
        f"  constant CAPS_ROM : csr_word_array_t(0 to {len(words) - 1}) := (",
        rom,
        "  );",
        f"  constant MEM_TEST : boolean := {str(caps.mem_test).lower()};",
        f"  constant XTRIG : boolean := {str(caps.xtrig).lower()};",
        f"  constant TRIG_SEQ : boolean := {str(caps.trig_seq).lower()};",
        f"  constant AUX : boolean := {str(caps.aux).lower()};",
        f"  constant SAMPLER : boolean := {str(caps.sampler).lower()};",
        f"  constant SAMPLER_MATCH : boolean := {str(caps.sampler_match and caps.sampler and caps.xtrig).lower()};",
        f"  constant HAS_JTAG : boolean := {str(caps.jtag).lower()};",
        f"  constant INJECTOR : boolean := {str(caps.inject).lower()};",
        f"  constant LA_FILTER : boolean := {str(caps.la_filter).lower()};",
        f"  constant LA_PULSE : boolean := {str(caps.la_pulse).lower()};",
        f"  constant LA_STREAM_EVENTS : boolean := {str(caps.la_stream_events).lower()};",
        f"  constant LA_STREAM_GAPS : boolean := {str(caps.la_stream_gaps).lower()};",
        f"  constant OBS_PORT : boolean := {str(caps.fpga_obs).lower()};",
        f"  constant SYNC : boolean := {str(caps.sync).lower()};",
        f"  constant XOUT_COUNT : natural := {caps.output_actions if caps.outputs else 0};",
        f"  constant LA_INPUTS : natural := {caps.la_inputs};",
        f"  constant SWO_MANCHESTER : boolean := {str(caps.swo_manchester).lower()};",
        f"  constant UART_FLOW : boolean := {str(bool(caps.uart_flow)).lower()};",
        f"  constant MEM_ADDR_BITS : natural := {_mem_addr_bits(caps.mem_bytes)};",
        f"  constant EVENT_RING_BITS : natural := {caps.event_ring_bits};",
        f"  constant CAP_RATIO : natural := {caps.cap_ratio};",
        f"  constant LA_LANES : positive := {caps.la_lanes};",
        f"  constant LA_LANE_ADDR_BITS : positive := {_la_lane_addr_bits(caps)};",
        f"  constant LA_FINE_PINS : boolean := {str(caps.la_fine_pins).lower()};",
        f"  constant LA_LANE_IN_AGE : natural := {caps.la_lane_in_age};",
        f"  constant LA_TESTGEN : boolean := {str(caps.la_testgen).lower()};",
        f"  constant EVENT_BYTE_PACK : boolean := {str(caps.event_byte_pack).lower()};",
        f"  constant SAMPLER_CHECK_MAX : positive := {caps.sampler_check_max};",
        "end package;",
        "",
    ])


# ホスト側へ生成する定数
HOST_CONSTANTS = {
    "CAPS_MAGIC": int.from_bytes(b"FCAP", "little"),
    "CAPS_END": TYPE_END, "CAPS_PROTOCOL_VERSION": TYPE_PROTOCOL_VERSION, "CAPS_BOARD": TYPE_BOARD,
    "CAPS_DESIGN": TYPE_DESIGN, "CAPS_SYS_CLOCK_HZ": TYPE_SYS_CLOCK_HZ,
    "CAPS_TIMEBASE_HZ": TYPE_TIMEBASE_HZ, "CAPS_HOST_LINK": TYPE_HOST_LINK,
    "CAPS_CHANNELS": TYPE_CHANNELS, "CAPS_LA_CHANNELS": TYPE_LA_CHANNELS,
    "CAPS_LA_DEPTH": TYPE_LA_DEPTH, "CAPS_LA_SAMPLE_HZ": TYPE_LA_SAMPLE_HZ, "CAPS_MEM_BYTES": TYPE_MEM_BYTES,
    "CAPS_MEM_TEST": TYPE_MEM_TEST, "CAPS_FEATURES": TYPE_FEATURES,
    "FEATURE_XTRIG": FEATURE_XTRIG, "FEATURE_TRIG_SEQ": FEATURE_TRIG_SEQ, "FEATURE_AUX": FEATURE_AUX, "FEATURE_SAMPLER": FEATURE_SAMPLER,
    "FEATURE_LA_FILTER": FEATURE_LA_FILTER, "FEATURE_LA_PULSE": FEATURE_LA_PULSE,
    "FEATURE_LA_STREAM_EVENTS": FEATURE_LA_STREAM_EVENTS, "FEATURE_LA_STREAM_PS": FEATURE_LA_STREAM_PS, "FEATURE_LA_PACK": FEATURE_LA_PACK,
    "FEATURE_LA_TESTGEN": FEATURE_LA_TESTGEN,
    "FEATURE_FPGA_OBS": FEATURE_FPGA_OBS,
    "FEATURE_OBS_MATCH": FEATURE_OBS_MATCH, "FEATURE_SAMPLER_MATCH": FEATURE_SAMPLER_MATCH, "FEATURE_SYNC": FEATURE_SYNC,
    "FEATURE_EVENT_BYTE_PACK": FEATURE_EVENT_BYTE_PACK, "FEATURE_LA_STREAM_USAGE": FEATURE_LA_STREAM_USAGE,
    "FEATURE_DAP_LINE_TIME": FEATURE_DAP_LINE_TIME,
    "FEATURE_LA_STREAM_GAPS": FEATURE_LA_STREAM_GAPS, "FEATURE_WATCH_START": FEATURE_WATCH_START,
    "SYNC_RX_LATENCY_TICKS": SYNC_RX_LATENCY_TICKS, "SYNC_TX_LATENCY_TICKS": SYNC_TX_LATENCY_TICKS,
    "SYNC_RX_PHASE_SPAN_TICKS": SYNC_RX_PHASE_SPAN_TICKS,
    "CAPS_BUILD": TYPE_BUILD,
    "CAPS_LA_TESTGEN_HZ": TYPE_LA_TESTGEN_HZ, "CAPS_LA_FINE": TYPE_LA_FINE,
    "CAPS_LA_STREAM_BYTES_PER_S": TYPE_LA_STREAM_BYTES_PER_S,
    "CAPS_DEBUG": TYPE_DEBUG, "CAPS_LA_DELAY": TYPE_LA_DELAY, "CAPS_LA_CH_TIMING": TYPE_LA_CH_TIMING,
    "LA_CH_TIMING_UNKNOWN_SKEW": LA_CH_TIMING_UNKNOWN_SKEW, "LA_CH_TIMING_UNKNOWN_SPAN": LA_CH_TIMING_UNKNOWN_SPAN, "DEBUG_JTAG": DEBUG_JTAG, "DEBUG_INJECT": DEBUG_INJECT,
    "CAPS_LA_LAYOUT": TYPE_LA_LAYOUT, "LA_CH_NONE": LA_CH_NONE, "LA_CH_PIN": LA_CH_PIN,
    "LA_CH_INTERNAL": LA_CH_INTERNAL, "LA_CH_INPUT": LA_CH_INPUT,
    "DEBUG_SWO_MANCHESTER": DEBUG_SWO_MANCHESTER, "DEBUG_JTAG_BIT_2HP": DEBUG_JTAG_BIT_2HP,
    "DEBUG_HALF_PERIOD_1": DEBUG_HALF_PERIOD_1,
    "CAPS_UART_FLOW": TYPE_UART_FLOW,
    "CAPS_TIMING": TYPE_TIMING, "TIMING_UNKNOWN_DELAY": TIMING_UNKNOWN_DELAY, "TIMING_UNKNOWN_SPAN": TIMING_UNKNOWN_SPAN,
    **{f"TIMING_SRC_{k.upper()}": v for k, v in TIMING_SOURCES.items()},
    **{f"TIMING_POINT_{k.upper()}": v for k, v in TIMING_POINTS.items()},
    **{f"TIMING_UNIT_{k.upper()}": v for k, v in TIMING_UNITS.items()},
    **{f"TIMING_EXCLUDES_{k.upper()}": v for k, v in TIMING_EXCLUDES.items()},
    "CAPS_OUTPUTS": TYPE_OUTPUTS, "OUTPUT_CHANNEL_MASK": OUTPUT_CHANNEL_MASK, "OUTPUT_PULL_UP": OUTPUT_PULL_UP,
    "LINK_USB_HS_ULPI": LINK_USB_HS_ULPI, "LINK_SERIAL": LINK_SERIAL, "LINK_ETH": LINK_ETH,
    "CH_CONTROL": CH_CONTROL, "CH_DAP": CH_DAP, "CH_SWO": CH_SWO, "CH_UART0": CH_UART0,
    "CH_CAPTURE": CH_CAPTURE, "CH_EVENT": CH_EVENT,
}
