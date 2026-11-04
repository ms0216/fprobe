# Shrike-Lite（Vicharak。RP2040 + Renesas ForgeFPGA SLG47910）

fprobe の観測の対象（MCU と FPGA が連携するシステムの題材）。デモは `target_ip/fprobe_obs/examples/shrike_lite/`。

## ピン（基板の回路図 Shrike-lite.kicad_sch から）

- FPGA の GPIOn = SLG47910 の GPIOn。IOB の座標（Workshop の I/O Planner の記録）: GPIO0〜4 = xy[0:6..10]、
  GPIO5〜7 = xy[0:22..24]、GPIO8〜10 = xy[31:27..25]（`fprobe_gw.toolchain.forgefpga.SHRIKE_LITE`）。
- FPGA の GPIO3〜6 = FPGA を書く SPI（GPIO3 = SCK、4 = SS、5 = SI、6 = SO）。RP2040 の GPIO2 = SCK、1 = CS、3 = MOSI、
  0 = MISO（SPI0）とつながる。書いた後は利用者の口。
- FPGA の GPIO16 = LED、GPIO17・18 = 抵抗を通って RP2040 の GPIO15・14（抵抗の有無は未確認。RP2040 から切り替えても
  FPGA に届かなかった）。GPIO18 の座標は xy[31:4]（見本 AN-FG-007、実機で確かめた）。
- **手元の Shrike は F_GPIO2 の入力が働かない**（2026-10-21）。観測の JTAG は TCK = F0・TMS = F18・TDI = F1・TDO = F7。
- RP2040: GPIO12 = FPGA の電源（PWR）、GPIO13 = EN。

## FPGA の書き方

RP2040 の MicroPython（Vicharak の版。COM16、シリアル <Shrike のシリアル>）の `shrike.reset(); shrike.flash('x.bin')`。
x.bin は Workshop の `FPGA_bitstream_MCU.bin` と同じ形（`fprobe_gw.toolchain.forgefpga.mcu_image`）。

## ビルド（GUI なし）

`fprobe_gw.toolchain.forgefpga.ForgeFpgaToolchain`: 付属の yosys（v37、synth_xilinx）と eda-placer（v22。引数は
「FPGA_DATA <束> <口>」、束 = base64(be32 長さ + zlib(be32 数 + 長さの表 + 文字列))）を Workshop と同じ引数で呼ぶ。
同じネットリストから Workshop とバイト単位で同じ MCU 用の .bin ができる（2026-10-20 に確かめた）。

MCU 用の .bin の頭（0 の 320 語の後の 10 語）は Workshop の値をそのまま使っている。意味は未解読:
3 語目（0x22002222）を 0x22222222 にしても動いた。6 語目（0x03000000）は GPIO2 の入力に効いているらしい（0 にすると
F_GPIO2 の入力の見え方が変わった）。GPIO の入力のプル・モードの設定の可能性が高い（R18 で調べる）。

## クロック

- 内蔵の発振器 50 MHz（`osc_en = 1`、CLK_t[0:0]_W_in0。RP2040 で 51 MHz と測った）。
- 「ロジックをクロックに」（LaC0: 源 xy[31:11] out0、有効 xy[31:12] out0、戻り CLK_t[0:0]_E_in0。LaC1 は out1・E_in1）。
  デモは 50 MHz を 2 分周して LaC0 で 25 MHz にしている（観測の回路の最高周波数の見積もりが約 32 MHz のため）。
- 桁上げの鎖は最大 10 ブロック（40 ビット）。

## 大きさ

ロジックのブロック 140 個（1 個 = 6 入力 LUT 4 個 + FF 8 個）。観測の回路 v2 の記録型は組 1・FIFO 4 でも約 124 ブロック
（入らない）、標本型は約 21 ブロック。デモ（SPI・計算器）は約 42 ブロック。

## 実機の確かめ（2026-10-21）

- PYNQ の fprobe から SWD（複数ドロップ、rp2040 core0、attach）でメールボックスを止めずに読めた。ロジアナ（深い
  バッファ）で MCU の目印（GPIO26 → JB1 = ch 10）を取り込めた。
- 観測の JTAG は 2026-10-20 22:58 に v1 の確かめ用の回路で ID "FOBS" を読めた（4 本とも正しかった）。2026-10-21 0 時台は
  TCK だけが FPGA に届かなかった: fprobe が TCK を Low に駆動している時点で F_GPIO2 が 1 や乱れた値に見え、線を手放して
  いる間は入力が浮いて揺れた（TMS・TDI・TDO は揺れなかった）。TCK の線（PYNQ の Raspberry Pi ヘッダ 15 番 → Shrike の
  左の列 下から 2 本目）の接触を確かめる。
  1 時ごろ、22:58 と同じ組み合わせ（v1 のエンジンの PYNQ の回路を作り直して書き、Shrike に Workshop で作った v1 の確かめ用の
  回路）でも ID が 0 になった。線を挿し直しても同じ。TCK の線を F0 に移すと TCK はきれいに見えた（そのまま返すと
  毎回 0、反転すると毎回 1）ので、PYNQ 側と線は正常で、**この Shrike の F_GPIO2 の入力が働かない**（22:58 には働いて
  いた。その後に傷んだか、未解読の設定の影響かは未確認。疑い: 元の main.py の点滅の回路が全ピンを出力にして TCK と
  ぶつかった）。観測の JTAG は TCK = F0・TMS = F18（座標 xy[31:4]、見本 AN-FG-007）に移し、標本型の観測が実機で動いた:
  毎秒約 1.3 万サンプル・取りこぼし 0、演算器の状態遷移・SPI のコマンド・演算中にリトライが来ることを確認した
  （gateware/tests/hil/test_shrike_hw.py の 4 件）。
