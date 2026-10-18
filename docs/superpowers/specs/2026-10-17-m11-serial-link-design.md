# M11 第 1 段: SerialLink（UART のホストリンク）と移植性の確認 設計

## 1. ゴールと完了条件

成功基準「移植性: コア RTL 無変更で、ボード層とホストリンク層の差し替えのみで別ボードにビルドできる構造」を実際に示す。
USB HS の PHY を持たない小型ボード（Tang Nano 9K / 4K 等）でも、同じ PC ソフト（Python ライブラリ・pyOCD・MCP）が
動くように、論理チャンネルを UART 1 本に多重化するホストリンク（SerialLink）を作る。

| 完了条件 | 検証 |
|---|---|
| SerialLink が論理チャンネル（CONTROL・DAP・SWO・UART0・STREAM）を UART 1 本で運ぶ | cocotb（フレーム・CRC・多重化・誤りフレームの破棄） |
| Primer 20K の構成 `fprobe_serial`（BL702 の UART、DDR3 なし、内蔵ロジアナ）で、ホストからケイパビリティ・CSR・pyOCD のデバッグ・ロジアナが使える | HIL（COM3 = Primer の BL702） |
| Tang Nano 9K（GW1NR-9）向けに、コア RTL を変えずにビルドが通る（書き込みはしない。9K はユーザーの別の検討で使用中） | ビルド（タイミング成立） |

## 2. フレーム（全体設計 §3.2 の契約）

```
UART: … 0x00 | COBS( ch(1) | payload(0..512) | crc16(2, LE) ) | 0x00 …
```

- COBS でバイト詰めし、0x00 を区切りにする（どこから受け始めても次の 0x00 で同期できる）。
- CRC-16/CCITT-FALSE（多項式 0x1021、初期値 0xFFFF）を ch と payload に対して計算。合わないフレームは捨てる
  （数を CSR に出す）。パケットのチャンネル（CONTROL・DAP）はホストが応答待ちで再送を判断する。
- ch: 0 = CONTROL、1 = DAP、2 = SWO、3 = UART0、4 = STREAM（全体設計の CAPTURE / EVENT をまとめたもの。
  USB の EP6 と同じ内容）。パケットのチャンネル（0, 1）は 1 フレーム = 1 パケット。バイト列のチャンネル（2〜4）は、
  たまった分（最大 512 バイト）か、少し待って（約 100 µs）たまった分を 1 フレームで送る。
- 送信の優先: CONTROL > DAP > UART0 > SWO > STREAM（応答の遅れを小さく、大量のストリームは後回し）。
- 流量制御: CONTROL・DAP はホストが応答を待ってから次を送る（1 件ずつ）。PC → ターゲットの UART0 はホストが
  ターゲットのボーレートに合わせて送る（v1 では FPGA 側から受け取り可能量を知らせない。溢れたら UART ブリッジの
  overrun に数える）。
- ボーレート: ジェネリック（既定 2 Mbaud。BL702 の USB-UART の上限を実機で確かめて決める）。8N1。

## 3. 構成

- RTL `rtl/hostlink/serial_link.vhd`: uart_rx / uart_tx（既存）、COBS の復号 / 符号化、CRC16、チャンネルの
  振り分けと合流。コア側の I/F は usb_device と同じ（ctrl_out/in + last、dap_out/in + last、swo_in、uart_out/in、
  stream_in）。クロックは sys 1 つ（UART は非同期の 2 線なので、USB のようなクロックドメインは無い）。
- ボード層: Primer 20K のトップ `primer20k_fprobe_serial_top`（clk27 → PLL → sys、BL702 の UART、J12、LED）。
  Tang Nano 9K のボード定義とトップ（ピン・PLL は Nano 9K の回路図から）。
- コア: `fprobe_core` をそのまま（MEM_ADDR_BITS = 0、LA_INTERNAL = true）。
- ケイパビリティ: HOST_LINK = 2（LINK_SERIAL）。
- ホスト: `fprobe.transport.LinkTransport`（CONTROL）、`Session.open_serial(port)`、pyOCD 用の DAP の接続口
  （CMSIS-DAP のパケットを ch 1 で運ぶ）、ストリームは ch 4。

## 4. 判断

| 判断 | 理由 | 誤っていた場合のコスト |
|---|---|---|
| COBS + ch + CRC16（全体設計どおり） | 区切りが明確で再同期が容易、CRC で化けたフレームを捨てられる | 低 |
| 外付け MCU を使わず BL702 の USB-UART をそのまま使う | 追加の部品・ファームウェアが要らない。ホストは COM ポートを開くだけ | 低（速度が足りなければ外付け MCU を後で足す） |
| Primer で SerialLink の HIL を行う | 9K はユーザーの別の検討で使用中で触れない。Primer の BL702 にも UART がある | 低 |
| v1 は PC → ターゲットの UART0 の流量制御なし | 実用上ターゲットのボーレート ≤ リンクの速度。必要なら受け取り可能量のフレームを足す | 低 |

## 5. 結果（2026-10-17）

- cocotb（serial_link）: 0 と 254 バイト超えを含むパケット、CRC 異常の破棄と計数、LINE_CODING、応答と 600 バイトの
  ストリームのフレーム化、512 バイトちょうどの CONTROL 応答の後の空フレーム、UART0 の FIFO。
- 実機（Primer 20K、`fprobe_serial`、COM3 = BL702、2 Mbaud）: USB 版と同じ fprobe_core と同じホストソフトで、
  ケイパビリティ・CSR、pyOCD のデバッグ（CPUID、SRAM 1 KB の往復）、内蔵ロジアナの捕捉が通った（`test_serial_link_hw.py`）。
- 資源（Primer、内蔵ロジアナ、外部メモリなし）: Logic 11889（58%）、BSRAM 8。sys 100.29 MHz で成立。
- 当初はフレームのバッファ（515 バイト × 2）が複数口の読み書きのため FF と選択回路になり、LUT が上限を超えた。
  書き込み 1 口・読み出し 1 口に直して BSRAM に収めた。
- ホスト（サブエージェントが実装）: `fprobe.serial_link`、`LinkTransport`、`Session.open_serial`、pyOCD の DAP 接続口、
  MCP の `--port`。
- 次: Tang Nano 9K（GW1NR-9、LUT 約 8640）向けのビルド。コアは Primer のシリアル版で約 1.2 万なので、
  小さい FPGA 向けに機能をジェネリックで減らす（ケイパビリティで申告し、PC 側はそれを見て動く）。

## 6. Tang Nano 9K（2026-10-17）

- 構成 `fprobe_nano9k`: GW1NR-LV9QN88PC6/I5、sys 54 MHz（27 MHz × 2。GW1NR-9 の論理部の速度に合わせる）、
  SerialLink（BL702、2 Mbaud）、内蔵ロジアナ。ボード定義 `boards/nano9k.py`（Sipeed の回路図・公式サンプルの .cst から）、
  ボード定数 `nano9k_pkg`、トップ `nano9k_fprobe_top`（Primer のシリアル版と同じ作り）。
- コアはジェネリックで機能を減らす: `XTRIG = false`（クロストリガ）、`TRIG_SEQ = false`（多段トリガ）。外した CSR の窓は
  バスエラーで答え（`csr_absent`）、ケイパビリティ `FEATURES`（13）で申告する（無ければ全部ある）。
- ビルド: 54 MHz で成立、Logic 7481 / 8640（87%）、CLS 95%、BSRAM 9 / 26。書き込みはしていない（9K はユーザーの別の検討で使用中）。
- 成功基準「コア RTL 無変更で、ボード層とホストリンク層の差し替えのみで別ボードにビルドできる」をビルドの水準で満たした。
- 注意: CLS 95% は Primer で STA 合格でも動かなかった水準。実機で使う前に、さらに機能を減らすか（SWO・JTAG のジェネリック）、
  Nano 9K の BL702 の USB シリアル番号を確かめて書き込みツールの識別を用意する。
