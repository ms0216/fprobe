# M5: JTAG（CMSIS-DAP の JTAG）設計

- 日付: 2026-10-16
- 親: `2026-10-15-fprobe-architecture-design.md` §4.2（JtagPhy）

## 1. ゴールと完了条件

| 完了条件 | 測り方 |
|---|---|
| pyOCD が JTAG で Cortex-M をデバッグできる（DP・AP・メモリ） | HIL: Nucleo F401RE（チェーン = JTAG-DP IR4 + BSC IR5）で CPUID、SRAM 2 KB の書き込み・読み出し一致（TCK 1 / 8 MHz）、その後 SWD に戻せる |
| CMSIS-DAP の JTAG_Sequence / JTAG_Configure / JTAG_IDCODE、Transfer / TransferBlock の dap_index | シミュレーション（TAP 状態機械 + チェーンのモデル） |
| Cortex-A / ARM11（Raspberry Pi 2 / Zero）は実機入手時に OpenOCD で検証 | — |

## 2. 構成

- **jtag_phy**（`rtl/debug/`）: swd_phy と同じ命令 I/F。JTAG-DP の違いを PHY の中に閉じ込める:
  - AP → APACC、DP の A=0 読み → IDCODE 命令（32 ビット）、A=0 書き → ABORT、その他の DP → DPACC。IR は前回と同じなら省く。
  - JTAG-DP は読み値が次のスキャンで返る。AP の読みはポストのまま返し（SWD と同じ。dap_processor が RDBUFF で回収）、
    DP の読み（RDBUFF 以外）は続けて RDBUFF をスキャンして値を返す → **dap_processor の転送処理とクロストリガの割り込みは
    SWD と共通のまま使える**。
  - ACK は SWD の符号に読み替え（OK/FAULT 010 → 001、WAIT 001 → 010）。
  - 各スキャンのビット列（TMS 列・TDI 列、最大 64 ビット）は転送の受付直後に組み立てておき、スキャンの終わりは
    「取り出し → 判定」の 2 周期で処理する（可変位置の組み立て・取り出しを同じ周期に重ねない）。
  - OP_SEQ_OUT = SWJ_Sequence（TMS に出す）、OP_SEQ_IN = JTAG_Sequence（TMS 一定、TDI 出力、TDO 取り込み）。
- **debug_port**: swd_phy と jtag_phy を DAP_Connect のポートで切り替える。TCK/SWCLK・TMS/SWDIO は共用、TDI は J12 の
  TDI、TDO は SWO と同じピン（標準の 10 / 20 ピンコネクタと同じ割り当て）。応答は 1 段レジスタを通す。
- **dap_processor**: Connect(2)、JTAG_Sequence、JTAG_Configure（最大 8 台、IR 32 ビットまで。構成時に IR の累積和を作り、
  選んだ装置の位置は加算器の連鎖なしで求める）、JTAG_IDCODE、Transfer / TransferBlock の dap_index。
- **ホスト**: `Session.debug(jtag_chain=[4, 5])`。pyOCD にはチェーン指定が無い（IR 4 の 1 台を仮定）ので、DAPAccess の
  JTAG 構成を差し替える。

## 3. 判断

| 判断 | 理由 | 誤っていた場合のコスト |
|---|---|---|
| JTAG-DP の違いを PHY に閉じ込める（dap_processor の転送処理は共通） | 状態機械を 2 系統持たずに済み、インジェクタ・モニタもそのまま JTAG で動く | 低 |
| （一時）sys を 100.29 → 90 MHz に下げた | JTAG 追加で dap_processor（70 状態超）が 100 MHz に届かなくなった | 解消済み: dap_processor をコマンド別に分割して 100.29 MHz に戻した（2026-10-16-dap-processor-split-design.md） |
| スキャン 1 回 = 最大 64 ビット | IR の合計 58 ビット・DR の前後装置 7 台までで足りる（Cortex-A の DAP + 数台） | 低 |
| nTRST は持たない | J12 に余りピンが 1 本しかない。TAP のリセットは TMS で行う | 低 |
