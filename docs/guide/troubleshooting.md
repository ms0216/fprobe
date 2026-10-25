# トラブルシューティング（症状 → 原因 → 対処）

コードと [../boards/pynq-z2.md](../boards/pynq-z2.md) に書かれた事実だけを載せています。確かめていないことは「未確認」と書きます。

## つなぐ・書き込む

| 症状 | 原因 | 対処 |
|---|---|---|
| `fprobe` の CLI・Python・実機試験が機器を開けない | `fprobe-daemon`（MCP が起こしたものも）が機器を握っている。デーモンは MCP の終了後も常駐する | デーモンを止める。Windows の libusb は機器の全インターフェースを 1 つのハンドルで握るので、pyOCD と `fprobe.Device` を別々に開くこともできない（`Session` を使う） |
| 書き込み（`fprobe_gw program` / `build --program`）が拒否される | 対象の口が一意でない、または IDCODE が一致しない（Primer は `0x0000081B`。PYNQ-Z2 は Zynq-7020 であること） | `python -m fprobe_gw detect` で見える機器を確かめる。同じ FT2232 系のボードが複数あるとき、書き込みツールは USB シリアル番号から口を引いて照合するので、取り違えたまま書くことはない |
| ビットストリームが書けない（「成功したビルドの印が無い」） | ビルドの印 `inputs.sha256` が隣に無い | 外で作ったビットストリームなら `--unverified`。PYNQ の `--relay` には印が要る |
| PYNQ-Z2 に Ethernet でつながらない（IP が変わった） | 既定は `192.168.77.2`（USB の LAN アダプタで直結し、PC 側 `192.168.77.1/24`）。接続先は環境変数 `FPROBE_PYNQ_HOST`（ホスト側の補助ツール用）で替える | `ping 192.168.77.2`。IP を替えたら `fprobe-mcp --eth <新しい IP>` / `fprobe-daemon --eth <新しい IP>` も合わせる |
| PYNQ-Z2 の Linux が固まった | シフタ有効のまま PL を書き直した／PS7 を持たない回路でシフタを有効にした（2026-10-20）。応答の無い PL の番地を読んでも固まる | `fprobe_gw build/program --board pynq_z2` の手順（書く前に中継を止めてシフタを切る）を使う。手で書かない。PYNQ の Linux 経由の書き込み（`pynq.Bitstream`）も固まった（2026-10-19） |
| 中継 `fprobe-relay` が止まる | 終了コード 2 = 印が無い、3 = PL の ID・版が違う、4 = 動作中に ID が読めなくなった | `python -m fprobe_gw pynq status`。回路を書き直したら `build --program` / `program --relay` で中継を起動し直す。2 と 3 は systemd が起動し直さない |
| UDP のデータが欠ける | UDP は落ちうる | `s.link.lost_frames`（FPGA → PC の通し番号の欠けを数える）。バイト列のチャンネルの欠けは黙って見逃さない設計 |

## ターゲットに（SWD / JTAG で）つながらない

1. 未接続のまま `target_diagnose`（MCP）を呼ぶ。電圧 → ピン → SWD → JTAG のチェーン → DP → AP・ROM テーブル → コア → 部品の判別 → 保護を
   順に調べ、段ごとに読めた事実（`steps` の `status`）を返す。試す速さは `frequencies_hz` に自分で渡す（暗黙に下げない）。
2. **SWD の ACK エラー**: 応答が OK 以外（WAIT / FAULT / 応答なし）のとき、どの ACK だったかが `target_diagnose` の SWD の段に出る。
   配線（SWCLK・SWDIO・GND）、ターゲットの電源（3.3 V のみ）、速度（`frequencies_hz` を下げて試す）、保護（STM32 の RDP・nRF の APPROTECT）を段ごとに確かめる。
   ACK ごとの原因の一覧はコードに無い（診断は事実を返すだけで、断定しない）。
3. Primer で JTAG / SWD / ロジアナの専用の入力が必要なとき、`target_connect` は要る線の構成でなければ自動で書き替える（数十秒、結果の
   `design_switch`。ロジアナ・記録の状態は消える）。意図しない書き替えを避けたいときは `probe_design_get` で今の構成を確かめる。

## FPGA の観測（fpga_obs）

| 症状 | 原因 | 対処 |
|---|---|---|
| `fpga_obs_open` が失敗: 「観測の回路が答えない（ID …）」 | ID が読めない。コードが挙げる確かめる点は、鎖の位置、`user_ir`、配線、TCK の速さ（1 クロック版は対象の clk の 1/8 以下）。ID の値が `0x00000000` や `0xFFFFFFFF` のときの読み方はコードに書かれていない（未確認） | 配線（TCK・TMS・TDI・TDO・GND、TDI/TDO の取り違え）、対象の電源と書き込み、`chain`（`ir_len` 等）、`user_ir`（`obs_soft_tap` は 2）、`half_period` を大きくして TCK を下げる |
| `fpga_obs_open` が失敗: 「ID の応答が id_scans = N スキャンで来なかった」 | `id_scans` を渡して上限まで待ったが、ID の応答が来なかった。`fpga_obs_status` が `stuck` かつ `id_timeout` になる | `id_scans` は使う側が渡す上限（0 = 上限なし）。上の ID が読めない場合と同じ点を確かめる |
| `fpga_obs_status` が `stuck` | 観測の回路が答えなくなって止まった（対象の FPGA を書き直した・電源を入れ直した等） | `fpga_obs_status` の `config` と同じ設定で `fpga_obs_open` し直す。見続けるなら `auto_reopen=true`（1 秒ごとに開き直す。止まった回路の線をまた動かすので、使うかは利用者が選ぶ） |
| 値が化ける・欠ける | TCK を上げすぎ。Primer の J12 のジャンパ線の実測（2026-10-20）: 1・10・12.5 MHz は欠け・溢れ 0、16.7 MHz からは TDO が化けた（往復の遅れが半周期に収まらない）。環境が違えば境目も違う（未確認） | `half_period` を大きくして TCK を下げる。線は短く、GND を近くに |
| `fpga_obs_read` が空 | `events_start` をしていない、または観測を開いていない | `events_start`。`fpga_obs_status` の `running`・`scans` を見る |
| `value = null` が出る | 溢れ（FIFO が一杯で値の変化を記録できなかった）。`lost` が失った変化の数（`LOST_BITS` の上限で止まる） | `FIFO_DEPTH` を大きく、TCK を上げる、観測する信号を減らす（使う側の判断） |
| 時刻が `null`（`time_basis` が `null`） | 時刻合わせの印が来ないまま鎖が終わった／窓の最初の鎖の始めの前の値 | `sync_period` を 0 にしていないか（標本型を除き印が要る）。窓を広げる |
| `recording_correlate` の `gaps` | データの欠け: `reads_missing` / `read_failed` / `pins_missing` / `fpga_lost` / `events_error` / `recording_dropped`。その区間の結果・候補は数えられていない | 結果を読む前に `gaps`・`warnings`・`judged`・`excluded`・`recording_status` の `watch_running`・`events_errors` を確かめる。0 回は「原因でない」ではない |
| `recording_status` の `watch_running` が false | MCU のメモリの読みが動いていない（記録にメモリが入らず欠ける） | `watch_status` の `running`、`watch_set` をやり直す |

## 既知の制限

- **イベントの書き込み数 `EVENTS_WRITTEN`（イベントリングの `written`）は 32 ビットで回り込みます。** ホストは `written` が前回見た値より
  小さいことを「記録の始め直し」と見る（`EventCursor`、`probe_window` など）ため、回り込みと区別できません。回り込むまでの時間は
  2^32 ÷（イベントの毎秒の件数）です。これは計算式による見積りで、実測ではありません。毎秒 100 万件なら約 71 分、毎秒 7 千件の FPGA 観測なら約 7 日。
  対処は未実装です（世代カウンタを回路かプロトコルに足す設計判断が要る）。重い記録を長時間行うときは、1 回の記録を回り込みの前に区切ってください。
- FPGA の観測（`fpga_obs`）は PYNQ-Z2 の `fprobe_pynq`・`fprobe_pynq_deep` だけ（[../architecture/feature-matrix.md](../architecture/feature-matrix.md)）。
- 1 クロック版の観測 IP は TCK ≤ 対象の clk / 8。
- 起動の途中（PL が未構成の間）の PYNQ-Z2 の端子の状態は未確認（[../boards/pynq-z2.md](../boards/pynq-z2.md)）。

## MCP・デーモン

[mcp.md](mcp.md) の「よくある失敗」を参照（デーモンが機器を握っている、LAN 公開のトークン、一時停止中、道具の名前）。
