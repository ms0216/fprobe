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
| `xtrig` | `0x00000500` | `0x40` | クロストリガ（docs/superpowers/specs/2026-10-16-m9-cross-trigger-design.md）。源: bit0 = ロジアナのトリガ、bit1 = DAP の番地一致、bit2 = ホストの指令（command.fire）、bit3 = 多段トリガ（trigseq）、bit4 = FPGA の観測の値の一致（obs.match）、bit5 = サンプラの値の一致（sampler_match。構成によって無い） |
| `mem` | `0x00000600` | `0x40` | 外部メモリ（DDR3 等）の状態とメモリ試験（docs/superpowers/specs/2026-10-16-m8-ddr3-design.md）。番地・数は拍（MEM_BEAT_BYTES バイト）単位 |
| `la_deep` | `0x00000700` | `0x40` | ロジアナの深いバッファ（外部メモリ。docs/superpowers/specs/2026-10-16-m8-ddr3-design.md §5）。前段の設定（rate・trig_*）は la ブロックと共通。番地・数は拍（MEM_BEAT_BYTES バイト）単位 |
| `events` | `0x00000800` | `0x40` | DAP トランザクションのイベント記録（docs/superpowers/specs/2026-10-16-m9b-dap-events-design.md）。1 件 = 1 拍。読み出しは la_deep の read_region = 1 |
| `debug` | `0x00000900` | `0x40` | SWD / JTAG の物理層の調整 |
| `trigseq` | `0x00000A00` | `0x40` | 多段トリガ（docs/superpowers/specs/2026-10-16-m9c-trigger-sequencer-design.md）。事象: bit0 = パターン A、bit1 = パターン B、bit2 = DAP の番地一致、bit3 = ホストの指令。発火は xtrig の源 bit3 |
| `aux` | `0x00000B00` | `0x40` | 補助マイコン（Raspberry Pi Pico）の計測値（docs/superpowers/specs/2026-10-17-m11-aux-pico-design.md）。最新のターゲット電圧（補助マイコンの ch 0）。全チャンネルの値はイベント（種類 6）に時刻付きで残る |
| `sampler` | `0x00000C00` | `0x40` | DAP サンプラ（F4: 変数のライブ観測。docs/superpowers/specs/2026-10-17-f4-live-sampler-design.md）。最大 8 か所（各 1〜16 語）を周期的に読み、1 語ごとにイベント（種類 7）にする |
| `sampler_match` | `0x00000C40` | `0x40` | サンプラの値の一致（R21。docs/superpowers/specs/2026-10-23-sampler-match-trigger-design.md）。サンプラの標本（ACK OK・番地が addr）が (値 xor value) and mask = 0 に入った瞬間（前の標本が不一致）にクロストリガの源 bit5。構成によって無い（FEATURES bit10。無ければ読むとエラー） |
| `xtrig_out` | `0x00000D00` | `0x40` | クロストリガの動作: 出力の端子（docs/superpowers/specs/2026-10-18-u41-xtrig-pin-output-design.md）。動作 k（同時に 2 本）が、ボードの出力の端子の候補（ケイパビリティ OUTPUTS）の 1 本を動かす。源は xtrig と同じ（bit0 ロジアナのトリガ、bit1 DAP の番地一致、bit2 ホストの指令、bit3 多段トリガ、bit4 FPGA の観測の値の一致、bit5 サンプラの値の一致） |
| `obs` | `0x00000E00` | `0x40` | 対象の FPGA の観測（docs/superpowers/specs/2026-10-20-fpga-observation-v2-design.md）。対象の JTAG のユーザーの DR の観測の回路（target_ip/fprobe_obs）をスキャンし続け、記録をイベント 8、時刻合わせをイベント 9 にする |
| `sync` | `0x00000F00` | `0x40` | ボードの間の時刻の同期（docs/superpowers/specs/2026-10-22-modular-design.md §3.2）。親は period ごとにパルスと自分の時刻を sync_out に出し、子は sync_in で受けた組（自分の時刻・親の時刻）をイベント 10 にする |
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
| `0x00000500` | `route` | 5:0 | `to_la` | RW | `0x0` | ロジアナをトリガする源 |
| `0x00000500` | `route` | 13:8 | `to_inject` | RW | `0x0` | 割り込み書き込み（既定は停止）をする源 |
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
| `0x00000900` | `sample_delay` | 10:8 | `jtag` | RW | `0x0` | TDO の取り込みを次の TCK の立ち下がりの直前から遅らせる sys 周期数（0〜7）。TDO が立ち下がりから 2 × half_period 周期より遅れて届くとき（R16。docs/superpowers/specs/2026-10-23-jtag-tdo-sample-design.md） |

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

## sampler_match

| アドレス | レジスタ | ビット | フィールド | 種類 | 初期値 / 値 | 説明 |
|---|---|---|---|---|---|---|
| `0x00000C40` | `addr` | 31:0 | `addr` | RW | `0x0` | 比べる番地（サンプラが読む番地の 1 語） |
| `0x00000C44` | `mask` | 31:0 | `mask` | RW | `0x0` | 比べるビット |
| `0x00000C48` | `value` | 31:0 | `value` | RW | `0x0` | 比べる値 |
| `0x00000C4C` | `control` | 0 | `enable` | RW | `0x0` | 1 で比べる（0→1 で数・状態・時刻を消す。条件を変えるときは 0 にしてから書き、1 に戻す） |
| `0x00000C4C` | `control` | 8 | `matched` | RO | — | 最後の標本が一致 |
| `0x00000C4C` | `control` | 9 | `seen_any` | RO | — | 有効にした後に標本があった |
| `0x00000C50` | `seen` | 31:0 | `seen` | RO | — | 有効にした後に比べた標本（ACK OK・番地が一致）の数 |
| `0x00000C54` | `count` | 31:0 | `count` | RO | — | 発火の回数 |
| `0x00000C58` | `time_lo` | 31:0 | `time_lo` | RO | — | 最後に発火した標本を受けた時刻（下位） |
| `0x00000C5C` | `time_hi` | 31:0 | `time_hi` | RO | — | 同（上位 16 ビット。時刻は 48 ビット = イベントの時刻と同じ幅） |
| `0x00000C60` | `prev_lo` | 31:0 | `prev_lo` | RO | — | その直前の（不一致の）標本を受けた時刻（下位）。条件に入ったのはこの時刻と time の間 |
| `0x00000C64` | `prev_hi` | 31:0 | `prev_hi` | RO | — | 同（上位 16 ビット） |

## xtrig_out

| アドレス | レジスタ | ビット | フィールド | 種類 | 初期値 / 値 | 説明 |
|---|---|---|---|---|---|---|
| `0x00000D00` | `status` | 1:0 | `active` | RO | — | 動作 k が動いている（端子を駆動している） |
| `0x00000D04` | `when0` | 7:0 | `mask` | RW | `0x0` | 端子の並びで見るロジアナの ch（0 なら並びは使わない） |
| `0x00000D04` | `when0` | 15:8 | `value` | RW | `0x0` | 見る ch の値 |
| `0x00000D04` | `when0` | 21:16 | `sources` | RW | `0x0` | 条件にする源（xtrig の源のビット） |
| `0x00000D04` | `when0` | 24 | `any` | RW | `0x0` | 1: 見る ch のどれかが一致で成立、0: すべて一致で成立 |
| `0x00000D08` | `ctrl0` | 0 | `enable` | RW | `0x0` | 有効（0 で状態を消し、駆動しない） |
| `0x00000D08` | `ctrl0` | 5:4 | `mode` | RW | `0x0` | 0 脈、1 保持（無効にするまで）、2 条件の間 |
| `0x00000D08` | `ctrl0` | 8 | `level` | RW | `0x1` | 動いている間に出す値（1 高、0 低） |
| `0x00000D08` | `ctrl0` | 12 | `claim` | RW | `0x0` | 端子を出力に使う（その端子の機能の出力を止め、機能の入力には 1 を入れる） |
| `0x00000D08` | `ctrl0` | 23:16 | `length` | RW | `0x1` | 脈の長さ（刻みの数。0 は脈を出さない） |
| `0x00000D08` | `ctrl0` | 26:24 | `unit` | RW | `0x0` | 脈の刻み = 16^unit 周期 |
| `0x00000D08` | `ctrl0` | 29:28 | `pin` | RW | `0x0` | 動かす端子（ケイパビリティ OUTPUTS の候補の番号） |
| `0x00000D0C` | `when1` | 7:0 | `mask` | RW | `0x0` | 端子の並びで見るロジアナの ch（0 なら並びは使わない） |
| `0x00000D0C` | `when1` | 15:8 | `value` | RW | `0x0` | 見る ch の値 |
| `0x00000D0C` | `when1` | 21:16 | `sources` | RW | `0x0` | 条件にする源（xtrig の源のビット） |
| `0x00000D0C` | `when1` | 24 | `any` | RW | `0x0` | 1: 見る ch のどれかが一致で成立、0: すべて一致で成立 |
| `0x00000D10` | `ctrl1` | 0 | `enable` | RW | `0x0` | 有効（0 で状態を消し、駆動しない） |
| `0x00000D10` | `ctrl1` | 5:4 | `mode` | RW | `0x0` | 0 脈、1 保持（無効にするまで）、2 条件の間 |
| `0x00000D10` | `ctrl1` | 8 | `level` | RW | `0x1` | 動いている間に出す値（1 高、0 低） |
| `0x00000D10` | `ctrl1` | 12 | `claim` | RW | `0x0` | 端子を出力に使う（その端子の機能の出力を止め、機能の入力には 1 を入れる） |
| `0x00000D10` | `ctrl1` | 23:16 | `length` | RW | `0x1` | 脈の長さ（刻みの数。0 は脈を出さない） |
| `0x00000D10` | `ctrl1` | 26:24 | `unit` | RW | `0x0` | 脈の刻み = 16^unit 周期 |
| `0x00000D10` | `ctrl1` | 29:28 | `pin` | RW | `0x0` | 動かす端子（ケイパビリティ OUTPUTS の候補の番号） |

## obs

| アドレス | レジスタ | ビット | フィールド | 種類 | 初期値 / 値 | 説明 |
|---|---|---|---|---|---|---|
| `0x00000E00` | `control` | 0 | `enable` | RW | `0x0` | 1 で始める（TAP を戻し、IR = USER、ID の 2 つのレジスタを読んで確かめ、スキャンを繰り返す）。0 で TAP を Test-Logic-Reset に戻してから線を手放す |
| `0x00000E00` | `control` | 1 | `release` | RW | `0x0` | 1 で TAP を戻さずにすぐ線を手放す（止める） |
| `0x00000E04` | `half_period` | 15:0 | `half_period` | RW | `0xA` | TCK の半周期（sys の周期数、2 以上） |
| `0x00000E08` | `chain` | 5:0 | `ir_len` | RW | `0x4` | 対象の IR の長さ（1〜32） |
| `0x00000E08` | `chain` | 11:6 | `ir_before` | RW | `0x0` | 対象より TDO 側の装置の IR の合計ビット数 |
| `0x00000E08` | `chain` | 17:12 | `ir_after` | RW | `0x0` | 対象より TDI 側の装置の IR の合計ビット数 |
| `0x00000E08` | `chain` | 20:18 | `dr_before` | RW | `0x0` | 対象より TDO 側の装置の数 |
| `0x00000E08` | `chain` | 23:21 | `dr_after` | RW | `0x0` | 対象より TDI 側の装置の数 |
| `0x00000E0C` | `user_ir` | 31:0 | `user_ir` | RW | `0x2` | 対象の USER の命令の値（obs_soft_tap は 2） |
| `0x00000E10` | `words` | 8:0 | `words` | RW | `0x10` | 1 スキャンで受ける記録の語数（1〜256） |
| `0x00000E14` | `sync_period` | 15:0 | `sync_period` | RW | `0x10` | 印（時刻合わせ。op 3、番号つき）を載せる間隔（スキャンの数。0 = 自動では載せない） |
| `0x00000E18` | `cmd` | 15:0 | `cmd` | RW | `0x0` | 命令 |
| `0x00000E1C` | `id_scans` | 7:0 | `id_scans` | RW | `0x0` | ID の応答を待つ上限 |
| `0x00000E20` | `cmd_push` | 0 | `cmd_push` | PULSE | `0x0` | 命令を積む（待ち行列が一杯なら捨てて status.cmd_dropped） |
| `0x00000E24` | `status` | 0 | `running` | RO | — | スキャンを繰り返している |
| `0x00000E24` | `status` | 1 | `id_ok` | RO | — | 開いてから ID が "FOBS" と照合できた（stuck の後も残る: stuck と組で、答えていた後に止まったのか、初めから答えないのかが分かる） |
| `0x00000E24` | `status` | 2 | `stuck` | RO | — | 観測の回路が答えない（見出しの印が 0xA6 でない・ID が違う・ID の応答がid_scans スキャンで来ない）ので止めた |
| `0x00000E24` | `status` | 3 | `cmd_pending` | RO | — | 待ち行列に命令がある |
| `0x00000E24` | `status` | 4 | `overflow_seen` | RO | — | 最後の見出しの溢れの印 |
| `0x00000E24` | `status` | 5 | `cmd_dropped` | RO | — | 一杯で捨てた命令がある（enable の 0→1 で消える） |
| `0x00000E24` | `status` | 6 | `target_running` | RO | — | 最後の見出しの「対象の clk 側が記録中」（対象のリセット等で止まったら 0） |
| `0x00000E24` | `status` | 7 | `driving` | RO | — | 対象の JTAG の線を駆動している（止めた後は 0 になるまで待つ） |
| `0x00000E24` | `status` | 8 | `id_timeout` | RO | — | stuck の理由: id_scans スキャン待って ID の応答が来なかった（0 なら答えが違った） |
| `0x00000E28` | `scans` | 31:0 | `scans` | RO | — | 行ったスキャンの数（イベント 8 の address） |
| `0x00000E2C` | `read_value` | 31:0 | `read_value` | RO | — | 最後の応答の記録: 番号(31:24。応答のたびに 1 増える) | 番地(19:16) | 値(15:0) |
| `0x00000E30` | `id` | 31:0 | `id` | RO | — | 始めに読んだ観測の回路の ID |
| `0x00000E34` | `match` | 15:0 | `mask` | RW | `0x0` | 一致を比べる値のビット |
| `0x00000E34` | `match` | 31:16 | `value` | RW | `0x0` | 一致の値 |
| `0x00000E38` | `match_change` | 15:0 | `change` | RW | `0x0` | 前の値からどれかが変わったら比べるビット（0 = 一致に入った瞬間） |
| `0x00000E38` | `match_change` | 16 | `enable` | RW | `0x0` | 1 で一致を源にする |
| `0x00000E3C` | `match_count` | 31:0 | `match_count` | RO | — | 値の一致が成り立った回数（enable の 0→1 で 0 に） |

## sync

| アドレス | レジスタ | ビット | フィールド | 種類 | 初期値 / 値 | 説明 |
|---|---|---|---|---|---|---|
| `0x00000F00` | `control` | 0 | `enable` | RW | `0x0` | 子として受けて組をイベント 10 にする |
| `0x00000F00` | `control` | 1 | `master` | RW | `0x0` | 親として sync_out に出す |
| `0x00000F04` | `period` | 31:0 | `period` | RW | `0x186A0` | 親のパルスの間隔（タイムベースの刻み）。1 回の送りは 730 刻みで、送りの間に 0 を 20 刻み置くので、約 750 より短いと約 750 ごと |
| `0x00000F08` | `status` | 15:0 | `pairs` | RO | — | 受けた組の数（CRC が合ったもの。16 ビットで一周） |
| `0x00000F08` | `status` | 31:16 | `crc_errors` | RO | — | CRC が合わず捨てた組の数（0xFFFF で止まる） |
