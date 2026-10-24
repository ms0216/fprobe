# ヒューリスティックの監査: ゲートウェア（gateware/rtl・gateware/fprobe_gw）

2026-10-23。担当範囲は `gateware/rtl` の .vhd すべてと `gateware/fprobe_gw` の .py すべて（テストは除く）。
コードは変更していない。分類は H（ヒューリスティック）・F（事実・仕様で決まる規則）・E（使う側が明示して設定する値）・
O（操作・構造の定数）・?（判断がつかない）。

## 件数のまとめ

| 分類 | 件数 |
|---|---|
| H | 5 |
| ? | 1 |
| F（判定・加工に関わるもの） | 27 |
| E（判定・加工に関わるもの） | 13 |
| O（判定・加工に関わるもの） | 21 |

VHDL の数値の定数（ビット幅・番地・状態の番号・規格の値など、明らかな F/O）は 1 件ずつではなく §1 の件数と
§4 のファイルごとの件数で示す。

## 1. 実行した検索

すべて ripgrep 15.2.0。VHDL は大文字小文字を区別しない（`-i`。VHDL が区別しないため）、Python は区別する。

### VHDL（gateware/rtl、130 ファイル）

| # | パターン | ヒット（行 / ファイル） | うち生成物 | うち rtl/usb | うち rtl/boards | 1 件ずつ見た後の扱い |
|---|---|---|---|---|---|---|
| 1a | `:=\s*\d+` | 437 / 88 | 81 | 68 | 69 | 信号の初期値・ビット幅・generic の既定。下記の判定に関わるもの以外は O/F として除外 |
| 1b | `generic` | 194 / 73 | 0 | 23 | 47 | generic 宣言・generic map。数値の既定を持つ generic は別に全件を列挙して確認（`^\s+[A-Z_]+\s*:\s*(positive\|natural\|integer)\s*:=\s*\d`、88 件）。判定に関わるのは §2・§3 のもの |
| 1c | `constant\s+\w+\s*:\s*\w+\s*:=` | 422 / 53 | 250 | 42 | 62 | 生成物（CSR の番地・caps・USB ROM）250 件・ボードのピン番号/PLL 62 件・DAP の命令 ID 等。生成物とボード以外の数値の定数（約 130 件）は全件を目で確認 |
| 1d | `=\s*\d+\b` | 928 / 100 | 95 | 162 | 77 | 比較の多くは状態の番号・ビットの位置・0/1。2 桁以上の数との比較（`(>=?\|<=?\|/=\|=)\s*\d{2,}`）を別に全件（約 90 件）確認。コメントだけの行 90 |
| 1e | `>\s*\d+\|<\s*\d+` | 129 / 39 | 19 | 4 | 27 | 1d の 2 桁以上の比較の確認に含む |
| 1f | `to_unsigned\(\d+` | 34 / 14 | 0 | 1 | 0 | 2 桁以上は dap_cmd_config の 100（E）・65535（F）・obs_jtag_engine の 256（O）・udp_stack（F）だけ |
| 2 | `timeout\|timer\|count\|stuck\|limit\|max\|retry\|watchdog\|expired` | 774 / 75 | 70 | 137 | 25 | `count` は大半が数の信号（scan_count 等）・ジェネリック名。`timeout\|watchdog\|retry\|expired\|limit\|timer` に絞った 120 行余りを全件確認。コメントだけの行 46 |
| 3 | `推定\|経験\|目安\|みなす\|とみなし\|らしい\|疑い\|glitch\|noise\|filter\|debounce\|stuck` | 62 / 24 | 26 | 2 | 6 | 全件確認。`filter` は CSR で設定する la_filter（E）、`stuck` は obs_jtag_engine（H の 1）、`みなす` 2 件（CTS の既定 = O、usb_ep_in の小休止 = O）、`推定` は sampler_match の「推定しない」 |

除外の理由（共通）: 生成物（`rtl/*/generated/`）は fprobe_gw の CSR 表・caps・USB ディスクリプタから作られた番地・
ビット幅・既定値で、元の Python 側で確認した。ボードのパッケージ・トップの数値はピン番号・PLL の分周・FIFO の深さ・
パケットの大きさ（O）。USB・Ethernet・JTAG・SWD・UART の時間とビットの位置は規格で決まる（F）。

### Python（gateware/fprobe_gw、41 ファイル）

| # | パターン | ヒット（行 / ファイル） | 1 件ずつ見た後の扱い |
|---|---|---|---|
| 4a | `[<>]=?\s*-?\d*\.?\d+` | 48 / 13 | ビットの位置（`1 << n`）27 件、入力の検査（範囲・長さ）、タイミングの合否（F）、Vivado の VCO 範囲（F）。全件確認 |
| 4b | `\b\d+\.\d+\b` | 69 / 22 | 周波数・ns の設計値（ボードの値 = F/O）、版番号、パス。caps.py:41 の 0.5（H）、pll.py:75 の 0.005（?）。全件確認 |
| 4c | `=\s*-?\d+(\.\d+)?\s*[,)]` | 82 / 18 | ピンの drive・番号、ディスクリプタの既定引数、CSR の既定値（E）、時間切れ（O）。全件確認 |
| 4d | `^[A-Z_]+\s*=` | 181 / 30 | 種類の番号・ビットの印・パス・ID（O）。caps.py の遅れの定数（H 2 件を含む）、build.py / vivado_build.py / programmer.py の時間切れ（O）。全件確認 |
| 4e | `推定\|経験\|目安\|typical\|guess\|heuristic\|みなす\|warn\|retry\|timeout` | 26 / 9 | `timeout` はサブプロセスの時間切れ（O）、`retry` は読み取り専用属性の削除の再試行（O）、`warn` は Gowin の警告の表示の設定（O）、`目安` は aux.samples の説明文（F、言い回しだけ）、`みなす` は CTS の既定（O） |

## 2. H と ? の全件

### H

| # | ファイル:行 | コード | 何をしているか | なぜ H か |
|---|---|---|---|---|
| H1 | gateware/rtl/debug/obs_jtag_engine.vhd:97（判定は 462・468） | `constant ID_SCANS : natural := 16; -- この数のスキャンで ID が来なければ答えない` / `if bad = '1' or (id_ok_r = '0' and id_wait = ID_SCANS) then stuck_r <= '1';` | 前の記録を吸い出した後、16 スキャンの間に ID の応答（"FOBS"）がそろわなければ「観測の回路が答えない」として stuck を立て、TAP を戻して止まる。stuck は CSR obs.status.stuck で結果として返る | 「N スキャン応答が無ければ答えないとみなす」を CSR でも generic でも変えられない固定値で持ち、その判定（stuck）を結果として返している。16 の根拠は書かれていない（仕様書にも無い）。対象の clk が遅い・CDC の遅れが大きい対象では、答えている回路を stuck と判定しうる |
| H2 | gateware/rtl/debug/dap_sampler.vhd:40（使うのは 445） | `RETRIES : positive := 255` / `elsif phy_rsp_ack = ACK_WAIT and tries < RETRIES then` | DAP サンプラの読みが WAIT を 255 回続けて受けたら諦めて errors に数え、そのか所を飛ばす | WAIT の再試行の上限を、ホストが DAP_TransferConfigure で設定する wait_retry とは別の固定値で持つ。fprobe_core はこの generic を渡しておらず暗黙の既定のまま。上限を超えたことが errors（誤りの数）という結果になる。errors は WAIT の上限超えと FAULT・無応答・パリティを区別せずに数える |
| H3 | gateware/rtl/debug/swd_injector.vhd:19（使うのは 203） | `RETRIES : positive := 255` / `elsif phy_rsp_ack = ACK_WAIT and tries < RETRIES then ... else result_r <= "01";` | 割り込みの書き込み（停止等）が WAIT を 255 回続けて受けたら中止し、結果 "01"（失敗）を返す | H2 と同じ。暗黙の固定の上限で「失敗」の結果を決めている。ホストの wait_retry と別の値 |
| H4 | gateware/fprobe_gw/caps.py:41（host/fprobe/generated.py:393 に写る） | `SYNC_RX_PHASE_TICKS = 0.5 # 子: 非同期に来る辺を自分の時計で取り込む位相の平均` | ボードの間の時刻の同期で、子が非同期の辺を取り込む遅れ（実際は 0〜1 刻みのどこか）を平均 0.5 刻みとしてホストに渡し、時刻の換算に使わせる | 「辺の位相が一様に分布するなら平均は 0.5」という仮定に頼る推定（CLAUDE.md の例「ランダムな時刻なら」の見込み）。事実は「0 以上 1 未満の刻み」の範囲で、1 つの点に縮めている。コメントの根拠は「実機で約 0.5 刻み早く出た」という 1 つの観測 |
| H5 | gateware/fprobe_gw/caps.py:160（ケイパビリティ TYPE_LA_DELAY、caps.py:60・206） | `return 2 * (front + LA_INTERNAL_DELAY) + 1, 2 * (front + LA_DEEP_DELAY) + 1` | ロジアナの時刻がピンの変化より遅い分を「段数 + 非同期の平均 0.5 刻み」（半刻みの単位で +1）として申告し、ホストが時刻を直す | H4 と同じ。段数（3・4 刻み）は回路で決まる事実だが、+0.5 は位相が一様という仮定による平均。事実は「段数〜段数+1 刻みの範囲」 |

### ?

| # | ファイル:行 | コード | 何をしているか | 判断がつかない理由 |
|---|---|---|---|---|
| ?1 | gateware/fprobe_gw/toolchain/pll.py:75（判定は 93） | `tolerance: float = 0.005` / `if best is None or best[0][0] / fout_mhz > tolerance: raise PllSolveError(...)` | 目的の周波数との誤差が 0.5% を超える PLL の解しか無ければビルドを失敗にする。呼び出し（primer20k.py:106・130、nano9k.py:78）は既定のまま | ビルドの合否を暗黙の既定の許容で決めている点は原則の「既定の暗黙の値」に当たる。一方、得られた周波数は SYS_CLK_HZ / TIMEBASE_HZ としてそのまま申告され、測定の結果には推定が入らない。0.5% の根拠（USB・UART の許容等）は書かれていない |

## 3. 判定・加工に関わる F・E・O

### F（27）

- gateware/rtl/usb/usb_timing_pkg.vhd:8-15 — USB 2.0 の時間（リセット SE0 5 us・チャープ 2 ms・HS の無活動 3 ms 等。USB 2.0 7.1.7.5/7.1.7.6）
- gateware/rtl/usb/usb_reset.vhd:221-278 — 上の時間でリセット・HS/FS・サスペンドを判定（規格）
- gateware/rtl/usb/usb_protocol.vhd:28-29 — OUT/SETUP 後のデータ・IN 後の ACK を 128 周期（2.1 us）待つ（USB のバスのターンアラウンドの時間切れより長い値）
- gateware/rtl/debug/swo_manchester_rx.vhd:96-108 — T−T/4 未満は境目の変化、以上は中央の変化、T+T/4 で終わり（Manchester の符号の形で決まる中点。frame_error は端数ビットという事実）
- gateware/rtl/serial/uart_rx.vhd:44-45 — 半ビットで標本・停止ビット 0 で frame_error（8N1）
- gateware/rtl/core/sync_link.vhd:1-14 — パルスの中央でまだ 1 かを確かめてから読む・CRC が合わなければ捨てて crc_errors に数える（自前の線の形式で決まる）
- gateware/rtl/debug/obs_jtag_engine.vhd:392 — 見出しの印が 0xA6 でなければ bad → stuck（観測の回路 v2 の DR 形式で決まる）
- gateware/rtl/eth/rmii_rx.vhd:131・144 — 64 バイト未満・FCS の残差が合わないフレームを捨てる（IEEE 802.3）
- gateware/rtl/eth/udp_stack.vhd:315-323 — IP のヘッダのチェックサム・長さ・宛先の検査（RFC 791/768）
- gateware/rtl/debug/dap_cmd_swo.vhd:170 — SWO の分周を 65535 で飽和し、実際の速さを応答で返す（CMSIS-DAP の SWO_Baudrate は実際の値を返す規則）
- gateware/rtl/debug/dap_cmd_config.vhd:325 — SWJ_Clock の分周を 65535 で飽和（同上、PHY の 16 ビット）
- gateware/rtl/debug/swo_receiver.vhd:7-8 — フレーム異常・下流の詰まりで捨てたバイトを error / overrun として申告（黙って捨てない）
- gateware/rtl/core/aux_link.vhd:5-6 — 組の途中に先頭が来たら捨てて errors に数える（自前の 3 バイトの形式）
- gateware/rtl/la/la_encoder.vhd:9 — 拍を受けられなければ捨てて overrun を立てる（事実の申告）
- gateware/rtl/la/la_pulse.vhd:3-12 — 始まりを見ていない脈は幅が分からないので数えない（推定しない）
- gateware/rtl/debug/sampler_match.vhd:6 — 一致に入った時刻は「その間のどこか」とし推定しない
- gateware/rtl/debug/dap_monitor.vhd:9-10 — 応答 OK の転送だけ一致を数え、WAIT・FAULT もイベントには出す
- gateware/fprobe_gw/toolchain/timing.py:24 — setup/hold の TNS ≥ 0 でタイミングを満たす
- gateware/fprobe_gw/toolchain/timing.py:102 — 経路の表で余裕が負の経路を失敗とする
- gateware/fprobe_gw/toolchain/build.py:293-310 — SDC で制約したクロックがレポートに無い・周波数が違えば失敗
- gateware/fprobe_gw/toolchain/build.py:231 — `_CONSTRAINT_TOLERANCE = 1e-4`（レポートの MHz 小数 3 桁表示の丸めの分だけ許す）
- gateware/fprobe_gw/toolchain/build.py:140-185 — build_best: タイミングを満たした版のうち測った余裕（Fmax/制約 − 1）が最大のものを選ぶ
- gateware/fprobe_gw/toolchain/build.py:234・252 — 合成の警告 EX1998・IP 生成の出力の "ERROR" でビルドを失敗にする（CLAUDE.md の規則）
- gateware/fprobe_gw/toolchain/vivado.py:50 — MMCM の VCO 600〜1200 MHz（Xilinx のデータシート）
- gateware/fprobe_gw/toolchain/vivado.py:187 — Vivado の TNS が負なら失敗
- gateware/fprobe_gw/toolchain/pll.py:26-29 — Gowin rPLL の PFD・VCO・出力の範囲（データシート）
- gateware/fprobe_gw/csr/fprobe_map.py:239 — aux.samples は受け取った数（事実）。説明文の「動いているかの目安」は言い回しだけで回路に判定は無い


### E（13）

- gateware/rtl/debug/dap_cmd_config.vhd:83・187 — wait_retry の既定 100（ホストが DAP_TransferConfigure で設定。既定は Arm の CMSIS-DAP の参考実装と同じ）
- gateware/rtl/debug/dap_link.vhd:95・114 — WAIT を wait_retry 回まで再試行（上の値）
- gateware/rtl/debug/dap_cmd_transfer.vhd:307 — 値の一致の読みを match_retry 回まで（DAP_TransferConfigure で設定）
- gateware/rtl/debug/dap_cmd_config.vhd:183-186 — クロック・turnaround 1・idle 0 の既定（CMSIS-DAP の既定。ホストが SWJ_Clock・SWD_Configure・TransferConfigure で設定）
- gateware/rtl/la/la_filter.vhd・gateware/rtl/core/generated/csr_la.vhd:35-36 — 捨てる幅 filter.cycles と ch filter.channels は CSR（既定 0 = 捨てない）
- gateware/rtl/la/la_pulse.vhd:3-12 — 幅の min/max・一定時間は CSR la.pulse_min/pulse_max
- gateware/rtl/core/trig_seq.vhd:86・112-115 — 段の制限時間 within は CSR（0 = 制限なし）
- gateware/rtl/debug/dap_sampler.vhd:280-286 — 読みの周期・jitter は CSR sampler
- gateware/rtl/debug/obs_jtag_engine.vhd:46-52 — 値の一致の mask/value/change は CSR obs.match
- gateware/fprobe_gw/csr/fprobe_map.py:327-338 — obs の half_period 10・ir_len 4・user_ir 2・words 16・sync_period 16 は CSR の既定（PC が設定）
- gateware/fprobe_gw/csr/fprobe_map.py:379 — sync.period 100000 は CSR の既定
- gateware/fprobe_gw/csr/fprobe_map.py:201 — sample_delay.swd 1 は CSR の既定
- gateware/rtl/core/fprobe_core.vhd:56-58 — LA_FILTER / LA_PULSE の generic はケイパビリティ FEATURES で申告

### O（21）

- gateware/rtl/usb/usb_device.vhd:36・usb_ep_in.vhd:22 — FLUSH_CYCLES 600（10 us 入力が途切れたら途中のパケットを送る。区切りだけでデータは変えない）
- gateware/rtl/debug/swo_receiver.vhd:27 — 開始の窓 32 周期（下流が前の残りを捨てる間。受信を窓の分遅く始めたのと同じ）
- gateware/rtl/usb/usb_control_ep.vhd:31 — 内部の要求ハンドラが 32 周期で答えなければ STALL（内部の部品の約束）
- gateware/rtl/hostlink/serial_link.vhd:23・frame_mux.vhd:26・eth_link.vhd:18 — IDLE_CYCLES 64（バイト列のフレームを閉じる区切り）
- gateware/rtl/la/la_deep_buffer.vhd:101-107 — STREAM_CHUNK 256・LA_MIN 64・EV_MIN・AGE_BITS 14（ストリームのかたまりの大きさと出す時機）
- gateware/rtl/la/la_deep_buffer.vhd:21・29 — 未読がリング − STREAM_CHUNK・イベントの未読がリングの半分で書き込みを止める（overrun として申告する資源の方針）
- gateware/rtl/la/la_deep_buffer.vhd:582 — 読み出しの終わりの余裕 drain 31 周期
- gateware/rtl/la/event_ring.vhd:5 — 古いものから上書き（ev_lost で申告）
- gateware/rtl/boards/primer20k/primer20k_fprobe_top.vhd:325・fprobe_gw/csr/fprobe_map.py:67 — CTS の端子を選んでいなければ出ているとみなす（設定で選ぶ）
- gateware/rtl/debug/dap_cmd_config.vhd:351 — IR 長 0 または 32 超は DAP_ERROR（実装の上限）
- gateware/rtl/debug/obs_jtag_engine.vhd:214・553 — ir_len 32 超・words 256 超の制限（実装の上限）
- gateware/rtl/debug/dap_sampler.vhd:24-25 — FAULT 以外の誤りでは SELECT だけ戻す（動作の選び。結果の値は加工しない。ただし errors は種類を区別しない、H2 を参照）
- gateware/rtl/designs/bringup_core.vhd:21 — TX_MARGIN_PPM 5000（立ち上げ用の設計の送信の速さ）
- gateware/fprobe_gw/toolchain/build.py:30 — BUILD_TIMEOUT_S 20 分（引数 timeout_s で変えられる。失敗の理由は「N 秒で終わらない」と事実で出す）
- gateware/fprobe_gw/toolchain/vivado_build.py:22 — BUILD_TIMEOUT_S 40 分（同上）
- gateware/fprobe_gw/toolchain/programmer.py:26 — 書き込みの時間切れ 300 秒
- gateware/fprobe_gw/pynq_setup.py:42・45・49・74・81、toolchain/pynq_loader.py:61・79、toolchain/ftdi.py:92、toolchain/build.py:62、toolchain/forgefpga.py:212 — ssh・サブプロセスの時間切れ
- gateware/fprobe_gw/toolchain/pll.py:89 — 誤差が同じなら PFD の高い解を選ぶ（周波数は同じ）
- gateware/fprobe_gw/boards/primer20k.py:45 — 書き込みのケーブル 1 MHz（中断後に応答しなくなった経験から。書き込みの道具の設定）
- gateware/fprobe_gw/boards/primer20k.py:120 — BSRAM の行を避ける配置の制約（配置の結果から）
- gateware/fprobe_gw/designs.py:105-106・228-229 — cdc_max_delay_ns 8.0（最短の周期より短く）・-place_option/-route_option

## 4. 担当範囲と対象にしたファイル

- gateware/rtl の .vhd: 130 ファイル（生成物 30 を含む）。§1 の検索はすべて `rtl` 全体に `-g '*.vhd'` で実行した。
- gateware/fprobe_gw の .py: 41 ファイル（テストは fprobe_gw の下に無い）。§1 の検索はすべて `fprobe_gw` 全体に `-g '*.py'` で実行した。

VHDL の数値（検索 1a〜1f の合計、重複行を除いて 1317 行、105 ファイル）のファイルごとの件数（判定・加工に関わるものは §2・§3 に別に挙げた。残りはビット幅・番地・状態の番号・規格の値・FIFO の深さ = F/O）:

| ファイル | 件数 | ファイル | 件数 |
|---|---|---|---|
| boards/nano9k/nano9k_fprobe_top.vhd | 2 | la/la_capture.vhd | 5 |
| boards/nano9k/nano9k_pkg.vhd | 15 | la/la_deep_buffer.vhd | 25 |
| boards/primer20k/gowin_rpll.vhd | 7 | la/la_encoder.vhd | 2 |
| boards/primer20k/primer20k_bringup_top.vhd | 1 | la/la_filter.vhd | 6 |
| boards/primer20k/primer20k_ddr3.vhd | 3 | la/la_front.vhd | 3 |
| boards/primer20k/primer20k_fprobe_eth_top.vhd | 7 | la/la_mem_csr.vhd | 6 |
| boards/primer20k/primer20k_fprobe_serial_top.vhd | 2 | la/la_pulse.vhd | 5 |
| boards/primer20k/primer20k_fprobe_top.vhd | 12 | la/event_ring.vhd | 10 |
| boards/primer20k/primer20k_pkg.vhd | 24 | mem/axi_mem_port.vhd | 17 |
| boards/primer20k/primer20k_ulpi_probe_top.vhd | 1 | mem/la_mem_writer.vhd | 5 |
| boards/primer20k/primer20k_ulpi_reset.vhd | 2 | mem/mem_reader.vhd | 10 |
| boards/primer20k/primer20k_usbtest_top.vhd | 6 | mem/mem_test.vhd | 5 |
| boards/pynq_z2/pynq_z2_blinky_top.vhd | 3 | mem/mem_test_sys.vhd | 4 |
| boards/pynq_z2/pynq_z2_fprobe_top.vhd | 11 | serial/uart_bridge.vhd | 9 |
| boards/pynq_z2/pynq_z2_pkg.vhd | 18 | serial/uart_rx.vhd | 4 |
| boards/pynq_z2/xilinx_mmcm.vhd | 8 | serial/uart_tx.vhd | 7 |
| common/async_fifo.vhd | 2 | usb/generated/usb_descriptors_pkg.vhd | 14 |
| common/reset_sync.vhd | 1 | usb/ulpi_link.vhd | 18 |
| common/sdp_ram.vhd | 2 | usb/usb_cdc_acm_requests.vhd | 15 |
| common/stream_unpack.vhd | 6 | usb/usb_cdc_notify.vhd | 3 |
| common/sync_fifo.vhd | 4 | usb/usb_control_ep.vhd | 50 |
| common/text_report.vhd | 4 | usb/usb_device.vhd | 11 |
| common/text_report_pkg.vhd | 2 | usb/usb_ep_in.vhd | 31 |
| common/udiv.vhd | 7 | usb/usb_ep_out.vhd | 1 |
| common/udiv_shared.vhd | 1 | usb/usb_protocol.vhd | 8 |
| common/util_pkg.vhd | 2 | usb/usb_reset.vhd | 18 |
| core/aux_link.vhd | 14 | usb/usb_rx_packet.vhd | 13 |
| core/caps_rom.vhd | 1 | usb/usb_timing_pkg.vhd | 9 |
| core/control_plane.vhd | 15 | usb/usb_types_pkg.vhd | 15 |
| core/csr_bus_pkg.vhd | 2 | debug/dap_cmd_config.vhd | 34 |
| core/fprobe_core.vhd | 42 | debug/dap_cmd_sequence.vhd | 23 |
| core/generated/csr_events.vhd | 1 | debug/dap_cmd_swo.vhd | 6 |
| core/generated/csr_map_pkg.vhd | 41 | debug/dap_cmd_transfer.vhd | 14 |
| core/generated/csr_sampler_match.vhd | 1 | debug/dap_link.vhd | 3 |
| core/generated/csr_timebase.vhd | 1 | debug/dap_monitor.vhd | 3 |
| core/pin_select.vhd | 2 | debug/dap_packet.vhd | 13 |
| core/sync_link.vhd | 22 | debug/dap_pkg.vhd | 30 |
| core/trig_seq.vhd | 6 | debug/dap_processor.vhd | 6 |
| core/xtrig_out.vhd | 13 | debug/dap_sampler.vhd | 28 |
| designs/bringup_core.vhd | 16 | debug/generated/dap_info_pkg.vhd | 5 |
| designs/generated/*_caps_pkg.vhd（10 ファイル） | 各 20 | debug/jtag_phy.vhd | 24 |
| designs/ulpi_probe_core.vhd | 13 | debug/obs_jtag_engine.vhd | 36 |
| eth/rmii_rx.vhd | 10 | debug/sampler_match.vhd | 1 |
| eth/rmii_tx.vhd | 18 | debug/swd_injector.vhd | 14 |
| eth/udp_stack.vhd | 48 | debug/swd_phy.vhd | 20 |
| hostlink/eth_link.vhd | 8 | debug/swo_manchester_rx.vhd | 9 |
| hostlink/frame_mux.vhd | 47 | debug/swo_receiver.vhd | 13 |
| hostlink/serial_link.vhd | 25 | | |
| hostlink/zynq_frame_link.vhd | 7 | | |

数値に当たらなかった 25 ファイル（cdc_value・sync_bit・stream_slice・csr_absent・csr_decoder・生成物の CSR ブロックの多く等）も
検索 2・3 の対象には含めた。
