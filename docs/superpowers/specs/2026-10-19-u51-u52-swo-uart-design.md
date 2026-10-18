# U51 SWO の Manchester と高速化・U52 UART の拡充（設計）

台帳: docs/superpowers/plans/2026-10-18-backlog.md の U51・U52。要件: docs/reports/2026-10-18-gap-analysis.md の
SEG-24・IAR-01・ORB-03（U51）、SEG-26（U52）。

## 1. 方針

- FPGA の資源がほぼ満杯（B22）なので、足す回路は小さく、ジェネリックで外せるようにする。外した構成は
  ケイパビリティ（と CMSIS-DAP の DAP_Info）で申告し、PC 側はそれを見て断る。
- 機器固有の機能は足さない。Manchester も RTS/CTS も汎用の機能。

## 2. U51 SWO の Manchester 方式

### 2.1 線の形

Arm の TPIU の SWO（Manchester、TPIU_SPPR = 1）。休止は Low。フレームは開始ビット（値 1）、データ（バイトの
LSB から、バイト数は任意）、休止（Low が 1 ビット以上）。ビットの値は中央の変化の向き（1 = High → Low、
0 = Low → High）。同じ値が続くとビットの境目にも変化が入る。

### 2.2 回路（rtl/debug/swo_manchester_rx.vhd）

- 中央の変化ごとに時計を合わせ直す。直前の中央の変化からの経過 t（clk 周期）と 1 ビットの周期 T（= divisor）で、
  `t < T - floor(T/4)` の変化は境目（無視）、それ以上は中央（値 = 向き）。`T + floor(T/4)` まで変化が無ければ
  フレームの終わり（8 ビットに満たない端数があれば frame_error）。
- 開始は休止（Low）からの立ち上がり。半ビットより長く High なら Manchester の開始ではない（つないでいない線の
  プルアップ、NRZ の休止）: Low に戻るまで待つ。
- swo_receiver が uart_rx（NRZ）と並べて持ち、方式（DAP_SWO_Mode）で出力を選ぶ。B10 の開始の窓（flush）は両方式で共通。
- ジェネリック HAS_SWO_MANCHESTER（fprobe_core・dap_processor）。false なら回路が無く、DAP_SWO_Mode = 2 を断り、
  DAP_Info の Capabilities の bit3（SWO Manchester）とケイパビリティ DEBUG の bit2（DEBUG_SWO_MANCHESTER）を出さない。

### 2.3 速さ（sys_clk 100 MHz。divisor の下限 4 = 25 Mbit/s）

シミュレーション（位相を乱数にした 16 バイトのフレーム。tests/tb/swo_manchester_rx_tb.py・swo_receiver_modes_tb.py）で
掃いた、すべて正しく受けた範囲。速さのずれは正 = 送り手が遅い。線のひずみは High の区間の伸び縮み。

| divisor | 速さ | Manchester（ひずみ 0） | Manchester（±2 ns） | Manchester（±5 ns） | UART（NRZ） |
|---|---|---|---|---|---|
| 4 | 25 Mbit/s | -25〜0% | 欠ける | 欠ける | -1〜+5% |
| 5 | 20 Mbit/s | ±20% | -15〜+10% | -10〜0% | -3〜+4% |
| 6 | 16.7 Mbit/s | ±15% | ±10% | ±5% | -3〜+5% |
| 8 | 12.5 Mbit/s | ±25% | ±20% | -15〜+10% | -3〜+5% |
| 16 | 6.3 Mbit/s | ±25% | ±20% | ±20% | -3〜+5% |

- Manchester の限界は、境目の変化（T/2 ± 1 周期）と中央の変化（T ± 1 周期）を分けられること。T = 4 では余裕が
  1 周期未満で、線のひずみに耐えない。実用の上限は divisor = 5〜6（20〜16.7 Mbit/s）。
- UART（NRZ）は開始ビットからの数えだけなので速さのずれが 10 ビットで溜まる。窓が遅い側に寄るのは、uart_rx が
  同期化（2 段）の後に開始を見て半ビット数えるため（標本点が中央より 2〜3 周期遅い）。25 Mbaud（divisor 4）は
  TPIU の速さが 100.29 MHz / 4 に 1% 以内で合うときだけ。
- 実機の上限は配線（ジャンパ線のひずみ）次第。実機で確かめる手順は報告に書く。

### 2.4 PC 側

`swo.start(baud, mode="uart" | "manchester")`。pyOCD の swo_start は方式が UART に決まっているので、同じ手順
（Mode OFF・Transport なし → Transport 2 → Mode → Baudrate → Control）を方式付きで送る（fprobe.session.swo_start）。
manchester はケイパビリティ swo_manchester が無ければ断る。ターゲットの TPIU の SPPR = 1 はファームかデバッガで設定する
（swo.configure の TPIU の設定は NRZ のまま。Manchester の ACPR と速さの関係は実機で確かめてから足す）。

## 3. U52 UART の拡充

### 3.1 ブレーク

CDC-ACM の SEND_BREAK（wValue = ms、0 で止める、0xFFFF は止めるまで。Windows の usbser・Linux の cdc-acm は
0xFFFF と 0 を使う）を usb_cdc_acm_requests が数え、brk を fprobe_core（同期化して uart_bridge）へ。ブレークの間は
TX を Low に保ち、新しいバイトを受け取らない。TX をまだ駆動していなければ（B29）ブレークで駆動を始める。

### 3.2 ボーレートの範囲

既存のまま: 分周比 2〜2^20-1（100 MHz で約 95 baud〜50 Mbaud）。8N1 だけ（データ長・パリティ・停止ビットの
LINE_CODING は受けるが使わない）。7E1 等は回路が増えるので要望が出てから。

## 4. U52 RTS/CTS（ハードウェアのフロー制御）

### 4.1 振る舞い（uart_bridge、ジェネリック FLOW）

- 線はアクティブ Low（rts_n・cts_n）。
- 自動（CSR uart.flow.auto = 1）: CTS が出ている間だけ次のバイトを送り始める（送りかけは最後まで）。RTS は受信の
  下流（USB への FIFO）が受けられる間だけ出す。uart_rx の 1 バイトの保持があるので、相手が RTS を見て止まるまでの
  1 バイトは失わない。
- 手動（auto = 0）: RTS は COM ポートの RTS（SET_CONTROL_LINE_STATE）どおり。CTS は見ない。
- CDC-ACM には「フロー制御を有効に」の要求が無いので、auto は CSR（host の `uart.flow`）で有効にする。

### 4.2 端子（J12 の 8 本の取り合い）

J12: 0 SWCLK、1 TDI、2 nRESET、3 SWDIO、4 予備、5 UART RX、6 SWO/TDO、7 UART TX。

候補は **4 予備** と **1 TDI**（primer20k_pkg の UART_FLOW_PIN_CHOICES、boards/primer20k.py の uart_flow_pins）。
RTS・CTS どちらにも使え、CSR uart.flow の rts_pin・cts_pin で選び、rts_claim・cts_claim のときだけ取る
（クロストリガの出力の claim と同じ考え。取った端子の機能の出力は止める）。

- 予備（4）: 普段の機能が無く、プル無し → RTS に向く（取らない間は高インピーダンス）。
- TDI（1）: JTAG のときだけ使う（SWD の作業では空き）。プルアップ → CTS に向く（つないでいなければ「出ていない」=
  自動のとき送らない。安全側）。JTAG と一緒には使えない。
- SWO（6）は SWO・TDO と取り合うので候補にしない。UART RX/TX・SWD の線・nRESET は論外。
- 予備はクロストリガの出力の候補でもある。両方が取れば、pin_select の優先（gpio > クロストリガの出力 > 機能）で
  クロストリガが勝つ。

推奨の配線: RTS = J12 の予備（4）→ ターゲットの CTS、CTS = J12 の TDI（1）← ターゲットの RTS。

### 4.3 CSR（uart ブロック）

- `flow`（0x0C, RW）: auto[0]、rts_claim[4]、rts_pin[9:8]、cts_claim[12]、cts_pin[17:16]
- `lines`（0x10, RO）: cts[0]、rts[1]、host_rts[2]、host_break[3]

回路の無い構成でも読み書きできるが何もしない（claim は 0 に固定）。ケイパビリティ UART_FLOW（種類 17）が候補の
ロジアナのチャンネルの並び（Primer の USB の製品デザイン fprobe・fprobe_jtag は [4, 1]）。無ければ回路が無い。

### 4.4 CTS の状態の通知（SERIAL_STATE）

usb_cdc_notify が通知のエンドポイント（EP5、インタラプト IN）で SERIAL_STATE を、状態が変わるたびと構成の直後に
1 回ずつ送る。SERIAL_STATE には CTS のビットが無い（PSTN 1.2 表 31）ので、**CTS を bTxCarrier（OS では DSR）で
知らせる**。bRxCarrier（DCD）は 1 に固定（Linux の cdc-acm は DCD の 1 → 0 で切断するので変えない）。一度きりの
出来事のビット（ブレーク・フレーム異常・オーバーラン）は送らない（数は CSR uart の frame_errors・overruns）。
