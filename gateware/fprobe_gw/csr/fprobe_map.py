"""fprobe の CSR マップ（単一の定義元）。

変更したら `python -m fprobe_gw gen` で生成物を更新する（tests/test_generated.py が一致を検査する）。
"""

from ..events import EVENT_KINDS
from ..protocol import PROTOCOL_VERSION
from .model import Block, CsrMap, Field, Register

__all__ = ["EVENT_KIND_LA_GAP", "FPROBE_MAP", "LA_DEPTH", "LA_LANES_MAX", "LA_LANES_MAX_WORDS", "LA_LANES_MEM_WORDS", "LA_WIDTH", "MAGIC", "MEM_BEAT_BYTES", "STREAM_SEG_EVENTS", "STREAM_SEG_LA",
           "STREAM_PL_CHUNK_BEATS", "STREAM_SEG_MAGIC", "STREAM_SEG_MAX_BEATS", "UART_FLOW_PIN_CHOICES", "XOUT_ACTIONS", "XOUT_PIN_CHOICES"]

MAGIC = int.from_bytes(b"FPRB", "little")

# ロジアナ（fprobe_core の LA_WIDTH / LA_DEPTH と一致させる）
LA_WIDTH = 16
LA_DEPTH = 8192
# 1 周期に複数のサンプルの構成の内蔵バッファの窓（ワード）: レーン 2 本で 1 ワード、最大 8 レーン × 16384 語
LA_LANES_MAX = 8
LA_LANES_MAX_WORDS = 16384
LA_LANES_MEM_WORDS = LA_LANES_MAX // 2 * LA_LANES_MAX_WORDS

# クロストリガの動作「出力の端子」: 同時に動かせる動作の数（CSR の when・ctrl の組）と、端子の候補の数の上限
# （ctrl.pin の幅。fprobe_core の xout_* と一致させる。B22: 資源のため動作は 2 本）
XOUT_ACTIONS = 2
XOUT_PIN_CHOICES = 4

# UART の RTS/CTS（U52）の端子の候補の数の上限（flow.rts_pin・cts_pin の幅）
UART_FLOW_PIN_CHOICES = 4

# 外部メモリの 1 拍（mem_port のデータ幅）
MEM_BEAT_BYTES = 16

# ストリームの見出しの拍（ロジアナとイベントを 1 本に流す構成。la_deep_buffer の STREAM_EVENTS と一致させる。
# docs/superpowers/specs/2026-10-19-r1-unified-stream-design.md §3）: [7:0] 種類、[31:16] 中身の拍数、
# [63:32] 通し番号、[127:64] 印
STREAM_SEG_MAGIC = int.from_bytes(b"FPRBSEG\0", "little")
STREAM_SEG_LA = 1             # ロジアナの拍
STREAM_SEG_EVENTS = 2         # イベント（1 件 = 1 拍）
# 1 つのかたまりの中身の拍数の上限（ホストが受け付ける上限。見出しの拍数の欄は 16 ビット）。作る側ごとの上限:
#   - PL（la_deep_buffer の STREAM_CHUNK。窓経由の流れ）と、PYNQ の中継の複写の道（relay.h の RELAY_PS_CHUNK_MAX）: STREAM_PL_CHUNK_BEATS
#   - PYNQ の中継のゼロコピーの道（relay.h の RELAY_ZC_CHUNK_MAX = 64 KB）: これ。見出しを 4 KB ごとに挟むとゼロコピーの効果が消える
#     （docs/superpowers/specs/2026-11-01-bandwidth-observation-design.md 1.7.2・1.7.6）ので、PS 直読みの道だけかたまりを大きくする
STREAM_SEG_MAX_BEATS = 4096
STREAM_PL_CHUNK_BEATS = 256   # PL と中継の複写の道が作るかたまりの中身の拍数の上限

# ロジアナの垂れ流しの欠けの記録のイベントの種類（la_stream_gap の KIND_LA_GAP と一致させる。
# docs/superpowers/specs/2026-11-01-la-deep-fast-design.md §12 段 B）: [47:0] 拍を捨て始めた時刻、[79:48] 欠けの前に出した
# ロジアナの拍の数（流れの開始から。32 ビットで折り返す）、[119:80] 捨てたサンプルの数、[127:120] 種類
EVENT_KIND_LA_GAP = EVENT_KINDS["LA_GAP"][0]   # 定義元は fprobe_gw/events.py（la-deep-fast §12.7）

FPROBE_MAP = CsrMap(blocks=(
    Block("system", 0x0000, 0x40, doc="識別と疎通確認", registers=(
        Register.word("magic", 0x00, "CONST", MAGIC, doc='"FPRB"（リトルエンディアン）'),
        Register.word("version", 0x04, "CONST", PROTOCOL_VERSION, doc="CONTROL プロトコルの版"),
        Register.word("scratch", 0x08, "RW", doc="読み書き試験用（機能なし）"),
    )),
    Block("timebase", 0x0100, 0x40, doc="64 ビットのタイムベース（周波数はケイパビリティ TIMEBASE_HZ）", registers=(
        Register.word("time_lo", 0x00, "RO", doc="下位 32 ビット。読むと同時に上位を time_hi に保持する",
                      read_strobe=True),
        Register.word("time_hi", 0x04, "RO", doc="time_lo を読んだ時点の上位 32 ビット"),
    )),
    Block("gpio", 0x0200, 0x40, doc="拡張ヘッダ（Primer 20K は J12）の 8 本の汎用入出力。配線確認・ビットバング用",
          registers=(
        Register("drive", 0x00, (Field("drive", 0, 8, "RW"),), doc="出力値（ビット n = ピン n）"),
        Register("enable", 0x04, (Field("enable", 0, 8, "RW"),), doc="出力を有効にする（0 で Hi-Z。プルはボードの定義: Primer は 4・7 番がプル無し、ほかはプルアップ）"),
        Register("level", 0x08, (Field("level", 0, 8, "RO"),), doc="ピンの入力値（2 段で同期化）"),
        Register("io", 0x0C, (Field("io", 0, 8, "RO"),), write_to="drive",
                 doc="読むと level、書くと drive（マイコンのポートのレジスタと同じ）。CONTROL の EXCHANGE で 1 要求の中で"
                     "書いて読むを繰り返す（ビットバングの SPI 等。U42）。読みは同期化の 2 周期分、書く前の端子のことがある"),
    )),
    Block("uart", 0x0300, 0x40, doc="UART ブリッジ（CDC-ACM ⇄ ターゲット）の状態", registers=(
        Register.word("baud", 0x00, "RO", doc="現在のボーレート（COM ポートの設定に追従）"),
        Register.word("frame_errors", 0x04, "RO", doc="停止ビットが 0 だった受信の数"),
        Register.word("overruns", 0x08, "RO", doc="取り出される前に次が揃って失った受信の数"),
        Register("flow", 0x0C,
                 (Field("auto", 0, 1, "RW", doc="自動の RTS/CTS: CTS が出ている間だけ送り始め、受信の下流が満杯なら RTS を"
                                                 "下げる（0: RTS は COM ポートの RTS どおり、CTS は見ない）"),
                  Field("rts_claim", 4, 1, "RW", doc="RTS を端子の候補 rts_pin に出す（その端子の機能の出力は止める）"),
                  Field("rts_pin", 8, 2, "RW", doc="RTS の端子（ケイパビリティ UART_FLOW の候補の番号）"),
                  Field("cts_claim", 12, 1, "RW", doc="CTS を端子の候補 cts_pin から読む（その端子の機能の出力は止める）。"
                                                      "0 なら CTS は出ているとみなす"),
                  Field("cts_pin", 16, 2, "RW", doc="CTS の端子（ケイパビリティ UART_FLOW の候補の番号）")),
                 doc="ハードウェアのフロー制御（U52。回路の無い構成では読み書きできるが何もしない）。線はアクティブ Low"),
        Register("lines", 0x10,
                 (Field("cts", 0, 1, "RO", doc="CTS が出ている（端子が Low。cts_claim = 0 なら 1）"),
                  Field("rts", 1, 1, "RO", doc="RTS を出している（端子を Low に駆動する値）"),
                  Field("host_rts", 2, 1, "RO", doc="COM ポートの RTS（CDC-ACM の SET_CONTROL_LINE_STATE）"),
                  Field("host_break", 3, 1, "RO", doc="ブレークを送っている（CDC-ACM の SEND_BREAK）")),
                 doc="制御線の状態"),
    )),
    Block("la", 0x0400, 0x40, doc="ロジックアナライザの設定と状態（docs/superpowers/specs/2026-10-16-m7-logic-analyzer-design.md）",
          registers=(
        Register("control", 0x00, (Field("run", 0, 1, "RW", doc="0→1 で捕捉開始、1→0 で中止"),)),
        Register("rate", 0x04, (Field("rate", 0, 16, "RW"),), doc="サンプル間隔 - 1（sys クロック周期）"),
        Register.word("pretrigger", 0x08, "RW", doc="トリガ前に残すサンプル数（深さ - 1 まで）"),
        Register("trig_mask", 0x0C, (Field("trig_mask", 0, LA_WIDTH, "RW"),), doc="値一致で見るチャンネル"),
        Register("trig_value", 0x10, (Field("trig_value", 0, LA_WIDTH, "RW"),), doc="値一致の値"),
        Register("trig_rise", 0x14, (Field("trig_rise", 0, LA_WIDTH, "RW"),), doc="立ち上がりで成立するチャンネル"),
        Register("trig_fall", 0x18, (Field("trig_fall", 0, LA_WIDTH, "RW"),), doc="立ち下がりで成立するチャンネル"),
        Register("status", 0x1C, (Field("capturing", 0, 1, "RO"), Field("triggered", 1, 1, "RO"),
                                  Field("done", 2, 1, "RO")), doc="捕捉の状態"),
        Register.word("start_index", 0x20, "RO", doc="完了後、最も古いサンプルのバッファ位置"),
        Register.word("trigger_index", 0x24, "RO", doc="トリガのサンプルのバッファ位置"),
        Register.word("trigger_time_lo", 0x28, "RO", doc="トリガ時のタイムベース（下位）"),
        Register.word("trigger_time_hi", 0x2C, "RO", doc="トリガ時のタイムベース（上位）"),
        # U21（ケイパビリティ FEATURES の LA_FILTER / LA_PULSE がある構成だけ働く。無ければ書いても何もしない）
        Register("filter", 0x30, (Field("channels", 0, LA_WIDTH, "RW",
                                        doc="グリッチフィルタをかける ch（ビット n = ch n。外部の入力 = 拡張ヘッダの"
                                            "ピンと専用の入力（ケイパビリティ LA_LAYOUT）だけ効く。内部の信号は通さない）"),
                                  Field("cycles", 16, 8, "RW",
                                        doc="通す最短の脈（sys の周期）。それより短い変化を捨てる。0・1 はフィルタなし")),
                 doc="グリッチフィルタ（ロジアナ・多段トリガ・パルス幅の前。フィルタした ch は cycles - 1 周期遅れる）"),
        Register("pulse", 0x34, (Field("mode", 0, 2, "RW",
                                       doc="0 なし、1 幅（脈の終わりに、幅が pulse_min〜pulse_max なら成立）、"
                                           "2 一定時間変化しない（level が pulse_min 周期を超えて続いたら成立）"),
                                 Field("level", 4, 1, "RW", value=1, doc="脈の値（1 = High の脈）"),
                                 Field("channel", 8, 4, "RW", doc="見る ch")),
                 doc="パルス幅のトリガ。mode が 0 でなければ、トリガ = パターンの条件 かつ パルス幅の成立"),
        Register.word("pulse_min", 0x38, "RW", doc="パルス幅の下限（sys の周期）"),
        Register.word("pulse_max", 0x3C, "RW", 0xFFFFFFFF, doc="パルス幅の上限（sys の周期。全ビット 1 = 上限なし）"),
    )),
    Block("la_cap", 0x0C80, 0x40,
          doc="ロジアナの取り込みを sys の整数倍の速いクロックの領域で動かす構成（docs/superpowers/specs/2026-10-30-la-capture-clock-design.md）と、"
              "端子の直並列変換で sys の 1 周期に複数のサンプルを取る構成（2026-11-01-la-resolution-design.md）だけの情報。"
              "無い構成（比 1）では読むとエラー",
          registers=(
        Register("info", 0x00, (Field("ratio", 0, 4, "RO",
                                      doc="細かいサンプル（取り込み）の周期の数 / sys の周期（整数比。la_sample_hz / sys_clock_hz と同じ）"),
                                Field("trigger_phase", 8, 4, "RO",
                                      doc="最後のトリガのサンプルの、sys の周期の中での番号（0〜比 − 1。lanes > 1 ならトリガの語の中のレーン）。"
                                          "ピンが変わった細かいサンプルの番号 = trigger_time × 比 + trigger_phase − latency"),
                                Field("latency", 16, 8, "RO",
                                      doc="固定の遅れ（細かいサンプルの数。ピンが変わってからサンプルを取り、時刻を付けるまでの段数）"),
                                Field("lanes", 24, 4, "RO",
                                      doc="内蔵バッファの 1 語のサンプルの数。1 = 取り込みの領域で 1 周期 1 サンプル（la_mem に 1 サンプル 1 ワード）。"
                                          "2 以上 = 端子の直並列変換で sys の 1 周期に lanes サンプル（= 比。バッファは la_lanes_mem、"
                                          "pretrigger・start_index・trigger_index は語の数、la.rate は使わない。"
                                          "docs/superpowers/specs/2026-11-01-la-resolution-design.md）")),
                 doc="取り込みのクロックの比とトリガの位相"),
        Register("control", 0x04, (Field("selftest", 0, 1, "RW",
                                         doc="1 = ch LA_PIN_COUNT + 1 に、dap_busy の代わりに sys の周期ごとに反転する内部の信号を入れる"
                                             "（配線なしで取り込みの周波数を確かめる。取り込みの周期の数 / sys の周期 = 比のとき、同じ値が比個ずつ続く）"),),
                 doc="試験用の内部信号"),
    )),
    Block("la_testgen", 0x0CC0, 0x40,
          doc="外部ピン入力の限界を測る・流れる中身を確かめる試験用の方形波（ケイパビリティ la_testgen のある構成だけ。無い構成では読むとエラー。"
              "docs/superpowers/specs/2026-11-01-la-testgen-deep-design.md）。刻むクロックは、取り込みが速い構成（比 > 1）なら cap_clk、"
              "そうでなければ sys",
          registers=(
        Register("control", 0x00, (Field("enable", 0, 1, "RW",
                                         doc="1 = 試験用の方形波をピン（PYNQ: RPi ヘッダ 32 番）に出す。0 = 駆動しない（Hi-Z）"),
                                   Field("edge", 1, 1, "RW",
                                         doc="0 = 刻むクロックの立ち上がりで変える、1 = 立ち下がりで変える（半周期遅れる。位相の偏りの見落としを減らす）")),
                 doc="方形波の有効化"),
        Register.word("period", 0x04, "RW", 2,
                      doc="方形波の周期（刻むクロックの周期の数。周波数 = 刻むクロック / この値。2 未満は 2、4095 超は 4095 に丸める。回路は 12 ビット）"),
        Register.word("high", 0x08, "RW", 1,
                      doc="方形波の 1 の周期の数（デューティ = この値 / 周期。0 は 1、周期以上は周期 − 1 に丸める）"),
    )),
    Block("xtrig", 0x0500, 0x40,
          doc="クロストリガ（docs/superpowers/specs/2026-10-16-m9-cross-trigger-design.md）。"
              "源: bit0 = ロジアナのトリガ、bit1 = DAP の番地一致、bit2 = ホストの指令（command.fire）、"
              "bit3 = 多段トリガ（trigseq）、bit4 = FPGA の観測の値の一致（obs.match）、"
              "bit5 = サンプラの値の一致（sampler_match。構成によって無い）",
          registers=(
        Register("route", 0x00, (Field("to_la", 0, 6, "RW", doc="ロジアナをトリガする源"),
                                 Field("to_inject", 8, 6, "RW", doc="割り込み書き込み（既定は停止）をする源")),
                 doc="源 → 動作の対応"),
        Register("command", 0x04, (Field("fire", 0, 1, "PULSE", doc="ホストの指令（源 bit2）"),),
                 doc="1 を書くと源 bit2 を 1 回発火"),
        Register("inject_ap", 0x08, (Field("inject_ap", 0, 8, "RW"),), doc="割り込み書き込みの MEM-AP 番号"),
        Register.word("inject_addr", 0x0C, "RW", 0xE000EDF0, doc="割り込み書き込みの番地（既定 DHCSR）"),
        Register.word("inject_value", 0x10, "RW", 0xA05F0003, doc="割り込み書き込みの値（既定 C_HALT | C_DEBUGEN）"),
        Register("inject_status", 0x14, (Field("busy", 0, 1, "RO"),
                                         Field("result", 4, 2, "RO", doc="最後の要求: 0 成功 / 1 SWD の異常 / 2 未接続"),
                                         Field("ack", 8, 3, "RO", doc="最後の SWD 応答"),
                                         Field("count", 16, 16, "RO", doc="成功した回数")),
                 doc="割り込み書き込みの状態"),
        Register.word("inject_time_lo", 0x18, "RO",
                      doc="最後に書き込みが受理された語の DRW の転送の線上の点のタイムベース（下位。SWD は ACK の後の立ち上がり、"
                          "JTAG は Update-DR を出る立ち上がり。イベント 11 と同じ。docs/superpowers/specs/2026-11-01-time-accuracy-design.md R5）"),
        Register.word("inject_time_hi", 0x1C, "RO", doc="同（上位）"),
        Register.word("match_addr", 0x20, "RW", doc="番地一致: 番地"),
        Register.word("match_addr_mask", 0x24, "RW", 0xFFFFFFFF, doc="番地一致: 比べるビット"),
        Register.word("match_data", 0x28, "RW", doc="番地一致: 書き込み値"),
        Register.word("match_data_mask", 0x2C, "RW", doc="番地一致: 比べる値のビット（0 なら値を見ない）"),
        Register("match_mode", 0x30, (Field("on_write", 0, 1, "RW"), Field("on_read", 1, 1, "RW")),
                 doc="番地一致の向き（ホストの DRW アクセス）"),
        Register.word("match_count", 0x34, "RO", doc="一致した回数"),
        Register.word("match_time_lo", 0x38, "RO",
                      doc="最後に一致した転送の線上の点のタイムベース（下位。イベント 1 と同じ点。time-accuracy R5）"),
        Register.word("match_time_hi", 0x3C, "RO", doc="同（上位）"),
    )),
    Block("inject", 0x0540, 0x40,
          doc="割り込み書き込みの語の列（docs/superpowers/specs/2026-10-29-halt-watch-design.md §3.2）。語 0 は xtrig の "
              "inject_addr / inject_value、語 1〜extra はこの表。要求 1 回で語 0〜extra を順に書き、最後に CTRL/STAT を読んで"
              "書き込みの誤りを確かめる（JTAG-DP は FAULT を返さない）。故障注入のある構成だけ（無ければ読むとエラー）",
          registers=(
        Register("control", 0x00, (Field("extra", 0, 3, "RW", doc="語 0 の後に書く表の語の数（0〜7。止めてから変える）"),),
                 doc="語の数"),
        Register("entry", 0x04, (Field("entry", 0, 3, "RW", doc="書く表の位置（1〜7）"),), doc="表に書く位置"),
        Register.word("addr", 0x08, "RW", doc="書く語の番地"),
        Register.word("value", 0x0C, "RW", doc="書く語の値"),
        Register("entry_write", 0x10, (Field("entry_write", 0, 1, "PULSE", doc="1 で addr・value を表の entry に書く"),),
                 doc="表への書き込み（表は分散 RAM。読み返せない）"),
        Register("status", 0x14, (Field("written_words", 0, 4, "RO",
                                        doc="最後の要求で受理された DRW の書き込みの数（失敗なら、最後に受理された語が誤りのことがある）"),),
                 doc="状態"),
        Register.word("first_time_lo", 0x18, "RO",
                      doc="クロストリガの最後の要求で最初の語の DRW の書き込みの線上の点のタイムベース（下位。inject_time と同じ点。"
                          "始動の列では変わらない）"),
        Register.word("first_time_hi", 0x1C, "RO", doc="同（上位）。最後の語は xtrig の inject_time"),
        # 始動の列（docs/superpowers/specs/2026-11-03-halt-watch-fpga-restart-design.md §3.2）。FEATURES の WATCH_START がある構成だけ
        Register("start", 0x20, (Field("first", 0, 3, "RW", doc="始動の列の最初の語（0〜7。0 は xtrig の inject_addr / inject_value）"),
                                 Field("last", 4, 3, "RW", doc="始動の列の最後の語（first 以上。first より小さいと first の 1 語）"),
                                 Field("go", 8, 1, "PULSE",
                                       doc="1 で始動の列を要求する（割り込み書き込みが空いたら始める。クロストリガの要求と同時なら後）")),
                 doc="始動の列: ホストの要求で表の語 first〜last を、クロストリガの要求と同じ書き方で書く。成功すると "
                     "sampler_match の門（control.after_start）を開ける"),
        Register("start_status", 0x24, (Field("pending", 0, 1, "RO", doc="要求してから終わるまで 1"),
                                        Field("result", 4, 2, "RO", doc="最後の始動の列: 0 成功 / 1 SWD の異常 / 2 未接続"),
                                        Field("written_words", 8, 4, "RO",
                                              doc="最後の始動の列で受理された DRW の書き込みの数（失敗なら、最後に受理された語が"
                                                  "誤りのことがある）"),
                                        Field("count", 16, 16, "RO", doc="終わった始動の列の数（結果によらない）")),
                 doc="始動の列の状態"),
        Register.word("start_first_time_lo", 0x28, "RO",
                      doc="最後の始動の列で最初に受理された語の DRW の線上の点のタイムベース（下位）"),
        Register.word("start_first_time_hi", 0x2C, "RO", doc="同（上位）"),
        Register.word("start_last_time_lo", 0x30, "RO",
                      doc="最後の始動の列で最後に受理された語の DRW の線上の点のタイムベース（下位）"),
        Register.word("start_last_time_hi", 0x34, "RO", doc="同（上位）"),
    )),
    Block("mem", 0x0600, 0x40,
          doc="外部メモリ（DDR3 等）の状態とメモリ試験（docs/superpowers/specs/2026-10-16-m8-ddr3-design.md）。"
              "番地・数は拍（MEM_BEAT_BYTES バイト）単位", registers=(
        Register("status", 0x00, (Field("ready", 0, 1, "RO", doc="初期化・較正が済んだ"),
                                  Field("busy", 1, 1, "RO", doc="メモリ試験の実行中"),
                                  Field("done", 2, 1, "RO", doc="メモリ試験が終わった（結果が有効）"),
                                  Field("in_reset", 3, 1, "RO", doc="コントローラがリセット中（診断用）"),
                                  Field("clock_tick", 4, 1, "RO",
                                        doc="メモリ側クロックで数えた計数の 1 ビット（約 10 ms ごとに反転すれば動いている）")),
                 doc="メモリの状態"),
        Register("command", 0x04, (Field("test", 0, 1, "PULSE", doc="メモリ試験を始める"),)),
        Register.word("test_base", 0x08, "RW", doc="試験の先頭の拍"),
        Register.word("test_count", 0x0C, "RW", 1, doc="試験する拍の数（1 以上）"),
        Register.word("test_seed", 0x10, "RW", doc="パターンの種"),
        Register.word("errors", 0x14, "RO", doc="読み値が合わなかった拍の数"),
        Register.word("first_error", 0x18, "RO", doc="最初に合わなかった拍"),
        Register.word("write_cycles", 0x1C, "RO", doc="書き込みにかかったメモリクロックの周期数"),
        Register.word("read_cycles", 0x20, "RO", doc="読み出しにかかったメモリクロックの周期数"),
    )),
    Block("la_deep", 0x0700, 0x80,
          doc="ロジアナの深いバッファ（外部メモリ。docs/superpowers/specs/2026-10-16-m8-ddr3-design.md §5）。"
              "前段の設定（rate・trig_*）は la ブロックと共通。番地・数は拍（MEM_BEAT_BYTES バイト）単位",
          registers=(
        Register("control", 0x00, (Field("run", 0, 1, "RW", doc="0→1 で捕捉開始、1→0 で中止（完了した結果は残る）"),
                                   Field("rle", 1, 1, "RW", doc="1 = 変化点圧縮、0 = RAW（開始時に取り込む）"),
                                   Field("stream", 2, 1, "RW",
                                         doc="1 = 連続ストリーミング（トリガで止まらず、書きながら USB へ流す。"
                                             "docs/superpowers/specs/2026-10-16-m8c-la-streaming-design.md）"),
                                   Field("ps_read", 3, 1, "RW",
                                         doc="1 = ストリームを、チャンクを PL が読み戻さず PS が DDR のリングを直接読む形にする"
                                             "（stream と同時に 1 のときだけ。run の 0→1 の時点で取り込む。ケイパビリティ "
                                             "la_stream_ps のある構成だけ。docs/superpowers/specs/"
                                             "2026-10-30-la-stream-ps-direct-design.md）"),
                                   Field("pack", 4, 3, "RW",
                                         doc="使うチャンネルだけ詰めて流す（RAW のストリームだけ。run の間は変えない）。0 = 詰めない（16 ch）、"
                                             "1 = ch 0〜7（1 サンプル 8 ビット）、2 = ch 0〜3、3 = ch 0〜1、4 = ch 0 だけ（1 周期に複数のサンプルの構成 "
                                             "info.lanes > 1 だけ）。選んだチャンネルは 1 サンプルも欠けず、"
                                             "データ量が 1/2・1/4・1/8・1/16 になる（ケイパビリティ la_pack のある構成だけ）"),
                                   Field("step", 8, 2, "RW",
                                         doc="1 周期に複数のサンプルの構成（info.lanes > 1）の RAW のストリーム: 細かいサンプルの 2^step 個ごとに 1 つを記録する"
                                             "（語の中のレーン 0・2^step・…。2^step <= lanes。la.rate は語の間隔 − 1 で、2^step = lanes のときに使う。"
                                             "docs/superpowers/specs/2026-11-01-la-deep-fast-design.md）。run の間は変えない"),
                                   Field("first", 12, 4, "RW",
                                         doc="1 周期に複数のサンプルの構成（info.lanes > 1）の RAW のストリーム: pack で詰めて記録する最初の ch"
                                             "（ch first〜first + 16 / 2^pack − 1。15 を超える ch は 0）。0 = ch 0 から（1 周期 1 サンプルの構成は 0 だけ）。"
                                             "run の間は変えない"),
                                   Field("gaps", 16, 1, "RW",
                                         doc="1 = ストリームで溢れても止めず、捨てた拍の区間（欠け）をイベント（種類 EVENT_KIND_LA_GAP）に記録して続ける"
                                             "（stream と同時に 1 のときだけ。run の 0→1 の時点で取り込む。ケイパビリティ la_stream_gaps のある構成"
                                             "だけ。欠けの記録はイベントのリングに書くので events.enable が 1 の間だけ。docs/superpowers/specs/"
                                             "2026-11-01-la-deep-fast-design.md §12）。0 = 溢れたら書き込みを止める（overrun）")),
                 doc="捕捉の制御"),
        Register.word("post_beats", 0x04, "RW",
                      doc="トリガの拍の後に書く拍数。イベントも流す構成（FEATURES の LA_STREAM_EVENTS）のストリームでは、"
                          "最初に流すイベントの番号（events.written で数える。下位の capacity の分を使う）"),
        Register("status", 0x08, (Field("capturing", 0, 1, "RO"), Field("triggered", 1, 1, "RO"),
                                  Field("done", 2, 1, "RO"),
                                  Field("overrun", 3, 1, "RO", doc="メモリへの書き込みが追いつかず拍を捨てた"),
                                  Field("reading", 4, 1, "RO", doc="読み出し中"),
                                  Field("gap_open", 5, 1, "RO",
                                        doc="control.gaps の流れで、いま拍を捨てている（欠けの記録をまだ出していない。止めた後も次の開始まで残る）"),
                                  Field("trigger_slot", 8, 3, "RO", doc="トリガの拍の中の番号（RAW: サンプル、RLE: レコード）")),
                 doc="捕捉の状態"),
        Register.word("trigger_beat", 0x0C, "RO", doc="トリガを含む拍の番地"),
        Register.word("written", 0x10, "RO", doc="書いた拍の総数（容量で飽和）"),
        Register.word("trigger_time_lo", 0x14, "RO", doc="トリガ時のタイムベース（下位）"),
        Register.word("trigger_time_hi", 0x18, "RO", doc="トリガ時のタイムベース（上位）"),
        Register.word("read_first", 0x1C, "RW", doc="読み出す先頭の拍の番地"),
        Register.word("read_count", 0x20, "RW", doc="読み出す拍数"),
        Register("command", 0x24, (Field("read", 0, 1, "PULSE", doc="読み出しを始める（USB のストリーム EP へ）"),
                                   Field("read_ps", 1, 1, "PULSE",
                                         doc="read の代わりに、PS が DDR から直接読んで TCP で送る読み出しを頼む（ケイパビリティ la_stream_ps の構成だけ。"
                                             "PL は要求を窓に出し、PS が読み終えるまで status.reading = 1。docs/superpowers/specs/2026-10-30-la-stream-ps-direct-design.md）")
                                   )),
        Register("read_region", 0x28, (Field("read_region", 0, 1, "RW"),),
                 doc="読み出す領域（0 = ロジアナのリング、1 = イベントのリング）。read_first はその中の位置"),
        Register.word("ring_beats", 0x2C, "RO", doc="ロジアナのリングの拍数"),
        Register.word("stream_beats", 0x30, "RO",
                      doc="ストリーミングで書いた拍の総数（折り返す）。イベントも流す構成では、ストリームに出すと決めた"
                          "拍の総数（見出し・イベントを含む。docs/superpowers/specs/2026-10-19-r1-unified-stream-design.md）"),
        Register.word("la_written", 0x34, "RO",
                      doc="ロジアナの拍をリングに書いた総数（流れの開始から。折り返す。符号のバイト数 = この値 × 16。"
                          "docs/superpowers/specs/2026-11-01-la-deep-fast-design.md §6）"),
        Register.word("ring_unread", 0x38, "RO",
                      doc="リングの未読の拍の数（書いた − 読んだ。PL が読み戻す流れは読ませた分、PS が読む流れは PS が返した分。約 16 周期ごとの値）"),
        Register.word("ring_peak", 0x3C, "RO", doc="流れの開始からの ring_unread の最大（拍）"),
        Register.word("start_time_lo", 0x40, "RO",
                      doc="流れの起点のタイムベース（下位。info.start_valid = 1 のとき有効）。1 周期 1 サンプルの構成: run の後の最初の"
                          "サンプルの時刻を trigger_time と同じ段で（サンプル i のピンの時刻 = start_time + i × (la.rate + 1) − LA_DELAY の"
                          "深いバッファの段 − 位相の幅。ケイパビリティ TIMING の la_deep_start が申告する。時刻の正しさの R1）。"
                          "1 周期に複数のサンプルの構成: 流れの最初に符号にした語の時刻"),
        Register.word("start_time_hi", 0x44, "RO", doc="同（上位）"),
        Register("info", 0x48, (Field("lanes", 0, 4, "RO", doc="符号器の 1 語のサンプルの数（1 = 1 周期 1 サンプル）"),
                                Field("latency", 8, 8, "RO",
                                      doc="細かいサンプルの番号の式の固定の遅れ: 流れのサンプル s の値に変わったピンは、辺 "
                                          "start_time × lanes + s × 2^step × (la.rate + 1) − latency の後に変わった（lanes > 1 のとき）"),
                                Field("start_valid", 16, 1, "RO",
                                      doc="start_time が今の流れのもの（run の 0→1 で 0、最初のサンプル・語で 1。止めた後も次の run まで残る。"
                                          "1 周期 1 サンプルの構成は TIMING の la_deep_start を申告する回路だけが 1 にする）")),
                 doc="符号器の形と時刻の式"),
        Register.word("gap_last_beat", 0x4C, "RO",
                      doc="control.gaps の流れで最後に出した欠けの記録の位置（その前に出したロジアナの拍の数。gaps と一緒に、gap_last_beat を"
                          "先に読む: 連続読み出しで gap_last_beat → gaps の順。docs/superpowers/specs/2026-11-01-la-deep-fast-design.md §12）"),
        Register.word("gaps", 0x50, "RO",
                      doc="control.gaps の流れで出した欠けの記録の件数（流れの開始から。届いた拍の数を受け取った後に読めば、その拍に関わる"
                          "記録はこの件数の中にある）"),
    )),
    Block("events", 0x0800, 0x40,
          doc="DAP トランザクションのイベント記録（docs/superpowers/specs/2026-10-16-m9b-dap-events-design.md）。"
              "1 件 = 1 拍。読み出しは la_deep の read_region = 1",
          registers=(
        Register("control", 0x00, (Field("enable", 0, 1, "RW", doc="0→1 で記録を始める（リングの先頭から。ただしイベントを読む流れ（la_deep の stream）の間は先頭に戻さず、位置と written を続ける）"),)),
        Register.word("written", 0x04, "RO", doc="書いたイベントの総数の下位 32 ビット（リングで折り返す。最新は written - 1 番目。上位は written_hi）"),
        Register.word("lost", 0x08, "RO", doc="記録できなかったイベントの数"),
        Register.word("capacity", 0x0C, "RO", doc="イベントのリングの件数"),
        Register.word("written_hi", 0x10, "RO",
                      doc="書いたイベントの総数の上位 32 ビット（総数は written_hi:written の 64 ビット）。written を読んだときの上位を"
                          "保持するので、written に続けて（同じ連続読み出しの中で）読むこと。別々に読むと 2**32 の境で化ける"),
    )),
    Block("debug", 0x0900, 0x40, doc="SWD / JTAG の物理層の調整", registers=(
        Register("sample_delay", 0x00, (Field("swd", 0, 3, "RW", value=1,
                                              doc="SWDIO の取り込みを基準（SWCLK を立ち上げる周期）から遅らせる "
                                                  "sys 周期数（0〜7。2026-11-03 より前の回路は 0〜3 で bit2 が無い）。"
                                                  "配線とターゲットの応答遅延の補償。正しく読める範囲は "
                                                  "d·T ∈ (往復 − 2·half_period·T, 往復)（往復 = 端子の出力の段 + ターゲットの遅れ。"
                                                  "docs/superpowers/specs/2026-11-03-dap-line-rate-design.md §2）"),
                                        Field("jtag", 8, 3, "RW",
                                              doc="TDO の取り込みを次の TCK の立ち下がりの直前から遅らせる sys 周期数（0〜7）。"
                                                  "TDO が立ち下がりから 2 × half_period 周期より遅れて届くとき（R16。docs/superpowers/specs/2026-10-23-jtag-tdo-sample-design.md）"))),
    )),
    Block("trigseq", 0x0A00, 0x40,
          doc="多段トリガ（docs/superpowers/specs/2026-10-16-m9c-trigger-sequencer-design.md）。"
              "事象: bit0 = パターン A、bit1 = パターン B、bit2 = DAP の番地一致、bit3 = ホストの指令。"
              "発火は xtrig の源 bit3",
          registers=(
        Register("control", 0x00, (Field("enable", 0, 1, "RW", doc="1 で動作（0→1 で段 0 から。発火後は 0 に戻すまで止まる）"),
                                   Field("stages", 8, 3, "RW", 1, doc="使う段の数（1〜4）")),),
        Register("status", 0x04, (Field("stage", 0, 2, "RO", doc="今の段"),
                                  Field("fired", 8, 1, "RO", doc="発火した")),),
        Register("pattern_a", 0x08, (Field("mask", 0, 16, "RW", doc="値を比べるチャンネル"),
                                     Field("value", 16, 16, "RW", doc="比べる値")), doc="パターン A の値条件"),
        Register("edge_a", 0x0C, (Field("rise", 0, 16, "RW", doc="立ち上がりを見るチャンネル"),
                                  Field("fall", 16, 16, "RW", doc="立ち下がりを見るチャンネル")),
                 doc="パターン A のエッジ条件（どれかがその向きに変化。指定なしなら値条件だけ）"),
        Register("pattern_b", 0x10, (Field("mask", 0, 16, "RW"), Field("value", 16, 16, "RW")), doc="パターン B の値条件"),
        Register("edge_b", 0x14, (Field("rise", 0, 16, "RW"), Field("fall", 16, 16, "RW")), doc="パターン B のエッジ条件"),
        *(Register(f"stage{k}", 0x18 + 4 * k,
                   (Field("cond", 0, 4, "RW", doc="成立させる事象（ビットの論理和）"),
                    Field("count", 16, 16, "RW", 1, doc="この回数で次の段へ（0 は 1）")),
                   doc=f"段 {k}") for k in range(4)),
        *(Register.word(f"within{k}", 0x28 + 4 * (k - 1), "RW",
                        doc=f"段 {k} に入ってからこの周期数以内に進めなければ段 0 へ（0 = 制限なし）")
          for k in range(1, 4)),
        Register.word("fire_time_lo", 0x34, "RO", doc="発火した時のタイムベース（下位）"),
        Register.word("fire_time_hi", 0x38, "RO", doc="同（上位）"),
    )),
    Block("aux", 0x0B00, 0x40,
          doc="補助マイコン（Raspberry Pi Pico）の計測値（docs/superpowers/specs/2026-10-17-m11-aux-pico-design.md）。"
              "最新のターゲット電圧（補助マイコンの ch 0）。全チャンネルの値はイベント（種類 6）に時刻付きで残る",
          registers=(
        Register("target", 0x00, (Field("mv", 0, 14, "RO"),), doc="ターゲット電圧（mV）"),
        Register.word("samples", 0x04, "RO", doc="受け取った計測値の数（補助マイコンが動いているかの目安）"),
        Register.word("errors", 0x08, "RO", doc="形の崩れた受信（停止ビットの異常・組の途中の先頭）の数"),
    )),
    Block("sampler", 0x0C00, 0x40,
          doc="DAP サンプラ（F4: 変数のライブ観測。docs/superpowers/specs/2026-10-17-f4-live-sampler-design.md）。"
              "最大 8 か所（各 1〜16 語）を周期的に読み、1 語ごとにイベント（種類 7）にする",
          registers=(
        Register("control", 0x00, (Field("run", 0, 1, "RW", doc="1 で周期的に読む"),
                                   Field("ap", 8, 8, "RW", doc="MEM-AP の番号"),
                                   Field("jitter", 16, 4, "RW",
                                         doc="周期のゆらぎ k（0〜2^k − 1 の乱数を足し 2^(k−1) を引く。0 = なし）")),
                 doc="動作"),
        Register.word("period", 0x04, "RW", doc="巡回の周期（sys のクロック数。0 は止める）"),
        Register("slot_index", 0x08, (Field("slot_index", 0, 3, "RW", doc="書くか所（0〜7）"),),
                 doc="か所の表に書く位置"),
        Register.word("slot_addr", 0x0C, "RW", doc="書くか所の番地（語の境界、1 KB をまたがない）"),
        Register("slot_config", 0x10, (Field("enable", 0, 1, "RW", doc="読む"),
                                       Field("words", 8, 4, "RW", doc="語数 − 1（1〜16 語）")),
                 doc="書くか所の設定"),
        Register("slot_write", 0x14, (Field("slot_write", 0, 1, "PULSE",
                                            doc="1 で slot_addr・slot_config を か所 slot_index に書く"),),
                 doc="か所の表への書き込み（表は分散 RAM。読み返せない）"),
        Register.word("rounds", 0x18, "RO", doc="巡回の回数"),
        Register.word("errors", 0x1C, "RO", doc="中止した読みの数（FAULT・応答なし・パリティ）"),
        Register.word("overruns", 0x20, "RO", doc="前の巡回が終わらず飛ばした周期の数"),
        Register("status", 0x24, (Field("last_ack", 0, 3, "RO", doc="最後の ACK"),
                                  Field("busy", 8, 1, "RO", doc="読んでいる"),
                                  Field("held", 9, 1, "RO",
                                        doc="線を持っている（ターゲットの SELECT・CSW・TAR はサンプラの値。"
                                            "ホストの次の転送の前に戻す）")), doc="状態"),
        Register("check", 0x28, (Field("words", 0, 7, "RW", 1,
                                       doc="JTAG の誤りの確認の間隔 K（DRW の読みの数。止めてから書く）。K 回の読みごとに CTRL/STAT を"
                                           "読み、STICKYERR が無いと確かめるまで K 語を出さない（誤りなら、その区間をイベント 18 に）。"
                                           "1 = 語ごと（停止の見張りの形。リセット値）。0 は 1、max を超えれば max。SWD は使わない"
                                           "（値を返した AP の転送の ACK が確認）。"
                                           "docs/superpowers/specs/2026-11-01-bandwidth-observation-design.md 2.3"),
                                 Field("max", 8, 7, "RO", doc="K の上限（溜められる語の数。構成で決まる）")),
                 doc="誤りの確認の間隔（JTAG）"),
    )),
    Block("sampler_match", 0x0C40, 0x40,
          doc="サンプラの値の一致（R21。docs/superpowers/specs/2026-10-23-sampler-match-trigger-design.md）。"
              "サンプラの標本（ACK OK・番地が addr）が (値 xor value) and mask = 0 に入った瞬間（前の標本が不一致）に"
              "クロストリガの源 bit5。構成によって無い（FEATURES bit10。無ければ読むとエラー）",
          registers=(
        Register.word("addr", 0x00, "RW", doc="比べる番地（サンプラが読む番地の 1 語）"),
        Register.word("mask", 0x04, "RW", doc="比べるビット"),
        Register.word("value", 0x08, "RW", doc="比べる値"),
        Register("control", 0x0C, (Field("enable", 0, 1, "RW",
                                         doc="1 で比べる（0→1 で数・状態・時刻を消す。条件を変えるときは 0 にしてから書き、1 に戻す）"),
                                   Field("once", 1, 1, "RW",
                                         doc="1: 有効にした後の最初の 1 回だけ発火する（数・時刻・番号を上書きしない。停止の見張り）"),
                                   Field("after_start", 2, 1, "RW",
                                         doc="1: 門（割り込み書き込みの始動の列の成功）が開くまで標本を比べない。WATCH_START の構成だけ"
                                             "（docs/superpowers/specs/2026-11-03-halt-watch-fpga-restart-design.md §3.1）"),
                                   Field("first_sample", 3, 1, "RW",
                                         doc="1: 有効にした後（after_start なら門の後）のその番号の最初の標本が一致なら発火する"
                                             "（fire.first = 1、prev = 門を開けた時刻）。0: 最初の標本では発火しない。WATCH_START の構成だけ"),
                                   Field("matched", 8, 1, "RO", doc="最後の標本が一致"),
                                   Field("seen_any", 9, 1, "RO", doc="有効にした後に標本があった"),
                                   Field("started", 10, 1, "RO", doc="有効にした後に門が開いた（WATCH_START の構成だけ）")),
                 doc="前の標本が不一致・今の標本が一致で発火（first_sample = 0 なら、有効にした後の最初の標本では発火しない）"),
        Register.word("seen", 0x10, "RO", doc="有効にした後に比べた標本（ACK OK・番地が一致）の数"),
        Register.word("count", 0x14, "RO", doc="発火の回数"),
        Register.word("time_lo", 0x18, "RO", doc="最後に発火した標本の時刻（下位。DAP_LINE_TIME の回路はその読みを頼んだ転送の線上の点 = "
                                               "イベント 7 の時刻。docs/superpowers/specs/2026-11-01-time-accuracy-design.md R6）"),
        Register.word("time_hi", 0x1C, "RO", doc="同（上位 16 ビット。時刻は 48 ビット = イベントの時刻と同じ幅）"),
        Register.word("prev_lo", 0x20, "RO", doc="その直前の（不一致の）標本の時刻（下位。time と同じ意味）。条件に入ったのは prev の読みの"
                                               "後〜time の読みの値が返るまで（窓の後端はイベントの次のサンプラの件）"),
        Register.word("prev_hi", 0x24, "RO", doc="同（上位 16 ビット）"),
        Register.word("addr_mask", 0x28, "RW", 0xFFFFFFFF,
                      doc="比べる番地のビット（既定は全部 = 1 番地。0 のビットは見ない: 例 4 コアの DBGDSCR をまとめて見る）。"
                          "「入った」の判定はサンプラの標本の番号（か所 × 16 + 語）ごと"),
        Register("fire", 0x2C, (Field("index", 0, 7, "RO", doc="最後に発火した標本の番号（か所 × 16 + 語）"),
                                Field("first", 8, 1, "RO",
                                      doc="最後の発火は、その番号の最初の標本（first_sample。prev は門を開けた時刻、門が無ければ 0）")),
                 doc="最後に発火した標本"),
        # 停止の見張りの見逃しを確定する（docs/superpowers/specs/2026-10-29-halt-watch-design.md §3.6）。count から
        # unmatched3 までを 1 回の読み（11 語）で読めるように、この並びの最後に置く
        *(Register.word(f"unmatched{k}", 0x30 + 4 * k, "RO",
                        doc=f"有効にした後に不一致の標本を見た番号（ビット i = 番号 {32 * k} + i。番号 = か所 × 16 + 語）。"
                            "有効の 0→1 でだけ消える（once で発火を止めた後も記録する）。発火と同じ周期に更新する")
          for k in range(4)),
    )),
    Block("xtrig_out", 0x0D00, 0x40,
          doc="クロストリガの動作: 出力の端子（docs/superpowers/specs/2026-10-18-u41-xtrig-pin-output-design.md）。"
              f"動作 k（同時に {XOUT_ACTIONS} 本）が、ボードの出力の端子の候補（ケイパビリティ OUTPUTS）の 1 本を動かす。"
              "源は xtrig と同じ（bit0 ロジアナのトリガ、bit1 DAP の番地一致、bit2 ホストの指令、bit3 多段トリガ、"
              "bit4 FPGA の観測の値の一致、bit5 サンプラの値の一致）",
          registers=(
        Register("status", 0x00, (Field("active", 0, XOUT_ACTIONS, "RO", doc="動作 k が動いている（端子を駆動している）"),),
                 doc="状態"),
        *(r for k in range(XOUT_ACTIONS) for r in (
            Register(f"when{k}", 0x04 + 8 * k,
                     (Field("mask", 0, 8, "RW", doc="端子の並びで見るロジアナの ch（0 なら並びは使わない）"),
                      Field("value", 8, 8, "RW", doc="見る ch の値"),
                      Field("sources", 16, 6, "RW", doc="条件にする源（xtrig の源のビット）"),
                      Field("any", 24, 1, "RW", doc="1: 見る ch のどれかが一致で成立、0: すべて一致で成立")),
                     doc=f"動作 {k} の条件（端子の並び or 源）"),
            Register(f"ctrl{k}", 0x08 + 8 * k,
                     (Field("enable", 0, 1, "RW", doc="有効（0 で状態を消し、駆動しない）"),
                      Field("mode", 4, 2, "RW", doc="0 脈、1 保持（無効にするまで）、2 条件の間"),
                      Field("level", 8, 1, "RW", value=1, doc="動いている間に出す値（1 高、0 低）"),
                      Field("claim", 12, 1, "RW",
                            doc="端子を出力に使う（その端子の機能の出力を止め、機能の入力には 1 を入れる）"),
                      Field("length", 16, 8, "RW", value=1, doc="脈の長さ（刻みの数。0 は脈を出さない）"),
                      Field("unit", 24, 3, "RW", doc="脈の刻み = 16^unit 周期"),
                      Field("pin", 28, 2, "RW", doc="動かす端子（ケイパビリティ OUTPUTS の候補の番号）")),
                     doc=f"動作 {k} の動き"))),
    )),
    Block("obs", 0x0E00, 0x40,
          doc="対象の FPGA の観測（docs/superpowers/specs/2026-10-20-fpga-observation-v2-design.md）。対象の JTAG の"
              "ユーザーの DR の観測の回路（target_ip/fprobe_obs）をスキャンし続け、記録をイベント 8、時刻合わせを"
              "イベント 9 にする",
          registers=(
        Register("control", 0x00, (Field("enable", 0, 1, "RW",
                                         doc="1 で始める（TAP を戻し、IR = USER、ID の 2 つのレジスタを読んで確かめ、"
                                             "スキャンを繰り返す）。"
                                             "0 で TAP を Test-Logic-Reset に戻してから線を手放す"),
                                   Field("release", 1, 1, "RW", doc="1 で TAP を戻さずにすぐ線を手放す（止める）")),
                 doc="動作"),
        Register("half_period", 0x04, (Field("half_period", 0, 16, "RW", value=10),),
                 doc="TCK の半周期（sys の周期数、2 以上）"),
        Register("chain", 0x08, (Field("ir_len", 0, 6, "RW", value=4, doc="対象の IR の長さ（1〜32）"),
                                 Field("ir_before", 6, 6, "RW", doc="対象より TDO 側の装置の IR の合計ビット数"),
                                 Field("ir_after", 12, 6, "RW", doc="対象より TDI 側の装置の IR の合計ビット数"),
                                 Field("dr_before", 18, 3, "RW", doc="対象より TDO 側の装置の数"),
                                 Field("dr_after", 21, 3, "RW", doc="対象より TDI 側の装置の数")),
                 doc="JTAG の鎖での対象の位置（jtag_phy と同じ意味）"),
        Register.word("user_ir", 0x0C, "RW", value=2, doc="対象の USER の命令の値（obs_soft_tap は 2）"),
        Register("words", 0x10, (Field("words", 0, 9, "RW", value=16),),
                 doc="1 スキャンで受ける記録の語数（1〜256）"),
        Register("sync_period", 0x14, (Field("sync_period", 0, 16, "RW", value=16),),
                 doc="印（時刻合わせ。op 3、番号つき）を載せる間隔（スキャンの数。0 = 自動では載せない）"),
        Register("cmd", 0x18, (Field("cmd", 0, 16, "RW",
                                     doc="次に積む命令: op(3:0) | 番地(7:4) | データ(15:8)（op 1 書き・2 読み）"),),
                 doc="命令"),
        Register("id_scans", 0x1C, (Field("id_scans", 0, 8, "RW",
                                          doc="始めに ID の応答を待つスキャンの上限（使う側が渡す。0 = 上限なし）。"
                                              "上限まで来なければ stuck と id_timeout"),),
                 doc="ID の応答を待つ上限"),
        Register("cmd_push", 0x20, (Field("cmd_push", 0, 1, "PULSE",
                                          doc="1 で cmd を命令の待ち行列（深さ 4）に積む"),),
                 doc="命令を積む（待ち行列が一杯なら捨てて status.cmd_dropped）"),
        Register("status", 0x24, (Field("running", 0, 1, "RO", doc="スキャンを繰り返している"),
                                  Field("id_ok", 1, 1, "RO", doc="開いてから ID が \"FOBS\" と照合できた（stuck の後も残る: stuck と組で、答えていた後に止まったのか、初めから答えないのかが分かる）"),
                                  Field("stuck", 2, 1, "RO",
                                        doc="観測の回路が答えない（見出しの印が 0xA6 でない・ID が違う・ID の応答が"
                                            "id_scans スキャンで来ない）ので止めた"),
                                  Field("cmd_pending", 3, 1, "RO", doc="待ち行列に命令がある"),
                                  Field("overflow_seen", 4, 1, "RO", doc="最後の見出しの溢れの印"),
                                  Field("cmd_dropped", 5, 1, "RO", doc="一杯で捨てた命令がある（enable の 0→1 で消える）"),
                                  Field("target_running", 6, 1, "RO",
                                        doc="最後の見出しの「対象の clk 側が記録中」（対象のリセット等で止まったら 0）"),
                                  Field("driving", 7, 1, "RO",
                                        doc="対象の JTAG の線を駆動している（止めた後は 0 になるまで待つ）"),
                                  Field("id_timeout", 8, 1, "RO",
                                        doc="stuck の理由: id_scans スキャン待って ID の応答が来なかった（0 なら答えが違った）")),
                 doc="状態"),
        Register.word("scans", 0x28, "RO", doc="行ったスキャンの数（イベント 8 の address）"),
        Register.word("read_value", 0x2C, "RO",
                      doc="最後の応答の記録: 番号(31:24。応答のたびに 1 増える) | 番地(19:16) | 値(15:0)"),
        Register.word("id", 0x30, "RO", doc="始めに読んだ観測の回路の ID"),
        Register("match", 0x34, (Field("mask", 0, 16, "RW", doc="一致を比べる値のビット"),
                                 Field("value", 16, 16, "RW", doc="一致の値")),
                 doc="値の一致（クロストリガの源 bit4）: (値 xor value) and mask = 0"),
        Register("match_change", 0x38, (Field("change", 0, 16, "RW",
                                              doc="前の値からどれかが変わったら比べるビット（0 = 一致に入った瞬間）"),
                                       Field("enable", 16, 1, "RW", doc="1 で一致を源にする")),
                 doc="値の一致の起こり方"),
        Register.word("match_count", 0x3C, "RO", doc="値の一致が成り立った回数（enable の 0→1 で 0 に）"),
    )),
    Block("sync", 0x0F00, 0x40,
          doc="ボードの間の時刻の同期（docs/superpowers/specs/2026-10-22-modular-design.md §3.2）。親は period ごとに"
              "パルスと自分の時刻を sync_out に出し、子は sync_in で受けた組（自分の時刻・親の時刻）をイベント 10 にする",
          registers=(
        Register("control", 0x00, (Field("enable", 0, 1, "RW", doc="子として受けて組をイベント 10 にする"),
                                   Field("master", 1, 1, "RW", doc="親として sync_out に出す")),
                 doc="受ける・出す（両方 1 で、受けながら下流へも出す）"),
        Register.word("period", 0x04, "RW", value=100000, doc="親のパルスの間隔（タイムベースの刻み）。1 回の送りは 730 刻みで、送りの間に 0 を 20 刻み置くので、約 750 より短いと約 750 ごと"),
        Register("status", 0x08, (Field("pairs", 0, 16, "RO", doc="受けた組の数（CRC が合ったもの。16 ビットで一周）"),
                                  Field("crc_errors", 16, 16, "RO", doc="CRC が合わず捨てた組の数（0xFFFF で止まる）")),
                 doc="受けた組の数（enable の 0 → 1 で 0 から）"),
    )),
    Block("caps", 0x1000, 0x1000, external=True, doc="ケイパビリティ ROM（TLV。範囲外は 0）"),
    Block("la_mem", 0x10000, 4 * LA_DEPTH, external=True,
          doc="ロジアナのバッファ（1 サンプル = 1 ワード。start_index から時間順に読む）"),
    Block("la_lanes_mem", 0x40000, 4 * LA_LANES_MEM_WORDS, external=True,
          doc="1 周期に複数のサンプル（la_cap.info.lanes > 1）の内蔵バッファ（その構成だけ。無い構成では読むとエラー）。"
              "語 w（sys の 1 周期）のレーン 2j と 2j + 1（j = 0〜lanes/2 − 1）を、ワード番号 j × 深さ（語）+ w の下位 16 ビットと"
              "上位 16 ビットに置く（1 サンプル = 16 ch）。j ごとに start_index から時間順に読む。深さはケイパビリティ la_depth / lanes"),
))
