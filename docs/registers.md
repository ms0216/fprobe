# fprobe CSR レジスタ表

<!-- 自動生成ファイル。直接編集しないこと（python -m fprobe_gw gen で再生成）。生成元: gateware/fprobe_gw/csr/fprobe_map.py -->

CONTROL チャンネルの READ / WRITE で読み書きする（アドレスはバイト単位、32 ビットワード）。

| ブロック | ベース | サイズ | 内容 |
|---|---|---|---|
| `system` | `0x00000000` | `0x40` | 識別と疎通確認 |
| `timebase` | `0x00000100` | `0x40` | 64 ビットのタイムベース（周波数はケイパビリティ TIMEBASE_HZ） |
| `gpio` | `0x00000200` | `0x40` | 拡張ヘッダ（Primer 20K は J12）の 8 本の汎用入出力。配線確認・ビットバング用 |
| `uart` | `0x00000300` | `0x40` | UART ブリッジ（CDC-ACM ⇄ ターゲット）の状態 |
| `la` | `0x00000400` | `0x40` | ロジックアナライザの設定と状態（docs/superpowers/specs/2026-10-16-m7-logic-analyzer-design.md） |
| `xtrig` | `0x00000500` | `0x40` | クロストリガ（docs/superpowers/specs/2026-10-16-m9-cross-trigger-design.md）。源: bit0 = ロジアナのトリガ、bit1 = DAP の番地一致、bit2 = ホストの指令（command.fire）、bit3 = 多段トリガ（trigseq） |
| `mem` | `0x00000600` | `0x40` | 外部メモリ（DDR3 等）の状態とメモリ試験（docs/superpowers/specs/2026-10-16-m8-ddr3-design.md）。番地・数は拍（MEM_BEAT_BYTES バイト）単位 |
| `la_deep` | `0x00000700` | `0x40` | ロジアナの深いバッファ（外部メモリ。docs/superpowers/specs/2026-10-16-m8-ddr3-design.md §5）。前段の設定（rate・trig_*）は la ブロックと共通。番地・数は拍（MEM_BEAT_BYTES バイト）単位 |
| `events` | `0x00000800` | `0x40` | DAP トランザクションのイベント記録（docs/superpowers/specs/2026-10-16-m9b-dap-events-design.md）。1 件 = 1 拍。読み出しは la_deep の read_region = 1 |
| `debug` | `0x00000900` | `0x40` | SWD / JTAG の物理層の調整 |
| `trigseq` | `0x00000A00` | `0x40` | 多段トリガ（docs/superpowers/specs/2026-10-16-m9c-trigger-sequencer-design.md）。事象: bit0 = パターン A、bit1 = パターン B、bit2 = DAP の番地一致、bit3 = ホストの指令。発火は xtrig の源 bit3 |
| `aux` | `0x00000B00` | `0x40` | 補助マイコン（Raspberry Pi Pico）の計測値（docs/superpowers/specs/2026-10-17-m11-aux-pico-design.md）。最新のターゲット電圧（補助マイコンの ch 0）。全チャンネルの値はイベント（種類 6）に時刻付きで残る |
| `sampler` | `0x00000C00` | `0x80` | DAP サンプラ（F4: 変数のライブ観測。docs/superpowers/specs/2026-10-17-f4-live-sampler-design.md）。最大 8 か所（各 1〜16 語）を周期的に読み、1 語ごとにイベント（種類 7）にする |
| `xtrig_out` | `0x00000D00` | `0x40` | クロストリガの動作: 出力の端子（docs/superpowers/specs/2026-10-18-u41-xtrig-pin-output-design.md）。動作 k（同時に 2 本）が、ボードの出力の端子の候補（ケイパビリティ OUTPUTS）の 1 本を動かす。源は xtrig と同じ（bit0 ロジアナのトリガ、bit1 DAP の番地一致、bit2 ホストの指令、bit3 多段トリガ） |
| `caps` | `0x00001000` | `0x1000` | ケイパビリティ ROM（TLV。範囲外は 0） |
| `la_mem` | `0x00010000` | `0x8000` | ロジアナのバッファ（1 サンプル = 1 ワード。start_index から時間順に読む） |

## system

| アドレス | レジスタ | ビット | フィールド | 種類 | 初期値 / 値 | 説明 |
|---|---|---|---|---|---|---|
| `0x00000000` | `magic` | 31:0 | `magic` | CONST | `0x42525046` | "FPRB"（リトルエンディアン） |
| `0x00000004` | `version` | 31:0 | `version` | CONST | `0x2` | CONTROL プロトコルの版 |
| `0x00000008` | `scratch` | 31:0 | `scratch` | RW | `0x0` | 読み書き試験用（機能なし） |

## timebase

| アドレス | レジスタ | ビット | フィールド | 種類 | 初期値 / 値 | 説明 |
|---|---|---|---|---|---|---|
| `0x00000100` | `time_lo` | 31:0 | `time_lo` | RO | — | 下位 32 ビット。読むと同時に上位を time_hi に保持する |
| `0x00000104` | `time_hi` | 31:0 | `time_hi` | RO | — | time_lo を読んだ時点の上位 32 ビット |

## gpio

| アドレス | レジスタ | ビット | フィールド | 種類 | 初期値 / 値 | 説明 |
|---|---|---|---|---|---|---|
| `0x00000200` | `drive` | 7:0 | `drive` | RW | `0x0` | 出力値（ビット n = ピン n） |
| `0x00000204` | `enable` | 7:0 | `enable` | RW | `0x0` | 出力を有効にする（0 で Hi-Z。プルはボードの定義: Primer は 4・7 番がプル無し、ほかはプルアップ） |
| `0x00000208` | `level` | 7:0 | `level` | RO | — | ピンの入力値（2 段で同期化） |
| `0x0000020C` | `io` | 7:0 | `io` | RO（書くと `drive`） | — | 読むと level、書くと drive（マイコンのポートのレジスタと同じ）。CONTROL の EXCHANGE で 1 要求の中で書いて読むを繰り返す（ビットバングの SPI 等。U42）。読みは同期化の 2 周期分、書く前の端子のことがある |

## uart

| アドレス | レジスタ | ビット | フィールド | 種類 | 初期値 / 値 | 説明 |
|---|---|---|---|---|---|---|
| `0x00000300` | `baud` | 31:0 | `baud` | RO | — | 現在のボーレート（COM ポートの設定に追従） |
| `0x00000304` | `frame_errors` | 31:0 | `frame_errors` | RO | — | 停止ビットが 0 だった受信の数 |
| `0x00000308` | `overruns` | 31:0 | `overruns` | RO | — | 取り出される前に次が揃って失った受信の数 |
| `0x0000030C` | `flow` | 0 | `auto` | RW | `0x0` | 自動の RTS/CTS: CTS が出ている間だけ送り始め、受信の下流が満杯なら RTS を下げる（0: RTS は COM ポートの RTS どおり、CTS は見ない） |
| `0x0000030C` | `flow` | 4 | `rts_claim` | RW | `0x0` | RTS を端子の候補 rts_pin に出す（その端子の機能の出力は止める） |
| `0x0000030C` | `flow` | 9:8 | `rts_pin` | RW | `0x0` | RTS の端子（ケイパビリティ UART_FLOW の候補の番号） |
| `0x0000030C` | `flow` | 12 | `cts_claim` | RW | `0x0` | CTS を端子の候補 cts_pin から読む（その端子の機能の出力は止める）。0 なら CTS は出ているとみなす |
| `0x0000030C` | `flow` | 17:16 | `cts_pin` | RW | `0x0` | CTS の端子（ケイパビリティ UART_FLOW の候補の番号） |
| `0x00000310` | `lines` | 0 | `cts` | RO | — | CTS が出ている（端子が Low。cts_claim = 0 なら 1） |
| `0x00000310` | `lines` | 1 | `rts` | RO | — | RTS を出している（端子を Low に駆動する値） |
| `0x00000310` | `lines` | 2 | `host_rts` | RO | — | COM ポートの RTS（CDC-ACM の SET_CONTROL_LINE_STATE） |
| `0x00000310` | `lines` | 3 | `host_break` | RO | — | ブレークを送っている（CDC-ACM の SEND_BREAK） |

## la

| アドレス | レジスタ | ビット | フィールド | 種類 | 初期値 / 値 | 説明 |
|---|---|---|---|---|---|---|
| `0x00000400` | `control` | 0 | `run` | RW | `0x0` | 0→1 で捕捉開始、1→0 で中止 |
| `0x00000404` | `rate` | 15:0 | `rate` | RW | `0x0` | サンプル間隔 - 1（sys クロック周期） |
| `0x00000408` | `pretrigger` | 31:0 | `pretrigger` | RW | `0x0` | トリガ前に残すサンプル数（深さ - 1 まで） |
| `0x0000040C` | `trig_mask` | 15:0 | `trig_mask` | RW | `0x0` | 値一致で見るチャンネル |
| `0x00000410` | `trig_value` | 15:0 | `trig_value` | RW | `0x0` | 値一致の値 |
| `0x00000414` | `trig_rise` | 15:0 | `trig_rise` | RW | `0x0` | 立ち上がりで成立するチャンネル |
| `0x00000418` | `trig_fall` | 15:0 | `trig_fall` | RW | `0x0` | 立ち下がりで成立するチャンネル |
| `0x0000041C` | `status` | 0 | `capturing` | RO | — |  |
| `0x0000041C` | `status` | 1 | `triggered` | RO | — |  |
| `0x0000041C` | `status` | 2 | `done` | RO | — |  |
| `0x00000420` | `start_index` | 31:0 | `start_index` | RO | — | 完了後、最も古いサンプルのバッファ位置 |
| `0x00000424` | `trigger_index` | 31:0 | `trigger_index` | RO | — | トリガのサンプルのバッファ位置 |
| `0x00000428` | `trigger_time_lo` | 31:0 | `trigger_time_lo` | RO | — | トリガ時のタイムベース（下位） |
| `0x0000042C` | `trigger_time_hi` | 31:0 | `trigger_time_hi` | RO | — | トリガ時のタイムベース（上位） |
| `0x00000430` | `filter` | 15:0 | `channels` | RW | `0x0` | グリッチフィルタをかける ch（ビット n = ch n。外部の入力 = 拡張ヘッダのピンと専用の入力（ケイパビリティ LA_LAYOUT）だけ効く。内部の信号は通さない） |
| `0x00000430` | `filter` | 23:16 | `cycles` | RW | `0x0` | 通す最短の脈（sys の周期）。それより短い変化を捨てる。0・1 はフィルタなし |
| `0x00000434` | `pulse` | 1:0 | `mode` | RW | `0x0` | 0 なし、1 幅（脈の終わりに、幅が pulse_min〜pulse_max なら成立）、2 一定時間変化しない（level が pulse_min 周期を超えて続いたら成立） |
| `0x00000434` | `pulse` | 4 | `level` | RW | `0x1` | 脈の値（1 = High の脈） |
| `0x00000434` | `pulse` | 11:8 | `channel` | RW | `0x0` | 見る ch |
| `0x00000438` | `pulse_min` | 31:0 | `pulse_min` | RW | `0x0` | パルス幅の下限（sys の周期） |
| `0x0000043C` | `pulse_max` | 31:0 | `pulse_max` | RW | `0xFFFFFFFF` | パルス幅の上限（sys の周期。全ビット 1 = 上限なし） |

## xtrig

| アドレス | レジスタ | ビット | フィールド | 種類 | 初期値 / 値 | 説明 |
|---|---|---|---|---|---|---|
| `0x00000500` | `route` | 3:0 | `to_la` | RW | `0x0` | ロジアナをトリガする源 |
| `0x00000500` | `route` | 11:8 | `to_inject` | RW | `0x0` | 割り込み書き込み（既定は停止）をする源 |
| `0x00000504` | `command` | 0 | `fire` | PULSE | `0x0` | ホストの指令（源 bit2） |
| `0x00000508` | `inject_ap` | 7:0 | `inject_ap` | RW | `0x0` | 割り込み書き込みの MEM-AP 番号 |
| `0x0000050C` | `inject_addr` | 31:0 | `inject_addr` | RW | `0xE000EDF0` | 割り込み書き込みの番地（既定 DHCSR） |
| `0x00000510` | `inject_value` | 31:0 | `inject_value` | RW | `0xA05F0003` | 割り込み書き込みの値（既定 C_HALT | C_DEBUGEN） |
| `0x00000514` | `inject_status` | 0 | `busy` | RO | — |  |
| `0x00000514` | `inject_status` | 5:4 | `result` | RO | — | 最後の要求: 0 成功 / 1 SWD の異常 / 2 未接続 |
| `0x00000514` | `inject_status` | 10:8 | `ack` | RO | — | 最後の SWD 応答 |
| `0x00000514` | `inject_status` | 31:16 | `count` | RO | — | 成功した回数 |
| `0x00000518` | `inject_time_lo` | 31:0 | `inject_time_lo` | RO | — | 最後に書き込みが受理された時のタイムベース（下位） |
| `0x0000051C` | `inject_time_hi` | 31:0 | `inject_time_hi` | RO | — | 同（上位） |
| `0x00000520` | `match_addr` | 31:0 | `match_addr` | RW | `0x0` | 番地一致: 番地 |
| `0x00000524` | `match_addr_mask` | 31:0 | `match_addr_mask` | RW | `0xFFFFFFFF` | 番地一致: 比べるビット |
| `0x00000528` | `match_data` | 31:0 | `match_data` | RW | `0x0` | 番地一致: 書き込み値 |
| `0x0000052C` | `match_data_mask` | 31:0 | `match_data_mask` | RW | `0x0` | 番地一致: 比べる値のビット（0 なら値を見ない） |
| `0x00000530` | `match_mode` | 0 | `on_write` | RW | `0x0` |  |
| `0x00000530` | `match_mode` | 1 | `on_read` | RW | `0x0` |  |
| `0x00000534` | `match_count` | 31:0 | `match_count` | RO | — | 一致した回数 |
| `0x00000538` | `match_time_lo` | 31:0 | `match_time_lo` | RO | — | 最後に一致した時のタイムベース（下位） |
| `0x0000053C` | `match_time_hi` | 31:0 | `match_time_hi` | RO | — | 同（上位） |

## mem

| アドレス | レジスタ | ビット | フィールド | 種類 | 初期値 / 値 | 説明 |
|---|---|---|---|---|---|---|
| `0x00000600` | `status` | 0 | `ready` | RO | — | 初期化・較正が済んだ |
| `0x00000600` | `status` | 1 | `busy` | RO | — | メモリ試験の実行中 |
| `0x00000600` | `status` | 2 | `done` | RO | — | メモリ試験が終わった（結果が有効） |
| `0x00000600` | `status` | 3 | `in_reset` | RO | — | コントローラがリセット中（診断用） |
| `0x00000600` | `status` | 4 | `clock_tick` | RO | — | メモリ側クロックで数えた計数の 1 ビット（約 10 ms ごとに反転すれば動いている） |
| `0x00000604` | `command` | 0 | `test` | PULSE | `0x0` | メモリ試験を始める |
| `0x00000608` | `test_base` | 31:0 | `test_base` | RW | `0x0` | 試験の先頭の拍 |
| `0x0000060C` | `test_count` | 31:0 | `test_count` | RW | `0x1` | 試験する拍の数（1 以上） |
| `0x00000610` | `test_seed` | 31:0 | `test_seed` | RW | `0x0` | パターンの種 |
| `0x00000614` | `errors` | 31:0 | `errors` | RO | — | 読み値が合わなかった拍の数 |
| `0x00000618` | `first_error` | 31:0 | `first_error` | RO | — | 最初に合わなかった拍 |
| `0x0000061C` | `write_cycles` | 31:0 | `write_cycles` | RO | — | 書き込みにかかったメモリクロックの周期数 |
| `0x00000620` | `read_cycles` | 31:0 | `read_cycles` | RO | — | 読み出しにかかったメモリクロックの周期数 |

## la_deep

| アドレス | レジスタ | ビット | フィールド | 種類 | 初期値 / 値 | 説明 |
|---|---|---|---|---|---|---|
| `0x00000700` | `control` | 0 | `run` | RW | `0x0` | 0→1 で捕捉開始、1→0 で中止（完了した結果は残る） |
| `0x00000700` | `control` | 1 | `rle` | RW | `0x0` | 1 = 変化点圧縮、0 = RAW（開始時に取り込む） |
| `0x00000700` | `control` | 2 | `stream` | RW | `0x0` | 1 = 連続ストリーミング（トリガで止まらず、書きながら USB へ流す。docs/superpowers/specs/2026-10-16-m8c-la-streaming-design.md） |
| `0x00000704` | `post_beats` | 31:0 | `post_beats` | RW | `0x0` | トリガの拍の後に書く拍数。イベントも流す構成（FEATURES の LA_STREAM_EVENTS）のストリームでは、最初に流すイベントの番号（events.written で数える。下位の capacity の分を使う） |
| `0x00000708` | `status` | 0 | `capturing` | RO | — |  |
| `0x00000708` | `status` | 1 | `triggered` | RO | — |  |
| `0x00000708` | `status` | 2 | `done` | RO | — |  |
| `0x00000708` | `status` | 3 | `overrun` | RO | — | メモリへの書き込みが追いつかず拍を捨てた |
| `0x00000708` | `status` | 4 | `reading` | RO | — | 読み出し中 |
| `0x00000708` | `status` | 10:8 | `trigger_slot` | RO | — | トリガの拍の中の番号（RAW: サンプル、RLE: レコード） |
| `0x0000070C` | `trigger_beat` | 31:0 | `trigger_beat` | RO | — | トリガを含む拍の番地 |
| `0x00000710` | `written` | 31:0 | `written` | RO | — | 書いた拍の総数（容量で飽和） |
| `0x00000714` | `trigger_time_lo` | 31:0 | `trigger_time_lo` | RO | — | トリガ時のタイムベース（下位） |
| `0x00000718` | `trigger_time_hi` | 31:0 | `trigger_time_hi` | RO | — | トリガ時のタイムベース（上位） |
| `0x0000071C` | `read_first` | 31:0 | `read_first` | RW | `0x0` | 読み出す先頭の拍の番地 |
| `0x00000720` | `read_count` | 31:0 | `read_count` | RW | `0x0` | 読み出す拍数 |
| `0x00000724` | `command` | 0 | `read` | PULSE | `0x0` | 読み出しを始める（USB のストリーム EP へ） |
| `0x00000728` | `read_region` | 0 | `read_region` | RW | `0x0` | 読み出す領域（0 = ロジアナのリング、1 = イベントのリング）。read_first はその中の位置 |
| `0x0000072C` | `ring_beats` | 31:0 | `ring_beats` | RO | — | ロジアナのリングの拍数 |
| `0x00000730` | `stream_beats` | 31:0 | `stream_beats` | RO | — | ストリーミングで書いた拍の総数（折り返す）。イベントも流す構成では、ストリームに出すと決めた拍の総数（見出し・イベントを含む。docs/superpowers/specs/2026-10-19-r1-unified-stream-design.md） |

## events

| アドレス | レジスタ | ビット | フィールド | 種類 | 初期値 / 値 | 説明 |
|---|---|---|---|---|---|---|
| `0x00000800` | `control` | 0 | `enable` | RW | `0x0` | 0→1 で記録を始める（リングの先頭から） |
| `0x00000804` | `written` | 31:0 | `written` | RO | — | 書いたイベントの総数（リングで折り返す。最新は written - 1 番目） |
| `0x00000808` | `lost` | 31:0 | `lost` | RO | — | 記録できなかったイベントの数 |
| `0x0000080C` | `capacity` | 31:0 | `capacity` | RO | — | イベントのリングの件数 |

## debug

| アドレス | レジスタ | ビット | フィールド | 種類 | 初期値 / 値 | 説明 |
|---|---|---|---|---|---|---|
| `0x00000900` | `sample_delay` | 1:0 | `swd` | RW | `0x1` | SWDIO の取り込みを基準（SWCLK を立ち上げる周期）から遅らせる sys 周期数。配線とターゲットの応答遅延の補償 |

## trigseq

| アドレス | レジスタ | ビット | フィールド | 種類 | 初期値 / 値 | 説明 |
|---|---|---|---|---|---|---|
| `0x00000A00` | `control` | 0 | `enable` | RW | `0x0` | 1 で動作（0→1 で段 0 から。発火後は 0 に戻すまで止まる） |
| `0x00000A00` | `control` | 10:8 | `stages` | RW | `0x1` | 使う段の数（1〜4） |
| `0x00000A04` | `status` | 1:0 | `stage` | RO | — | 今の段 |
| `0x00000A04` | `status` | 8 | `fired` | RO | — | 発火した |
| `0x00000A08` | `pattern_a` | 15:0 | `mask` | RW | `0x0` | 値を比べるチャンネル |
| `0x00000A08` | `pattern_a` | 31:16 | `value` | RW | `0x0` | 比べる値 |
| `0x00000A0C` | `edge_a` | 15:0 | `rise` | RW | `0x0` | 立ち上がりを見るチャンネル |
| `0x00000A0C` | `edge_a` | 31:16 | `fall` | RW | `0x0` | 立ち下がりを見るチャンネル |
| `0x00000A10` | `pattern_b` | 15:0 | `mask` | RW | `0x0` |  |
| `0x00000A10` | `pattern_b` | 31:16 | `value` | RW | `0x0` |  |
| `0x00000A14` | `edge_b` | 15:0 | `rise` | RW | `0x0` |  |
| `0x00000A14` | `edge_b` | 31:16 | `fall` | RW | `0x0` |  |
| `0x00000A18` | `stage0` | 3:0 | `cond` | RW | `0x0` | 成立させる事象（ビットの論理和） |
| `0x00000A18` | `stage0` | 31:16 | `count` | RW | `0x1` | この回数で次の段へ（0 は 1） |
| `0x00000A1C` | `stage1` | 3:0 | `cond` | RW | `0x0` | 成立させる事象（ビットの論理和） |
| `0x00000A1C` | `stage1` | 31:16 | `count` | RW | `0x1` | この回数で次の段へ（0 は 1） |
| `0x00000A20` | `stage2` | 3:0 | `cond` | RW | `0x0` | 成立させる事象（ビットの論理和） |
| `0x00000A20` | `stage2` | 31:16 | `count` | RW | `0x1` | この回数で次の段へ（0 は 1） |
| `0x00000A24` | `stage3` | 3:0 | `cond` | RW | `0x0` | 成立させる事象（ビットの論理和） |
| `0x00000A24` | `stage3` | 31:16 | `count` | RW | `0x1` | この回数で次の段へ（0 は 1） |
| `0x00000A28` | `within1` | 31:0 | `within1` | RW | `0x0` | 段 1 に入ってからこの周期数以内に進めなければ段 0 へ（0 = 制限なし） |
| `0x00000A2C` | `within2` | 31:0 | `within2` | RW | `0x0` | 段 2 に入ってからこの周期数以内に進めなければ段 0 へ（0 = 制限なし） |
| `0x00000A30` | `within3` | 31:0 | `within3` | RW | `0x0` | 段 3 に入ってからこの周期数以内に進めなければ段 0 へ（0 = 制限なし） |
| `0x00000A34` | `fire_time_lo` | 31:0 | `fire_time_lo` | RO | — | 発火した時のタイムベース（下位） |
| `0x00000A38` | `fire_time_hi` | 31:0 | `fire_time_hi` | RO | — | 同（上位） |

## aux

| アドレス | レジスタ | ビット | フィールド | 種類 | 初期値 / 値 | 説明 |
|---|---|---|---|---|---|---|
| `0x00000B00` | `target` | 13:0 | `mv` | RO | — |  |
| `0x00000B04` | `samples` | 31:0 | `samples` | RO | — | 受け取った計測値の数（補助マイコンが動いているかの目安） |
| `0x00000B08` | `errors` | 31:0 | `errors` | RO | — | 形の崩れた受信（停止ビットの異常・組の途中の先頭）の数 |

## sampler

| アドレス | レジスタ | ビット | フィールド | 種類 | 初期値 / 値 | 説明 |
|---|---|---|---|---|---|---|
| `0x00000C00` | `control` | 0 | `run` | RW | `0x0` | 1 で周期的に読む |
| `0x00000C00` | `control` | 15:8 | `ap` | RW | `0x0` | MEM-AP の番号 |
| `0x00000C00` | `control` | 19:16 | `jitter` | RW | `0x0` | 周期のゆらぎ k（0〜2^k − 1 の乱数を足し 2^(k−1) を引く。0 = なし） |
| `0x00000C04` | `period` | 31:0 | `period` | RW | `0x0` | 巡回の周期（sys のクロック数。0 は止める） |
| `0x00000C08` | `slot_index` | 2:0 | `slot_index` | RW | `0x0` | か所の表に書く位置 |
| `0x00000C0C` | `slot_addr` | 31:0 | `slot_addr` | RW | `0x0` | 書くか所の番地（語の境界、1 KB をまたがない） |
| `0x00000C10` | `slot_config` | 0 | `enable` | RW | `0x0` | 読む |
| `0x00000C10` | `slot_config` | 11:8 | `words` | RW | `0x0` | 語数 − 1（1〜16 語） |
| `0x00000C14` | `slot_write` | 0 | `slot_write` | PULSE | `0x0` | か所の表への書き込み（表は分散 RAM。読み返せない） |
| `0x00000C18` | `rounds` | 31:0 | `rounds` | RO | — | 巡回の回数 |
| `0x00000C1C` | `errors` | 31:0 | `errors` | RO | — | 中止した読みの数（FAULT・応答なし・パリティ） |
| `0x00000C20` | `overruns` | 31:0 | `overruns` | RO | — | 前の巡回が終わらず飛ばした周期の数 |
| `0x00000C24` | `status` | 2:0 | `last_ack` | RO | — | 最後の ACK |
| `0x00000C24` | `status` | 8 | `busy` | RO | — | 読んでいる |
| `0x00000C24` | `status` | 9 | `held` | RO | — | 線を持っている（ターゲットの SELECT・CSW・TAR はサンプラの値。ホストの次の転送の前に戻す） |

## xtrig_out

| アドレス | レジスタ | ビット | フィールド | 種類 | 初期値 / 値 | 説明 |
|---|---|---|---|---|---|---|
| `0x00000D00` | `status` | 1:0 | `active` | RO | — | 動作 k が動いている（端子を駆動している） |
| `0x00000D04` | `when0` | 7:0 | `mask` | RW | `0x0` | 端子の並びで見るロジアナの ch（0 なら並びは使わない） |
| `0x00000D04` | `when0` | 15:8 | `value` | RW | `0x0` | 見る ch の値 |
| `0x00000D04` | `when0` | 19:16 | `sources` | RW | `0x0` | 条件にする源（xtrig の源のビット） |
| `0x00000D04` | `when0` | 20 | `any` | RW | `0x0` | 1: 見る ch のどれかが一致で成立、0: すべて一致で成立 |
| `0x00000D08` | `ctrl0` | 0 | `enable` | RW | `0x0` | 有効（0 で状態を消し、駆動しない） |
| `0x00000D08` | `ctrl0` | 5:4 | `mode` | RW | `0x0` | 0 脈、1 保持（無効にするまで）、2 条件の間 |
| `0x00000D08` | `ctrl0` | 8 | `level` | RW | `0x1` | 動いている間に出す値（1 高、0 低） |
| `0x00000D08` | `ctrl0` | 12 | `claim` | RW | `0x0` | 端子を出力に使う（その端子の機能の出力を止め、機能の入力には 1 を入れる） |
| `0x00000D08` | `ctrl0` | 23:16 | `length` | RW | `0x1` | 脈の長さ（刻みの数。0 は脈を出さない） |
| `0x00000D08` | `ctrl0` | 26:24 | `unit` | RW | `0x0` | 脈の刻み = 16^unit 周期 |
| `0x00000D08` | `ctrl0` | 29:28 | `pin` | RW | `0x0` | 動かす端子（ケイパビリティ OUTPUTS の候補の番号） |
| `0x00000D0C` | `when1` | 7:0 | `mask` | RW | `0x0` | 端子の並びで見るロジアナの ch（0 なら並びは使わない） |
| `0x00000D0C` | `when1` | 15:8 | `value` | RW | `0x0` | 見る ch の値 |
| `0x00000D0C` | `when1` | 19:16 | `sources` | RW | `0x0` | 条件にする源（xtrig の源のビット） |
| `0x00000D0C` | `when1` | 20 | `any` | RW | `0x0` | 1: 見る ch のどれかが一致で成立、0: すべて一致で成立 |
| `0x00000D10` | `ctrl1` | 0 | `enable` | RW | `0x0` | 有効（0 で状態を消し、駆動しない） |
| `0x00000D10` | `ctrl1` | 5:4 | `mode` | RW | `0x0` | 0 脈、1 保持（無効にするまで）、2 条件の間 |
| `0x00000D10` | `ctrl1` | 8 | `level` | RW | `0x1` | 動いている間に出す値（1 高、0 低） |
| `0x00000D10` | `ctrl1` | 12 | `claim` | RW | `0x0` | 端子を出力に使う（その端子の機能の出力を止め、機能の入力には 1 を入れる） |
| `0x00000D10` | `ctrl1` | 23:16 | `length` | RW | `0x1` | 脈の長さ（刻みの数。0 は脈を出さない） |
| `0x00000D10` | `ctrl1` | 26:24 | `unit` | RW | `0x0` | 脈の刻み = 16^unit 周期 |
| `0x00000D10` | `ctrl1` | 29:28 | `pin` | RW | `0x0` | 動かす端子（ケイパビリティ OUTPUTS の候補の番号） |
