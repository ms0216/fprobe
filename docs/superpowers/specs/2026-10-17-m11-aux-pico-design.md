# M11 第 4 段: 補助マイコン（Raspberry Pi Pico）の計測値 設計

## 1. ゴールと完了条件

全体設計の「補助マイコン（電圧監視・補助 GPIO）」の第 1 段。Primer には ADC が無いので、手持ちの Raspberry Pi Pico
（RP2040）の ADC でターゲットの電源電圧を測り、FPGA に送る。PC からは Pico は見えず、fprobe の CSR・イベント・
MCP の道具として見える（Pico の USB は給電とプログラムの書き込みだけ）。

| 完了条件 | 検証 |
|---|---|
| FPGA が Pico の計測値の組を受け、途中から受け始めても揃い、崩れた受信を数える | cocotb（aux_link） |
| 最新値が CSR aux に、各値がイベント（種類 6）に時刻付きで残る | ビルド + HIL |
| Nucleo の 3.3V が 3.3 V ± 0.1 V と読める（`fprobe.aux_mcu.Aux`、MCP の `target_voltage`） | HIL |

## 2. 補助マイコンの選定（2026-10-17）

手持ちの候補のうち Pico（RP2040）にした。Pico 2（RP2350）は入力ピンのエラッタ（E9）があり、ESP32 系の ADC は
非線形（0 V・上限付近が使えない）、Arduino（ATmega328P）は 5 V 系で 10 ビット。将来は INA226 等の電流・電圧
モジュールを FPGA の I2C に直結する案（補助マイコン不要、電流も測れる）が上位互換になる。

## 3. 接続と形式

| Pico | Primer / ターゲット |
|---|---|
| GP0（UART0 TX） | J14（pmod0）の 5 番（P9）= `aux_rx` |
| GP1（UART0 RX） | J14 の 7 番（T8）。将来の FPGA → Pico（補助 GPIO の指示）。v1 は未使用 |
| GND | J14 の GND |
| GP26（ADC0） | ターゲットの電源（Nucleo の 3V3。3.3 V を超える場合は分圧が要る） |

- UART 8N1、1 Mbaud。1 件 = 3 バイト: `1 | ch(7)`、`0 | mV[13:7]`、`0 | mV[6:0]`。先頭だけ最上位ビットが 1 なので
  区切りが自明（COBS・CRC は不要な短さ・頻度）。ch 0 = ターゲット電圧。1 ms ごと（16 回平均）。
- Pico のプログラムは MicroPython（`host/firmware/aux_pico/main.py`）。ADC の基準（Pico の 3V3）は `VREF_MV` で校正する。

## 4. FPGA とホスト

- `rtl/core/aux_link.vhd`: uart_rx + 組の復号。fprobe_core のジェネリック `AUX`（ケイパビリティ FEATURES の bit2 と
  一致させる）で有無を決め、無い構成では CSR の窓はバスエラーで答える。
- CSR `aux`（0x0B00）: target（ch 0 の最新値、mV）、samples、errors。イベント（種類 6）: data = ch << 16 | mV（全チャンネル）。CSR は使う ch 0 だけにした（製品デザインは CLS 95% で、4 チャンネル分の CSR で配線が 20 分に収まらなくなった。2026-10-17）。
- ホスト: `fprobe.aux_mcu.Aux(dev).read()`（live = 読む間に samples が増えた）、events の `"aux"`、MCP の `target_voltage`、
  書き込み `python -m fprobe.aux_flash <Pico のシリアル番号>`。

## 5. 判断

| 判断 | 理由 | 誤っていた場合のコスト |
|---|---|---|
| 計測値を FPGA へ流し、PC へは fprobe 経由 | 時刻をタイムベースで付けられる（ロジアナ・DAP と相関）。PC 側は機器が増えない | 低 |
| MicroPython | Pico への書き込みがファイルのコピーで済み、校正値もすぐ変えられる。1 kHz なら十分速い | 低（速い ADC の取り込みが要るなら C に） |
| 3 バイトの固定形式 | 頻度・長さが小さく、先頭ビットで同期できる | 低 |
