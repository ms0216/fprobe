"""時刻の表: 各情報源の時刻が、本当の出来事から何刻みずれるか（回路で決まる値だけ）の正。

仕様: docs/superpowers/specs/2026-11-01-time-accuracy-design.md §4.2。CSR の fprobe_map・ディスクリプタの fprobe_device と
同じ扱いで、ケイパビリティの TLV（LA_DELAY・LA_CH_TIMING・TIMING）・ホストの定数・VHDL のパッケージ（timing_pkg。RTL が
段数に使う）はここから生成する。値は回路を読んで出した段数で、gateware/tests の cocotb の試験（la_filter・la_capture・
la_cap_domain・la_deep_buffer・deep_axi・la_probe_inputs・uart_rx・uart_tx・swo_receiver・event_collector・sync_link）が
回路と一致することを確かめる。

時刻の約束（どの源も同じ）: 時刻 T の瞬間 = タイムベースの値が T になる clk の立ち上がり（時刻 T の周期 [T, T + 1) の頭）。
「点」は端子の上の出来事（入力の辺・出力の辺）。
- 出力: 時刻 T の周期の頭の辺で端子が変わるとき delay = 時刻 − T（点は 時刻 − delay ちょうど）。
- 非同期の入力: 試験で入力の辺を「時刻が T になる辺の直後」（= 周期 [T, T + 1) の中）に起こしたとき、回路が付けた時刻が T + delay。
  その周期の中のどこで変わっても同じ時刻になるので、点は 時刻 − delay 以上 時刻 − delay + span 未満のどこか（span = 位相の幅）。
  tests/tb/la_capture_tb.py・uart_rx_tb.py・la_deep_buffer_tb.py が、シミュレーションの時刻（ps）で辺と入力の変化の位置を明示して
  （tests/tb/common.py の TimebaseClock）、ホストの式（fprobe.la.pin_time・fprobe.timing）の範囲が本当の変化の時刻を含むことを
  確かめる（2026-11-02 の S1b。それまでホストは 1 刻み前の [時刻 − delay − span, 時刻 − delay) を返していた。設計書 §10.4 の 2）。
- 取り込みの領域（cap_cycle）・1 周期に複数のサンプル（fine_sample）も同じ: 細かい周期 n の瞬間 = その辺（sys の辺 T は
  細かい辺 T × 比）で、「辺 n の後に変わった」入力は n 以上 n + 1 未満。

段階 S0 でロジアナの段数・ch ごとのずれと、ボードの間の同期の段数を置いた。段階 S1 で、連続記録の起点（R1）・内部の ch の
段数（R2）・UART 受信の時刻（R3）と、情報源ごとの点の申告（TLV TIMING）を足した。段階 S1b で、多段トリガの発火の時刻・
クロストリガとロジアナの間（外部トリガの揃え・ロジアナのトリガを源にしたときの時刻）・深いバッファの詰め（la_pack）・
1 周期に複数のサンプルの構成のロジアナの軸と内部の ch の揃えを足した。段階 S2 で DAP の線上の点（R5〜R7。下の DAP_*）を
足し、TLV TIMING の情報源 dap_swd・dap_jtag で申告する（設計書 §12.4。出力の形なので span 0、点は 時刻 − delay ちょうど）。
段階 S3（設計書 §13）で、Manchester のバイトの時刻を最初のビットの中央に（R4）、ボードの出力の段（BOARD_OUTPUT_TICKS。R9）を
UART 送信・同期の出力・クロストリガの出力の端子にも入れ、補助マイコンの組の時刻（aux）とクロストリガの出力（xtrig_out・
xtrig_out_pins）の申告を足した。
"""

import struct
from dataclasses import dataclass

__all__ = ["BOARD_OUTPUT_TICKS", "DAP_POINT_JTAG", "DAP_POINT_SWD", "LA_CAP_LATENCY_BASE", "LA_DEEP_DELAY",
           "LA_DEEP_PACK_DELAY", "LA_DEEP_XTRIG_DELAY", "LA_FILTER_DELAY", "LA_FRONT_EXT_ALIGN", "LA_INTERNAL_DELAY",
           "LA_LANE_PIPE_CYCLES", "LA_PHASE_SPAN_TICKS", "LA_XTRIG_DELAY", "LaChannelTiming", "SWO_MANCHESTER_BIT_DELAY",
           "SYNC_RX_LATENCY_TICKS", "SYNC_RX_PHASE_SPAN_TICKS", "SYNC_TX_LATENCY_TICKS", "TIMING_EXCLUDES",
           "TIMING_POINTS", "TIMING_SOURCES", "TIMING_UNITS", "TIMING_UNKNOWN_DELAY", "TIMING_UNKNOWN_SPAN",
           "TRIG_SEQ_EVENT_DELAY", "TRIG_SEQ_PIN_DELAY", "TimingPoint", "UART_RX_START_DELAY", "UART_TX_START_DELAY",
           "XTRIG_LA_EXT_DELAY", "XTRIG_OUT_PIN_DELAY", "XTRIG_OUT_SOURCE_DELAY", "board_output_ticks",
           "dap_output_ticks", "la_cap_latency", "la_cap_xtrig", "la_channel_timing",
           "la_deep_lane_latency", "la_delay_ticks", "la_lane_latency", "render_package", "timing_bytes",
           "timing_points"]

# --- ロジアナ: 入り口から時刻の付くまでの段数（刻み。辺の直後に変わった入力について） -----------------------------
LA_FILTER_DELAY = 3          # la_filter: 2 段の同期化 + 1（LA_FILTER の構成の外の信号だけ。その後の la_front の同期化も残る）。
                             # 内部の ch（sys の FF の出力）は fprobe_core・la_cap_domain が同じ段数だけ遅らせて揃える（R2。la_probe_inputs）
LA_INTERNAL_DELAY = 3        # la_front → la_capture の trigger_time
LA_DEEP_DELAY = 4            # la_front → la_encoder（la_deep_buffer）の trigger_time。連続記録の起点 start_time も同じ段（R1）
LA_PHASE_SPAN_TICKS = 1      # 非同期のピンを取り込む位相の幅（同期化の最初の段がどの刻みで変化を見たかは測っていない）
LA_CAP_LATENCY_BASE = 5      # 取り込みの領域（la_cap_domain）: n = time × RATIO + phase − (この値 − RATIO)（cap の周期。RATIO <= 5）
LA_LANE_PIPE_CYCLES = 3      # 1 周期に複数のサンプル（la_lane_front）: 入力の語が sample に出るまでの段。la_lane_capture の latency =
                             # LANES × (この値 + 1) + IN_AGE（la_lane_e2e_tb が ISERDESE2 の模型を通して確かめる）

# --- クロストリガ ↔ ロジアナ（rtl/core/fprobe_core.vhd・rtl/la/la_front.vhd・la_lane_front.vhd・la_record.vhd・la_deep_buffer.vhd） ---
XTRIG_LA_EXT_DELAY = 1       # クロストリガの源のパルスの周期 S（= その源の時刻: match_time・seq_time 等）→ ロジアナの外部トリガ
                             # la_ext が 1 の周期 S + 1（fprobe_core のレジスタ 1 段）
LA_FRONT_EXT_ALIGN = 2       # la_front の probes の同期化（s1・s2）の段: 外部トリガをこの段数（＋ probes が前に通った段数）遅らせ、
                             # 外部トリガが 1 の周期に la_front の入り口にあった probes のサンプルに付ける（S1b の N3。外部トリガの
                             # トリガ時刻をピンの時間軸で外部トリガの来た周期に揃える。rate > 0 なら、その周期以後の最初のサンプル）
LA_XTRIG_DELAY = 1           # 内蔵のロジアナ（la_record）のトリガ時刻 → クロストリガの源（triggered の立ち上がりの周期。la_record の
                             # state の段）。la_capture・la_lane_capture
LA_DEEP_XTRIG_DELAY = 0      # 深いバッファ: 符号器がトリガを見た周期（triggered の立ち上がり）がトリガ時刻そのもの
LA_DEEP_PACK_DELAY = 1       # 深いバッファの詰め（la_pack、pack ≠ 0）: 語が揃った次の周期に出す段。トリガのサンプルが語の最後なら
                             # この段だけ、語の j 番目（0 = 最も古い）なら さらに (k − 1 − j) × (rate + 1) 周期遅れる（k = 1 語のサンプル数）

# --- 多段トリガ（rtl/core/trig_seq.vhd。発火の時刻 seq_time は fprobe_core が fire の周期の time を取る） ---------------
TRIG_SEQ_PIN_DELAY = 4       # trig_seq の probes の入り口の辺（辺の直後に変わった入力）→ seq_time（2 段の同期化・判定・段の更新・fire）。
                             # ピンは probes の前の段（グリッチフィルタの LA_FILTER_DELAY。内部の ch は揃えの段）がさらに足される
TRIG_SEQ_EVENT_DELAY = 2     # match・host（1 周期のパルス。その周期の time = 源の時刻）→ seq_time

# --- UART・SWO（rtl/serial/uart_rx.vhd・uart_tx.vhd、rtl/debug/swo_receiver.vhd・swo_manchester_rx.vhd） ---------------
UART_RX_START_DELAY = 2      # 開始ビットの立ち下がり → 時刻（uart_rx: 2 段の同期化の後、S_IDLE で 0 を見た周期の time。R3）。
                             # SWO の UART 方式も同じ部品。divisor・送り手の速さ・ホストへの経路の待ちに依らない
SWO_MANCHESTER_BIT_DELAY = 2  # SWO の Manchester 方式: バイトの最初（LSB）のデータビットの中央の変化 → 時刻（2 段の同期化の後、
                              # 変化を見つけた周期の time。R4、段階 S3）。ビットごとに同期し直すので送り手の速さ・divisor に依らない。
                              # S1〜S2 の回路は「最後（8 番目）のビットの中央 + 3」（点 last_bit_center）を申告していた
UART_TX_START_DELAY = 0      # UART 送信の開始ビットの立ち下がり（uart_tx の FF の出力が変わる辺）→ 時刻。受け渡しの周期 + 1（集約の 1 段）
                             # = 送信器が開始ビットを出す辺。端子はさらにボードの出力の段（BOARD_OUTPUT_TICKS）の後

# --- ボードの間の時刻の同期（rtl/core/sync_link.vhd） -------------------------------------------------------------
SYNC_RX_LATENCY_TICKS = 2    # 子: sync_in が 1 になった周期 → 組の子の時刻（sync_link の RX_LATENCY_TICKS）
SYNC_TX_LATENCY_TICKS = 1    # 親: 送る時刻を決めた周期 → sync_out が 1（同 TX_LATENCY_TICKS）
SYNC_RX_PHASE_SPAN_TICKS = 1   # 子: 非同期に来る辺を自分の時計で取り込む位相の幅。辺は sync_in が 1 になった周期の
                               # 0 以上 1 未満刻み前のどこか（どこかは測っていない。平均等の値を仮定しない）

# --- クロストリガの動作「出力の端子」（rtl/core/xtrig_out.vhd。段階 S3 の D2） ------------------------------------------
XTRIG_OUT_SOURCE_DELAY = 2   # 源のパルスの周期 S（その源の時刻）→ 出力の FF（drive）が変わる辺 S + 2（cond・drive の 2 段）。
                             # 端子はさらにボードの出力の段の後。脈（mode 0）の長さは刻み 16^unit 周期の L 個で、最初の刻みは
                             # 時刻のビット 4·unit の変化で数えるので (L − 1)·16^unit + 1 以上 L·16^unit 以下（S から 1 つに決まる。
                             # ホストの fprobe.timing.xtrig_out_window。xtrig_out_tb が確かめる）
XTRIG_OUT_PIN_DELAY = 4      # 端子の並びの条件: 見る端子の辺（辺 T の直後の変化）→ 出力の FF の辺 T + 4（同期化 2・cond・drive）

# --- DAP の転送の線上の点（段階 S2。rtl/debug/swd_phy.vhd・jtag_phy.vhd の rsp_time） ------------------------------
# 点は PHY の出力の FF（swclk / tck を駆動するレジスタ）で数える。端子に出るのは、その後のボードの出力の段の分だけ後
# （DAP_OUTPUT_TICKS）。申告は TLV TIMING の情報源 dap_swd・dap_jtag（点 = 端子の SWCLK / TCK の立ち上がり、
# delay = −DAP_OUTPUT_TICKS: 時刻（FF が変わる周期）は端子の辺より段の分だけ前。上の時刻の約束の出力の形）。ホストは
# Capabilities.dap_output_ticks（= −delay）として点に足す。点の前後の位置は設定で決まる式で、ホストの fprobe.timing
# （swd_header_to_point_ticks 等）にあり、cocotb の swd_phy_tb・jtag_phy_tb が RTL と比べる。
DAP_POINT_SWD = "swd_ack3_rise"          # ACK の 3 ビット目の Low 相が終わって SWCLK が 1 になった最初の周期（TIMING_POINTS の名前）
DAP_POINT_JTAG = "jtag_update_dr_exit"   # その転送の DR スキャンの Update-DR を出る TCK が 1 になった最初の周期（同）
# ボードごとの、コアの出力の FF から端子までの FF の段数（刻み）。fprobe の出力（SWCLK/TCK・SWDIO/TMS・TDI・UART 送信・
# クロストリガの出力の端子・同期の出力）のどれも同じ（R9。段階 S2 は DAP だけ、S3 で全部の出力）。
#   PYNQ-Z2: pynq_z2_fprobe_top.vhd の pad_o / pad_t（JA の IOB の OLOGIC。stop-precision §3.1）と sync_pad（同期の出力の
#            IOB の FF。段階 S3 で足した）で 1 段
#   それ以外（Primer 20K・Nano 9K 等）: トップに出力の FF が無い（0）
# 対象の FPGA の観測の TCK・TMS・TDI（obs_*）は含まない（端子に組み合わせで出る。観測のイベントの時刻は端子の辺と同じ。
# test_obs_e2e の test_event_times_are_the_tck_edges_on_the_pin）
BOARD_OUTPUT_TICKS = {"PYNQ-Z2": 1}              # ボードの名前（fprobe_gw.boards の Board.name）→ 段数


def board_output_ticks(board: str) -> int:
    """そのボードの、コアの出力の FF から端子までの段数（刻み）。"""
    return BOARD_OUTPUT_TICKS.get(board, 0)


def dap_output_ticks(board: str) -> int:
    """そのボードの DAP の端子の出力の段数（刻み。TLV TIMING の dap_swd・dap_jtag の delay の符号を反した値）。"""
    return board_output_ticks(board)


# ロジアナの ch の種類（caps.LA_CH_* と同じ値。caps がこのモジュールを読むので、ここでは数で持つ）
_PIN, _INTERNAL, _INPUT = 1, 2, 3

# --- TLV TIMING（情報源ごとの点の申告。docs/superpowers/specs/2026-11-01-time-accuracy-design.md §4.2） ------------------
# 1 件 8 バイト: u8 情報源・u8 点・u8 単位・u8 含まない項目（ビット）・i16 段数（時刻 = 点 + 段数。-32768 = 分からない）・
# u16 位相の幅（同じ単位。0xFFFF = 分からない）。申告の無い情報源は、ホストが持つ「その版の回路のいまの意味」で読む
# （時刻の意味が変わった種類は、同じ種類の番号のまま、この申告で区別する。設計書 §6 の判断 3）。
TIMING_SOURCES = {
    "la": 1,              # 内蔵のロジアナ（la_capture）のトリガ時刻・各サンプル（sys の構成）
    "la_deep": 2,         # 深いバッファのトリガ時刻
    "la_deep_start": 3,   # 深いバッファの連続記録のサンプル 0（CSR la_deep.start_time。R1。この申告があれば回路が記録する）
    "la_cap": 4,          # 取り込みの領域の内蔵のロジアナ（単位 cap の周期: 時刻 = time × RATIO + phase）
    "uart_rx": 5,         # UART 受信のバイト（種類 3・13）
    "uart_tx": 6,         # UART 送信のバイト（種類 4・14）
    "swo_uart": 7,        # SWO の UART 方式のバイト（種類 5・15）
    "swo_manchester": 8,  # SWO の Manchester 方式のバイト（種類 5・15）
    "sync_rx": 9,         # ボードの間の同期の組の子の時刻（種類 10）
    "sync_tx": 10,        # 同、親が線を立てる時刻（CSR sync）
    # --- 段階 S1b ---
    "la_lanes": 11,       # 1 周期に複数のサンプルの内蔵のロジアナ（la_lane_capture）: 時刻 = trigger_time × lanes + trigger_lane
                          # （単位 fine_sample。delay = CSR la_cap.info.latency と同じ値）
    "la_deep_lanes": 12,  # 同、深いバッファの垂れ流し（la_lane_encoder）: 時刻 = start_time × lanes + s × 間隔（delay = la_deep.info.latency）
    "la_ext": 13,         # 外部トリガ（クロストリガ → ロジアナ）: 源のパルスの周期 S → トリガのサンプルのピンの時間軸の時刻（下限）。
                          # 時刻 = S + delay（rate > 0 なら、さらに次のサンプルまでの待ち 0〜rate サンプル周期）
    "la_xtrig": 14,       # 内蔵のロジアナ（sys）のトリガを源にしたクロストリガ: 時刻 = 源のパルスの周期、点 = トリガのサンプルのピン
    "la_deep_xtrig": 15,  # 同、深いバッファ（詰めない: pack = 0）
    "la_deep_packed": 16, # 深いバッファの詰め（pack ≠ 0）のトリガ時刻と源: 語の最後のサンプルのときの段数。ほかは
                          # (k − 1 − j) × (rate + 1) 周期さらに遅い（ホストが書いた詰めと rate から）
    "la_cap_xtrig": 17,   # 同、取り込みの領域（単位 cap_cycle: 時刻 = 源の周期 × 比）
    "la_lanes_xtrig": 18, # 同、1 周期に複数のサンプル（単位 fine_sample: 時刻 = 源の周期 × lanes）
    "seq_pin": 19,        # 多段トリガの発火の時刻 seq_time ← パターン A・B のピン・専用の入力の変化
    "seq_internal": 20,   # 同 ← 内部の ch（sys の FF。位相の幅なし）
    "seq_input_lanes": 21,  # 同 ← 1 周期に複数のサンプルの構成の専用の入力（ISERDESE2 の最も新しいレーン。単位 fine_sample:
                            # 時刻 = seq_time × lanes）
    "seq_event": 22,      # 同 ← match・host（1 周期のパルスの周期）
    # --- 段階 S2 ---
    "dap_swd": 23,        # SWD の転送の線上の点（種類 1・7・11・12・17・18。FEATURES の DAP_LINE_TIME。段階 S2 の R5）
    "dap_jtag": 24,       # JTAG の転送の線上の点（同）
    # --- 段階 S3 ---
    "aux": 25,            # 補助マイコンの計測値（種類 6）: その組の先頭のバイトの開始ビットの辺（B2）
    "xtrig_out": 26,      # クロストリガの出力の端子 ← 源のパルス: 時刻 = 源のパルスの周期 S（その源の時刻）、点 = 端子の出力の変化
    "xtrig_out_pins": 27, # 同 ← 端子の並びの条件: 時刻 = 端子の出力の変化（記録はされない。ロジアナで見る辺との関係）、点 = 見た端子の辺
}
TIMING_POINTS = {
    "pin_edge": 1,         # 入力のピンの変化（ロジアナのサンプル）
    "start_bit_edge": 2,   # UART の開始ビットの立ち下がり（受信は端子の入力、送信は端子の出力）
    "last_bit_center": 3,  # Manchester のバイトの最後のビットの中央の変化
    "line_edge": 4,        # 同期の線の立ち上がり
    "source_pulse": 5,     # 回路の中の 1 周期のパルスの周期（クロストリガの源・多段トリガの事象。その源が記録する時刻と同じ）
    "internal_edge": 6,    # 回路の中の sys の FF が変わった辺
    "swd_ack3_rise": 7,        # SWD: ACK の 3 ビット目の Low 相の後の SWCLK の立ち上がり（端子の出力。DAP_POINT_SWD）
    "jtag_update_dr_exit": 8,  # JTAG: その転送の Update-DR を出る TCK の立ち上がり（端子の出力。DAP_POINT_JTAG）
    "first_bit_center": 9,     # Manchester のバイトの最初（LSB）のデータビットの中央の変化（段階 S3 の R4）
    "output_edge": 10,         # 端子の出力の変化（クロストリガの出力）
}
TIMING_UNITS = {"tick": 0, "cap_cycle": 1,
                "fine_sample": 2}    # 1 周期に複数のサンプルの細かいサンプル（1 / la_sample_hz。辺 n = sys の辺 T のとき n = T × lanes）
TIMING_EXCLUDES = {           # 時刻に含まない項目（分からない部分。名前で返す）
    "input_routing": 1 << 0,    # 端子 → 最初の FF の配線・入力の緩衝器（ch・配置で違う）
    "output_delay": 1 << 1,     # FF → 端子の出力の緩衝器・配線
    "target_internal": 1 << 2,  # 対象の中の遅れ（ITM・TPIU の FIFO 等）
    "cable": 1 << 3,            # 線の長さ
    "sender_rate": 1 << 4,      # 送り手の実際のビットの長さ（Manchester の開始の辺からの位置）
}
TIMING_UNKNOWN_DELAY = -0x8000
TIMING_UNKNOWN_SPAN = 0xFFFF


@dataclass(frozen=True)
class TimingPoint:
    """1 つの情報源の時刻の意味: 時刻 = 点 + delay（unit の単位）。非同期の入力は、点が 時刻 − delay 以上
    時刻 − delay + span 未満のどこか（span = 0 なら時刻 − delay ちょうど）。None = 回路で決まっていない。"""
    source: str
    point: str
    delay: int | None
    span: int | None
    unit: str = "tick"
    excludes: tuple[str, ...] = ()


def la_delay_ticks(la_filter: bool) -> tuple[int, int]:
    """ロジアナの時刻がピンの変化より遅い段数（内蔵, 深いバッファ。刻み）= ピンの時間軸の申告（TLV LA_DELAY）。
    取り込みの領域（cap_ratio > 1）の内蔵のロジアナは、この値ではなく CSR la_cap_info の latency で軸が決まる。"""
    front = LA_FILTER_DELAY if la_filter else 0
    return front + LA_INTERNAL_DELAY, front + LA_DEEP_DELAY


def la_cap_latency(ratio: int) -> int:
    """取り込みの領域の固定の遅れ（cap の周期。CSR la_cap_info.latency と同じ値）。"""
    return LA_CAP_LATENCY_BASE - ratio


def la_lane_latency(lanes: int, in_age: int) -> int:
    """1 周期に複数のサンプルの内蔵バッファの固定の遅れ（細かいサンプル。CSR la_cap.info.latency と同じ値。la_lane_capture）。"""
    return lanes * (LA_LANE_PIPE_CYCLES + 1) + in_age


def la_deep_lane_latency(lanes: int, in_age: int) -> int:
    """1 周期に複数のサンプルの深いバッファの垂れ流しの固定の遅れ（細かいサンプル。CSR la_deep.info.latency と同じ値。
    la_lane_encoder の LATENCY_FINE。tests/tb/la_deep_lanes_tb.py が回路の申告と一致を確かめる）。"""
    return lanes + in_age


# 取り込みの領域のロジアナのトリガを源にしたクロストリガ: 源の周期 S（sys）× 比 − ピンの細かい周期 n の範囲（cap の周期）の最小。
# cap → sys の受け渡しで、トリガの cap の周期が sys の周期のどこにあるかにより 比 通りに散る（範囲の幅 = 比）。la_cap_domain_tb が
# 比 2〜4 の全部の位相で確かめる
LA_CAP_XTRIG_BASE = 8


def la_cap_xtrig(ratio: int) -> tuple[int, int]:
    """取り込みの領域の la_cap_xtrig の (delay, span)（cap の周期）: n は S × 比 − delay 以上 S × 比 − delay + span 未満。"""
    return LA_CAP_XTRIG_BASE + ratio - 1, ratio


@dataclass(frozen=True)
class LaChannelTiming:
    """ロジアナの 1 本の ch の時刻。ch の変化の時刻 = 時間軸の時刻（ピンの時刻: LA_DELAY・la_cap_info で直したもの）
    + internal（内蔵のバッファ）/ deep（深いバッファ）刻み。その時刻から span 刻み未満のどこかで変わった。
    None = 回路で決まっていない・試験で確かめていない（ホストは補正せず「分からない」と返す）。"""
    internal: int | None
    deep: int | None
    span: int | None


def la_channel_timing(caps) -> list[LaChannelTiming]:
    """ch ごとの時刻（TLV LA_CH_TIMING の中身。ch の並びは caps.la_layout と同じ）。

    - ピン・専用の入力: 時間軸そのもの（0）。非同期なので位相の幅 LA_PHASE_SPAN_TICKS。
      フィルタをかけた ch（CSR la.filter）は cycles − 1 周期さらに遅れるが、それは設定なのでホストが足す（ここでは 0）。
    - 内部の ch（SWDIO を駆動中・DAP の処理中）: sys の FF の出力。フィルタのある構成では、フィルタと同じ段数の遅れを
      入れてピンと揃えてある（R2。la_probe_inputs。cocotb の la_probe_inputs_tb がピンと同じ周期に来ることを確かめる）ので 0。
      取り込みの領域（cap_ratio > 1）も、sys → cap の 1 段を含めて同じ段数に揃えてある（la_cap_domain_tb）ので 0。
      sys に同期した信号なので位相の幅は無い（0）。
    """
    from .caps import la_layout                      # caps がこのモジュールを読むので、使う時に

    if getattr(caps, "la_lanes", 1) > 1:
        return _la_lane_channel_timing(caps, la_layout(caps))
    out = []
    for b in la_layout(caps):
        kind = b >> 4
        if kind in (_PIN, _INPUT):
            out.append(LaChannelTiming(0, 0, LA_PHASE_SPAN_TICKS))
        elif kind == _INTERNAL:
            out.append(LaChannelTiming(0, 0, 0))
        else:
            out.append(LaChannelTiming(None, None, None))
    return out


def _la_lane_channel_timing(caps, layout: bytes) -> list[LaChannelTiming]:
    """1 周期に複数のサンプルの構成（la_lanes > 1。fprobe_core の la_lane_capture。docs/superpowers/specs/2026-11-01-la-resolution-design.md）。

    時間軸は細かい ch（専用の入力 = ISERDESE2 のレーン）の n = trigger_time × N + trigger_lane − latency（CSR la_cap_info）で、
    LA_DELAY ではない（取り込みの領域の構成と同じ。la_delay_ticks の注）。
    - 専用の入力: 時間軸そのもの（0）。非同期で、変化はその細かいサンプルの 1 個前の辺の後のどこか = 1 刻み未満（LA_PHASE_SPAN_TICKS）。
    - 拡張ヘッダのピン（la_fine_pins）: 専用の入力と同じ包み（xilinx_la_iserdes）の同じ段数を通るので、専用の入力と同じ（0）。
      cocotb（tests/tb/fprobe_core_lanes_tb.py）がピンと専用の入力で同じ n になることを確かめる。端子から ISERDESE2 までの
      配線の差（ch の間のずれ）は含まない（実機で測る値）。
    - 内部の ch・拡張ヘッダのピン（la_fine_pins でない）: sys の信号（ピンは sys で 2 段の同期化）を、レーン k ごとに選んだ周期の
      値にして並べる（la_lane_inputs。S1b の N2）。レーン k は細かい辺 N × w − (N − 1 − k) − IN_AGE のサンプルなので、その辺の
      前の sys の周期の値を入れると、内部の ch（sys の FF。辺 T で変わる = 細かい辺 N × T の直後）は時間軸そのもの（0、位相の幅 0）。
      sys で取ったピンは、変わった sys の周期 [T, T + 1) の次の辺 T + 1 に軸の上で来る（ずれ −1 刻み、位相の幅 1 刻み）。
      la_lane_inputs_tb が細かい辺の番号で確かめる（揃える前は内部の ch が N + IN_AGE 細かいサンプル早く見えた）。
    - 深いバッファ: 深いバッファの構成（mem_bytes > 0。fprobe_pynq_deep_fast）だけが持ち、内蔵バッファは無い（内蔵の欄は None）。
      細かい ch の時間軸は la_deep.info の式（n = start_time × N + s × 2^step × (rate + 1) − latency。la_lane_encoder）で、
      細かい ch はその軸そのもの（0）。cocotb（tests/tb/la_deep_lanes_tb.py）が端子 → ISERDESE2 の模型 → リングで確かめる。
      内部の ch・sys で取るピンは同じ la_lane_inputs を通るので、内蔵バッファと同じ。それ以外の構成は None。"""
    fine_pins = getattr(caps, "la_fine_pins", False)
    deep = bool(getattr(caps, "mem_bytes", 0))

    def row(skew: int, span: int) -> LaChannelTiming:
        return LaChannelTiming(None, skew, span) if deep else LaChannelTiming(skew, None, span)

    out = []
    for b in layout:
        kind = b >> 4
        if kind == _INPUT or (kind == _PIN and fine_pins):
            out.append(row(0, LA_PHASE_SPAN_TICKS))
        elif kind == _PIN:
            out.append(row(-1, LA_PHASE_SPAN_TICKS))
        elif kind == _INTERNAL:
            out.append(row(0, 0))
        else:
            out.append(LaChannelTiming(None, None, None))
    return out


def timing_points(caps) -> list[TimingPoint]:
    """構成が持つ情報源の点（TLV TIMING の中身）。DAP の転送・サンプラ・割り込みの点（段階 S2。R5〜R7）は dap_swd・
    dap_jtag（申告の無い古い回路は、ホストが FEATURES の DAP_LINE_TIME の無い意味 = 設計書 §2.1 で読む）。1 周期に複数の
    サンプルの構成（la_lanes > 1）のロジアナの軸は細かいサンプル（fine_sample）で、CSR（la_cap.info・la_deep.info）の latency と
    同じ値を申告する。"""
    lanes = getattr(caps, "la_lanes", 1)
    in_age = getattr(caps, "la_lane_in_age", 0)
    filt = bool(getattr(caps, "la_filter", False))
    internal, deep = la_delay_ticks(filt)
    out = []
    pin = ("input_routing",)
    has_deep = bool(caps.mem_bytes and not caps.mem_test)
    if caps.la_channels and lanes == 1:
        if caps.cap_ratio > 1:
            out.append(TimingPoint("la_cap", "pin_edge", la_cap_latency(caps.cap_ratio), 1, "cap_cycle", pin))
            out.append(TimingPoint("la_cap_xtrig", "pin_edge", *la_cap_xtrig(caps.cap_ratio), "cap_cycle", pin))
        elif caps.la_depth:
            out.append(TimingPoint("la", "pin_edge", internal, LA_PHASE_SPAN_TICKS, "tick", pin))
            out.append(TimingPoint("la_xtrig", "pin_edge", internal + LA_XTRIG_DELAY, LA_PHASE_SPAN_TICKS, "tick", pin))
        if has_deep:
            out.append(TimingPoint("la_deep", "pin_edge", deep, LA_PHASE_SPAN_TICKS, "tick", pin))
            out.append(TimingPoint("la_deep_start", "pin_edge", deep, LA_PHASE_SPAN_TICKS, "tick", pin))
            out.append(TimingPoint("la_deep_xtrig", "pin_edge", deep + LA_DEEP_XTRIG_DELAY, LA_PHASE_SPAN_TICKS, "tick",
                                   pin))
            if getattr(caps, "la_pack", False):
                out.append(TimingPoint("la_deep_packed", "pin_edge", deep + LA_DEEP_PACK_DELAY, LA_PHASE_SPAN_TICKS,
                                       "tick", pin))
    if caps.la_channels and lanes > 1:
        if caps.la_depth:
            latency = la_lane_latency(lanes, in_age)
            out.append(TimingPoint("la_lanes", "pin_edge", latency, 1, "fine_sample", pin))
            # 源 S = トリガ時刻 + LA_XTRIG_DELAY、n = トリガ時刻 × N + レーン − latency（レーン 0〜N − 1）
            out.append(TimingPoint("la_lanes_xtrig", "pin_edge", latency + LA_XTRIG_DELAY * lanes, lanes, "fine_sample",
                                   pin))
        if has_deep:
            out.append(TimingPoint("la_deep_lanes", "pin_edge", la_deep_lane_latency(lanes, in_age), 1, "fine_sample", pin))
    if caps.la_channels and (caps.la_depth or (has_deep and lanes == 1)):     # トリガ取り込みのある構成（垂れ流しだけの N 並列を除く）
        out.append(TimingPoint("la_ext", "source_pulse", XTRIG_LA_EXT_DELAY, 0, "tick", ()))
    if getattr(caps, "trig_seq", False) and caps.la_channels:
        # 多段トリガの probes は fprobe_core の la_probes（sys の構成: フィルタを通ったピンと、揃えた内部の ch。取り込みの
        # 領域・1 周期に複数のサンプルの構成: 素の値）
        front = LA_FILTER_DELAY if filt and caps.cap_ratio == 1 and lanes == 1 else 0
        out.append(TimingPoint("seq_pin", "pin_edge", TRIG_SEQ_PIN_DELAY + front, LA_PHASE_SPAN_TICKS, "tick", pin))
        out.append(TimingPoint("seq_internal", "internal_edge", TRIG_SEQ_PIN_DELAY + front, 0, "tick", ()))
        out.append(TimingPoint("seq_event", "source_pulse", TRIG_SEQ_EVENT_DELAY, 0, "tick", ()))
        if lanes > 1 and getattr(caps, "la_inputs", 0):
            # 専用の入力は最も新しいレーン（細かい辺 N × w − IN_AGE のサンプル）を sys の値として受ける: seq_time = w + 4
            out.append(TimingPoint("seq_input_lanes", "pin_edge", (TRIG_SEQ_PIN_DELAY + 1) * lanes + in_age, lanes,
                                   "fine_sample", pin))
    out_ticks = board_output_ticks(getattr(caps, "board", ""))    # ボードの無い構成の模型（試験）は 0 段
    out.append(TimingPoint("uart_rx", "start_bit_edge", UART_RX_START_DELAY, LA_PHASE_SPAN_TICKS, "tick", pin))
    out.append(TimingPoint("uart_tx", "start_bit_edge", UART_TX_START_DELAY - out_ticks, 0, "tick", ("output_delay",)))
    out.append(TimingPoint("swo_uart", "start_bit_edge", UART_RX_START_DELAY, LA_PHASE_SPAN_TICKS, "tick",
                           ("input_routing", "target_internal")))
    if getattr(caps, "swo_manchester", False):
        out.append(TimingPoint("swo_manchester", "first_bit_center", SWO_MANCHESTER_BIT_DELAY, LA_PHASE_SPAN_TICKS, "tick",
                               ("input_routing", "target_internal")))
    if getattr(caps, "aux", False):
        # 組の先頭のバイトの開始ビット（uart_rx の時刻）。補助マイコンが ADC で変換してから送り始めるまでは対象の中（分からない）
        out.append(TimingPoint("aux", "start_bit_edge", UART_RX_START_DELAY, LA_PHASE_SPAN_TICKS, "tick",
                               ("input_routing", "target_internal")))
    if getattr(caps, "sync", False):
        out.append(TimingPoint("sync_rx", "line_edge", SYNC_RX_LATENCY_TICKS, SYNC_RX_PHASE_SPAN_TICKS, "tick",
                               ("input_routing", "cable")))
        out.append(TimingPoint("sync_tx", "line_edge", -SYNC_TX_LATENCY_TICKS - out_ticks, 0, "tick",
                               ("output_delay", "cable")))
    if getattr(caps, "outputs", None):
        out.append(TimingPoint("xtrig_out", "output_edge", -XTRIG_OUT_SOURCE_DELAY - out_ticks, 0, "tick", ("output_delay",)))
        out.append(TimingPoint("xtrig_out_pins", "pin_edge", XTRIG_OUT_PIN_DELAY + out_ticks, LA_PHASE_SPAN_TICKS, "tick",
                               ("input_routing", "output_delay")))
    # DAP の線上の点（段階 S2。どの構成も SWD の PHY を持ち、JTAG は DEBUG_JTAG の構成だけ）。時刻は PHY の出力の FF が変わる
    # 周期で、端子の辺はボードの出力の段の分だけ後（delay = −段数。出力の形）
    out.append(TimingPoint("dap_swd", DAP_POINT_SWD, -out_ticks, 0, "tick", ("output_delay",)))
    if getattr(caps, "jtag", False):
        out.append(TimingPoint("dap_jtag", DAP_POINT_JTAG, -out_ticks, 0, "tick", ("output_delay",)))
    return out


def timing_bytes(caps) -> bytes:
    """TLV TIMING の中身（1 件 8 バイト。上の TIMING_* の符号）。"""
    out = bytearray()
    for p in timing_points(caps):
        excludes = 0
        for name in p.excludes:
            excludes |= TIMING_EXCLUDES[name]
        out += struct.pack("<BBBBhH", TIMING_SOURCES[p.source], TIMING_POINTS[p.point], TIMING_UNITS[p.unit], excludes,
                           TIMING_UNKNOWN_DELAY if p.delay is None else p.delay,
                           TIMING_UNKNOWN_SPAN if p.span is None else p.span)
    return bytes(out)


# VHDL のパッケージに出す定数（RTL が段数に使う・assert に使う）
_VHDL_CONSTANTS = ("LA_FILTER_DELAY", "LA_INTERNAL_DELAY", "LA_DEEP_DELAY", "LA_CAP_LATENCY_BASE", "LA_LANE_PIPE_CYCLES",
                   "LA_FRONT_EXT_ALIGN", "XTRIG_LA_EXT_DELAY", "UART_RX_START_DELAY",
                   "SWO_MANCHESTER_BIT_DELAY", "UART_TX_START_DELAY", "SYNC_RX_LATENCY_TICKS", "SYNC_TX_LATENCY_TICKS")


def render_package() -> str:
    """時刻の表の VHDL パッケージ（rtl/core/generated/timing_pkg.vhd）。"""
    g = globals()
    lines = [
        "-- 自動生成ファイル。直接編集しないこと（python -m fprobe_gw gen で再生成）。",
        "-- 生成元: gateware/fprobe_gw/timing.py（時刻の表。各情報源の時刻が本当の出来事から何刻みずれるか）",
        "-- 仕様: docs/superpowers/specs/2026-11-01-time-accuracy-design.md §4.2",
        "package timing_pkg is",
        *(f"  constant {name} : integer := {g[name]};" for name in _VHDL_CONSTANTS),
        "end package;",
        "",
    ]
    return "\n".join(lines)
