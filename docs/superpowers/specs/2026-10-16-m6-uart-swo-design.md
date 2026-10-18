# M6: UART ブリッジ（CDC-ACM）と SWO 設計

- 日付: 2026-10-16
- 親: `2026-10-15-fprobe-architecture-design.md` §4.4

## 1. ゴールと完了条件

| 完了条件 | 測り方 |
|---|---|
| PC の COM ポート（CDC-ACM）とターゲットの UART が双方向につながり、ボーレートは COM ポートの設定（SET_LINE_CODING）に追従する | HIL: Nucleo の USART1 を SWD 経由で設定し、COM ⇄ USART1 で往復 |
| 受信の異常（フレーム異常・オーバーラン）が CSR で観測できる | シミュレーション + HIL |
| SWO（UART 形式）を CMSIS-DAP の SWO コマンドで受け取れる | 第 2 段（下記） |

## 2. 構成

```
 usb ドメイン                              │ sys ドメイン
 usb_device CDC OUT ─ async_fifo ──────────┼→ fprobe_core ─ uart_bridge ─ TX → J12 pmod1[7]（ターゲットの RX）
 usb_device CDC IN  ← async_fifo ←─────────┼←                            ← RX ← J12 pmod1[5]（ターゲットの TX）
 line_rate + line_changed ─ cdc_value ─────┼→ baud / baud_load
```

- `uart_bridge`（`rtl/serial/`、ボード非依存）: 8N1。分周比 round(CLK_HZ / baud) を共通の割り算器 `udiv` で求め、
  送受信に同じ値を使う。リセット直後 115200 baud。データビット・パリティ・ストップビットの設定は無視する（8N1 のみ）。
- `cdc_value`（`rtl/common/`）: めったに変わらない多ビット値のドメイン乗せ換え（保持 + トグル）。
- CSR `uart` ブロック: `baud`（現在のボーレート）、`frame_errors`、`overruns`（RO, 32 ビットのカウンタ）。
- ケイパビリティの `CHANNELS` に `CH_UART0` を加える。

## 3. SWO（第 2 段）

CMSIS-DAP の SWO コマンド（Transport / Mode / Baudrate / Control / Status / ExtendedStatus / Data）と、
SWO の受信器（UART 形式。`uart_rx` を J12 pmod1[6] で使う）、受信 FIFO。DAP_Info の Capabilities に SWO UART を立てる。

## 4. 判断

| 判断 | 理由 | 誤っていた場合のコスト |
|---|---|---|
| 8N1 のみ | ターゲットのログ用途はほぼ 8N1。パリティ等は要求が出たら uart_tx / uart_rx を拡張 | 低 |
| 送受信の分周比は同じ値（bringup のような送信側の速め補正はしない） | ブリッジは連続送信を受けるのがターゲットで、相手の許容範囲に入る | 低 |
