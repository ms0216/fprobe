# M6 第 2 段: SWO（UART 方式）設計

- 日付: 2026-10-16
- 親: `2026-10-15-fprobe-architecture-design.md` §4.4（SwoReceiver）、M6 第 1 段 `2026-10-16-m6-uart-swo-design.md`

## 1. ゴールと完了条件

| 完了条件 | 測り方 |
|---|---|
| pyOCD（標準の CMSIS-DAP v2 SWO 手順）で SWO を受けられる | HIL: Nucleo の ITM にデバッガから書いたバイトが、ITM パケットとして pyOCD の `swo_read` に届く（2 Mbaud） |
| 停止中は捨て、取りこぼし・フレーム異常は状態で申告する（黙って捨てない） | シミュレーション |

## 2. 構成

- `dap_processor` に CMSIS-DAP の SWO コマンド: SWO_Transport（0 なし / 2 エンドポイントのみ）、SWO_Mode（0 / 1 UART のみ）、
  SWO_Baudrate（分周 = round(CLK_HZ / baud)、4〜65535。応答は実際の値）、SWO_Control、SWO_Status、SWO_Data（常に 0 バイト）。
  DAP_Info: ケイパビリティ bit2（SWO UART）・bit6（SWO ストリーム）、0xFD（バッファ 2048 バイト）。
- `swo_receiver`（`rtl/debug/`）: uart_rx + 状態（error / overrun、開始で消える）。受信中だけ下流へ出す。
- 経路: J12 の SWO ピン → swo_receiver（sys）→ 非同期 FIFO 2048 → USB の SWO エンドポイント（EP3 IN）。
- SWO のピンはロジアナの ch6 にも入っている（波形と同時に見られる）。
- MCP: `swo_start` / `swo_read`（pyOCD のプローブ API 経由）。

## 3. 判断

| 判断 | 理由 | 誤っていた場合のコスト |
|---|---|---|
| 転送はエンドポイント方式だけ（DAP_SWO_Data は 0 バイトを返す） | pyOCD・OpenOCD 等は EP があればそちらを使う。DAP の応答経路に大きなバッファを持たずに済む | 低 |
| Manchester は後回し | STM32・nRF・RP2040 の SWO は UART 方式で足りる | 低 |
| SWO のデコード（ITM / DWT パケット）と時刻付け、イベントとの相関はホスト側・M9 第 2 段 | まずは標準ツールで使えること | 低 |
