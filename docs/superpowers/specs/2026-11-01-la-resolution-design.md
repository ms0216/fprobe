# ロジアナの時間分解能（サンプリング）を理論の限界まで上げる（設計）

2026-11-01。状態: 段 1 を実装・実機確認（§9・§10）。段 2（案 B: VCO 1200 MHz・N = 8・800 MS/s、PMOD JA も ISERDESE2。段 5 の JA を前倒し）を実装・ビルド（§11）・実機確認（§12。2026-11-01。SWD・UART の JA の O 経由は未確認）。段 3・4 は `2026-11-01-la-deep-fast-design.md`（§13）。IDELAY の掃引・段 3 以降は設計。対象: 全機能の基準の PYNQ-Z2（xc7z020clg400-1、速度グレード -1）。Primer は扱わない。
上位の方針: `2026-10-29-performance-limits-study.md`（利用者が承認済み）の §0.1・§1・§2.2・§2.3・§2.1。
前の段: `2026-10-30-la-capture-clock-design.md`（取り込みを 300 MHz の別の領域に）、`2026-10-31-la-testgen-design.md`（外部ピンの限界の測定）、
`2026-10-31-la-stream-pack-design.md`（チャンネル詰め）、`2026-10-30-la-stream-ps-direct-design.md`（PS 直読みの垂れ流し）。

書き分け: **［事実］** は既存のコード・ビルドの報告・実測の文書で確かめたこと、**［推測］** は測っていない・資料で確かめていない見積り、
**［決める］** は利用者の判断が要ること。数値には根拠（データシートの表、コードの行、実測の文書の節）を付ける。

---

## 0. 結論の要約

- 時間分解能の本当の律速は **端子の取り込みの方式**（いまはファブリックの FF で 1 周期 1 サンプル、300 MHz = 3.33 ns）。
  ISERDES（端子の中の直並列変換）で 1 ピンを DDR で取り込み、sys の 1 周期に N サンプルを渡せば、内部の回路は sys（100 MHz）のまま、
  **600 MS/s（N = 6、いまのクロックのまま）→ 800 MS/s（N = 8、MMCM の VCO を 1200 MHz に）**まで上がる。部品の上限は
  DDR 受信 950 Mb/s（-1）＝ 約 1.05 ns で、そこまで行くには別の MMCM・BUFIO・非整数比の受け渡しが要り、得るものは 800 MS/s の 1.19 倍。
- **時刻の分解能**（エッジがいつか）と **最小のパルス幅**（どこまで細い脈が見えるか）は別の限界。前者は ISERDES で上がる。後者は端子の
  アナログの帯域（3.3 V の LVCMOS の入力・基板・相手の駆動）で決まり、ISERDES では上がらない。いまの実測は、ループバック全体で約 13 ns
  （`2026-10-31-la-testgen-design.md` §5）で、取り込み回路だけの限界は未分離。
- 内蔵バッファは BRAM の帯域では律速しない（幅を広げて sys で書けば、BRAM の上限 388 MHz から遠い）。律速は **容量**: 16 ch・800 MS/s で
  全 BRAM を使っても約 0.37 ms（計算）。
- 垂れ流しは PC への帯域（実測の壁 約 50 MB/s、1 GbE の理論 約 118 MB/s）が律速で、細かいサンプルを欠けなく長時間流せるのは **RLE**
  （変化点の数で決まる。50 MB/s で毎秒 1250 万回の変化）。RAW は 1〜2 ch でも 600 MS/s 以上は 50 MB/s を超える。
- 最初の 1 歩（§5）: PMOD JB の 6 本（入力専用）だけに ISERDES（N = 6、いまのクロック）を入れ、内蔵バッファの取り込みを 1 周期 6 サンプルにする。

---

## 1. 部品ごとの理論の限界

### 1.1 端子（PMOD JA・JB、RPi ヘッダ）

［事実］端子のバンクと規格（ビルド `C:\fw51\Default\io.rpt`、`fprobe_pynq_cap` の版。`fprobe_gw/boards/pynq_z2.py` 47〜58 行の割り当て）:

| 用途 | ピン | バンク | 種類 |
|---|---|---|---|
| ロジアナ ch 0〜7（PMOD JA、`pmod1`。SWD・JTAG と兼用、双方向） | Y18 Y19 Y16 Y17 U18 U19 W18 W19 | 34 | HR、LVCMOS33 |
| ロジアナ ch 10〜15（PMOD JB の 1〜4・7〜8 番、`la_in`。入力専用） | W14 Y14 T11 T10 V16 W16 | 34 | HR、LVCMOS33 |
| 試験用の方形波（RPi ヘッダ 32 番、`testgen`） | B20 | 35 | HR、LVCMOS33、DRIVE 8・SLEW FAST |
| 入力クロック 125 MHz | H16（MRCC） | 35 | HR |

ロジアナの外部ピン 14 本は **すべてバンク 34**（1 つのクロック領域）にある。Zynq-7020 の PL の I/O はすべて HR バンク（HP バンクは無い）。

| 項目 | いまの値 | 理論の限界（-1） | 根拠 | 律速 |
|---|---|---|---|---|
| 端子からの取り込みの方式 | ファブリックの FF（cap_clk 300 MHz で 2 段の同期化。IOB・ILOGIC は 0 個） | ISERDES（ILOGIC）の DDR 受信 **950 Mb/s** | ［事実］`io.rpt`・`util.rpt` の ILOGIC 0/125、`la_filter.vhd` 3 行（2 段で同期化）。上限は性能検討書 §0.1（DS187 v1.21）の「端子の取り込み（LVDS、DDR/SDR 受信）950 Mb/s」 | 取り込みの方式 |
| サンプルの間隔 | 3.33 ns（300 MS/s） | 1.05 ns（950 MS/s） | 同上 | 同上 |
| SDR の受信の上限 | — | 性能検討書 §0.1 には DDR の値しか無い。［推測］同じ表の SDR の値は DDR より低い（記憶では -1 で 680 Mb/s。**DS187 の該当表で要確認**） | — | — |
| IDELAY | 使っていない | 1 段 約 78 ps、32 段（約 2.5 ns の範囲）。-1 は基準 200 MHz のみ（1/(32 × 2 × FREF)） | 性能検討書 §0.1 | IDELAYCTRL の基準クロック（いまの MMCM では作れない。§3.2） |
| 3.3 V の LVCMOS の信号の品質 | ループバック全体で、幅 約 13 ns 以上のパルスは全部見えて幅が一致、10 ns 前後は ±1 サンプル、6.7 ns 以下は不安定、3.3 ns は見えない | **データシートの保証は無い**（950 Mb/s は差動の値）。［推測］基板の直列抵抗・プル・相手の駆動で決まる | ［事実］`2026-10-31-la-testgen-design.md` §4・§5、`2026-10-30-la-stream-ps-direct-design.md` §10.17 | アナログの帯域（未分離） |
| 立ち上がりと立ち下がりの差 | 1 の区間が +1、0 の区間が −1 サンプル（合計は周期と一致）。ずれ方はビルドの配置ごとに変わる | — | ［事実］`2026-10-31-la-testgen-design.md` §4・§5（再ビルド版 2） | ［推測］最初の FF がファブリックにあり、端子からの配線の長さが配置で変わるため（未確認） |
| 入力の制約 | 無い（IOB・入力遅延・ASYNC_REG） | — | ［事実］性能検討書 §1 の表。`fprobe_gw` に `IOB`・`ASYNC_REG` の指定は無い（grep） | — |

- ［推測］PMOD の線の基板上の直列抵抗・ESD 部品の有無は確かめていない（PYNQ-Z2 の回路図で要確認）。抵抗があれば、入力の容量との時定数で、
  細いパルスの限界を決める。
- 「時刻の分解能」と「最小のパルス幅」を混ぜない。ISERDES はエッジの時刻を細かくするが、アナログの帯域で丸まった脈は見えるようにならない。
  見えない脈を見えるようにする回路（推定・補間）は入れない（CLAUDE.md の原則）。

### 1.2 内部の取り込みクロック

| 部品 | いまの値 | 理論の限界（-1） | 根拠 | 律速・使い道 |
|---|---|---|---|---|
| BUFG（グローバルクロック） | 2 本使用（sys 100 MHz・cap 300 MHz）。cap の配線後の Fmax 300.6〜305.8 MHz | **464 MHz** | 性能検討書 §0.1。［事実］`util.rpt` BUFGCTRL 2/32、`2026-10-30-la-capture-clock-design.md` §10.1・`la-testgen` §4 の Fmax | 1 サンプル/周期の取り込みは、これと BRAM（388 MHz）で頭打ち |
| BUFIO | 0 本 | **600 MHz**（ISERDES・端子だけに配れる。1 バンク） | 性能検討書 §0.1、`util.rpt` BUFIO 0/16 | DDR 950 Mb/s（475 MHz）を取るときだけ要る |
| BUFR | 0 本 | **315 MHz**（クロック領域の中） | 同上、`util.rpt` BUFR 0/16 | BUFIO と組む ISERDES の CLKDIV |
| MMCM の VCO | 900 MHz（125 / 5 × 36） | 1200 MHz | 性能検討書 §0.1。［事実］`pynq_z2.py` 74 行 `MmcmSolution(fin_mhz=125.0, mult=36.0, divclk=5, out_div=9.0, cap_div=3)` | sys と整数比で作れる速いクロックの組 |
| MMCM の出力の分周 | 整数（CLKOUT0 だけ 0.125 刻みの小数可） | — | ［推測］UG472 の記述（記憶。要確認） | 200 MHz（IDELAY の基準）が VCO 900 では整数で作れない |

- ［推測］BUFIO は、同じクロック領域の MMCM から専用の経路（HPC）で、またはそのバンクのクロック入力ピン（MRCC/SRCC）から駆動する
  （UG472。要確認）。いまの MMCM は入力ピン H16（バンク 35）の近くに置かれているはずで（配置の報告で要確認）、バンク 34 の BUFIO を
  駆動するには、バンク 34 の領域に 2 つ目の MMCM が要る。BUFG で ISERDES を動かす方式（§3.2）なら、MMCM は 1 つのまま。

### 1.3 内蔵の記録（BRAM）

［事実］いまの内蔵バッファ: 幅 16 ビット × 深さ 8192（`fprobe_map.py` 15〜16 行 `LA_WIDTH = 16`・`LA_DEPTH = 8192`、`fprobe_core.vhd` 246 行
`LA_ADDR_BITS = 13`）。cap 構成（300 MS/s）で 27.3 µs。ビルド全体の BRAM は 11 / 140 タイル（`util.rpt`）。

| 項目 | いまの値 | 理論の限界 | 根拠 | 律速 |
|---|---|---|---|---|
| BRAM の周波数 | 300 MHz（cap 領域で 16 ビットを 1 周期に 1 語） | 388.20 MHz | 性能検討書 §0.1 | 1 サンプル/周期のままなら、これが上限 |
| BRAM の帯域（幅 × 周波数） | 16 ビット × 300 MHz = 4.8 Gb/s | 1 タイル（RAMB36、単純 2 ポートで 72 ビット）× 388 MHz = 27.9 Gb/s。140 タイルで 約 3.9 Tb/s | ［推測］RAMB36 の構成は UG473 の値（記憶。要確認） | **律速しない**: 端子 14 本 × 950 Mb/s = 13.3 Gb/s より 2 桁上 |
| 容量 | 128 Kbit | 140 タイル × 36 Kbit = 約 5.0 Mbit（ほかの回路が使う分を除くと約 4.8 Mbit） | ［事実］`util.rpt` の 140 タイル | **律速は容量**。16 ch・800 MS/s（12.8 Gb/s）で約 0.37 ms（計算） |

- 幅を N 倍にして sys（100 MHz）で書けば、BRAM の周波数（388 MHz）から遠く、配線の余裕も大きい（性能検討書 §0.1 の「幅を広げて遅い領域に
  置けば避けられる」）。

### 1.4 深いバッファ（DDR の HP ポート）

| 項目 | いまの値 | 理論の限界 | 根拠 | 律速 |
|---|---|---|---|---|
| HP ポート | HP0 1 本、64 ビット × 100 MHz（800 MB/s の口）に 16 B を 3 サイクルに 1 回。見積り 約 530 MB/s | HP の口は 250 MHz（DS187 表 19）× 64 ビット = 2 GB/s / 本、4 本 | ［事実］性能検討書 §1・§0.1 | 書き手の作り（3 サイクルに 1 回、1 本） |
| 実測の書き込み | 100 MS/s × 16 ch = 200 MB/s を overrun なし（8 MB を 0.041 s） | — | ［事実］`2026-10-30-la-capture-clock-design.md` §10.3 | 符号器が sys で 1 サンプル/周期 |
| DDR そのもの | — | ［推測］PYNQ-Z2 の DDR3 は 16 ビット・1050 Mb/s で 2.1 GB/s（基板の資料の値、要確認）。PS（Linux・中継の読み）と共有 | — | 共有の DDR |
| 容量（リング） | 32 MB | 同じ（Linux の外に置いた領域） | 性能検討書 §1 | 200 MB/s で 0.16 s、1.6 GB/s なら 20 ms |

### 1.5 PC への帯域

| 項目 | いまの値 | 理論の限界 | 根拠 | 律速 |
|---|---|---|---|---|
| 持続できる垂れ流し | **約 50 MB/s**（25 MS/s・16 ch の 60 秒を 11 回中 11 回欠けなし。ピーク 約 54 MB/s） | 1 GbE の中身 約 118 MB/s（1 Gb/s × 1448 / 1538） | ［事実］`2026-10-30-la-stream-ps-direct-design.md` §10.13・§10.14・§10.16 | PYNQ の 2 コアの TCP の処理（CPU0 の softirq・CPU1 の送信が各 約 85 %）と PC の受信 |

---

## 2. 使い方ごとの到達点

記号: N = sys の 1 周期のサンプル数、ch = 記録するチャンネル数。計算の値は ［推測］（作って測るまでは計算）。

### 2.1 内蔵バッファ（短い区間を細かく。トリガで止める）

| 構成 | サンプル | 間隔 | 1 語の幅 | 深さ（sys 周期） | 時間の長さ | BRAM（RAMB36、計算） |
|---|---|---|---|---|---|---|
| いま（`fprobe_pynq_cap`） | 300 MS/s | 3.33 ns | 16 | 8192 サンプル | 27.3 µs | 4 |
| N = 6（いまのクロック） | 600 MS/s | 1.67 ns | 96 | 8192 | 81.9 µs（49,152 サンプル） | 24（1K × 36 を 3 列） |
| N = 8（VCO 1200 MHz） | 800 MS/s | 1.25 ns | 128 | 8192 | 81.9 µs（65,536 サンプル） | 32 |
| N = 8、深さ 16384 | 800 MS/s | 1.25 ns | 128 | 16384 | 164 µs | 64 |
| 上限（全 BRAM、16 ch） | 800 MS/s | 1.25 ns | 128 | 約 37,000 | 約 0.37 ms | 約 133 |

- チャンネル数との兼ね合い: 容量で律速するので、時間の長さは ch に反比例する。使う ch だけを内蔵バッファに詰める（`la_pack` の考え方を内蔵バッファに
  広げる）と、8 ch で 2 倍、2 ch で 8 倍。どの ch を残すかは使う側が明示する（既定にしない）。
- 内部の信号（ch 8 = SWDIO 駆動中、ch 9 = DAP 処理中。`fprobe_core.vhd` 772〜781 行）は sys の信号なので、N 本のレーンに同じ値が並ぶ
  （時刻の分解能は sys の 10 ns）。これは ch ごとの事実としてケイパビリティで申告する（§3.6）。

### 2.2 深いバッファ（DDR、トリガ取り込み・垂れ流しの溜め）

- RAW 16 ch で N = 8 なら 1.6 GB/s。HP の口 1 本（2 GB/s）と DDR（［推測］2.1 GB/s、PS と共有）にほぼ等しく、**現実的でない**（［推測］）。
- 現実的な組: RAW 2 ch × 800 MS/s = 200 MB/s（実測で通った書き込みの速さ）、4 ch × 800 MS/s = 400 MB/s（いまの書き手の見積り 530 MB/s 以下）。
  32 MB のリングで 0.16 s・0.08 s。
- RLE（1 変化点 4 バイト。`la_encoder.vhd` 4〜6 行）なら、DDR の書き込み 530 MB/s の見積りで毎秒約 1.3 億回の変化まで（計算）。

### 2.3 垂れ流し（長時間・間引きなし）

RAW のデータ量 = ch / 8 × サンプル数（バイト/s）。間引きは入れない（`2026-10-31-la-stream-pack-design.md` §1。利用者の方針）。

| ch（詰め） | 50 MB/s（実測の壁）で欠けなし | 118 MB/s（1 GbE の理論）なら |
|---|---|---|
| 16（詰めない） | 25 MS/s（実測） | 59 MS/s |
| 8 | 50 MS/s（実測） | 118 MS/s |
| 4 | 100 MS/s（実測。いまの sys の上限） | 236 MS/s |
| 2 | 200 MS/s（いまは sys の 100 MS/s で頭打ち） | 472 MS/s |
| 1（いまは無い: `pack` は 16・8・4・2） | 400 MS/s | 944 MS/s |

- ［事実］いまの深いバッファの符号器は sys で 1 サンプル/周期（`2026-10-31-la-stream-pack-design.md` §1）なので、2 ch でも 100 MS/s で頭打ち。
  N サンプル/周期の入口を作れば、2 ch で 200 MS/s（50 MB/s）まで RAW で流せる。600〜800 MS/s の RAW は、1 ch でも 75〜100 MB/s で、いまの壁を超える。
- **細かいサンプルを長時間流す本筋は RLE**: 費用は変化点の数に比例し（1 変化点 4 バイト）、50 MB/s で毎秒 1250 万回、118 MB/s で毎秒約 2950 万回の変化まで（計算）。
  変化の無い区間は、繰り返しの数（16 ビット、65535 サンプル）が飽和するごとに 1 レコード（800 MS/s で 81.9 µs ごと = 約 49 KB/s）。
  ［事実］RLE の実機の垂れ流しは、入力に変化の無い状態でしか測っていない（`ps-direct` §10.8）。
- RLE の弱点（事実として申告する）: 変化が瞬間的に集中すると、sys の 1 周期に最大 N レコード（N = 8 で 32 バイト/周期 = 3.2 GB/s）が出る。
  平均がリンクに収まっても、溜め（FIFO・DDR のリング）を超えれば overrun。overrun は黙らず記録する（いまの `overrun` と同じ）。

---

## 3. 根本の設計

### 3.1 全体の形

```
[ボード固有: rtl/boards/pynq_z2/]                    [ボード非依存のコア: rtl/la/, rtl/core/]
 端子 ─ IBUF ─(IDELAYE2)─ ISERDESE2 ─┐  sys の 1 周期に
                 CLK = 速いクロック    ├─ N サンプル × ch  ─→ la_lanes（N 本のレーン）─→ トリガ（N 並列）─→ 内蔵の記録（幅 N × 16）
                 CLKDIV = sys         ┘  （時間の古いものが下位）                       ├→ 詰め（N × ch ビット/周期）─→ 符号器 ─→ DDR
 xilinx_la_iserdes（ラッパー）                                                          └→ RLE（N 並列の変化点）──────────┘
```

- 速い領域はラッパーの中の ISERDES だけにする。コアは sys（100 MHz）の 1 領域で、「1 周期に N サンプル」の幅の回路になる。
  `2026-10-30-la-capture-clock-design.md` の cap 領域（300 MHz で 1 サンプル/周期）は、ISERDES の構成では使わない（cap_clk は ISERDES の CLK にだけ使う）。
- N = 1 は今の回路と同じ（既存の試験がそのまま通ることで確かめる。`CAP_RATIO = 1` と同じ流儀）。

### 3.2 クロックの組（ISERDES の CLK と CLKDIV）

ISERDES の DDR は、CLK の両エッジで取り、CLKDIV の 1 周期に DATA_WIDTH 個を出す（CLKDIV = CLK × 2 / DATA_WIDTH）。
CLKDIV を sys（100 MHz）にそろえると、ISERDES の出力がそのまま sys の 1 周期の N サンプルになり、受け渡しの回路（FIFO）が要らない。

| 案 | VCO | CLK（BUFG） | DATA_WIDTH = N | CLKDIV | サンプル | IDELAY の基準 200 MHz | 備考 |
|---|---|---|---|---|---|---|---|
| **A（最初の 1 歩）** | 900 MHz（いま） | 300 MHz（いまの cap_clk、÷3） | 6 | sys 100 MHz（÷9） | **600 MS/s**（1.67 ns） | 作れない（900 / 4.5 は CLKOUT0 だけ。CLKOUT0 は sys） | MMCM を変えない。N が 2 の累乗でないので、DDR への詰め（128 ビットの拍）に変換（ギアボックス）が要る |
| **B（目標）** | 1200 MHz（125 / 5 × 48） | 400 MHz（÷3） | 8 | sys 100 MHz（÷12） | **800 MS/s**（1.25 ns） | ÷6 で作れる | 300 MHz も ÷4 で作れる（いまの cap 構成を残せる）。VCO が上限ちょうど。BUFG 400 < 464 |
| C（部品の上限） | 950 MHz | 475 MHz（÷2。BUFG の 464 を超えるので **BUFIO**） | 8 | 118.75 MHz（BUFR） | 950 MS/s（1.05 ns） | 作れない（別のクロックが要る） | sys（÷9.5、CLKOUT0 の小数）と非整数比 → 非同期の受け渡し。バンク 34 の領域に 2 つ目の MMCM。得るものは B の 1.19 倍 |

- ［事実］ISERDES の CLK・CLKDIV を両方 BUFG（同じ MMCM の出力）から配る方式が使えること、DDR で DATA_WIDTH = 6 が使えること（DDR は 4・6・8・10・14）は、
  UG471 v1.10 の p.151（Table 3-3）・p.153（NETWORKING の Clocking Methods）で確かめた（§8 に原文）。
- ［推測］DDR の両エッジで取るので、サンプルの間隔は CLK のデューティ比の誤差だけ交互に違う（MMCM の出力のデューティの精度。DS187 の値を要確認）。
  間隔の不均一は補正せず、測った値（§3.5 の IDELAY の掃引）として記録する。
- B は MMCM の設定を変えるので、PYNQ の全構成に効く（sys は 100 MHz のまま）。`pynq_z2.py` の `sync_pll` と `pynq_z2_pkg.vhd` の両方を変え、
  `test_vhdl_package_matches_board_definition` を通す（性能検討書 §2.10 の注意）。

### 3.3 トリガ・エンコーダ（RLE）・チャンネル詰めを 1 周期に N サンプルの幅に

コアの入口: `la_lanes`（N × 16 ビット、レーン k = その sys 周期の k 番目のサンプル、k = 0 が最も古い）。`la_pack` の「時間の古いサンプルが下位」と同じ向き。

- **トリガ（la_front の N 並列版）**: レーンごとに値一致（N 個の 16 ビットの比較）、エッジはレーン k と k − 1（レーン 0 は前の周期のレーン N − 1）の差。
  成立したレーンのうち最も古いもの（優先エンコーダ）を `trigger_lane` として出す。外部トリガ（`ext_trigger`）は sys の信号なので、その周期のレーン 0 として扱い、
  そう申告する。sys の FAST 版と同じく、比較を 1 段先に評価してパイプラインにする（sys の余裕が小さいため。［事実］deep の sys の Fmax は 107〜109 MHz）。
- **サンプル間隔（rate）**: 単位を細かいサンプルにする。rate > 0（遅いサンプリング）は、使う側が明示したときだけ（いまの `rate` と同じ。既定は最高）。
- **グリッチフィルタ・パルス幅のトリガ**: N 並列にするには、1 周期の中の連続の長さ（先頭・末尾の同じ値の数）を数える回路が要る。最初の段では作らず、
  ISERDES の構成ではケイパビリティ `la_filter`・`la_pulse` を偽にする（PC 側はケイパビリティを見て出さない）。［決める］§6 の 4。
- **内蔵の記録（la_capture）**: 1 語 = N × 16 ビット、sys で書く。プリトリガ・ポストトリガは語（sys 周期）単位で数え、トリガの位置は（語の番号, レーン）。
- **チャンネル詰め（深いバッファ・垂れ流し）**: N × ch ビット/周期を 128 ビットの拍に詰める。N が 2 の累乗なら、`la_pack` を広げるだけで割り切れる
  （N = 8、ch = 2 で 16 ビット/周期 = 8 周期で 1 拍）。N = 6 は割り切れないので、ギアボックス（余りを次の拍に持ち越す）が要る。［決める］§6 の 1。
- **RLE（N 並列の変化点）**: レーン間の変化の位置（N ビット）を毎周期求め、変化点ごとに（値, 前の変化からのサンプル数）のレコードを作る。1 周期に最大 N レコード。
  プレフィックス和で詰めて、レコードの FIFO に入れ、128 ビットの拍（4 レコード）で出す。レコードの形（上位 16 = 繰り返しの数、下位 16 = 値）は変えず、
  数の単位だけを細かいサンプルにする（ホストは `la_sample_hz` で換算する。いまと同じ）。溢れたら overrun を立てる（黙らない）。

### 3.4 入力の制約

- 取り込みの最初の FF は端子の ILOGIC（ISERDES）そのものになる。ファブリックの配置に依らないので、［推測］いまビルドごとに変わる ±1 サンプルの偏り
  （`la-testgen` §5）は、ビルドによらず一定になるはず（**作って、2 つ以上のビルドで同じ測定をして確かめる**）。
- 入力は非同期（相手のクロックと関係の無い信号をサンプルする）なので、相手のクロックに対する `set_input_delay` は無い。端子 → ISERDES の経路は
  専用の配線で固定。Vivado の制約は、端子からの経路を非同期の入力として扱う（`set_false_path -from [get_ports ...]`。全部の端子で同じ指定）。
- ISERDES は内部で複数段のレジスタを通る。［推測］準安定の対策として足りるかは UG471 で確かめ、足りなければコア側で sys の 1 段を足す（遅れは固定で、申告する）。
- **チャネル間のずれ**は 3 つの事実で記録する（補正値を本体に決め打ちしない。CLAUDE.md の原則）:
  1. Vivado のモデルの値: `report_datasheet` の、端子ごとのクロックに対する入力の setup・hold（ビルドごとに出す。ビルドの記録に残す）。
  2. 実測: 同じ方形波を複数の入力に同時に入れて、エッジのレーンの差を測る（§4.3。配線の長さの差も含んだ値として記録する）。
  3. IDELAY の掃引（B の構成で使える）: 1 本の入力の遅れを 78 ps ずつ動かし、エッジが隣のレーンに移るタップを測る。サンプルの間隔（DDR の両エッジの不均一を含む）と、
     チャネルごとの位置を、タップの数で求める。
  - 結果は、ホスト側の較正の記録（設計 ID・測った日・方法・不確かさ付き）にし、時刻の補正は使う側が記録を明示して選んだときだけ行う。
    補正しない値（生のレーン）は常に返す。
- 立ち上がりと立ち下がりの遅れの差（いま、1 の区間が +1・0 の区間が −1 サンプルに見えるもの）も、チャネルごとの事実として同じ記録に入れる。
- ピンの共用: PMOD JA（ch 0〜7）は DAP の出力・入力と兼用（双方向）。［推測］ISERDES を置いたピンでも、ISERDES の組み合わせの出力（O）で DAP がその入力を読める
  （UG471 で要確認）。最初の段は入力専用の JB（ch 10〜15）だけにして、この問題を避ける。

### 3.5 時刻の付け方

- タイムベース（`time_count`、64 ビット・sys）は変えない（`2026-10-30-la-capture-clock-design.md` §5 と同じ判断。他のモジュールの時刻の意味を変えない）。
- 細かい時刻（細かいサンプルの数） n = `trigger_time` × N + `trigger_lane` − `latency`。いまの cap 構成の式（同 §10: n = `trigger_time` × R + `trigger_phase` − `latency`）の
  R を N、phase をレーンに置き換えたもの。ホストの `Capture.trigger_cap_cycles`（`host/fprobe/la.py` 274 行）がそのまま使える。
- `latency`（細かいサンプルの数）は、端子から記録までの固定の段数。シミュレーション（ISERDES の模型を含む）と実機（§4.3）で測った値を、
  info のレジスタで申告する（決め打ちの定数にしない。いまの `la_cap.info.latency` と同じ）。
- 他の情報源（イベント・DAP・SWO）との比較: それらは sys の刻み（10 ns、同期化で ±1 刻み。性能検討書 §1）なので、ロジアナが 1.25 ns でも、
  情報源の間の時刻の差の不確かさは他方の分が残る。それぞれの不確かさを別々に返す（性能検討書 §2.3 の 1）。

### 3.6 ボード非依存のコアとボード固有の部分

| 層 | 置き場 | 中身 |
|---|---|---|
| ボード固有のラッパー | `rtl/boards/pynq_z2/xilinx_la_iserdes.vhd`（`xilinx_mmcm.vhd` と同じ流儀） | IBUF・（IDELAYE2・IDELAYCTRL）・ISERDESE2 を ch ごとに。出力をレーンの順（古いものが下位）に並べ替える。ベンダーの部品はここだけ |
| シミュレーションの模型 | `tests/rtl/sim_xilinx_prims.vhd`（MMCM の模型と同じ所） | ISERDESE2 の使う形（NETWORKING・DDR・DATA_WIDTH 6/8）だけの振る舞いの模型 |
| ボードのトップ | `pynq_z2_fprobe_top.vhd` | ラッパーをつなぐ。JA は当面ファブリックの入力のまま（§3.4） |
| ボードの記述 | `fprobe_gw/boards/pynq_z2.py` | MMCM の出力の組（案 B なら VCO 1200）、ISERDES を置くピンの組 |
| コア | `fprobe_core.vhd`（generic `LA_LANES`、既定 1） | `la_lanes` を受ける。N = 1 は今の回路のまま |
| 他のボード（Primer・Nano 等） | — | N = 1（ラッパーなし）。同じ PC のソフトが動く |

- **ケイパビリティ**（PC 側はこれを見て動く）:
  - `la_sample_hz` = N × sys（細かいサンプルの周波数）。ホストは、いまと同じく `la_sample_hz / sys_clock_hz` で N を知り、info のレジスタの N と突き合わせる
    （合わなければ読み出しを止める。いまの `LogicAnalyzer.read` と同じ）。
  - ch ごとの細かさ: ISERDES を通る ch と、sys で取ってレーンに複製する ch（内部の 2 本・当面の JA）を区別するビットの並び（info のレジスタ、または LA_LAYOUT の拡張）。
    ホストは ch ごとの時刻の分解能を API で返し、画面はそれを描くだけ（画面で判断しない）。
  - `la_filter`・`la_pulse` は、N 並列で作るまで ISERDES の構成では偽。
  - ［事実］いまは 1 つの構成が内蔵バッファと深いバッファを両方は持たない（`designs.py` 128 行 `assert not (cap and deep)`）。両方を持つ構成を作るなら、
    `la_sample_hz` を内蔵と垂れ流しで別々に申告する項目が要る。［決める］§6 の 5。

---

## 4. 確かめ方

### 4.1 シミュレーション（GHDL 6 + cocotb 2）

- ISERDESE2 の模型: 使う形の振る舞い（両エッジでの取り込み、CLKDIV ごとの出力、ビットの順）。**模型の正しさ**は、一度だけ Vivado の xsim（UNISIM の本物の模型）で
  同じ入力を流して比べ、出力の並びが一致することを確かめる（記録を試験のデータとして残す）。ビットの順（Q1〜Q8 のどれが最も古いか）は UG471 と、この比較で決める。
- ラッパー（`xilinx_la_iserdes`）: 既知のパターンを入れて、レーンの順（古いものが下位）・遅れの段数。
- コア（N = 6・8）: 端子に 1 細かいサンプルずつずらしたエッジを入れ、`trigger_lane` と記録の位置が 1 ずつ動く。値一致・エッジ（レーン境界・周期境界をまたぐもの）・
  外部トリガ（レーン 0 として報告）・プリトリガ・ポストトリガ・欠けなし（連続のカウンタ）。`latency` の申告値と測った値の一致。
- N = 1 の回帰: 既存の試験がすべて変更なしで通る。
- 詰め・RLE（段 3 以降）: N 並列の変化点の詰め、1 周期に N レコードのとき、FIFO が溢れたら overrun、展開すると欠けも重複も無い。

### 4.2 ビルドのタイミング（Vivado 2025.2）

- sys（100 MHz）の WNS・TNS・Fmax を、足す前（`fprobe_pynq_cap` の sys 103〜105 MHz）と比べる。幅 96〜128 ビットのトリガの比較が sys の余裕を食う恐れ（［推測］）。
- ISERDES の CLK（300 / 400 MHz）・CLK → CLKDIV の経路、パルス幅の違反（BUFG・ISERDES の上限）。`util.rpt` の ILOGIC（ISERDES）の数 = ISERDES を置いた ch の数。
- `report_datasheet` の端子ごとの setup・hold を、ビルドの記録に残す（§3.4 の 1）。
- 合成の警告 EX1998 に相当する「駆動されない配線」が無いこと（CLAUDE.md の全信号の明示駆動）。

### 4.3 実機（PYNQ-Z2。la_testgen のループバック）

［事実］配線: RPi ヘッダ 32 番（方形波、B20）→ PMOD JB の 1 番（ch 10、W14）。GND の 2 本目（RPi ヘッダ 34 番 ↔ JB の 5 番）もつながっている
（利用者、2026-11-01）。`la-testgen` §5 の「GND の 2 本目を並べた再測定」はこれで行える。

1. **段 0（回路を変えない）: いまの `fprobe_pynq_cap` で `test_pynq_cap_testgen_hw.py` を再実行**し、GND を 2 本にした効果（細いパルスの見え方・±1 の偏り）を、
   1 本のときの記録と比べる。ここで分かること: 13 ns の限界のうち、GND の戻りの分。
2. **ISERDES の構成**（段 1 以降）: 方形波は cap_clk（300 MHz）で刻むので、周期・high の 1 単位 = 3.33 ns = 細かいサンプル 2 個（案 A）。
   - 周期・high を変えた方形波で、1 と 0 の連続の長さが細かいサンプルで 2 × 設定 ± 測った偏り、合計が 2 × 周期（欠けも重複も無い）。
   - `edge = 1`（立ち下がりで変える = 1.67 ns 遅れる）で、エッジがちょうど 1 細かいサンプル動く（案 A）。**時間分解能 1.67 ns の直接の確認**。
   - 同じ測定を 2 つのビルドで行い、偏りがビルドによらないか（§3.4 の推測の確かめ）。
   - 細いパルス（high = 1・2・3）の見え方を、300 MS/s のときの記録と比べる（アナログの帯域の限界は ISERDES では動かないはず、の確かめ。［推測］）。
3. **チャネル間のずれ**（配線の追加が要る。［決める］§6 の 6）: 方形波を JB の 2 本以上に同時に入れ、エッジのレーンの差を測る。案 B なら IDELAY の掃引も。
4. 方形波の出力の細かさ: いまの方形波の出力はファブリックの FF（［推測］）。出力も OSERDES・ODDR にすれば 1.67 ns 刻みの刺激が作れる（汎用の試験のパターン出力。
   測定器の自己検証）。［決める］§6 の 7。

---

## 5. 段階

| 段 | 作るもの | 確かめ | 得るもの |
|---|---|---|---|
| 0 | なし（再測定だけ） | §4.3 の 1 | GND 2 本の効果。アナログの限界の切り分けの最初 |
| **1（最初の 1 歩）** | ラッパー `xilinx_la_iserdes`（案 A: N = 6、CLK = いまの cap_clk 300 MHz、CLKDIV = sys）を **JB の 6 本だけ**に。ISERDES の模型。コアの `LA_LANES`・`la_lanes`、N 並列のトリガ（値一致・エッジ・外部）、幅 96 の内蔵の記録（深さ 8192 周期）。info（N・latency・ch ごとの細かさ）、ケイパビリティ、ホストの時刻（`trigger_lane`）。新しい構成（仮名 `fprobe_pynq_lanes`）。フィルタ・パルス幅は無し（申告） | §4.1・§4.2・§4.3 の 2 | 内蔵バッファで **600 MS/s・1.67 ns**、82 µs。ch 10〜15 の時刻の分解能が 2 倍 |
| 2 | 案 B（VCO 1200・N = 8・IDELAYCTRL）。IDELAY の掃引の試験 | ロック・タイミング・掃引で間隔と ch ごとの位置 | **800 MS/s・1.25 ns**、間隔とずれの実測の記録 |
| 3 | N 並列の詰め（2 の累乗の N）を深いバッファ・垂れ流しの入口に | 2 ch・200 MS/s を RAW で 50 MB/s、欠けなし | 垂れ流しの RAW の上限が sys の 100 MS/s を超える |
| 4 | N 並列の RLE | 方形波を RLE で 800 MS/s の垂れ流し、展開が RAW と一致 | 細かい時刻のまま長時間（変化点の数が律速） |
| 5 | JA（ch 0〜7）にも ISERDES（DAP との共用の確認の後） | DAP の動作が変わらない、SWD・JTAG の回帰 | 全 14 本の外部ピンで細かい時刻 |
| 6 | N 並列のフィルタ・パルス幅のトリガ | 既存の試験の N 版 | 機能の欠けを埋める |
| 7（候補） | 案 C（950 MS/s）、キャリーチェーンの TDC（性能検討書 §2.3 の 3） | — | 必要になったときに別の設計書で |

段 1 で `fprobe_pynq_cap`（300 MS/s、1 サンプル/周期）は残す（比べるため。消すかは段 2 の後に決める）。

---

## 6. 決める必要のあること（利用者に聞く分岐）

1. **目標の N と MMCM**: 案 A（N = 6、いまのクロック）で始めて案 B（N = 8、VCO 1200 MHz）に進む（推奨）／最初から案 B／案 C（950 MS/s）まで行く。
   案 B は VCO が上限ちょうどで、PYNQ の全構成の MMCM が変わる（sys は 100 MHz のまま）。案 A の N = 6 は、段 3 の詰めでギアボックスが要る。
2. **最初に ISERDES を置くピン**: JB の 6 本だけ（推奨。入力専用で DAP と無関係）／JA も最初から（DAP の入力を ISERDES の O から取る確認が要る）。
3. **内蔵バッファの深さ（BRAM の割り当て）**: 8192 周期（N = 6 で 24 タイル・82 µs、推奨の最初）／16384 周期（N = 8 で 64 タイル・164 µs）／もっと（他の機能の BRAM と取り合う）。
4. **フィルタ・パルス幅のトリガ**: ISERDES の構成では当面無し（ケイパビリティで偽を申告。推奨）／段 1 から N 並列で作る。
5. **構成の分け方**: 新しい構成（内蔵バッファだけ、仮名 `fprobe_pynq_lanes`）／内蔵と深いバッファを両方持つ構成（`la_sample_hz` を 2 つ申告する変更が要る）。
6. **チャネル間のずれの測定の配線**: RPi ヘッダ 32 番の方形波を、JB の 1 番に加えて JB の 2 番（など）にも同時につなぐか。つなぐ場合は、配線の指示に
   電源の順・電圧・逆流の注意を書く（2 本とも FPGA の入力で、方形波の出力は有効にしたとき以外 Hi-Z）。
7. **方形波の出力を OSERDES・ODDR にするか**（1.67 ns 刻みの刺激。測定器の自己検証用の汎用の機能）。
8. **較正の記録と補正**: ずれ・偏りの記録を、ホストのどこに置き、どう指定させるか（設計 ID ごとのファイル／API の引数）。既定で補正しない、は原則どおり。

### 主担当の判断（2026-11-01）

1. 案 A（N = 6、いまの 300 MHz を CLK、CLKDIV = sys 100 MHz、600 MS/s）から始める。N は generic にして、案 B（N = 8、400 MHz）へ
   MMCM の変更だけで進める形にする（実装: N = 2 × `cap_ratio`。`designs.py` の `_pynq(lanes=True)`、ラッパーと模型は 6・8 の両方を試験）。
2. ISERDES を置くのは PMOD JB の 6 本（`la_in`、ロジアナの ch 10〜15）だけ。
3. 内蔵バッファの深さは、PYNQ の BRAM の空きの半分以内で最大。`fprobe_pynq_cap` の使用 11 / 140 タイル（`C:\fw51\Default\util.rpt`）から
   空き約 129、その半分約 64 タイル以内の 2 の累乗で **16384 語**（N = 6: 96 ビット × 16384 = 48 タイル、N = 8: 64 タイル。32768 語は 96 タイルで超える）。
   = 98,304 サンプル、163.8 µs。ビルドの資源の報告で確かめる（§9）。
4. フィルタ・パルス幅のトリガは、ISERDES の構成では当面無し（ケイパビリティ `la_filter`・`la_pulse` を偽で申告。ホストは使おうとすると断る）。
5. 構成は新しい構成 `fprobe_pynq_fast`（`fprobe_pynq_cap` は変えない）。
6. 配線の追加（複数入力への同時の方形波）はしない。
7. 方形波の出力の OSERDES 化はしない。
8. 較正の記録は後回し。

---

## 7. 設計原則との整合

- ヒューリスティックを入れない: 本体はレーンの生の値・トリガのレーン・固定の遅れ（測った値）を返すだけ。間隔の不均一・チャネル間のずれ・立ち上がりと立ち下がりの差は、
  測った事実（方法・不確かさ付き）として記録し、補正は使う側が明示して選ぶ。見えない脈を推定で埋めない。
- 間引きをしない: 垂れ流しは RAW（詰めた ch は欠けない）か RLE（無損失）。遅いサンプリング（`rate`）は使う側が明示したときだけ。溢れは overrun として記録する。
- 画面は考えない薄い層: 細かい時刻・ch ごとの分解能はホストが計算して API で返す。画面はそれを描くだけ。
- 移植性: ISERDES・IDELAY・BUFG の使い方はボードのラッパーの中だけ。コアは「1 周期に N サンプル」の口だけ。ケイパビリティで N（`la_sample_hz`）と ch ごとの細かさを申告し、
  N = 1 のボードでも同じ PC のソフトが動く。
- 特定のターゲットのための機能ではない: 端子の取り込みの細かさは、どの対象にも効く汎用の機能。方形波の出力は測定器の自己検証の道具。
- 全信号を明示的に駆動: N = 1 の分岐・generate で外した側（使わないレーン、フィルタ・パルス幅の無い構成）の信号も駆動する。

## 8. 未確認・リスク

- ［事実］UG471 v1.10（2018-05-08。Xilinx の PDF。原本 docs.amd.com の版が取れず、同じ版の写し https://0x04.net/~mwk/doc/xilinx/ug/ug471_7Series_SelectIO.pdf で読んだ）で確かめたこと（段 1 の実装に要るもの）:
  - **DDR の DATA_WIDTH = 6 は可**: Table 3-3「NETWORKING / DDR: 4, 6, 8, 10, 14」（p.151）。属性の表「If DATA_RATE = DDR, value is limited to 4, 6, 8, 10, or 14」。
    UG953（ISERDESE2 の頁）も同じ文。Vivado 2025.2 の UNISIM の模型（`data/verilog/src/unisims/ISERDESE2.v`）の DRC も「NETWORKING DDR 4, 6, 8, 10, 14」。
  - **CLK・CLKDIV を両方 BUFG で配れる**: 「ISERDESE2 Clocking Methods / NETWORKING Interface Type」（p.153）: 有効な組は「CLK driven by BUFIO, CLKDIV driven by BUFR」と
    「CLK driven by MMCM or PLL, CLKDIV driven by CLKOUT[0:6] of same MMCM or PLL」だけで、「When using a MMCM to drive the CLK and CLKDIV of the ISERDESE2,
    the buffer types suppling the ISERDESE2 can not be mixed. For example, if CLK is driven by a BUFG, then CLKDIV must be driven by a BUFG as well.」。
    案 A（cap_clk 300 MHz と sys 100 MHz、同じ MMCM の CLKOUT1・CLKOUT0、どちらも BUFG）はこれに当たる。なお v1.10 の改訂履歴に「CLK driven by BUFG and CLKDIV
    driven by a different BUFG」の項目（MMCM の条件の無い形）を NETWORKING から外したとある。同じ MMCM から割らない 2 本の BUFG は使えない。
    CLK と CLKDIV は「nominally phase-aligned」であること（同じ MMCM の整数比の出力は時刻 0 の立ち上がりがそろう）。
  - **Q1〜Q8 の時間の順**: 「The first data bit received appears on the highest order Q output.」（p.146、Registered Outputs - Q1 to Q8）。DATA_WIDTH = 6 では Q6 が最も古く、Q1 が最も新しい。
  - CLKB: 「In any mode other than MEMORY_QDR, connect CLKB to an inverted version of CLK.」（p.148）。ラッパーは `IS_CLKB_INVERTED = '1'` で端子の中の反転を使う。
  - 遅れ: 「When the ISERDESE2 interface type is NETWORKING, the latency is two CLKDIV cycles.」（ISERDESE2 Latencies）。
  - RST: UG953「it must be treated as a synchronous reset to the CLKDIV time domain and have a minimum pulse of one CLKDIV cycle」（ラッパーは sys のリセット）。
- ［事実］Vivado 2025.2 の UNISIM の ISERDESE2 を xsim で動かして記録した（`gateware/tests/xsim/run_iserdes_ref.py` → `gateware/tests/data/iserdes_ref_w6.txt`・`_w8.txt`。
  NETWORKING・DDR・DATA_WIDTH 6 と 8、CLK と CLKDIV は時刻 0 で立ち上がりがそろう）。CLK の辺に番号 i を付け（CLKDIV の立ち上がり w と同時の辺が i = N × w）、辺 i のサンプルを b(i) とすると、
  立ち上がり w の後の **Qj = b(N × w − (j + N − 4))**（N = 6: Q1 = b(6w − 3)〜Q6 = b(6w − 8)、N = 8: Q1 = b(8w − 5)〜Q8 = b(8w − 12)）。
  UG471 の順（最も上の Q が最も古い）と一致。GHDL の模型（`gateware/tests/rtl/sim_xilinx_prims.vhd` の ISERDESE2）はこの式で作り、`tests/test_xilinx_la_iserdes.py` が記録との一致を確かめる。
  §4.1 の「模型の正しさは一度だけ xsim で比べる」はこれで行った。
- まだ確かめていないこと: 組み合わせの出力 O で DAP が入力を読めるか（段 5 用）、内部の段数が準安定の対策として足りるか（ISERDES の中の FF は 2 段以上あるが、
  MTBF の値は資料に無い）、BUFIO を駆動できる元（案 C 用）。DS187 で確かめること: SDR の受信の上限、MMCM の出力のデューティの精度（DDR の両辺の間隔の不均一）、IDELAYCTRL の基準の許容範囲。
- PYNQ-Z2 の回路図で確かめること: PMOD の線の直列抵抗・ESD 部品（アナログの帯域）、DDR3 の幅と速さ。
- sys の余裕（7〜9 %）を、幅 96〜128 ビットのトリガが食う恐れ。パイプラインで逃がすが、外部トリガ・クロストリガの遅れが sys の周期の分だけ増える（遅れは申告する）。
- VCO 1200 MHz（上限ちょうど）のロックとジッタは、実機で確かめる。
- 1 GbE の理論（118 MB/s）に近づくのは中継・PC の改善（`ps-direct` §10）で、この設計の範囲の外。垂れ流しの細かさは、それまで RLE が前提。

## 9. 段 1 の実装の記録（2026-11-01）

### 9.1 作ったもの

| 層 | ファイル | 中身 |
|---|---|---|
| ボード固有（ベンダーの部品はここだけ） | `gateware/rtl/boards/pynq_z2/xilinx_la_iserdes.vhd` | ch ごとに ISERDESE2（NETWORKING・DDR・DATA_WIDTH = LANES（6・8）・IOBDELAY NONE・NUM_CE 1・`IS_CLKB_INVERTED = '1'`）。出力 q のレーン k の ch c = q(k × CHANNELS + c)、レーン k = Q(LANES − k) |
| | `pynq_z2_pkg.iserdes_newest_age(lanes)` | 語の最も新しいレーンの遅れ = lanes − 3 辺（UNISIM の記録） |
| | `pynq_z2_fprobe_top.vhd` | LA_LANES > 1 のとき la_in → ラッパー（CLK = cap_clk、CLKDIV = sys、RST = sys_rst）→ コアの `la_input_lanes`。LA_LANES = 2 × CAP_RATIO を assert |
| 模型 | `gateware/tests/rtl/sim_xilinx_prims.vhd` の ISERDESE2 | §8 の式どおりの振る舞い。`tests/data/iserdes_ref_w6/8.txt`（UNISIM を xsim で）と一致を試験 |
| コア | `rtl/la/la_record.vhd` | la_capture の記録の部分（状態・BRAM）を分けたもの。la_capture = la_front + la_record（既存の試験がそのまま通る） |
| | `rtl/la/la_lane_front.vhd` | N 並列のトリガ（値一致・エッジ・外部トリガ = 次の語のレーン 0）。3 段のパイプライン。捕捉の最初の語のレーン 0 はエッジを見ない |
| | `rtl/la/la_lane_capture.vhd` | la_lane_front + la_record（1 語 = N × 16 ビット、語で数える）。latency = N × 4 + IN_AGE（N = 6 で 27） |
| | `rtl/la/la_mem_csr.vhd` | generic SLICES: 窓のワード j × 深さ + w = 語 w のビット 32j〜32j + 31（レーン 2j・2j + 1） |
| | `fprobe_core.vhd` | generic `LA_LANES`・`LA_LANE_ADDR_BITS`・`LA_LANE_IN_AGE`、口 `la_input_lanes`。拡張ヘッダのピン（2 段同期）と内部の 2 本は全レーンに同じ値。多段トリガには最も新しいレーン。窓 la_mem はエラー、la_lanes_mem を出す |
| CSR | `fprobe_map.py` | `la_cap.info.lanes`（bit 24〜27）、窓 `la_lanes_mem`（0x40000、0x40000 バイト = 最大 8 レーン × 16384 語）。`ratio` は「細かいサンプル / sys」、`trigger_phase` はレーン |
| ケイパビリティ | `caps.py` | `la_lanes`、TLV 22 `LA_TESTGEN_HZ`（方形波を刻むクロック。暗黙の規則に頼らない）、TLV 23 `LA_FINE`（細かい ch のビットマップ。0xFC00）。21 は時刻の正しさ S0 の `LA_CH_TIMING`。`la_filter`・`la_pulse` は偽。時間軸は la_cap.info の latency で決まり、ホストは LA_DELAY を引かない（取り込みの領域の構成と同じ。S0 が直した規則）。ch ごとの時刻（`fprobe_gw/timing.py` の `_la_lane_channel_timing` → LA_CH_TIMING）: 専用の入力 = 軸そのもの（0、位相の幅 1 刻み）、拡張ヘッダのピン・内部の ch = 確かめていない（None）、深いバッファ無し（None） |
| 構成 | `designs.py` の `fprobe_pynq_fast` | N = 2 × cap_ratio = 6、la_sample_hz 600 MHz、la_depth 16384 × 6 = 98304 サンプル（163.8 µs） |
| ホスト | `host/fprobe/la.py`・`la_wave.py`・`caps.py`・`probe_la.py` | `LogicAnalyzer.lanes`、pretrigger をサンプルから語に（切り上げ）、rate の指定は断る、la_lanes_mem から時間順に並べる、`Capture.channel_period_s(ch)`、`la_collect` に `channel_period_ns`・`trigger_fine_sample`（API の要約も更新、`python -m fprobe.api gen`） |

時刻: ピンが変わった細かいサンプルの番号 n（辺 n の後に変わり辺 n + 1 で取られた）= `trigger_time × N + trigger_lane − latency`。
細かい ch（ch 10〜15）についての式。sys で取る ch（0〜9）は値が sys の周期ごとで、細かい ch との時刻の関係は確かめていない（申告は「分解能 = sys の周期」だけ）。

### 9.2 試験（GHDL 6 + cocotb 2）

- `tests/test_xilinx_la_iserdes.py`（N = 6・8）: 模型 = UNISIM の記録、ラッパーのレーンの順・ch の取り違えなし。
- `tests/test_la_lane_capture.py`（N = 6・8。端子 → ISERDESE2 の模型 → la_lane_capture）: ピンを変える辺を 2N + 1 通りずらして n の式が一致、
  記録のトリガの位置が辺 n + 1 のサンプル、記録に欠け・重複なし（辺の番号のカウンタ）、立ち下がり・値一致・1 サンプルの脈・外部トリガ（レーン 0）・プリトリガ。
  遅れを 1 ずらすと失敗することも確かめた（試験が効いている）。
- `tests/test_fprobe_core_lanes.py`（CSR 越しの端から端）: ケイパビリティ・info（比 6・レーン 6・遅れ 27）・la_mem がエラー、辺を 1 ずつずらすと n が 1 ずつ動く、
  la_lanes_mem の位置、sys の ch が全レーンに同じ値で欠けなし。
- ホスト `host/tests/test_la_lanes.py`: 時間順の並べ方・トリガの位置・細かい時刻・ch ごとの分解能・info の不一致で読まない・プリトリガの語・rate を断る・新しい TLV。

### 9.3 ビルド（Vivado 2025.2、`python -m fprobe_gw build --build-dir C:/fw60 fprobe_pynq_fast`、`C:\fw60\top.bit`。PYNQ には書いていない）

- タイミング: すべて合格。sys_clk 100 MHz で WNS 0.868 ns（Fmax 109.5 MHz）、cap_clk 300 MHz で WNS 0.490 ns（Fmax 351.7 MHz）、
  sys → cap（la_testgen の受け渡し）WNS 0.063 ns、WHS 0.029 ns。比べ: `fprobe_pynq_cap` は sys 106.8 MHz（`docs/boards/pynq-z2.md`）。
- 資源（`util.rpt`）: LUT 8850 / 53200（16.6 %）、FF 11164（10.5 %）、BRAM 30 / 140 タイル（RAMB36 28・RAMB18 4）、ILOGIC 6（ISERDESE2 6 = JB の 6 本）、
  BUFGCTRL 2、MMCM 1。la_lane_capture: LUT 263・FF 368・BRAM 23 タイル。
- ［事実］BRAM は計算（96 ビット × 16384 = 48 タイル）より少ない 23 タイル。全レーンに同じ値を置く sys の ch（ピン 8 本・内部 2 本）の列を、合成が 1 本にまとめたため
  （［推測］記録する一意のビットは 10 + 6 × 6 = 46 ビット/語 ≒ 21 タイル）。§6 の 3 の上限（約 64 タイル）の中。
- `fprobe_pynq_cap` の比べ: LUT 9205・BRAM 11（`C:\fw51`）。
- 端子からの経路は制約していない（非同期の入力。`set_input_delay` も `set_false_path` も無く、Vivado は時間を測らない）。§4.2 の `report_datasheet` は未取得。
- 作り直し（2026-11-01、時刻の正しさ S0 を取り込んだ後。TLV の番号の付け替え・LA_CH_TIMING を含む。`C:\fw61\top.bit`。PYNQ には書いていない）:
  すべて合格。sys 107.2 MHz、cap_clk 341.5 MHz、sys → cap WNS 0.046 ns、WHS 0.024 ns。LUT 8941（16.8 %）、BRAM 30 タイル、ISERDESE2 6。


## 10. 実機の確認（2026-11-01、`C:w61`、ID fc2dcdc35a6745c1、PYNQ に実行時の書き込み）

配線: RPi ヘッダ 32 番（la_testgen）→ PMOD JB の 1 番（ch 10）、GND は 34 番 ↔ JB の 5 番。`test_pynq_fast_testgen_hw.py` 6 件通過。

| 方形波（cap_clk の単位、1 単位 = 細かいサンプル 2 個） | 1 の長さ（細かいサンプル） | 0 の長さ |
|---|---|---|
| 周期 300、High 150 | 301・302（設定 300） | 298・299 |
| 周期 60、High 30 | 61・62（設定 60） | 58・59 |
| 周期 30、High 15 | 31 のみ（設定 30） | 29 のみ |
| 周期 20、High 10 | 21 のみ（設定 20） | 19 のみ |

- **1 回の取り込みの中で、変化の位置が細かいサンプルの格子に揃う**（周期 30 で、立ち上がり 1638 回がすべて同じ位置、立ち下がりも同じ）。1.67 ns より細かい揺れは見えない。
- **奇数の長さ（21・31・61）が出る** = cap_clk（300 MS/s、長さは必ず偶数）より細かく取れていることの直接の証拠。
- **1 の長さが設定より +1 細かいサンプル（1.67 ns）長い**。毎回同じなので、線のアナログの性質（立ち上がりと立ち下がりの遅れの差。10/31 の測定でも「立ち上がりが早く、立ち下がりが遅く見える一定の差」）と見ている［推測: 出力の駆動と入力のしきい値のどちらの寄与かは分けていない］。本体は補正しない（事実として記録。較正は使う側）。
- 細いパルス（edge 0・1 とも）: 3.3 ns（2 サンプル）は見えない、6.7 ns は 1228/1228 見えて長さ 2 サンプル（縮む）、10.0 ns は長さ 5・6（設定 6）、13.3 ns は長さ 8（正確）。見える最小の幅は 300 MS/s の構成と同じ（アナログの帯域で決まり、ISERDES では変わらない、という設計の予想どおり）。
- 試験の直し: 「edge を変えると位置の偶奇が入れ替わる」は、取り込みのたびに方形波の位相が変わるので成り立たない（取り込みをまたいで位置を比べていた）。取り込みの中の格子と、奇数の長さで確かめる形にした。

## 11. 段 2 の実装の記録（2026-11-01。案 B: 800 MS/s・PMOD JA も ISERDESE2）

### 11.1 クロックの組（主担当の判断）

| 出力 | 分周（VCO 1200 MHz = 125 / 5 × 48） | 周波数 | 使う所 |
|---|---|---|---|
| CLKOUT0（BUFG） | ÷ 12 | sys 100 MHz | 全部の回路（ISERDESE2 の CLKDIV、タイムベース、PS との AXI） |
| CLKOUT1（BUFG） | ÷ (12 / CAP_RATIO) | cap_clk = sys × CAP_RATIO | `fprobe_pynq_cap`: ÷ 4 = 300 MHz（内蔵ロジアナ一式。前と同じ周波数）。`fprobe_pynq_fast`: ÷ 3 = **400 MHz**（ISERDESE2 の CLK。DDR で 800 MS/s。試験用の方形波もこれで刻む） |
| （空き） | ÷ 6 | 200 MHz | IDELAYCTRL の基準（下の判断。いまは出さない） |

- **VCO は全部の PYNQ の構成で 1200 MHz**（`fprobe_gw/boards/pynq_z2.py` の `sync_pll` と `pynq_z2_pkg.vhd` の `MMCM_*`。
  一致は `tests/test_boards_pynq_z2.py::test_vhdl_package_matches_board_definition`）。sys は 100 MHz のまま。cap_clk の分周は
  ボードの定数ではなく構成の `cap_ratio` から決まる（`MmcmSolution.cap_div(ratio)` と VHDL の `pynq_z2_pkg.mmcm_cap_div(ratio)` が
  同じ式 out_div / ratio。BUFG の上限 464 MHz を超える比・割り切れない比は Python で ValueError、VHDL で assert）。
  12 は 2・3・4・6 で割れるので、200・300・400 MHz が同じ VCO から作れる（600 MHz は BUFG を超えるので不可）。900 MHz の版は 400・200 MHz が作れなかった。
- 理由: 構成ごとに別の MMCM の設定（VCO 900 / 1200）を持つより、1 つの VCO から構成が比を選ぶ形のほうが、ボードの記述・VHDL の定数・試験が 1 組で済む。
  `fprobe_pynq_cap` の cap_clk は 300 MHz のまま（÷ 4）で、回路も申告（la_sample_hz 300 MHz）も変わらない。
- ［推測・要実機確認］VCO 1200 MHz は -1 の上限ちょうど（性能検討書 §0.1）。範囲の中なので Vivado は通すが、ロックとジッタは PYNQ で確かめる
  （LED 0 = ロック）。全部の PYNQ の構成に効く: **次に `fprobe_pynq_deep` 等を作り直すと VCO 1200 MHz になる**。
- N = 2 × CAP_RATIO = 8（`pynq_z2_fprobe_top.vhd` の assert）。la_testgen は cap_clk（400 MHz）で刻むので、方形波の 1 単位 = 2.5 ns = 細かいサンプル 2 個
  （段 1 と同じ「1 単位 = 2 サンプル」。ケイパビリティ LA_TESTGEN_HZ = 400 MHz）。
- **IDELAYCTRL は作らない**（判断）。理由: (1) UG471 v1.10 p.124「If the IDELAYE2 or ODELAYE2 primitives are instantiated, the IDELAYCTRL module must also be
  instantiated」= IDELAYCTRL は IDELAYE2 の較正のためだけの部品で、IDELAYE2 が無いと役目が無い。(2) IDELAYE2 を入れて掃引するには、タップを
  CSR から動かす口（VAR_LOAD・CE/INC）・ISERDESE2 の `IOBDELAY` の変更・掃引の手順と記録（§3.4 の 3、§6 の 8 の較正の記録）が要り、それは較正の段の仕事。
  (3) IDELAYE2 を通すと、タップ 0 でも端子 → ISERDESE2 の遅れが変わり、latency の申告（`iserdes_newest_age`）と UNISIM の記録の比べを作り直すことになる。
  基準（UG471 p.124: REFCLK は BUFG か BUFH で配る。属性 REFCLK_FREQUENCY は 190〜210、既定 200）は、この VCO なら空きの出力 ÷ 6 = 200 MHz で作れる
  ので、較正の段で MMCM の VCO を変えずに足せる。

### 11.2 資料で確かめた事実（UG471 v1.10、§8 と同じ写し。PMOD JA を ISERDESE2 で取れるか）

- p.15「Each IOB has a direct connection to an ILOGIC/OLOGIC pair containing the input and output logic resources for data and 3-state control for the IOB.
  Both ILOGIC and OLOGIC can be configured as ISERDES and OSERDES」: 入力の部品（ILOGIC = ISERDESE2）と出力の部品（OLOGIC）は端子ごとに別にある。
- p.127「Combinatorial Output Data and 3-State Control Path: The combinatorial output paths create a direct connection from the FPGA logic to the output driver or
  output driver control. These paths are used automatically by software when: 1. There is direct (unregistered) connection from logic resources in the FPGA logic
  to the output data or 3-state control.」: いまの JA の出力（`pin_select` の組み合わせの出力と 3 状態の制御）は OLOGIC の組み合わせの経路を通り、ILOGIC を使わない。
- p.147「Combinatorial Output - O: The combinatorial output port (O) is an unregistered output of the ISERDESE2 module. This output can come directly from the data
  input (D)」: ISERDESE2 を置いた端子の値を、ファブリック（DAP の SWDIO・TDO・SWO・UART RX・nRESET・gpio・多段トリガ・RTS/CTS）は O から読める。
  ISERDESE2 を置くと端子の IBUF の出力は ISERDESE2 の D にだけつなぐので、トップはファブリックで JA を読む所をすべて O（`ja_in`）に替えた。
- p.156「By using the ISERDESE2 as a feedback port, it can not be used as an input for external data.」: 両立しないのは OFB（OSERDESE2 からの折り返し）を
  使うときだけ。fprobe は OSERDESE2 も OFB も使わない（`OFB_USED = "FALSE"`）。
- 結論: **JA の 8 本（SWCLK・TDI・nRESET・SWDIO・予備・UART RX・SWO・UART TX）はすべて ISERDESE2 で取れる**（出力として駆動している線も、
  端子に出ている値をそのまま細かく取る）。sys のまま残した線は無い。Vivado の配置・DRC はビルドで通った（§11.5）。［推測・要実機確認］
  DAP の動作が変わらないこと（O は D の組み合わせで、ファブリックの読みは IBUF 直結とほぼ同じ遅れ。SWD・JTAG の回帰を実機で）。

### 11.3 作ったもの

| 層 | ファイル | 中身 |
|---|---|---|
| ボード | `fprobe_gw/boards/pynq_z2.py`・`pynq_z2_pkg.vhd` | VCO 1200 MHz（mult 48・divclk 5・sys ÷ 12）。`mmcm_cap_div(ratio)`。`MMCM_CAP_DIV`・`CAP_CLK_HZ` の定数を除いた（比は構成ごと） |
| | `fprobe_gw/toolchain/vivado.py`・`boards/__init__.py` | `MmcmSolution.cap_div(ratio)`・`cap_hz(ratio)`（固定の `cap_div` 欄を除いた）。`Board.cap_clock(ratio)` |
| | `xilinx_la_iserdes.vhd` | 口 `o`（ISERDESE2 の O = 端子の値）。注に UG471 の根拠 |
| | `pynq_z2_fprobe_top.vhd` | `LA_FINE_PINS` なら 1 つの包み（ch 0〜7 = JA、8〜13 = JB）。JA を読む所はすべて `ja_in`（= O）、細かいサンプルは `la_pin_lanes` へ |
| コア | `fprobe_core.vhd` | generic `LA_FINE_PINS`、口 `la_pin_lanes`。真ならピンの ch をレーンから記録（sys の 2 段の同期化をしない）。`la_pins` は sys の経路（多段トリガ・gpio）に残る |
| 申告 | `caps.py`・`timing.py` | `Capabilities.la_fine_pins` → LA_FINE に ch 0〜7（`fprobe_pynq_fast` は 0xFCFF）。LA_CH_TIMING: 細かいピン = 専用の入力と同じ（ずれ 0・位相の幅 1）。内部の 2 本は確かめていない（None）のまま |
| 構成 | `designs.py` | `fprobe_pynq_fast`: cap_ratio 4、N 8、la_sample_hz 800 MHz、la_depth 16384 × 8 = 131072 サンプル（163.8 µs）。`fprobe_pynq_cap` は cap_ratio 3（定数 `PYNQ_Z2_CAP_RATIO`） |

- 時刻: n = `trigger_time × 8 + trigger_lane − latency`、latency = 8 × 4 + (8 − 3) = 37（細かいサンプル。la_cap.info で申告し、ホストはそれを使う）。
- ホストは変えていない（N・LA_FINE・LA_CH_TIMING をケイパビリティから読む作りのため）。

### 11.4 試験

- `tests/test_fprobe_core_lanes.py`（CSR 越しの端から端）を N = 6（専用の入力だけ。段 1）と N = 8・ピンも（段 2 = いまの `fprobe_pynq_fast`）で回す。
  N = 8 では、ピン 0（ch 0）でも辺を 1 つずつずらすと n が 1 つずつ動き記録の位置もそこ、同じ細かい辺の後に変えたピン 0 と専用の入力 0 が
  記録の同じサンプルで変わりトリガの n も同じ（時刻の表の「ピンのずれ 0」の根拠）、内部の ch（selftest の ch 9）は語の 8 サンプルに同じ値で語ごとに反転、
  ケイパビリティ LA_FINE = 0xFCFF・info（比 8・レーン 8・遅れ 37）。コアの `if LA_FINE_PINS` を外すと N = 8 の 2 件が落ちることを確かめた（試験が効いている）。
- `tests/test_designs_pynq.py`・`tests/test_boards_pynq_z2.py`: 構成の申告（800 MHz・0xFCFF・方形波 400 MHz・ch ごとの時刻）、cap_clk の制約 400 MHz、
  MMCM の比の検査（比 5 は割り切れない、600 MHz は BUFG を超える、VCO 1250 は範囲外）、Python と VHDL の MMCM の一致。
- 実機の試験 `gateware/tests/hil/test_pynq_fast_testgen_hw.py` を、N・方形波のクロックをケイパビリティから読む形にした（1 単位 = la_sample_hz / testgen_clock_hz）。
  段 1 の実機の事実に沿う: 1 回の取り込みの中で変化が格子に揃う（周期の余りが 1 通り）、1 の長さに 1 単位の倍数でないもの（奇数）が出る、
  1 の長さ − 設定を事実として print、取り込みをまたいで位置を比べない。`FPROBE_PYNQ_TESTGEN_CH` で受ける ch を JA に替えられる。
- `tests/test_toolchain_vivado.py`: ソースの隣の `<entity>.xdc` を `read_xdc -ref <entity>` で合成の前に読むこと、PYNQ の構成で `cdc_value.xdc` が見つかること。
- `tests/test_la_testgen.py`（2 つのクロック・同じクロック）: 下のカウンタの作り替えの後も、周期・high・丸め・止める・リセット・立ち下がりの半周期がそのまま通る。

### 11.5 ビルド（Vivado 2025.2、`python -m fprobe_gw build --build-dir C:/fw62 fprobe_pynq_fast`、`C:\fw62\top.bit`。PYNQ には書いていない）

- 1 回目と 2 回目は不合格で、400 MHz に上げた cap_clk の領域の 3 か所を直した（どれも la_testgen の回路。ISERDESE2 そのものの経路は問題なし）:
  1. 方形波の下りカウンタ（「cnt = 0 なら読み込み、でなければ −1」）: −0.094 ns → ゼロの判定 z・やり直し・次に読み込む値をレジスタにし、選択を足し算の入口に入れた →
     まだ −0.024 ns（12 ビットのキャリー 3 段）→ 下 4 ビットと上 8 ビットに分けた（上は「下 4 ビットがいま 0」のレジスタで −1）。
  2. 設定の受け渡し `cdc_value` の held → value_r（設計で 3 周期以上ある経路を Vivado が 1 周期 2.5 ns で検査）: −0.068 ns →
     モジュールの制約 `rtl/common/cdc_value.xdc`（多周期 2）。ツールはソースの隣の `<entity>.xdc` を `read_xdc -ref` で読む（`fprobe_gw/toolchain/vivado.py` の
     `module_constraints`。入力の要約 inputs.sha256 にも入れた）。
  3. sys の rst を cap_clk に 1 段で渡す所: −0.018 ns → 2 段の同期化（`sync_bit`）に替え、`rtl/common/sync_bit.xdc` で最初の段までを時間を測らない経路にし、
     `sync_bit` に `ASYNC_REG` を付けた（全部の構成の `sync_bit` に効く。どれも非同期の入力の同期化）。
- 結果（3 回目）: すべて合格。sys_clk 100 MHz で WNS 0.474 ns（Fmax 105.0 MHz）、cap_clk 400 MHz で WNS 0.083 ns（Fmax 413.7 MHz）、sys → cap WNS 1.588 ns。
- コミット a6d03fd0（注の直しだけ）から作り直した版が `C:\fw62\top.bit`（inputs.sha256 990a2d6a…）: すべて合格。sys_clk WNS 0.370 ns（Fmax 103.8 MHz）、
  cap_clk WNS 0.221 ns（Fmax 438.8 MHz）、sys → cap WNS 1.491 ns、WHS 0.011 ns（全体）。比べ: 段 1（C:\fw61）は sys 107.2 MHz・cap_clk 300 MHz で 341.5 MHz。
- 資源（`util.rpt`、作り直した版）: LUT 9317 / 53200（17.5 %）、FF 12127（11.4 %）、BRAM 64 / 140 タイル（RAMB18 4 を含む）、**ILOGIC 14（ISERDESE2 14 = JA 8 + JB 6）**、BUFGCTRL 2、MMCM 1。
  la_lane_capture: LUT 443・FF 619・BRAM 57 タイル（段 1 は 23）。la_testgen: LUT 171・FF 317。
- ［事実］BRAM は 57 タイルで、計算（128 ビット × 16384 = 64 タイル）より少ない。全レーンに同じ値を置く内部の 2 本の列を合成が 1 本にまとめたため
  （［推測］一意のビットは 14 × 8 + 2 = 114 ビット/語 ≒ 51 タイル + 端数）。§6 の 3 の上限（約 64 タイル）の中。
- JA の 8 本に ISERDESE2 を置いても DRC・配置は通った（IOBUF の O → ISERDESE2 の D、ファブリックは O を読む）。
- 端子からの経路は今までどおり制約していない。`report_datasheet` は未取得。

### 11.6 実機の確認の手順（主担当が PYNQ に書いた後）

1. 書く: `python -m fprobe_gw program C:\fw62\top.bit --board pynq_z2 --relay`（シリアル 1234-TUL・IDCODE の照合はツールが行う）。LED 0（MMCM のロック）が点くこと
   ＝ VCO 1200 MHz でロックした（初めての VCO）。
2. 回帰: `test_pynq_link_hw.py` 等で DAP（SWD・JTAG）・UART が前と同じに動くこと（JA の入力を ISERDESE2 の O から読むようになった）。
3. 分解能: `FPROBE_PYNQ_HOST=<家の LAN のアドレス> pytest --hil gateware/tests/hil/test_pynq_fast_testgen_hw.py -s`（RPi 32 番 ↔ JB の 1 番、GND 34 番 ↔ JB の 5 番）。
   print の「N 8、800 MS/s、方形波 400 MHz、細かい ch 0xfcff」、1 の長さ − 設定（段 1 は +1）、細いパルスの見え方（2.5 ns 刻み）を記録する。
4. JA の細かさ（任意）: ターゲットを外し、RPi 32 番の線を JA の予備（JA7 = U18、ch 4、基板の引き下げあり）に移して
   `FPROBE_PYNQ_TESTGEN_CH=4` で同じ試験。方形波の出力は 3.3 V で、有効にしたとき以外は Hi-Z（JA の予備は fprobe が駆動しない設定のまま）。


## 12. 段 2 の実機の確認（2026-11-01、`C:w64` = 段 2 + JTAG の IOB などの統合版、ID ac5140394148d544）

- MMCM（VCO 1200 MHz）はロックした（回路が動き、ケイパビリティが読めた）。N 8、800 MS/s、方形波 400 MHz、細かい ch 0xFCFF。
- `test_pynq_fast_testgen_hw.py` 6 件通過（直した後）。

| 方形波（1 単位 = 2.5 ns = 細かいサンプル 2 個） | 1 の長さ − 設定（細かいサンプル） |
|---|---|
| 周期 300・60・20 | +2（= 2.5 ns） |
| 周期 30 | +2 または +3（取り込みにより） |

| 細いパルス | edge 0 の長さ | edge 1 の長さ |
|---|---|---|
| 2.5 ns・5.0 ns | 見えない | 見えない |
| 7.5 ns（6 サンプル） | 4（1639 回） | 3 |
| 10.0 ns（8） | 7 | 8 |
| 12.5 ns（10） | 10 | 10・11 |
| 15.0 ns（12） | 12 | 13 |

- 1 回の取り込みの中で、変化は 1.25 ns の格子に揃う（立ち上がり 2184 回がすべて同じ位置）。
- 細いパルスで奇数の長さ（3・7・11・13）が出る = 方形波の刻み（400 MHz、偶数の長さ）より細かく取れている証拠。半周期の方形波は、アナログの差が +2 で偶数のまま（試験はパルスで確かめる形にした）。
- 見える最小の幅は約 7.5 ns（600 MS/s の構成では 6.7 ns が見えた。方形波の刻みが違うので同じ条件の比較ではない）。
- JA の入力が ISERDESE2 の O を通るようになった後の JTAG（PYNQ → Pi 2B の IDCODE、取り込みの遅れ 0〜7 × 12.5・16.67・25 MHz）: `C:w63` と同じ窓（遅れ 3〜5 で 25 MHz 10/10）。退行なし。
- 確認の後、PYNQ を永続版（`C:w63`、ID 6c8adbb7d8e19001）に戻した。
- 未確認: SWD・UART が JA の O 経由で前と同じに動くこと（対象のマイコンが PMOD に無い）。

## 13. 段 3・4（垂れ流しの N 並列の詰め・RLE）

`2026-11-01-la-deep-fast-design.md`（構成 `fprobe_pynq_deep_fast`）に分けた。§5 の段 3・段 4 をまとめて作り（RAW の詰めは N = 8 の 2 の累乗で
ギアボックスは要らない）、溢れの事実・リングと帯域の使用量・起点の時刻を足した。
