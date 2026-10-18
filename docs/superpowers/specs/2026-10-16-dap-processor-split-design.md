# dap_processor の分割 設計

- 日付: 2026-10-16
- 親: `2026-10-15-m4-swd-cmsis-dap-design.md` §4、`2026-10-16-m5-jtag-design.md` §3（sys を 90 MHz に下げた判断）

## 1. 動機と完了条件

dap_processor は 1 つの状態機械（約 80 状態）に、パケットの枠・PHY への転送・全コマンドを持つ。共通手順（引数の読み取り・
応答の書き込み・転送）から戻る先 `ret` が全状態の多入力 mux になり、JTAG の追加で 100 MHz に届かなくなった
（sys を 90 MHz に下げて凌いでいる）。コマンドを足すたびに悪化する構造なので分ける。

| 完了条件 | 測り方 |
|---|---|
| dap_processor のポート（外から見た振る舞い）は変えない | 既存のテスト（dap_processor_tb / swd_injector_tb / swo_tb / dap_jtag_tb）がそのまま通る |
| sys を 100 MHz 台に戻してタイミングを満たす | 合成（place/route は既定でも満たすこと） |
| 実機で回帰なし | HIL 全件 |

## 2. 構成

```
dap_processor（外形は今と同じ）
 ├ dap_packet          パケットの枠: コマンド ID を読み、担当ユニットを起動。引数の読み取り（get）、応答への追記（put）、
 │                     先頭の書き直し（ユニットの完了時に header）、要求の残りの読み捨て、応答バッファ（BSRAM）と送出、
 │                     未対応コマンド（0xFF）、TransferAbort（応答なし）
 ├ dap_link            PHY への命令: 転送（WAIT を wait_retry 回まで再試行、応答の符号化、値一致の比較）と生の命令（シーケンス・ピン）
 ├ dap_cmd_config      Info / HostStatus / Connect / Disconnect / TransferConfig / SWD_Configure / Delay / ResetTarget /
 │                     SWJ_Clock / JTAG_Configure。設定（enable・SWCLK・ターンアラウンド・再試行・JTAG のチェーン）を持ち、
 │                     JTAG の装置番号からチェーン上の位置を出す
 ├ dap_cmd_swo         SWO_Transport / Mode / Baudrate / Control / Status / Data（分周の割り算器を自分で持つ）
 ├ dap_cmd_sequence    SWJ_Pins / SWJ_Sequence / SWD_Sequence / JTAG_Sequence（nRESET の同期化と µs の待ちを持つ）
 └ dap_cmd_transfer    Transfer / TransferBlock / WriteAbort / JTAG_IDCODE（DAP.c と同じ意味論。装置番号を出す）
```

### ユニットの共通 I/F（dap_unit_pkg）

- `start`（1 周期）で起動、`done`（1 周期）で終了。終了時に `header`（応答の [1..header_n] を書き直す値）と `header_n`。
- 引数: `get_req`（1 周期）+ `get_n`（1〜8）→ `get_ack`（1 周期）+ `arg`（64 ビット、リトルエンディアン。パケットが尽きたら 0）。
- 応答: `put_req` + `put_n`（1〜8）+ `put_val` → `put_ack`。応答の先頭（コマンド ID）は dap_packet が書く。
  先頭を後で書き直すコマンド（Transfer 等）は、場所を put で 0 として確保しておき、done の header で書き直す。
- PHY（dap_link を使うユニットだけ）: `link_req` + 命令（`link_op`・`link_count`・`link_request`・`link_data`）→
  `link_ack` + 結果（`link_resp`・`link_rdata`・`link_matched`・`link_val`）。

dap_processor は、起動したユニット（`active`、レジスタ）の要求だけを dap_packet / dap_link へ通す。ユニット同士は
設定の信号（dap_cmd_config の出力など）でだけつながる。

## 3. 判断

| 判断 | 理由 | 誤っていた場合のコスト |
|---|---|---|
| 共通手順を「戻り先の状態」ではなく、要求・応答の I/F を持つ別 entity にする | 戻り先の mux が消え、各ユニットの状態機械は 10〜25 状態になる | 低 |
| 引数の読み取り・追記は 1 回ごとに 1〜2 周期の受け渡しが増える | DAP の処理時間は SWD の転送（数百周期）が支配的 | 低 |
| SWO のボーレート計算は SWJ_Clock と割り算器を共有しない | ユニットを独立させる。割り算器 1 つ分の LUT 増 | 低 |
| 新しい entity の単体テストは dap_packet のみ。各コマンドユニットは dap_processor の結合テスト（既存 4 本）で検証する | ユニットの I/F はパケット単位の振る舞いの内部分割であり、意味は CMSIS-DAP のパケットで決まる | 低 |

## 4. 結果

- dap_processor（1237 行・約 80 状態）→ 構造記述 + 6 部品（各 10〜40 状態）。DAP の部品は sys の最悪経路から外れた。
- 続いて最悪経路になった UART ブリッジの入力（送信側の ready → 非同期 FIFO の読み出し）に、制御・DAP と同じ
  レジスタスライスを入れた。
- sys 100.29 MHz で Fmax 103.0 MHz（タイミング重視の配置配線。既定の配置配線でも 100.35 MHz で満たす）。
