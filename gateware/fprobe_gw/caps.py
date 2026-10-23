"""ケイパビリティ（デバイスが申告する機能・構成）の定義と TLV 符号化。

仕様: docs/superpowers/specs/2026-10-15-m3-control-plane-design.md §6
"""

import struct
from dataclasses import dataclass

from .csr.fprobe_map import MEM_BEAT_BYTES, FPROBE_MAP, LA_WIDTH, UART_FLOW_PIN_CHOICES, XOUT_ACTIONS, XOUT_PIN_CHOICES

__all__ = ["Capabilities", "HOST_CONSTANTS", "encode", "la_layout", "render_package"]

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
FEATURE_FPGA_OBS = 1 << 7    # 対象の FPGA の観測（CSR obs・イベント 8・9。大きい FPGA の構成だけ）
FEATURE_OBS_MATCH = 1 << 8   # 観測の値の一致をクロストリガの源 bit4 に（CSR obs.match。観測の口とクロストリガがある構成）
FEATURE_SAMPLER_MATCH = 1 << 10   # サンプラの値の一致をクロストリガの源 bit5 に（CSR sampler_match。R21）
FEATURE_SYNC = 1 << 9        # ボードの間の時刻の同期（CSR sync・イベント 10・端子 sync_in / sync_out）
SYNC_RX_LATENCY_TICKS = 2    # 子: sync_in が 1 になった周期 → 組の子の時刻（rtl/core/sync_link.vhd の RX_LATENCY_TICKS）
SYNC_TX_LATENCY_TICKS = 1    # 親: 送る時刻を決めた周期 → sync_out が 1（同 TX_LATENCY_TICKS）
SYNC_RX_PHASE_TICKS = 0.5    # 子: 非同期に来る辺を自分の時計で取り込む位相の平均（ロジアナの申告の遅れの 0.5 と同じ。
                             # 2026-10-22 の実機で、これを入れないと子の出来事が約 0.5 刻み早く出た）
TYPE_DEBUG = 14              # u32 ビットマップ（DEBUG_*。デバッグポートの構成。無い古いデバイスは全部ある）
DEBUG_JTAG = 1 << 0          # JTAG の PHY（無ければ DAP_Connect の JTAG は失敗）
DEBUG_INJECT = 1 << 1        # 故障注入 = 割り込み書き込み（swd_injector。xtrig の inject_*）
DEBUG_SWO_MANCHESTER = 1 << 2   # SWO の Manchester 方式（U51。DAP_Info の Capabilities bit3 と同じ）。古いデバイスは無い
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
TYPE_LA_DELAY = 19           # ロジアナの時刻（trigger_time）がピンの変化より遅い分: u16 × 2（内蔵・深いバッファ）、
                             # 半刻みの単位（非同期のピンの同期化の平均 0.5 刻みを含む）。無い古いデバイスは 0（直さない）
# ロジアナの入り口から時刻の付くまでの段数（刻み。辺の直後に変わった入力について。gateware/tests の
# la_filter・la_capture・deep_axi の試験が回路と一致することを確かめる）
LA_FILTER_DELAY = 3          # la_filter: 2 段の同期化 + 1（LA_FILTER の構成だけ。その後の la_front の同期化も残る）
LA_INTERNAL_DELAY = 3        # la_front → la_capture の trigger_time
LA_DEEP_DELAY = 4            # la_front → la_encoder（la_deep_buffer）の trigger_time
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
    # UART の RTS/CTS（U52）: 端子の候補 j のロジアナのチャンネル（flow.rts_pin・cts_pin の番号の順）。空なら回路が無い
    uart_flow: tuple[int, ...] = ()
    # 外部メモリの無い構成のイベント記録の件数（2**event_ring_bits、0 = 無し）。ROM には入れない（PC は EVENTS_CAPACITY を
    # 読む）。回路の generic（生成パッケージの EVENT_RING_BITS）だけに使う
    event_ring_bits: int = 0

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
        if self.event_ring_bits and self.mem_bytes:
            raise ValueError("event_ring_bits は外部メモリの無い構成だけ（外部メモリのある構成は深いバッファに記録する）")
        if self.inject and not self.xtrig:
            raise ValueError("故障注入（inject）はクロストリガ（xtrig）の一部: xtrig=False なら inject=False")
        if len(self.outputs) > XOUT_PIN_CHOICES:
            raise ValueError(f"出力の端子の候補は {XOUT_PIN_CHOICES} 本まで")
        if not 1 <= self.output_actions <= XOUT_ACTIONS:
            raise ValueError(f"出力の動作は 1〜{XOUT_ACTIONS} 本")
        if any(not 0 <= ch <= OUTPUT_CHANNEL_MASK for ch, _ in self.outputs):
            raise ValueError("出力の端子のチャンネルが範囲外")
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


def la_delay_halves(caps: Capabilities) -> tuple[int, int]:
    """ロジアナの時刻がピンの変化より遅い分（内蔵, 深いバッファ）。半刻みの単位、非同期の平均 0.5 刻みを含む。"""
    front = LA_FILTER_DELAY if caps.la_filter else 0
    return 2 * (front + LA_INTERNAL_DELAY) + 1, 2 * (front + LA_DEEP_DELAY) + 1


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
                                | FEATURE_FPGA_OBS * caps.fpga_obs
                                | FEATURE_OBS_MATCH * (caps.fpga_obs and caps.xtrig)
                                | FEATURE_SYNC * caps.sync
                                | FEATURE_SAMPLER_MATCH * (caps.sampler_match and caps.sampler and caps.xtrig))),
        _tlv(TYPE_DEBUG, u32(DEBUG_JTAG * caps.jtag | DEBUG_INJECT * caps.inject
                             | DEBUG_SWO_MANCHESTER * caps.swo_manchester)),
        *([_tlv(TYPE_OUTPUTS, bytes([caps.output_actions,
                                     *(ch | OUTPUT_PULL_UP * pull_up for ch, pull_up in caps.outputs)]))]
          if caps.outputs else []),
        *([_tlv(TYPE_UART_FLOW, bytes(caps.uart_flow))] if caps.uart_flow else []),
        *([_tlv(TYPE_LA_DELAY, struct.pack("<HH", *la_delay_halves(caps)))] if caps.la_channels else []),
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
        f"  constant OBS_PORT : boolean := {str(caps.fpga_obs).lower()};",
        f"  constant SYNC : boolean := {str(caps.sync).lower()};",
        f"  constant XOUT_COUNT : natural := {caps.output_actions if caps.outputs else 0};",
        f"  constant LA_INPUTS : natural := {caps.la_inputs};",
        f"  constant SWO_MANCHESTER : boolean := {str(caps.swo_manchester).lower()};",
        f"  constant UART_FLOW : boolean := {str(bool(caps.uart_flow)).lower()};",
        f"  constant MEM_ADDR_BITS : natural := {_mem_addr_bits(caps.mem_bytes)};",
        f"  constant EVENT_RING_BITS : natural := {caps.event_ring_bits};",
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
    "FEATURE_LA_STREAM_EVENTS": FEATURE_LA_STREAM_EVENTS, "FEATURE_FPGA_OBS": FEATURE_FPGA_OBS,
    "FEATURE_OBS_MATCH": FEATURE_OBS_MATCH, "FEATURE_SAMPLER_MATCH": FEATURE_SAMPLER_MATCH, "FEATURE_SYNC": FEATURE_SYNC,
    "SYNC_RX_LATENCY_TICKS": SYNC_RX_LATENCY_TICKS, "SYNC_TX_LATENCY_TICKS": SYNC_TX_LATENCY_TICKS,
    "SYNC_RX_PHASE_TICKS": SYNC_RX_PHASE_TICKS,
    "CAPS_BUILD": TYPE_BUILD,
    "CAPS_DEBUG": TYPE_DEBUG, "CAPS_LA_DELAY": TYPE_LA_DELAY, "DEBUG_JTAG": DEBUG_JTAG, "DEBUG_INJECT": DEBUG_INJECT,
    "CAPS_LA_LAYOUT": TYPE_LA_LAYOUT, "LA_CH_NONE": LA_CH_NONE, "LA_CH_PIN": LA_CH_PIN,
    "LA_CH_INTERNAL": LA_CH_INTERNAL, "LA_CH_INPUT": LA_CH_INPUT,
    "DEBUG_SWO_MANCHESTER": DEBUG_SWO_MANCHESTER, "CAPS_UART_FLOW": TYPE_UART_FLOW,
    "CAPS_OUTPUTS": TYPE_OUTPUTS, "OUTPUT_CHANNEL_MASK": OUTPUT_CHANNEL_MASK, "OUTPUT_PULL_UP": OUTPUT_PULL_UP,
    "LINK_USB_HS_ULPI": LINK_USB_HS_ULPI, "LINK_SERIAL": LINK_SERIAL, "LINK_ETH": LINK_ETH,
    "CH_CONTROL": CH_CONTROL, "CH_DAP": CH_DAP, "CH_SWO": CH_SWO, "CH_UART0": CH_UART0,
    "CH_CAPTURE": CH_CAPTURE, "CH_EVENT": CH_EVENT,
}
