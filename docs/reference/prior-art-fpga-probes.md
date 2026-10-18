# 先行事例: FPGA で作ったデバッグプローブ・ロジアナ（2026-10-16 調査）

Web 調査のみ（コードは取り込んでいない）。いずれも HDL の言語が違う（Verilog / Amaranth / Migen）ので、
取り入れる場合は設計を参考にした VHDL での書き直しになる。

## 主なプロジェクト

| 名前 | ライセンス | 対象 | 要点 |
|---|---|---|---|
| [HFLink](https://github.com/LX050724/HFLink) | Apache-2.0 | Gowin Arora V | CMSIS-DAP、**SWD 最大 60 MHz**（サンプリング点・遅延タップ可変）、RAM 書き込み 3.8 MB/s、SWO 100 Mbaud（NRZ / Manchester、4 倍オーバーサンプリング）、HSS（周期メモリサンプリング）。Gowin の暗号化 IP（Cortex-M1・USB）に依存 |
| [Orbtrace](https://github.com/orbcode/orbtrace) | BSD-3 | ECP5 | CMSIS-DAP、**パラレルトレース（1/2/4 bit DDR、TPIU）**、SWO、Orbuculum 連携 |
| [Glasgow](https://github.com/GlasgowEmbedded/glasgow) | 0BSD | iCE40 | swd/jtag-probe（DDR 取り込みで往復遅延を補償）、**analyzer（変化点イベント・THROTTLE / OVERRUN マーカー）**、probe-rs ネイティブ |
| [LUNA](https://github.com/greatscottgadgets/luna) | BSD-3 | ECP5 他 | ULPI の USB HS ゲートウェア |
| [LiteScope](https://github.com/enjoy-digital/litescope) | BSD-2 | 汎用 | 組み込みロジアナ（RLE、VCD / .sr 出力） |
| [open-logic-bit](https://github.com/ultraembedded/openlogicbit) | Apache-2.0 | Spartan-6 + DDR | 16〜32ch・RLE・DDR バッファ・sigrok |
| DSLogic-hdl | **GPL-2.0+** | Spartan-6 | コードは参照しない（GPL の義務が及ぶ） |
| SUMP2 | 不明 | iCE40 | ライセンス不明のため参照しない |

ホスト側の連携先: probe-rs（MIT/Apache）、Orbuculum（BSD-3、SWO/ITM/TPIU デコード）、OpenCSD（BSD-3、ETM デコード）。

## fprobe に取り入れる候補（優先度順）

| 優先 | 項目 | 参考 | 工数 |
|---|---|---|---|
| 高 | SWD/JTAG のサンプリング点・遅延タップを可変にし、**自動キャリブレーション**（DPIDR を読みながら掃引し合格範囲の中央を採用）。（2026-10-16 実測: 往復遅延は約 20 ns で、25 MHz は補償なしで余裕約 10 ns。16.7 MHz は SWJ_Clock の丸めが原因だった。50 MHz を狙うときに必要） | HFLink `DAP_BaudGenerator.v`、Glasgow `IOStreamer(offset)` | 中 |
| 高 | ワイヤを途切れさせない: コマンド間のアイドル除去、ブロック転送の連続化（実効 KB/s を J-Link 並みに） | HFLink、Glasgow | 中 |
| 高 | DAP の内部イベント（要求・ACK・WAIT・FAULT・番地・データ）を LA と同じタイムスタンプのイベントストリームに（全体設計の EventFabric / TransactionMonitor） | Glasgow analyzer | 中 |
| 高 | LA を**変化点イベント形式で DDR3 へストリーミング**し、THROTTLE / OVERRUN / DONE をマーカーで明示 | Glasgow analyzer、open-logic-bit | 中〜大 |
| 中 | SWO の強化（Manchester、4 倍オーバーサンプリング + エッジ再同期、分数分周、TPIU demux） | HFLink `DAP_SWO.v`、Orbtrace | 小〜中 |
| 中 | HSS 相当: ハードウェアの周期メモリサンプリング（停止中は自動停止）を LA の時間軸に | HFLink HSS | 中 |
| 中 | パラレルトレース（ETM）受信と Orbuculum 互換出力。試験には TRACED ピンのあるターゲットが要る | Orbtrace `traceIF.v` | 大 |
| 低 | sigrok / PulseView 直結（.sr、TCP） | Glasgow analyzer2 | 小 |
