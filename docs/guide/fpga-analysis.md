# FPGA 解析の通しの手順

対象の FPGA の中の信号を、fprobe が JTAG で読み、MCU（SWD）・ピン（ロジアナ）と同じ時間軸に並べて調べる手順です。
「自分の FPGA に観測 IP を入れる」から「結果の直前に多い出来事を調べる」までを順に書きます。各手順に、AI（MCP）に頼むときの
指示文の例を付けます。道具の名前は API のメソッド名の `.` を `_` にしたものです（[mcp.md](mcp.md)）。引数の正は
[../api.md](../api.md) と `host/api/fprobe_api.json` です。

**最初に知っておくこと**

- fprobe は測定器です。返すのは測った事実（回数・時刻・割合・分布と、欠けている所）で、原因の判定や候補の順位づけはしません。
  たとえば `recording_correlate` が返す `preceded`（結果の直前に候補があった割合）と `coverage`（記録のうち候補の後の窓に当たる
  時間の割合）は、どう読むかを使う人・AI が決めます（どちらも 1 に近い候補は、いつも成り立っているだけかもしれません）。
- **FPGA の観測（`fpga_obs`）を持つ fprobe の構成は PYNQ-Z2 の `fprobe_pynq` と `fprobe_pynq_deep` だけです。**
  Tang Primer 20K の設計には無く（観測用の JTAG ピン群が Primer の設計に入っていない）、Nano 9K にもありません
  （[../architecture/feature-matrix.md](../architecture/feature-matrix.md)）。Primer は「観測される側」（対象）にはなれます。
- 動く構成は `probe_info` の capabilities の features で確かめられます。

## 全体の流れ

```
対象の FPGA ── 観測 IP（fprobe_obs）── JTAG 4 本（TCK・TMS・TDI・TDO）+ GND ── fprobe（PYNQ-Z2）
                                                                          │ SWD ── 対象の MCU
                                                                          │ ピン ── ロジアナの入力
                                                                          ▼
                                                      1 本の時間軸（タイムベース）の「記録」
```

## 1. 自分の FPGA に観測 IP を入れる

観測 IP は `target_ip/fprobe_obs/`（Apache-2.0。詳細は [../../target_ip/fprobe_obs/README.md](../../target_ip/fprobe_obs/README.md)）です。
FPGA の大きさで 2 つの型から選びます。PC のソフトは同じで、見出しの印（0xA6 / 0xA7）で型を見分けます。

| 型 | 回路 | 分かること | 大きさの目安 |
|---|---|---|---|
| 記録型 | `obs_core`（+ `obs_soft_tap`） | 値のすべての変化を対象の clk の周期の精度で。組の切り替え・溢れの数 | 組 1・FIFO 4 で SLG47910 のブロック約 124（SLG47910 には入らない。Tang Nano 9K 等なら 1 割前後） |
| 標本型 | `obs_snap_core` | fprobe のスキャンごとの値と、その間の変化の回数 | 約 21 ブロック |

### 記録型の入れ方（要約。正は fprobe_obs の README）

1. 構成に合わせて、`target_ip/fprobe_obs/rtl/` の次のファイルを足す（VHDL-2008）:
   - ベンダーの JTAG ユーザー DR を使う（Xilinx・Intel）: `obs_async_fifo.vhd`、`obs_core.vhd`。
   - ふつうのピン 4 本に TAP を作る（`obs_soft_core`。`ONE_CLOCK` が true でも false でも）: `obs_async_fifo.vhd`、`obs_core.vhd`、`obs_soft_tap.vhd`、`obs_soft_core.vhd`、**`obs_tck_sampler.vhd`**。`ONE_CLOCK = false` でも `obs_tck_sampler.vhd` を省くとビルドが止まる（GHDL で確認。Vivado・Gowin・Quartus は未確認）。
2. `obs_core` の JTAG の口を、FPGA の JTAG のユーザー DR につなぐ。ベンダー別:

| ベンダー・状況 | つなぎ方 |
|---|---|
| Xilinx | `BSCANE2`（`JTAG_CHAIN => 1` 等）の `TCK`・`TDI`・`TDO`・`SEL`・`CAPTURE`・`SHIFT`・`UPDATE`・`RESET`（→ `tlr`）を `obs_core` に |
| Intel（MAX10 等） | `sld_virtual_jtag` の同じ意味の口 |
| Gowin、Lattice など、JTAG のユーザーの命令を中から使えない FPGA。または FPGA の JTAG と別の口にしたいとき | `rtl/obs_soft_core.vhd`（`obs_soft_tap` + `obs_core`。IEEE 1149.1 の TAP、IR 4 ビット: 0x1 IDCODE・0x2 USER・0xF BYPASS）を、ふつうのピン 4 本に置く。TDO は `tdo_oe` が 1 のときだけ出す（3 ステートのピン）。`ONE_CLOCK = true`（既定）は TCK・TMS・TDI を `clk` で標本化して全部を `clk` で動かす（`rtl/obs_tck_sampler.vhd` も足す。TCK のクロックの網が要らず小さい。**TCK は `clk` の 1/8 以下**） |

Lattice 専用のプリミティブ（ユーザー JTAG 等）を使った例は、このリポジトリには無い（未確認）。`obs_soft_core` のピン 4 本の方式なら、
ベンダーを問わず VHDL-2008 が読める合成ツールで使える。

3. `probes` に見たい信号（16 ビット × `GROUPS`）、`clk` に対象の回路のクロック、`rst` に同期のリセットをつなぐ。
4. 2 クロック版（TCK のクロックの網で動かす。BSCANE2・`sld_virtual_jtag`、`ONE_CLOCK = false`）は、TCK と `clk` が非同期なので、
   タイミングの制約で別のグループにする。例（Vivado）: `set_clock_groups -asynchronous -group [get_clocks <TCK>] -group [get_clocks <clk>]`。

| generic | 既定 | 意味 |
|---|---|---|
| `GROUPS` | 1 | 16 ビットの組の数（`SELECT` で 1 組を選ぶ。256 以下） |
| `FIFO_DEPTH` | 512 | 記録をためる数（2 の累乗、4〜32768） |
| `CLOCK_HZ` | 0 | `clk` の周波数（PC に申告する。0 = 申告しない） |
| `LOST_BITS` | 16 | 溢れで失った数の幅（1〜16） |
| `ONE_CLOCK` | false | `tck` に `clk` をつなぎ、`tck_ce` で TCK の立ち上がりを渡す（`obs_soft_core` が使う） |

どの generic も PC にレジスタで申告されるので、PC のソフトは同じです。

### 標本型の入れ方

`rtl/obs_tck_sampler.vhd`・`rtl/obs_soft_tap.vhd`・`rtl/obs_snap_core.vhd` を足し、`obs_snap_core` をピン 4 本につなぐ。
`probes` は 16 ビット、`clk`・`rst` は対象の回路のもの。TCK は `clk` の 1/8 以下。標本型は命令もレジスタも無く、
fprobe のスキャンごとの値と、その間の変化の回数（255 で止まる）が分かる。標本の間の変化の中身は分からない（「切れ目」として示される）。

### 例（リポジトリにあるもの）

| 例 | 場所 | 内容 |
|---|---|---|
| Tang Primer 20K | `target_ip/fprobe_obs/examples/primer20k_counter/` | 27 MHz のカウンタ。`obs_soft_tap` + `obs_core` の 2 クロック版。J12 のピン 4 本（TCK 5 番・TMS 6 番・TDI 7 番・TDO 8 番）。`GROUPS = 2`。fprobe_gw の設計名 `obs_target_primer` |
| Shrike-Lite | `target_ip/fprobe_obs/examples/shrike_lite/` | RP2040 + ForgeFPGA（SLG47910）。標本型 `obs_snap_core`。MCU と FPGA の連携の不具合 4 種を切り替えて調べるデモ（[README](../../target_ip/fprobe_obs/examples/shrike_lite/README.md)） |

Xilinx 7 系の例: `target_ip/fprobe_obs/examples/xilinx_bscan/xilinx_obs_counter_top.vhd`（`BSCANE2` の `JTAG_CHAIN => 1` に `obs_core` を `GROUPS => 2` でつなぐ。`probes` は 32 ビットのカウンタ）。
**確認の範囲**: Vivado 2025.2 で xc7z020clg400-1 向けに合成が通ることまで（エラー無し。Slice LUT 435・Slice Registers 307・Block RAM 0.5 タイル）。実機の JTAG の鎖での動作（`fpga_obs_open` で ID が読めること）は**未確認**。実際の設計では `fpga_obs_open` の `chain`（`ir_len` は 7 系で 6、鎖に他の装置があれば `ir_before` などを足す）と、TCK と `clk` の非同期の制約（`set_clock_groups -asynchronous`）を自分で用意する。

### 信号の割り付け

`probes[15:0]` を 1 組とし、組ごとに信号のビットの割り当てを決めます（例: 組 0 の `[0]` = busy、`[3:1]` = state）。
割り付けは IP の側には要らず、手順 4 で fprobe 側に名前として定義します（回路は生のビットを運ぶだけ）。

### JTAG の鎖での位置

観測 IP が鎖の中で単独でなければ（他の装置がいれば）、次の項目を渡します（`fpga_obs_open` の `chain`）。正は [../registers.md](../registers.md) の `obs` の `chain`。

| 項目 | 意味 | 既定 |
|---|---|---|
| `ir_len` | 対象の IR の長さ（1〜32） | 4 |
| `ir_before` | 対象より TDO 側の装置の IR の合計ビット数 | 0 |
| `ir_after` | 対象より TDI 側の装置の IR の合計ビット数 | 0 |
| `dr_before` | 対象より TDO 側の装置の数 | 0 |
| `dr_after` | 対象より TDI 側の装置の数 | 0 |

ほかに `user_ir`（対象の USER の命令の値。既定 2。`obs_soft_tap` は 2）。鎖に他の装置が無い単独の場合は `chain` を省略してよい。

**鎖の位置の調べ方（事実だけ）**: API の `jtag.scan` は鎖の装置の数・IDCODE・IR 長を調べるが、走査するのは **CMSIS-DAP 側の JTAG の口**（PYNQ-Z2 では PMOD JA）で、FPGA 観測の口（Raspberry Pi ヘッダの 4 本）は走査しない（コード: `probe_lowlevel.py` の `jtag_scan` は DAP の JTAG の口を使う）。観測の口で鎖を走査する道具は今は無い。次のどちらかで調べる:
- ベンダーのツール（例: Vivado の Hardware Manager）で鎖の装置と IR 長を見る。Xilinx の 7 系の FPGA は IR が 6 ビット（上の例の `ir_len`）。他の装置の IR 長はそのデータシート／BSDL による。
- 一時的に対象の JTAG を DAP の JTAG の口につなぎ、`jtag.scan` で装置と IR 長を読む（`ir_length_source` が `table` / `measured` / `ambiguous` のどれかで返る。`ambiguous` は境目が一つに決まらず、推測しない）。
`ir_before` / `dr_before` は **TDO 側**（fprobe から見て対象の後ろ）の装置の IR の合計と数、`ir_after` / `dr_after` は **TDI 側**（対象の前）。取り違えると ID が読めず `stuck`（`id_timeout`）になる。

## 2. fprobe と対象を配線する

PYNQ-Z2 の「対象の FPGA の観測の口」は Raspberry Pi のヘッダの 4 本です（[../boards/pynq-z2.md](../boards/pynq-z2.md)）。

| 役目 | ヘッダの番号 | 向き（fprobe から見て） |
|---|---|---|
| TCK | 15（GPIO22） | 出力 |
| TMS | 16（GPIO23） | 出力 |
| TDI | 18（GPIO24） | 出力 |
| TDO | 22（GPIO25） | 入力 |
| GND | 20 | — |

- 電圧は **3.3 V のみ**。**ヘッダの 1・17 番（3V3）と 2・4 番（5V）には何もつながない。5V のピンに触れると FPGA を壊す。**
  対象の FPGA の I/O が 3.3 V でなければレベル変換が要る。
- 先に GND をつなぎ、対象の電源を入れてから信号線をつなぐ。
- 線は `fpga_obs_open` の前と `fpga_obs_close` の後は駆動しない（Z）。
- 基板の 4 本の引き上げ・引き下げの抵抗の有無は未確認（初めてつなぐ前に、電圧を測る）。
- 対象の TDO 側は、対象の FPGA の TDO ピンを fprobe の TDO（ヘッダ 22 番）へ、fprobe の TDI（ヘッダ 18 番）を対象の TDI へ（交差させない）。
  対象が Primer の `obs_target_primer` なら、Primer の J12 は番号ではなく印刷の FPGA のピン名でつなぐ（ヘッダ 15 → J14、16 → J16、18 → M14、22 ← M15、GND は 20 番 → J12 の GND。2026-10-20 に番号の取り違えがあった）。
- 時刻の照合に使うなら、対象の信号の一つ（例: カウンタの 1 ビット）をロジアナの専用の入力（PMOD JB の 1〜4・7〜8 番 = ch 10〜15）にも配線する。

## 3. 接続して観測を開く

指示文の例:

> `probe_info` で fpga_obs の機能があることを確かめてから、`fpga_obs_open` で観測を開いて。TCK は 5 MHz 前後、
> `id_scans` は 20 にして。開けたら `fpga_obs_status` を見せて。

呼ぶ道具と引数:

```
fpga_obs_open(half_period=10, words=64, sync_period=100, id_scans=20)   # chain は鎖に他の装置があるときだけ
events_start()                                                         # 値をイベントの記録に残す（これが無いと fpga_obs_read は空）
fpga_obs_status()
```

- `half_period` は TCK の半周期（sys クロックの周期数）。sys が 100 MHz なら TCK ≈ 100 MHz / (2 × half_period) で、10 なら 5 MHz。
  1 クロック版の観測 IP は TCK ≤ 対象の clk / 8 にする。既定は 10、指定できるのは 2〜65535。
- `words` は 1 スキャンの記録の語数（1〜256）。標本型は 1。`sync_period` は印（時刻合わせ）を載せる間隔（スキャンの数。0 = 載せない）。標本型は 0。
- `id_scans` は、始めに ID の応答を待つスキャンの上限（0〜255、0 = 上限なし）。**使う側が渡す引数**で、既定の暗黙の上限は無い。
- `auto_reopen=true`: 回路が答えなくなって止まったら（対象の FPGA を書き直した等）、1 秒ごとに同じ設定で開き直す。
  止まった回路の線をまた動かすので、利用者が選んだときだけ使う。

期待する結果（`fpga_obs_open` が返す申告）: `id`、`mode`（`stream` = 記録型 / `snapshot` = 標本型）、`version`、`width`（16）、`groups`、
`fifo_depth`、`clock_hz`、`sync_delay`、`sample_delay`、`lost_bits`。`fpga_obs_status` は `running`・`id_ok`・`stuck`・`overflow_seen`・
`scans` 等を返す。`id_ok` が真で `stuck` が偽なら読めている。

開けないとき: `stuck`・`id_timeout` の意味などは [troubleshooting.md](troubleshooting.md)。

## 4. 信号の名前と幅を定義する

組ごとにビットの位置（`lsb`）・幅（`width`）・名前・書式（`hex` / `dec` / `bin` / `enum`）を置きます。名前の重複・ビットの重なり・
16 ビットの外は断られます。画面の状態（別の窓・AI と共有）と、記録中・開いている記録にも反映されます（後から変えても、記録の生の値は変わらず、段の名前が変わる）。

指示文の例:

> 組 0 の bit0 を `busy`、bit3..1 を `state`（0=IDLE, 1=BUSY, 2=DONE）として定義して。

```
fpga_obs_signals_set(signals={"groups": {"0": [
    {"name": "busy",  "lsb": 0, "width": 1, "format": "bin"},
    {"name": "state", "lsb": 1, "width": 3, "format": "enum", "enum": {"0": "IDLE", "1": "BUSY", "2": "DONE"}}]}})
fpga_obs_signals_get()
```

## 5. 値を読む・数える・条件で数える

指示文の例:

> 直近 2000 件の観測値を見せて。次に、直近 5000 件で `state=BUSY` に入った回数と `busy rose` の回数を数えて。

```
fpga_obs_read(last=2000)          # 続けて読むなら since に前回の next
fpga_obs_count(conditions=["state=BUSY", "busy rose"], last=5000)
```

- `fpga_obs_read`: 各項目は `time_us`（fprobe の時刻、µs）・`value`（`null` は溢れの切れ目で `lost` = 失った変化の数）・
  `target_cycles`・`time_basis`（時刻の決め方: `pairs` = 印の組 2 つ以上の直線 / `previous_ratio` = 印 1 つと前の鎖の傾き /
  `nominal_ratio` = 印 1 つと申告の `CLOCK_HZ` の比 / `null` = 時刻なし）。
- `fpga_obs_count`: **`last` は使う側が明示する**（既定の値は無い）。条件ごとに `count` と、数えた範囲の事実
  （`values`・`t0_us`・`t1_us`・`span_s`・`gaps`・`hidden`・`lost`）を返す。「変化」は同じ組の直前の値と比べる。
  条件の書き方: `<信号> changed / rose / fell`、`<信号>=<値>`（列挙の名前も）を `and` でつなぐ。瞬間の項（`changed`・`rose`・`fell`）が 1 つ以上要る
  （`fpga_obs_trigger_set` の説明では `state=BUSY` だけの書き方が「入った瞬間」の条件の例として挙がっている。`fpga_obs_count` に渡す条件は、瞬間の項を付けた形が確かめた形）。
  読めない条件は `error` に理由が入る。
- 両側の今の値をまとめて見るのは `system_status`（MCU の番地の値と FPGA の各組の最新の値）。MCU の番地は 1 ワードずつ順に読むので、
  走っている MCU では同じ瞬間の値ではない。

## 6. 条件で MCU を止める・ロジアナを動かす

FPGA の条件が成り立ったら、ハードウェアのクロストリガ（源 `obs_match`）で MCU を止める（`halt`）、ロジアナを動かす（`la`）。
**ケイパビリティ `obs_match` が要る**（PYNQ-Z2 の 2 設計にある）。

指示文の例:

> FPGA の `state=BUSY` に入った瞬間に MCU を止めて、止まった場所と呼び出し履歴を見せて。終わったらトリガを解除して。

```
fpga_obs_trigger_set(condition="state=BUSY", halt=true)
target_status()                       # 止まったか。debug_backtrace でスタックを見る
debug_backtrace()
fpga_obs_trigger_status()             # 成り立った回数 count
fpga_obs_trigger_clear()
```

成り立った瞬間の前後のピンの波形を取るときは、`fpga_obs_trigger_set(condition=..., halt=false, la=true)` のあと、
`la_deep_capture(mask=1, value=1, fall=1, rle=false)`（波形では成り立たない条件にして、クロストリガだけで捕捉する）、
最後に `fpga_obs_trigger_clear`。内蔵のロジアナ（BSRAM）の構成（`fprobe_pynq`）では `la_capture` を使う。

遅れ（実測）: 2026-10-22 の Shrike-Lite での実測（条件の値の立ち上がりから DHCSR の停止の書き込みまで）は、
SWD 4 MHz で 101 µs、1 MHz で 206〜362 µs。構成要素は、観測のスキャン 1 回分（標本型・TCK 2.5 MHz で約 15 µs）+ SWD の書き込み
（ホストの CSW・TAR の控えがあれば 4 転送 = 約 190 SWCLK、無ければ読んで戻す転送が足される）。

## 7. MCU の変数・ピンと並べて「結果の直前に多い出来事」を調べる

記録を始め、MCU のカウンタ等の番地（`watch_set`）・ピン（`recording_la_stream_start`）・FPGA（`events_start` 済み）を 1 つの記録にまとめ、
数秒〜数十秒動かし、止めてから突き合わせます。

指示文の例:

> 記録を始めて、MCU の `g_errors` を `errors` の名前で 200 Hz で見張り、ピンも記録に入れて。30 秒後に止めて、
> `errors changed` の直前 10 ms に多い出来事を調べて。候補は `restart changed and state=BUSY` と `in0 rose`。結果のうち上位の候補の例の時刻の波形も見せて。

```
recording_start(name="run1")
watch_set(expressions=["g_errors"], names=["errors"], rate_hz=200)
recording_la_stream_start()           # ピンをロジアナの流れで記録へ
...                                   # 30 秒待つ
recording_la_stream_stop()
watch_stop()
recording_stop()
recording_correlate(effect="errors changed",
                    causes=["restart changed and state=BUSY", "in0 rose"],
                    window_s=0.010)
```

- `causes` を省くと、記録の変数から候補（信号の変化・列挙の値に入った瞬間・ピンの rose / fell・ほかのメモリの変化）を作って調べる。
  切る前の数 `causes_total`・切ったか `truncated`・使った `limit` が返る。
- 変数は MCU のメモリの名前（`watch_set` の `names`）か番地、FPGA の信号の名前、ピンの名前か `ch<N>`。同じ名前は `mcu.` / `fpga.` / `pin.` で区別する。
- 返るのは測った事実: `preceded`・`coverage`・`followed`・遅れ（秒）・例の時刻（タイムベース）。並びは `preceded` の大きい順（同じなら条件の文字の順）で、これは判定ではなく並べ方。**原因の判定はしない**。
- 結果を読む前に**欠けを確かめる**: `recording_status` の `watch_running`・`events_errors`、結果の `gaps`・`warnings`・`judged`・`excluded`・`reads`（MCU の読みの回数・間隔・いちばん長い空白）。
  `gaps` の `reason`: `reads_missing`（`gap_s` を渡したときだけ）/ `read_failed` / `pins_missing` / `fpga_lost` / `events_error` / `recording_dropped`。その間の結果・候補は数えられていない。
  `fpga_hidden` は標本型で標本の間に見えなかった変化の数。
- 期待する結果の例（Shrike-Lite のデモの実測の一部。[README](../../target_ip/fprobe_obs/examples/shrike_lite/README.md)）: 題材 S2 で `dropped changed` は preceded 1.0（2/2）・coverage 0.20 %、
  `state changed and state=BUSY` は preceded 1.0・coverage 99.97 %。どちらも preceded が 1 だが、後者はほぼいつも成り立っているので区別にならない。これを読み分けるのは使う側。
- めったに起きない不具合: 記録中に `recording_trigger_set`（条件が成り立つたびに印、`action` が stop なら `post_s` 後に記録を止める）。
  区間（リクエスト等）ごとの集計は `recording_transactions`、値が続いた時間の分布は `recording_durations`。
- 例の時刻を人の画面に見せるのは `ui_state_set`。仮説・結論は `recording_mark`。波形を人が見るのは `recording_export`（format perfetto）。

## 8. 時刻合わせの仕組みと精度

- 観測 IP は時刻を数えません。時刻のある記録（値・刻み・印）が、前の時刻のある記録からの対象の clk の周期数 `delta` を持ちます。PC が足し合わせます。
- **印**（`sync_period` ごと）: fprobe が Update-DR の瞬間の自分の時刻を番号つきで記録に書き、対象の側の記録にも同じ番号の印が入ります。
  印の組で、対象の clk の周期と fprobe の時刻の対応（傾き）を求めます。`fpga_obs_read` の `time_basis` が、その決め方（上の 5）。
- **申告の遅れ**: 観測 IP が申告するレジスタ `SYNC_DELAY`（印の瞬間が Update-DR より何周期遅いか。半周期の単位、±0.5 周期。2 クロック版 7、1 クロック版 13）と
  `SAMPLE_DELAY`（1 = 値の記録の時刻は変化より 1 周期遅い）を、PC が時刻から引く。
- 実測（2026-10-20、Primer の 27 MHz カウンタを PYNQ-Z2 の fprobe で観測。TCK 5 MHz 前後）: 同じビットの変化をロジアナと観測で比べた差は、遅れの申告で直して
  -12 ns（-17〜-6 ns、標準偏差 3 ns）。直す前は -149 ns。この数字は、その配線・その設定での実測。別の配線・TCK では確かめていない。
- 標本型の時刻は、fprobe の Capture-DR の瞬間（スキャンの間隔が分解能）。
- 印が来ないまま鎖が終わると時刻が決まらない記録になる（`recording_events` の fpga の `value_untimed` 等）。

## 9. 溢れ（gap）の意味

- 記録型で FIFO が一杯になると、値の変化を記録できません。このとき「溢れ」の記録（失った変化の数 `lost`）が入り、**鎖が切れます**。
  `fpga_obs_read` では `value = null`（切れ目）で `lost` が数。`LOST_BITS` の上限で止まるので、`lost` は「上限以上」の場合がある。
- 標本型の「切れ目」は、標本の間に見えなかった変化（`hidden`。255 で止まる）。
- 解析の結果の `gaps`（`fpga_lost` 等）は、その区間の結果・候補を数えていない印。0 回は「原因でない」ではない。
- 溢れを減らすには `FIFO_DEPTH` を大きく、TCK を上げ、`words` を大きく、または観測する信号を減らす（どれも使う側の判断）。

## 10. 制限

- **FPGA の観測（`fpga_obs`）があるのは `fprobe_pynq` と `fprobe_pynq_deep` だけ。** Primer 系・Nano 9K・`memtest` には無い。
  観測の値一致（`obs_match`）は `fpga_obs` とクロストリガの両方が要る。
- TCK: 1 クロック版の観測 IP は **TCK ≤ 対象の clk / 8**。`half_period` は 2 以上。TCK を上げすぎると TDO が化ける（Primer の J12 の
  ジャンパ線での実測: 1・10・12.5 MHz は欠け・溢れ 0、16.7 MHz からは化けた。2026-10-20。往復の遅れが半周期に収まらない。より速い TCK は未確認）。
- 標本型は変化の中身が分からない（見えない変化は `hidden` の数だけ）。
- 観測は 1 度に 16 ビットの 1 組（`SELECT` で切り替える）。
- **既知の制限: イベントの書き込み数 `EVENTS_WRITTEN`（イベントリングの `written`）は 32 ビットで回り込みます。** ホストは
  `written` が前回見た値より小さいことを「記録の始め直し」と見る（`EventCursor`、`probe_window` など）ため、回り込みと区別できません。
  回り込むまでの時間は 2^32 ÷（イベントの毎秒の件数）です（計算式による見積りで、実測ではありません）。毎秒 100 万件なら約 71 分、
  毎秒 7 千件の FPGA 観測なら約 7 日。対処は未実装です（世代カウンタを回路かプロトコルに足す設計判断が要る）。
  重い記録を長時間行うときは、1 回の記録を回り込みの前に区切ってください。
