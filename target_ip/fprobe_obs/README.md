# fprobe_obs — 対象の FPGA の中を JTAG で観測する回路

対象の FPGA に入れる小さな回路。fprobe（大きい FPGA の構成。例: PYNQ-Z2）が対象の JTAG から読み出し、
MCU のイベントやロジアナと同じ時刻で並べる。ライセンスは **Apache-2.0**（[LICENSE](LICENSE)）。
fprobe 本体（gateware/ は GPL-3.0）のファイルは使っていないので、製品の FPGA にそのまま入れられる。

形式は v2（[仕様](../../docs/superpowers/specs/2026-10-20-fpga-observation-v2-design.md)）。FPGA の大きさで 2 つの型から選ぶ。
fprobe と PC のソフトは同じで、見出しの印（0xA6 / 0xA7）で型を見分ける。

| 型 | 回路 | 分かること | 大きさの目安（SLG47910 のブロック、1 ブロック ≒ LUT 4 個） |
|---|---|---|---|
| 記録型 | `obs_core`（+ `obs_soft_tap`） | 値のすべての変化を対象の clk の周期の精度で。組の切り替え・溢れの数 | 組 1・FIFO 4 で約 124（SLG47910 には入らない。Tang Nano 9K 等なら 1 割前後） |
| 標本型 | `obs_snap_core` | fprobe のスキャンごとの値と、その間の変化の回数 | 約 21 |

## 入れ方（記録型）

1. `rtl/obs_async_fifo.vhd` と `rtl/obs_core.vhd` を足す（VHDL-2008）。
2. `obs_core` の JTAG の口を、FPGA の JTAG のユーザーの DR につなぐ。
   - Xilinx: `BSCANE2`（`JTAG_CHAIN => 1` 等）の `TCK`・`TDI`・`TDO`・`SEL`・`CAPTURE`・`SHIFT`・`UPDATE`・`RESET`（→ `tlr`）。
   - Intel（MAX10 等）: `sld_virtual_jtag` の同じ意味の口。
   - JTAG のユーザーの命令を中から使えない FPGA（Gowin 等）や、FPGA の JTAG と別の口にしたいとき: `rtl/obs_soft_core.vhd`
     （`obs_soft_tap` + `obs_core`。IEEE 1149.1 の TAP、IR 4 ビット: 0x1 IDCODE・0x2 USER・0xF BYPASS）をふつうのピン 4 本に
     置く。TDO は `tdo_oe` が 1 のときだけ出す（3 ステートのピン）。`ONE_CLOCK = true`（既定）は TCK・TMS・TDI を `clk`
     で標本化して全部を `clk` で動かす（`rtl/obs_tck_sampler.vhd` も足す。TCK のクロックの網が要らず小さい。TCK は `clk` の
     1/8 以下）。
3. `probes` に見たい信号（16 ビット × GROUPS）、`clk` に対象の回路のクロック、`rst` に同期のリセット。
4. 2 クロック版（TCK のクロックの網で動かす。BSCANE2・`sld_virtual_jtag`、`ONE_CLOCK = false`）は、TCK と `clk` が
   非同期なので、タイミングの制約でその 2 つを別のグループにする（受け渡しは `obs_async_fifo` と同期化の段がする）。
   例（Vivado）: `set_clock_groups -asynchronous -group [get_clocks <TCK>] -group [get_clocks <clk>]`。
   Gowin・Quartus も同じ意味の制約（非同期のクロックの組）を書く。1 クロック版（`ONE_CLOCK = true`）は TCK を
   クロックにしないので要らない。

| generic | 既定 | 意味 |
|---|---|---|
| `GROUPS` | 1 | 16 ビットの組の数（SELECT で 1 組を選ぶ。256 以下） |
| `FIFO_DEPTH` | 512 | 記録をためる数（2 の累乗、4〜32768） |
| `CLOCK_HZ` | 0 | `clk` の周波数（PC に申告する。0 = 申告しない） |
| `LOST_BITS` | 16 | 溢れで失った数の幅（1〜16。上限で止まる。「上限以上」になるだけで値の記録は変わらない） |
| `ONE_CLOCK` | false | `tck` に `clk` をつなぎ、`tck_ce` で TCK の立ち上がりを渡す（`obs_soft_core` が使う） |

どの generic も PC にレジスタで申告するので、同じ PC のソフトがそのまま動く。

## 入れ方（標本型）

`rtl/obs_tck_sampler.vhd`・`rtl/obs_soft_tap.vhd`・`rtl/obs_snap_core.vhd` を足し、`obs_snap_core` をピン 4 本につなぐ。
`probes` は 16 ビット、`clk`・`rst` は対象の回路のもの。TCK は `clk` の 1/8 以下。

## DR の形式（記録型、1 回のスキャン）

TDI の**最後の** 16 ビットが命令。Update-DR で実行する（16 ビット未満のスキャンは何もしない）。鎖の中で TDI 側に他の
装置があるときは、その BYPASS の数だけ前にずらして送る。

| ビット | 意味 |
|---|---|
| 3:0 | op: 0 = 何もしない、1 = レジスタの書き、2 = レジスタの読み（応答の記録が来る）、3 = 印（時刻合わせ。データ = 番号） |
| 7:4 | レジスタの番地 |
| 15:8 | データ |

TDO（32 ビットの語、下位ビットから）: 見出し 1 語 = `count`(15:0。この後の有効な記録の数) | bit16 開始してから溢れた |
bit17 clk 側が記録中 | 31:24 = 0xA6。続けて記録 `count` 語、その後は 0。記録は最後のビットまで送り出したときに取り除く。

## 記録（32 ビット、31:30 が種類）

時刻は数えない。時刻のある記録（値・刻み・印）が、前の時刻のある記録からの `clk` の周期数 `delta` を 29:16 に持つ。
PC が足し合わせ、印（fprobe が Update-DR の瞬間の自分の時刻を番号つきで記録する）で fprobe の時刻に結ぶ。

| 種類 | 形 | 意味 |
|---|---|---|
| 0 値 | `delta`(29:16) `value`(15:0) | 選んだ組が変わった。`delta` 0 = 鎖の始め（開始・溢れの後） |
| 1 刻み | `delta`(29:16) 0(15:0) | 変化なしで `delta` が 16383 に達した |
| 1 印 | `delta`(29:16) 1(15) 番号(14:7) `late`(6:0) | 印の命令を受けた。瞬間 = この記録の時刻 − `late` |
| 2 溢れ | `lost`(15:0) | FIFO が一杯で記録できなかった値の変化の数。鎖が切れる |
| 3 制御 | 種類(29:26) | 0 = 組の番号（7:0）、1 = 応答 番地(19:16) 値(15:0) |

開始・溢れの後・組の切り替えでは「組 →（溢れ）→ 値」（PC 側はどちらの順でも読む）。印の後は、変化の無い周期に組と今の値を書き直す（記録の途中の
印から読み始めても組・時刻・値が分かる）。印・応答・書き直しは変化の無い周期にだけ書くので、値の時刻は常に正確。

## レジスタ（記録型、16 ビット）

| 番地 | 名前 | 内容 |
|---|---|---|
| 0・1 | ID | 下位 0x4253・上位 0x464F（"FOBS"） |
| 2 | VERSION | 2 |
| 3 | GROUPS | generic |
| 4 | FIFO_DEPTH | generic |
| 5・6 | CLOCK_HZ | generic の下位・上位 |
| 7 | SYNC_DELAY | 印の瞬間が Update-DR より何周期遅いか（半周期の単位、平均、±0.5 周期）。2 クロック版 7、1 クロック版 13 |
| 8 | SAMPLE_DELAY | 1: 値の記録の時刻は変化より 1 周期遅い（probes が同じ `clk` の登録の出力のとき） |
| 9 | LOST_BITS | generic |
| 14 | CONTROL | bit0 = 開始（0 → 1 で始め直す。溢れの印も消える） |
| 15 | SELECT | 観測する組（GROUPS 以上は無視）。記録中は、変化の無い周期（か溢れの立て直し）に組の記録を書くまで前の組を見続ける |

TAP の Test-Logic-Reset で CONTROL と SELECT は 0 に戻る（たまった記録は残る）。

## 標本型の DR

USER の DR は 32 ビット: 0xA7(31:24) | 前の Capture-DR からの値の変化の回数(23:16。255 で止める) | Capture-DR の
周期の `probes`(15:0)。命令もレジスタも無い。fprobe は Capture-DR の瞬間の自分の時刻で値にし、回数から標本の間の
見えない変化を「切れ目（失った数）」として示す。
