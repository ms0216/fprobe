# M4: SWD + CMSIS-DAP v2 設計

- 日付: 2026-10-15
- 親: `2026-10-15-fprobe-architecture-design.md` §4.2（デバッグポート）、§5.1（USB 構成）

## 1. ゴールと完了条件

fprobe を **CMSIS-DAP v2 プローブ**として PC に見せ、SWD で Cortex-M をデバッグできるようにする。
DAP コマンドの解釈はすべてハードウェア FSM で行う（全体設計 §4.2 の判断）。

| 完了条件 | 測り方 |
|---|---|
| pyOCD / OpenOCD が追加ドライバなしで fprobe を CMSIS-DAP v2 プローブとして認識する | HIL（Windows、WinUSB 自動割当て） |
| Nucleo F401RE の DPIDR・メモリを読み書きでき、Flash 書き込みと停止・再開ができる | HIL（pyOCD） |
| CMSIS-DAP の意味論（WAIT 再試行・値一致・AP 読み出しのポスティング・FAULT・パリティ異常）どおり | シミュレーション（SWD ターゲットモデル） |
| 全 entity に cocotb テスト。合成でタイミング達成 | pytest / GowinBuilder |

速度目標（全体設計 §1.2）は M4 では計測して記録し、未達なら原因と対策を残す（SWD クロックは配線に依存する）。

## 2. USB 構成の変更

| IF | 内容 | エンドポイント |
|---|---|---|
| 0 | **CMSIS-DAP v2**（ベンダー、文字列 `fprobe CMSIS-DAP`） | EP2 OUT / EP2 IN（DAP）、EP3 IN（SWO。M6 まで送るもの無し） |
| 1 | fprobe 拡張（ベンダー、`fprobe control`） | EP1 OUT/IN（CONTROL）、EP6 IN |
| 2–3 | CDC-ACM（IAD） | EP5 IN（通知）、EP4 OUT/IN |

- 製品文字列も `fprobe CMSIS-DAP` にする（ツールによっては製品文字列で CMSIS-DAP を判定するため）。
- MS OS 2.0: IF0 に CMSIS-DAP v2 の標準 GUID `{CDB3B5AD-293B-4663-AA36-1AAE46463776}`、IF1 に fprobe の GUID。
  記述子を変えるので `bcdDevice` を上げる（Windows のキャッシュ対策）。
- エンドポイント番号は変えない（IF 番号だけずれる）。

## 3. 構成

```
 usb ドメイン                         │ sys ドメイン (100 MHz)
 usb_device EP2 OUT ─ async_fifo ─────┼→ dap_processor ─┬→ swd_engine ─ swd_phy ─ SWCLK / SWDIO
 usb_device EP2 IN  ← async_fifo ←────┼←                 └─ nRESET（オープンドレイン）、SWJ_Pins
```

| entity | 責務 |
|---|---|
| `swd_phy` | ピンの駆動とサンプリング。命令: ビット列の出力（最大 64 ビット、SWJ_Sequence / SWD_Sequence）、ビット列の入力、1 トランザクション（要求 8 ビット → ターンアラウンド → ACK 3 ビット → データ 32 + パリティ）。クロックは sys の分周（イネーブル方式、半周期 = DIV 周期）。ターンアラウンド長・データ相の常時実行（SWD_Configure）、アイドル周期（TransferConfigure）を設定で持つ |
| `dap_processor` | コマンドパケット（1 USB パケット）を解釈し、応答パケットを返す。Transfer / TransferBlock の再試行・一致・ポスティングもここ |
| `dap_info_pkg`（生成） | DAP_Info の文字列・値（Python の USB 定義と同じ源から生成） |

- ピン（Primer 20K、J12 = `pmod1`）: 5 `J14` SWCLK/TCK、6 `J16` SWDIO/TMS、7 `M14` SWO/TDO、8 `M15` nRESET、
  9 `T12` TDI。10 `R11` / 11 `T11` は UART（M6）、12 `P11` はロジアナ（M7）。
- SWD のタイミング（ARM の参照実装と同じ）: ホストは SWCLK を下げる前に SWDIO を変え、ターゲットは立ち上がりで取り込む。
  ターゲットは立ち上がりの後に SWDIO を変え、ホストは立ち上がりの直前（Low 期間の終わり）に取り込む。

## 4. 対応コマンド（M4）

| ID | コマンド | 備考 |
|---|---|---|
| 0x00 | DAP_Info | Vendor / Product / Serial / Protocol Version `2.1.1` / Firmware Version / Capabilities（SWD のみ）/ Packet Count / Packet Size。未知の ID は長さ 0 |
| 0x01 | DAP_HostStatus | 受け付けるだけ |
| 0x02 | DAP_Connect | 0 = 既定（SWD）/ 1 = SWD を受け付け、それ以外は 0（失敗）を返す |
| 0x03 | DAP_Disconnect | ピンを解放（Hi-Z） |
| 0x04 | DAP_TransferConfigure | アイドル周期・WAIT 再試行回数・一致再試行回数 |
| 0x05 | DAP_Transfer | 値一致（読み出し）・一致マスク（書き込み）・ポスティング・WAIT 再試行 |
| 0x06 | DAP_TransferBlock | 同上（一致なし） |
| 0x08 | DAP_WriteABORT | DP ABORT への書き込み |
| 0x09 | DAP_Delay | µs 単位 |
| 0x0A | DAP_ResetTarget | 実装なし（execute = 0）。リセットは SWJ_Pins の nRESET で行う |
| 0x10 | DAP_SWJ_Pins | SWCLK / SWDIO / nRESET の出力と読み返し、待ち時間 |
| 0x11 | DAP_SWJ_Clock | 要求周波数の 1% 増し以下で最も近い分周比（2026-10-16 変更。25 MHz の要求で 25.07 MHz） |
| 0x12 | DAP_SWJ_Sequence | SWDIO に最大 256 ビット |
| 0x13 | DAP_SWD_Configure | ターンアラウンド 1〜4、データ相の常時実行 |
| 0x1D | DAP_SWD_Sequence | 出力・入力の混在 |
| その他 | | `0xFF` を返す（JTAG は M5、SWO は M6、Queue/ExecuteCommands は必要になれば） |

- Packet Count = 4、Packet Size = 現在の MPS（HS 512 / FS 64）。応答 4 つ分を IN 側に溜められること。
- DAP_TransferAbort（0x07）は応答を返さないコマンド。M4 では受け取って捨てる（WAIT 再試行中の中断は未対応）。

## 5. 検証

- **SWD ターゲットモデル**（Python, cocotb）: ピンレベルで SWD を話し、DP（DPIDR・ABORT・CTRL/STAT・SELECT・RDBUFF）と
  MEM-AP（CSW・TAR・DRW・IDR、アドレス自動インクリメント）とメモリを持つ。WAIT / FAULT の注入、パリティ異常の注入、
  ラインリセットの検出。
- `swd_phy` 単体、`dap_processor` + `swd_phy` + モデル（実際の CMSIS-DAP コマンド列。pyOCD の接続手順を再現）。
- HIL: pyOCD で Nucleo に接続し、DPIDR、SRAM の読み書き、Flash への書き込みと照合、停止・再開。

## 6. 判断（ユーザー確認なしで決めたもの）

| 判断 | 理由 | 誤っていた場合のコスト |
|---|---|---|
| DAP_ResetTarget は実装なし（execute = 0） | 参照実装でもターゲット依存。ツールは nRESET か SYSRESETREQ を使う | 低 |
| TransferAbort は受け取って捨てる | 転送は µs 単位で終わる。WAIT 再試行中の中断が要れば後で追加 | 低 |
| JTAG / SWO / Atomic コマンドは後のマイルストーン | 段階的に Capabilities で申告する | なし |
