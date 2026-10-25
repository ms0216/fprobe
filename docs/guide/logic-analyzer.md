# ロジックアナライザ

FPGA のピンから出ている信号を読み、波形・トリガ・通信の解読に使う手順です。PYNQ-Z2 の fprobe（Ethernet、PS の中継経由）を前提に書きます。
書いてあるのは、コード・API の仕様・ボード定義・実機の記録で確かめたことだけです。確かめていないことは「未確認」と書きます。
MCP の登録は [MCP](mcp.md)、書き込みと接続は [はじめに](getting-started.md)、観測 IP と並べる通しの手順は [FPGA 解析](fpga-analysis.md) を見てください。

## 1. できること

| 項目 | 値 | 出典 |
|---|---|---|
| チャンネル数 | 16 ビット幅。うち外の信号が入るのは 14 本（後述） | ボード定義 `pynq_z2.py`・ケイパビリティ `la_layout` |
| サンプル速度 | 最大 100 MS/s（PYNQ-Z2。`la_sample_hz` = `sys_clock_hz` = 100 MHz）。分周は 1〜65536（`rate_hz` で指定。実際の周期は結果の `sample_period_ns` に返る） | feature-matrix・`la.py` |
| 深さ（内蔵、構成 `fprobe_pynq`） | 8192 サンプル（BSRAM）。`la_capture` で使う | feature-matrix |
| 深さ（深いバッファ、構成 `fprobe_pynq_deep`） | PS の DDR の 32 MB。RAW は 1 サンプル 2 バイトで、上限は 32 MB ÷ 2 バイト = 約 1670 万サンプル（100 MS/s で約 0.17 秒）。実際に使えるリングは `la_deep_arm` が返す `capacity_beats`（1 拍 = 16 バイト = RAW 8 サンプル）。イベント記録と同じ領域を分け合うので、これより小さい | feature-matrix・pynq-z2.md・`la.py` |
| トリガ | 値の一致（`mask`・`value`）、立ち上がり（`rise`）、立ち下がり（`fall`）、グリッチフィルタ、パルス幅、クロストリガ（`external_only`） | `la_arm` |
| 圧縮 | RLE（変化点圧縮）。深いバッファで使える | `la_deep_arm` |
| 解読 | uart / spi / i2c / swd / can / onewire / jtag / i2s / parallel / manchester と、その上の jtag_arm / spiflash / eeprom24 / modbus | `la_decode` |

構成は 2 つあり、どちらを書き込んでいるかで使う道具が変わります（`probe_info` で確かめる）。
- `fprobe_pynq`: 内蔵 8192 サンプル。`la_arm` / `la_collect` / `la_capture`。
- `fprobe_pynq_deep`: 深いバッファ。`la_deep_arm` / `la_deep_collect` / `la_deep_capture` / `la_stream`。内蔵の BSRAM は無い（`la_arm` / `la_capture` も使えるが、深いバッファの RAW で 8192 サンプルの窓を返す）。

## 2. 配線（PYNQ-Z2）

**入力は 3.3 V のみです。5 V を入れてはいけません。** PMOD の端子は FPGA に直結で、レベル変換も保護もありません。
1.8 V など別の電圧の対象は、事前にレベル変換（1.8 V → 3.3 V など）を通してからつなぎます。3.3 V 未満の信号は High と読めない場合があります（しきい値は未確認）。
対象と PYNQ の **GND を必ずつなぎます**（PMOD の規格では 5 番と 11 番が GND。JB も同じ並びと見込むが、基板で確かめたかは未確認）。3V3 の穴（JA の 6・12 番）には何もつながないでください。
つなぐ前に、対象の信号の電圧をテスターで確かめます。電源を入れる順序は、信号線は最後にします（先に信号だけが入ると、電源の無い側の端子へ電圧がかかります）。

| ch | 穴 | FPGA | 役目（既定の名前） | 基板の抵抗 |
|---|---|---|---|---|
| 0 | JA1 | Y18 | SWCLK のピン（`ch0`） | 引き下げ |
| 1 | JA2 | Y19 | `ch1` | 引き下げ |
| 2 | JA3 | Y16 | `ch2` | 引き上げ |
| 3 | JA4 | Y17 | SWDIO のピン（`ch3`） | 引き上げ |
| 4 | JA7 | U18 | `ch4` | 引き下げ |
| 5 | JA8 | U19 | `ch5` | 引き下げ |
| 6 | JA9 | W18 | `ch6` | 引き上げ |
| 7 | JA10 | W19 | `ch7` | 引き上げ |
| 8 | （内部） | — | `probe_drives_swdio`（fprobe が SWDIO を駆動している間 1） | — |
| 9 | （内部） | — | `dap_busy`（DAP が処理中の間 1） | — |
| 10〜15 | JB の 1・2・3・4・7・8 番 | W14 Y14 T11 T10 V16 W16 | 専用の入力 `in0`〜`in5` | JA と同じ並び（0〜5 番に対応: 下・下・上・上・下・下） |

- **ch8・ch9 は FPGA の端子ではなく、fprobe の内部の信号です**（ケイパビリティ `la_layout` の「内部」）。外の信号を読めるのは ch0〜7（JA）と ch10〜15（JB）の 14 本です。
- JA のうち SWCLK・SWDIO などはデバッグ用の穴と共用です。ロジアナとして読むだけなら、どの穴に何をつないでもかまいません（デバッガの機能と同時に使うときは役目が重なる）。
- 基板の引き上げ・引き下げの抵抗があるので、つなぐ信号が浮くと、その向きの値に読まれます（出典: docs/boards/pynq-z2.md）。
- JB の抵抗の向きを「JA と同じ並び」としたのは `pynq_z2.py` の `la_in` の定義による。基板の資料で JB 自体を確かめたかは未確認。
- ch の並びは機器が申告します。`probe_info` の `capabilities.la_layout` で、ch ごとの種類（ピン・内部・専用の入力・無し）を確かめます。

## 3. 取り込みの手順

道具の引数は [API 一覧](../api.md) が正です。ここでは使う順に書きます。トリガの条件は ch をビットで指定します（ch0 = 1、ch3 = 8、ch10 = 0x400）。

### 3.1 内蔵（8192 サンプル）: `la_capture`

> ロジアナで ch0 の立ち上がりをトリガに、100 MS/s で取って。トリガの前に 1024 サンプル残して。VCD は `C:` ではなく作業用のフォルダの `cap.vcd` に書いて。

```
la_capture(rise=1, pretrigger=1024, rate_hz=100000000, vcd_path="cap.vcd")
```

- 仕掛けてすぐ戻る版は `la_arm`、完了を待って読む版は `la_collect`。ターゲットの操作と同時に使うときは `la_arm` → 操作 → `la_collect`。
- `pretrigger` はトリガの前に残すサンプル数（既定 1024。0〜深さ − 1）。トリガの後は「深さ − pretrigger」サンプル。
- トリガの条件: 値の一致は `mask` と `value`（`(サンプル & mask) == (value & mask)`）。`rise` / `fall` は ch のビットで、そのどれかがその向きに変化したとき。値の一致と変化の両方を指定したら、両方が成り立ったときにトリガ。何も指定しなければすぐ捕捉。
- トリガが成立しないと `timeout_s`（既定 5 秒）で TimeoutError。
- 結果: `trigger_index`（トリガのサンプル番号）、`trigger_time`（トリガ時のタイムベース。ピンの変化の時刻の下限で、実際は `trigger_time_span` 刻みの幅のどこか）、`sample_period_ns`、`samples`、チャンネルごとの `channels`（`initial`・`edges`・`first_edges_ns`＝トリガを 0 ns とした先頭 16 個の変化の時刻・`first_edges_truncated`）、常に 0 で省いた ch の `constant_low`。

パターンの例:

| 条件 | 指定 |
|---|---|
| ch0 の立ち上がり | `rise=1` |
| ch3 の立ち下がり | `fall=8` |
| ch0 が High で ch1 が Low | `mask=3, value=1` |
| ch2 が Low の間に ch0 が立ち上がる | `mask=4, value=0, rise=1` |

グリッチフィルタとパルス幅（ケイパビリティ `la_filter`・`la_pulse` がある構成。feature-matrix では `fprobe_pynq`・`fprobe_pynq_deep` とも有）:

> ch0 の 50 ns より短い変化を捨てて、ch0 が 1 µs 以上 High の脈でトリガして。

```
la_capture(filter_channels=1, filter_ns=50, pulse={"channel": 0, "level": 1, "min_ns": 1000})
```

フィルタした ch は結果の `filter.delay_ns` だけ遅れて記録されます。フィルタをかけられるのは外の信号の ch（ch0〜7・10〜15）だけです。

### 3.2 深いバッファ: `la_deep_capture`

> 深いバッファで、ch0 の立ち下がりを RLE でトリガにして取って。前 4096 拍、後ろ 4096 拍。

```
la_deep_capture(fall=1, rle=True, before_beats=4096, after_beats=4096, vcd_path="deep.vcd")
```

- 仕掛けは `la_deep_arm`（`after_beats` = トリガの後に取る拍数）、読み出しは `la_deep_collect`（`before_beats` = トリガの前から読む拍数、`timeout_s` 既定 10 秒）。
- 1 拍 = 16 バイト。RAW は 1 拍 = 8 サンプル、RLE は 1 拍 = 4 変化点（レコード）。
- 前に残る拍数は、トリガまでに溜まった分より多くは読めません（少ないほうに切り詰める）。
- 結果は変化点の表で返り、長い記録も展開しません。`duration_s`（記録の長さ）、`samples`、`trigger_index`、`trigger_time`、`channels`（チャンネルごと）を返します。
- 深いバッファの前提: PS 側で DDR の上端を予約済みで、中継が準備を終えていること（6. 制限）。準備ができていないと、仕掛けの時点で「外部メモリの準備ができていない」の誤りになります。

### 3.3 RLE の使いどころ

RLE は、値が続く間を 1 つの記録（値 + 繰り返し数）にします。
- 変化が少ない信号（UART、I2C、遅いクロックやステータス）ほど、長い時間を取れます。
- 変化が多い信号（高速なクロックなど）では、変化のたびに記録が 1 つ増えるので、RAW より長くは取れません。トリガ後の拍が埋まるまでに時間がかかることもあります（`la_deep_collect` の時間切れは「トリガは成立したが、トリガ後の拍がまだ埋まらない」と別に報告される。`after_beats` を減らす・RAW にする・`timeout_s` を延ばす）。
- 1 つの記録で数えられる繰り返しは 65536 サンプルまで（それより長く続くときは同じ値の記録が続く。ホストは自動でつなぐ）。
- 速い信号を RAW で取るなら、100 MS/s で上限は 6. 制限の値。

### 3.4 止めずに流す: `la_stream`

> ロジアナを止めずに 10 秒流して、VCD に書いて。

```
la_stream(vcd_path="stream.vcd", duration_s=10, rate_hz=1000000, rle=True)
```

- トリガは使いません。記録の長さは保存先の空きで決まります。
- `overrun` が true なら、ホストが追いつかず途中で止まったことを表します（それまでの分は正しい）。
- PYNQ は PS の中継を通るので、ストリームが約 1〜2 MB/s を超えると制御の応答が遅れます（docs/boards/pynq-z2.md の実測）。RAW は 1 サンプル 2 バイトなので、1〜2 MB/s は約 0.5〜1 MS/s に当たります（この換算は算出）。それより速い信号は、変化の少ない信号を RLE で流すか、トリガ付きの `la_deep_capture` を使います。
- 記録（`recording_start`）と同時に流す版は `recording_la_stream_start` / `recording_la_stream_stop`（5 の流れで使う）。

### 3.5 トリガの状態の確認: `la_trigger_status`

仕掛けた条件（mask・value・rise・fall・rate_hz・pretrigger・フィルタ・パルス幅）と、捕捉の状態（capturing・triggered・done・trigger_time）を機器から読み戻します。トリガが成立しないときの切り分けに使います。トリガの回数を数えるレジスタは無い。

## 4. 結果の見方

### 4.1 PulseView で開く

`vcd_path` を渡すと VCD が書けます。PulseView 等で開きます（時刻 0 = 最初のサンプル、単位は ns。`trigger` という名前の信号がトリガのサンプルで 1）。
最後の捕捉は `la_export`（format は sr か vcd）でも書けます。sr は sigrok のセッションで、PulseView・sigrok-cli で開けます。ch の名前は `names` で付けられます（例 `{"5": "rx"}`）。
ファイルから読み戻すのは `la_load`。

> 最後の捕捉を `la_export` で sr に書いて。ch0 を clk、ch1 を data と名づけて。

### 4.2 画面の幅に縮める: `la_view`

`la_view` は最後の捕捉を width 画素（1〜8192、既定 1000）に縮めます。結果は ch ごとの文字列で、画素ごとに `0`（区間がずっと 0）・`1`（ずっと 1）・`x`（区間の中で変化がある）です。
縮めても変化は消えません。短い脈は、その画素が `x` になります（拡大すると中が見える）。`start`・`end` はサンプル番号で、範囲を指定して拡大します。

> 最後の捕捉の 1000 サンプル目から 2000 サンプル目までを 100 画素で見せて。

```
la_view(start=1000, end=2000, width=100)
```

### 4.3 時間・周期を測る: `la_measure`

範囲を指定して、ch ごとの立ち上がり・立ち下がりの数、High の割合、周波数、デューティ、周期・High・Low の幅の統計（最小・最大・平均・標準偏差と、その位置のサンプル番号）を返します。2 つの ch の間の遅れや、セットアップ・ホールドの違反の一覧も出せます（`relations`）。分解能は 1 サンプルです。

### 4.4 通信の解読: `la_decode`

`la_decoders` が使える解読器の一覧を返します。`la_decode` は最後の捕捉の通信を解読します。**通信の設定は既定を持たず、必須です**（線の割り当て・ボーレート等。つながない線は null を明示）。

> ch0 を SWCLK、ch3 を SWDIO として、最後の捕捉を SWD で解読して。

```
la_decode(protocol="swd", settings={"swclk": 0, "swdio": 3, "drive": null, "turnaround": 1, "orundetect": false})
```

- uart: `channel`・`baud`（波形から推定しない）・`data_bits`・`parity`・`stop_bits`・`invert`・`lsb_first`。
- spi: `sck`・`mosi`・`miso`・`cs`・`mode`・`msb_first`・`word_bits`・`cs_active_low`。
- i2c: `scl`・`sda`。
- swd の `drive` は、fprobe が SWDIO を駆動している間 1 になる信号の ch（内部の ch8 = `probe_drives_swdio`）。つながなければ null を明示。他の解読器の設定は `la_decoders` の `params` で確かめます（設定の名前が足りないと、要る名前を挙げた誤りが返る）。
- `start`・`end`・`kinds`・`errors_only`・`offset`・`limit` で絞ります。記録中なら注釈は共通の時間軸にも入ります。

## 5. 観測 IP と同じ時間軸で見る

ロジアナのサンプルと、FPGA の観測 IP（`fpga_obs`）の値・MCU の変数・DAP のイベントは、同じタイムベース（PYNQ-Z2 は 100 MHz）の時刻で記録に入ります。
`la_deep_collect` / `la_collect` の捕捉は、記録中（`recording_start` の後）なら、トリガのサンプルがトリガ時刻に来る位置で自動的に記録へ入ります。
止めずに流すなら `recording_la_stream_start` で記録に入れます。

> 記録を始めて、ピンをロジアナで流して 30 秒後に止めて。その間の FPGA の観測と MCU の変数と並べて調べて。

```
recording_start(name="run1")
recording_la_stream_start()           # ピンをロジアナの流れで記録へ
...                                   # 対象を動かす
recording_la_stream_stop()
recording_stop()
```

- 流している間は `la_capture` などのロジアナの道具は使えません（読み出しの経路が同じ）。
- 突き合わせる道具（`recording_correlate` ほか）と、観測 IP（`fpga_obs`）の使い方は [FPGA 解析](fpga-analysis.md) の 7 を見てください。
- 時刻の精度: ロジアナの入り口の遅れは、機器が申告する段数で補正されます（`trigger_time` はピンの変化の時刻の下限）。観測との差の実測は docs/boards/pynq-z2.md の O2（直した後で -12 ns、-17〜-6 ns）。

## 6. 制限

- サンプル速度は最大 100 MS/s（PYNQ-Z2）。それより速い信号は取れません。入力として読める最大の周波数（FPGA の入力の帯域・基板の配線・サンプルの取り込みの設定時間）は **未確認**です。100 MS/s のサンプルで読めるのは、概ねその半分（Nyquist）より遅い信号という一般則に留まります。
- 内蔵の深さは 8192 サンプル（100 MS/s で 81.92 µs）。
- 深いバッファは PS の DDR の 32 MB。**使う前に DDR の上端を予約する**必要があります（`python -m fprobe_gw pynq reserve` の後に再起動。docs/boards/pynq-z2.md）。予約が無い、または中継が準備を終えていないと、深いバッファの捕捉は始まりません。2026-10-20 には、この再起動がシャットダウンの途中で止まり、電源の入れ直しで起動した記録があります。
- 深いバッファのストリームが約 1〜2 MB/s を超えると、制御の応答が遅れます（上記）。
- RLE の 1 レコードの繰り返しは 65536 サンプルまで。
- 深いバッファの `before_beats` / `after_beats` の上限は、リングの拍数（`la_deep_arm` の `capacity_beats`）で決まります。`after_beats` が `capacity_beats` 以上だと ValueError。
- `rate_hz` は 100 MHz ÷ 1〜65536 の範囲に丸められます（最低約 1.5 kHz）。結果の `sample_period_ns` が実際の周期です。
- 入力は 3.3 V のみ（2 の電圧の注意）。ch8・ch9 は内部の信号で、外の信号は読めません。
- 使い方の不具合の台帳は docs/reports/bugs/la-host.md にあります（検出のみで、直していない項目がある。深いバッファの準備に失敗した直後にイベント記録の取り込みが止まったままになる、`pretrigger` の既定値が小さいバッファでは大きすぎる、など）。
