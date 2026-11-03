# TUL PYNQ-Z2（Zynq-7020）

fprobe の移植先（仕様 docs/superpowers/specs/2026-10-19-pynq-z2-port-design.md、計画 2026-10-19-pynq-z2-port.md・
2026-10-20-pynq-p3-deep-buffer.md）。ツール側の記述は `gateware/fprobe_gw/boards/pynq_z2.py` が正。

## 全体

```
PC ──USB の LAN（192.168.77.1）── PYNQ の eth0:2（192.168.77.2）
                                    PS（ARM、PynqLinux 3.0）: fprobe-relay（UDP 63920 ⇄ /dev/mem 0x40000000）
                                    │ M_AXI_GP0（AXI3 → AXI4-Lite の変換、ACLK = sys 100 MHz）
                                    PL: zynq_frame_link ⇄ frame_mux ⇄ fprobe_core ⇄ PMOD JA（デバッグの線）・JB（専用の入力）
                                    （fprobe_pynq_deep: S_AXI_HP0 ⇄ axi_mem_port ⇄ 深いバッファ。DDR の上端 32 MB）
```

## デバッグの線（PMOD JA）

基板に PMOD の抵抗がある（PYNQ の pmod の資料。2026-10-20 に実機で確認）: PMOD の 0・1・4・5 番（JA1・JA2・JA7・JA8）
= 引き下げ、2・3・6・7 番（JA3・JA4・JA9・JA10）= 引き上げ。FPGA の端子のプルもこれに合わせてある。
**キーの行のように「High で働く・浮くと拾う」線は、引き下げの穴（JA7・JA8）につなぐ。** JA10（引き上げ）に XIAO の行を
つないだら、走査の間に High に保たれてキーが押され続けた。

| 役目 | 穴（JA） | FPGA | ロジアナの ch | 基板の抵抗 | 今の配線（XIAO nRF52840） |
|---|---|---|---|---|---|
| SWCLK | JA1（左の列の 1 番目） | Y18 | 0 | 引き下げ | 裏のパッド SWCLK |
| TDI | JA2（左 2） | Y19 | 1 | 引き下げ | 2 列目のスイッチの列の側（読むだけ） |
| nRESET | JA3（左 3） | Y16 | 2 | 引き上げ | 裏のパッド RST |
| SWDIO | JA4（左 4） | Y17 | 3 | 引き上げ | 裏のパッド SWDIO |
| GND | JA5（左 5） | — | — | — | GND |
| 予備 | JA7（右 1） | U18 | 4 | 引き下げ | D5（U の行、1 kΩ） |
| UART RX | JA8（右 2） | U19 | 5 | 引き下げ | D6（J の行、1 kΩ） |
| SWO | JA9（右 3） | W18 | 6 | 引き上げ | — |
| UART TX | JA10（右 4） | W19 | 7 | 引き上げ | — |

3V3（JA6・JA12）には何もつながない。PMOD JB の 1〜4・7〜8 番はロジアナの専用の入力（ch 10〜15、プルは JA と同じ並び）。

**JA の出力は端子の直前の 1 段（出力は OLOGIC の ODDR、Hi-Z の制御は IOB の FF）から出る**（2026-11-01 に IOB の FF、2026-11-03 に出力を ODDR `xilinx_oddr` に。`pynq_z2_fprobe_top.vhd` の `pad_q`・`pad_t`。stop-precision §3.1、dap-line-rate §3）。ODDR の前半・後半が違うのは JTAG の half_period 1（TCK 50 MHz）の TMS だけで、TMS は TCK の立ち下がりの 5 ns 後に変わる。
TCK・TMS・TDI（SWCLK・SWDIO）の端子までの遅れが配置で変わらない。その代わり、JA のどの出力（SWD / JTAG・nRESET・UART TX・
GPIO・クロストリガの出力）も、回路の中で決めた周期の 1 周期（sys、10 ns）後に端子に出る（入力は変わらない）。SWD / JTAG の
往復（出力 → 対象 → 入力の取り込み）はこの 1 周期だけ長く、速い TCK では取り込みの遅れ（`sample_delay`）の合う値が 1 つ大きく
なりうる。Primer（Gowin）の構成はこの段を持たない。

**JA の出力の駆動は 16 mA・SLEW は FAST**（2026-11-02。前は 4 mA、次に 12 mA・SLOW）。ジャンパ線で Raspberry Pi 2B の JTAG を
動かすと、端が遅いほど転送が化ける。化けた転送は TDO が全部 0（ACK 000）で、ときに **DP の CTRL/STAT が 0 に書かれる**（プローブが
書いていない値。対象が別の書き込みを受けた）。デバッグの電源の要求が消えると、その後の AP の転送はつなぎ直すまで全部失敗する。
化けた書き込みがコアに届くと Linux が落ちることがある（Pi が自分で再起動した）。

実測（`fprobe_pynq_deep` を HEAD から駆動だけ変えて作った版、Pi の ARM 600 MHz、TRST はプルアップ。4 コアの DBGDSCR を
1 回の転送で読むのを 1 万回、失敗したらつなぎ直す。「0 書き」= 失敗の後の CTRL/STAT の電源の印が 0）:

| JA の駆動 | 4 MHz の失敗 | 1 MHz の失敗 | 4 MHz の 0 書き |
|---|---|---|---|
| 4 mA SLOW | つなぎ直しができない | — | — |
| 8 mA SLOW | 2627〜2734 | 2698 | 300〜335 |
| 12 mA SLOW | 33〜52 | 21〜26 | 5〜9 |
| 16 mA SLOW | 19〜29 | 17 | 6〜7 |
| 16 mA FAST | 3〜5 | 2 | 1 |

強いほど単調によくなる（線の反射なら強いほど悪くなる）。同じ線に他の機器の出力が無いことは確かめた（PYNQ が手放した線を
Primer は動かせなかった）。Pi の側の 6 本は ALT4（JTAG）で、出力ではない。16 mA・FAST でも 0 ではない（1 万回に数回）。
この前の 12 mA の表（「失敗 0」）は、回数が少なく 4 mA の回の TRST が浮いていたので、比べとしては弱かった。

16 mA・FAST の基準（2026-11-02、配線を変える前）: 線は**デュポンの線 2 本を継いだ 60 cm**（継ぎ目あり）、GND 2 本。Pi は 600 MHz・
起動から 1.5〜2.7 時間・CPU の温度 30.4〜31.5 ℃。5 分ごとに 4 MHz で 1 万回を 18 回: 失敗 1〜7（平均 約 3.6）、CTRL/STAT が 0 に
書かれたのは 18 万回で 3。時間・温度と共に増える傾向は無かった（その前に「起動からの時間で増える」と見えたのは、コアを止めては
再開する試験の最中だった）。周波数ごと（2 万回）: 250 kHz 0、500 kHz 4、1 MHz 6、4 MHz 14。線の長さ・継ぎ目・GND の取り方が
化けの有力な候補（推測、未測定）。線を変えたら同じ測り方で比べる。

**線を短くした後（2026-11-02 夜）**: 継ぎ目の無い短いデュポンの線 1 本ずつに差し替え（つなぐ穴は同じ。GND は Pi の 20 番（TCK の隣）と
14 番 → JA の GND の 2 本のまま）。同じ測り方で、4 MHz 1 万回 × 3 回: 失敗 0・0・0。2 万回: 1 MHz 0、4 MHz 0、10 MHz 0。CTRL/STAT が 0 に
書かれたのも 0。**化けの原因は線（長さと継ぎ目）だった**（16 mA・FAST のまま、PL・Pi の設定は同じ）。

## 対象の FPGA の観測の口（Raspberry Pi のヘッダ）

対象の FPGA の中を JTAG で観測する口（docs/superpowers/specs/2026-10-20-fpga-observation-design.md、CSR `obs`）。
基板の Raspberry Pi のヘッダの GPIO22〜25 を使う。ヘッダの番号は Raspberry Pi と同じ（1 番 = 3V3、2 番 = 5V、
奇数が内側の列）。FPGA のピンの出典は Xilinx/PYNQ v2.7.0 の Pynq-Z2 の base.xdc（rpi_gpio_tri_io[14〜17]）。

| 役目 | ヘッダの番号 | Raspberry Pi の名前 | FPGA | FPGA のプル |
|---|---|---|---|---|
| TCK（出力） | 15 | GPIO22 | U8 | 引き下げ |
| TMS（出力） | 16 | GPIO23 | W6 | 引き上げ |
| TDI（出力） | 18 | GPIO24 | Y7 | 引き上げ |
| TDO（入力） | 22 | GPIO25 | F20 | 引き上げ |
| GND | 20 | — | — | — |

- **1・17 番（3V3）と 2・4 番（5V）には何もつながない。** 5V のピンに触れると FPGA を壊す。
- 線は観測を始めるまで（`fpga_obs.open` の前）と、やめた後（`fpga_obs.close`）は駆動しない（Z）。
- 3.3 V のみ。基板にこの 4 本の引き上げ・引き下げの抵抗があるかは未確認（初めてつなぐときに、つなぐ前の電圧を測る）。
- 出典の確かめ: SoCDP8 の pynq-z2_constraints.xdc も U8 = GPIO22 = 15 番、W6 = GPIO23 = 16 番、Y7 = GPIO24 = 18 番、
  F20 = GPIO25 = 22 番（2026-10-20 に実機で 15 番から TCK が出ることも確かめた）。

### 実機の確かめ（2026-10-20、O1・O2。gateware/tests/hil/test_fpga_obs_hw.py）

相手は Primer（構成 obs_target_primer）。**Primer の J12 は番号でなく印刷の FPGA のピン名でつなぐ**（番号だけで伝えて
TCK・TMS が別の穴に入った）: ヘッダ 15 → J14、16 → J16、18 → M14、22 ← M15。Primer の T12（カウンタの 12 ビット）→ PMOD JB の
1 番（上の列の端）。GND はヘッダ 20 番 → J12 の GND。

- O1: 27 MHz / 4096 で変わるカウンタを 10 秒、TCK 1・10・12.5 MHz で欠け・溢れ 0（値 6,592/秒、変化の間隔はすべて 4096
  周期）。16.7 MHz からは今のジャンパ線では TDO が化ける（往復の遅れが半周期に収まらない。TDO の取り込みの点を遅らせる
  調整が要る。残務 R16）。
- O2: 同じビットの変化をロジアナと観測で比べた差は、遅れの申告で直して -12 ns（-17〜-6 ns、標準偏差 3 ns）。直す前は
  -149 ns（ロジアナの入り口 7.5 刻み・観測の写し取り 2.5 周期・記録 1 周期。残りの -12 ns は端子と線）。
- XIAO のサンプラの出来事と FPGA の観測が、同じイベントの記録に時刻の順で並ぶ。

## PS の DAP の JTAG（PJTAG、Raspberry Pi のヘッダ）

PS（Cortex-A9）の DAP を、fprobe の汎用の JTAG の口からジャンパ線でつなぐための端子
（docs/superpowers/specs/2026-10-23-zynq-pjtag-design.md）。fprobe_pynq・fprobe_pynq_deep の回路が、PS7 の PJTAG（EMIO）を
そのまま端子へ素通しする。**DAP がこの線につながるのは、起動のときに MIO2 = 1（independent）のときだけ**。今の基板
（MIO2 = 0、cascade）では PS はこの線を見ない（TDO の出力は出るが意味は無い）。

| 役目 | ヘッダの番号 | Raspberry Pi の名前 | FPGA | FPGA のプル |
|---|---|---|---|---|
| TCK（入力） | 23 | GPIO11 | W10 | 引き下げ |
| TMS（入力） | 24 | GPIO8 | F19 | 引き上げ |
| TDI（入力） | 19 | GPIO10 | V8 | 引き上げ |
| TDO（出力） | 21 | GPIO9 | V10 | — |
| GND | 25 | — | — | — |

- 出典は上の観測の口と同じ base.xdc（rpi_gpio_tri_io の GPIO8〜11）。この 4 本は実機ではまだ確かめていない。
- independent にすると、基板の USB の JTAG（hw_server）からは PS の DAP が見えなくなる（PL の書き込みはできる）。

## ボードの間の時刻の同期（Raspberry Pi のヘッダ）

`gateware/fprobe_gw/boards/pynq_z2.py` の `sync` の資源グループ: 親から入る `sync_in` = GPIO16（ヘッダ 36 番、B19、引き下げ）、
子へ出す `sync_out` = GPIO20（ヘッダ 38 番、A20）。

## PL の書き込み

PYNQ の Linux 経由（`pynq.Bitstream`・fpga_manager）で書くと Linux が固まった（2026-10-19）。JTAG（Vivado の hw_server）で
書く: `fprobe_gw.toolchain.pynq_loader`。口はシリアル `1234-TUL` の 1 本だけを選び、IDCODE が Zynq-7020（x3727093）である
ことを照合する。手順:

1. `sudo -n /usr/local/bin/fprobe-pl before`（SSH）: 中継を止め、「fprobe の回路が載っている」印 `/run/fprobe/pl-design` を
   消し、`fprobe-relay --isolate` で PS と PL の間のレベルシフタ（SLCR 0xF8000900）を 0 にする。
2. JTAG で書く。
3. fprobe の回路（PS7 と zynq_frame_link を持つ）なら `fprobe-pl after <id>`: 印を置いて中継を起動する。中継はシフタを
   0xF にし、ID（"FPLK"）と版を確かめてから受け渡しを始める。印が無ければ PL を読まずに終わる。

コマンド: `python -m fprobe_gw build <構成> --program`（構成が zynq_frame_link を持つなら中継を起動、`ps_axi` なら
シフタを有効にする）、`python -m fprobe_gw program <top.bit> --board pynq_z2 [--relay | --shifter]`。`--relay` には
ビットストリームの隣のビルドの印 `inputs.sha256`（先頭 16 文字が `after` の id になる）が要る。`--shifter` は
`ps_axi` の構成のビルドの置き場所（例 `build/zynq_dut/`）の top.bit にだけ使える。`--persistent`（起動時の置き換え）は未対応。
`python -m fprobe_gw pynq install | status | reserve | unreserve | ringmap | unringmap`: 中継と補助の据え付け（`FPROBE_PYNQ_SUDO_PASSWORD` が要る）・
状態・深いバッファの予約・ゼロコピーの道のモジュールの起動時の読み込み（下の「ゼロコピーの道のモジュールを起動時に読み込む」）。

接続先は環境変数で替える（既定は括弧内）: `FPROBE_PYNQ_HOST`（192.168.77.2）、`FPROBE_PYNQ_USER`（xilinx）、
`FPROBE_PYNQ_KEY`（`~/.ssh/fprobe_pynq`）。ユーザー名・ホスト名は SSH と sudoers のコマンドに入るので、英数字と `._-`（ホストは `:` も）だけを
受け付ける。`after` の id は 16 進（小文字）だけで、そうでなければ PYNQ に触れる前に断る。SSH は `BatchMode=yes`・
`ConnectTimeout=10`（パスワードを聞かない）、既定 60 秒、Vivado は 600 秒で打ち切り、どちらも `PynqError` で報告する。

**書き込みが途中で失敗したときの状態**: 手順 1 の後は中継が止まり、印が無く、シフタが無効（安全な側）。JTAG が失敗・時間切れでも
そのままで、中継は起動し直さない（PL の状態が不明なため）。もう一度 `program` を実行すれば `before` から繰り返す。
二重起動の排他は無い（同時に 2 つの `program` を走らせない）。

中継（`fprobe-relay`）の終了コード: 2 = 印が無い、3 = PL の ID・版が違う、4 = 動作中に ID が読めなくなった（100 ms ごとに確認）。
systemd のサービスは失敗で 2 秒後に起動し直すが、2 と 3 は起動し直さない。送り主を覚えるのは形の正しい（2〜514 バイト）
データグラムだけで、PC → PL が詰まっている（RX_STATUS の bit0）間は 1 つだけ保留して PC からの次を読まない。PL → PC は 1 周に最大 4 フレーム。

**Linux が固まった条件**（2026-10-20）: シフタ有効のまま PL を書き直した／PS7 を持たない回路でシフタを有効にした。
PS7 を持つ回路では固まらなかった。応答の無い PL の番地を読んでも固まる（中継は印が無ければ読まない）。

## 起動と電源

- `/boot/boot.py` は空（元は `boot.py.orig`）。PYNQ の標準の回路（base.bit）は PMODA を駆動してキーを押したので、起動時に
  書かない。起動直後の PL は未構成で、中継は印が無いので動かない。
- 起動の途中（PL が未構成の間）の端子の状態は未確認。キーの線は基板の引き下げの穴にあるので入らない見込み（要確認）。
- P3 の深いバッファ: `host/pynq/uEnv.txt`（今の起動の引数 + `mem=479M`）を /boot に、`host/pynq/fprobe-mem` を
  /etc/fprobe/mem に置いて再起動すると、DDR の上端 33 MB を Linux が使わなくなる。0x1E000000〜 の 32 MB は深いバッファ（中継が MEM_BASE に書く）、
  0x1DF00000〜 の 1 MB は Linux のアプリのデバッグのリング（docs/linux.md）。
  元に戻すには /boot/uEnv.txt を消す。
  2026-10-20 に実施: `pynq reserve` の再起動はシャットダウンの途中で止まった（DONE が点いたまま・シリアルも無反応・
  PC の USB の LAN のリンク無し）。電源の入れ直しで起動し、予約は入っていた（当時は mem=480M、Linux のメモリ 459 MB。今の uEnv.txt は mem=479M で、リング用の 1 MB を足した）。
  深いバッファの実機の試験（gateware/tests/hil/test_pynq_deep_hw.py）は 4 つとも通った（RAW・RLE の取り込み、
  中継の流し続け 2.19 MB/s 溢れなし、サンプラのイベント）。

## 使える機能（fprobe_pynq、2026-10-20 に XIAO で確認）

- SWD のデバッグ（pyOCD、attach）・RTOS（Zephyr のスレッド・スタック）・DWARF の式の評価（止めずに）・モニタ方式の
  ブレークポイント・ステップ。
- 内蔵ロジアナ（16 ch × 8192 サンプル、最大 100 MS/s）と解読（SWD 等）。クロストリガの出力（キーの自動操作）。
- イベント記録（BRAM のリング 1024 件。docs/superpowers/specs/2026-10-20-bram-event-ring-design.md）→ サンプラ・名前の
  ライブウォッチ・ロジアナと共通の時刻の記録。
- 深いバッファ（fprobe_pynq_deep、DDR の 32 MB）は予約（`python -m fprobe_gw pynq reserve`、再起動）の後。
- 実機試験: `FPROBE_PYNQ_HOST=192.168.77.2 FPROBE_PYNQ_TARGET=xiao pytest --hil gateware/tests/hil/test_pynq_link_hw.py`
  （6 件）、`test_pynq_deep_hw.py`（予約の後）。

## 内蔵ロジアナを 300 MHz で動かす構成（fprobe_pynq_cap、2026-10-30）

内蔵ロジアナ（フィルタ・パルス幅・前段・BRAM の記録）を、sys（100 MHz）の 3 倍の `cap_clk`（300 MHz）の領域で動かす構成。
設計は `docs/superpowers/specs/2026-10-30-la-capture-clock-design.md`。

- クロック: MMCM の VCO を 1200 MHz（125 / 5 × 48。2026-11-01 の段 2 から。その前は 900 MHz）にし、`CLKOUT0` ÷ 12 = sys 100 MHz、`CLKOUT1` ÷ 4 = cap_clk 300 MHz。**どの PYNQ 構成も VCO は 1200 MHz**（sys は 100 MHz のまま。cap_clk の比は構成が選ぶ: `fprobe_pynq_fast` は ÷ 3 = 400 MHz）。
- 時間の分解能: sys の 1 周期（10 ns）から、取り込みの 1 周期（3.33 ns）に。トリガの時刻は `trigger_time × 3 + trigger_phase − latency`（`la_cap.info`）。
- Vivado（xc7z020-1、静的タイミング）: `fprobe_pynq_cap` は cap_clk 302.1 MHz・sys_clk 106.8 MHz で setup・hold とも合格（余裕は小さい）。`fprobe_pynq` は sys_clk 108.9 MHz で合格。
- 配線なしの確認: `la_cap.control.selftest = 1` で ch 9 に sys の周期ごとに反転する内部信号を入れると、同じ値が 3 サンプルずつ続く（シミュレーションで確認済み）。
  実機は `gateware/tests/hil/test_pynq_cap_hw.py`（`FPROBE_PYNQ_HOST` を入れたときだけ）。
- 垂れ流し（深いバッファ・DDR）は sys の 1 サンプル/周期のままで、300 MHz の恩恵は無い。内蔵バッファ（短い区間を精密に見る）の改善。

## 外部ピンを 800 MS/s で取る構成（fprobe_pynq_fast、2026-11-01）

PMOD JA の 8 本（ch 0〜7、デバッグの線と兼用）と PMOD JB の 6 本（ch 10〜15）を端子の ISERDESE2 で取り、sys の 1 周期に 8 サンプル（1.25 ns 間隔）を
内蔵バッファに記録する構成。設計は `docs/superpowers/specs/2026-11-01-la-resolution-design.md`（段 1 = §9・§10、段 2 = §11）。

- クロック: MMCM の VCO 1200 MHz、cap_clk 400 MHz（÷ 3）の両方の辺 = ISERDESE2 の CLK、CLKDIV = sys 100 MHz。N = 2 × cap_ratio = 8。
- 内蔵バッファ: 16384 語 = 131072 サンプル（163.8 µs）。プリトリガ・位置は語（8 サンプル）の単位。間引き（rate）は無い。グリッチフィルタ・パルス幅のトリガも無い（ケイパビリティで偽）。
- 時刻: ピンが変わった細かいサンプルの番号 = `trigger_time × 8 + trigger_lane − latency`（`la_cap.info`、latency = 37）。ch 8・9（内部の信号）は sys の信号で、8 サンプルに同じ値が並ぶ（ケイパビリティ LA_FINE = 0xFCFF）。
- JA を ISERDESE2 に載せても、DAP・UART・gpio は ISERDESE2 の組み合わせの出力 O で端子を読む（出力の経路は前と同じ）。
- 試験用の方形波（RPi ヘッダ 32 番）は cap_clk（400 MHz）で刻む（1 単位 = 2.5 ns = 細かいサンプル 2 個）。実機の試験は `gateware/tests/hil/test_pynq_fast_testgen_hw.py`（`FPROBE_PYNQ_HOST`、32 番 ↔ JB の 1 番。`FPROBE_PYNQ_TESTGEN_CH` で JA の ch にも）。
- 段 1（600 MS/s・JB だけ、C:\fw61）の Vivado: sys 107.2 MHz・cap_clk 341.5 MHz で合格、LUT 16.8 %・BRAM 30 タイル・ISERDESE2 6。段 2（C:\fw62）: sys 103.8 MHz・cap_clk 438.8 MHz（400 MHz）で合格、LUT 17.5 %・BRAM 64 タイル・ISERDESE2 14（設計書 §11.5）。

## 性能（2026-10-20、XIAO nRF52840）

- CONTROL の往復 約 290 µs（PYNQ が UDP をそのまま返すだけで約 250 µs。中継と PL の分は小さい: PL の窓の読み 0.15 µs）。
- pyOCD の read32 の往復 約 380 µs、メモリの読み 297 KB/s（SWD 4 MHz）・532 KB/s（8 MHz）。
- 資源（fprobe_pynq）: LUT 約 11.5%・BRAM 6 タイル。sys 100 MHz を満たす。
- ストリームと制御は同じ中継を通る。**以下は旧い中継（窓の語ごとの読み）での記録（2026-10-21、Shrike-Lite のシステム解析）**: ロジアナの深いバッファのストリームが約 1〜2 MB/s を超えると、制御の応答がその後ろで待たされた（DAP サンプラ 1000 回/秒 + ピンのストリームで 2.3 MB/s → CSR の往復 0.4〜1.4 秒。サンプラの SWD の転送で内部の ch が細かく変わるのが流量の大半）。PS が DDR を直接読む版（下の節）では、ストリームが窓を通らないので、この待たされ方は無い（実測: 17 MS/s のストリーム中も、CSR の往復は中央値 4.6 ms）。ただし、サンプラのイベントも流す同じ条件では、測り直していない。システム解析の画面は、ピンも流すとき Ethernet ではメモリの読みを 200 回/秒にする（旧い中継の時の値）。USB（Primer）はエンドポイントが別なので起きない。

## Zynq DUT（Primer の fprobe が外からデバッグする側）

仕様 docs/superpowers/specs/2026-10-24-zynq-dut-environment-design.md、計画 docs/superpowers/plans/2026-10-24-zynq-dut-environment.md。
PYNQ を fprobe にせず、Primer の fprobe がデバッグされる側（DUT）として使う構成。構成名 `zynq_dut`（`python -m fprobe_gw build zynq_dut`）。

- **回路**: PS7（`ps7_bd.tcl`）+ AXI-Lite（0x40000000 から 4 KB）のサンプル生成 + FIFO（`dut_stream_core`）。レジスタ表は
  `host/pynq/dut/dut_regs.h`（VHDL と同じ語番地。`host/tests/test_dut_app.py` が照合）。注目信号 `dbg[5:0]` を PMOD JB の
  1〜4・7〜8 番（W14 Y14 T11 T10 V16 W16）に出す。拍（生成・読み・溢れ・捨て）は 4 クロック（40 ns）に伸ばす
  （1 クロックの脈はロジアナの標本の間に落ちて約 1 割が欠けた。実機 2026-10-25）。PS の PJTAG は RPi ヘッダへ素通し（上の節）。
- **書き方**: `python -m fprobe_gw program gateware/build/zynq_dut/top.bit --board pynq_z2 --shifter`（JTAG で書き、書いた後に
  `fprobe-pl enable` でレベルシフタを有効にする。PYNQ の `fprobe-pl` に `enable` を足したので、一度 `python -m fprobe_gw pynq install`
  が要る）。fprobe の回路ではないので中継は起動しない。
- **アプリ**: `host/pynq/dut/dut_app.c`（PYNQ の `~/dut` で `make`、`-marm`）。`sudo ./dut_app --period 100000 --seconds 5`。
  `--delay-us <n>` で読み手を遅らせると FIFO が溢れ、`--fault <n>` で n 個に 1 個を PL が捨てる。
  Linux のプローブは関数 `dut_consume`（引数 = 取り出した番号）に付ける。
- **ロジアナの ch（Primer の fprobe_la、J14 の T8 P8 R8 T7 T6 P6 = ch10〜15）**: 10 = 生成の拍、11 = FIFO 満杯、12 = DATA の読み、
  13 = 溢れの拍、14 = 不具合で捨てた拍、15 = 生成が有効。**JB の 3 番と 4 番の線が R8 と T7 で交差している**ため、
  dbg[2]（溢れ）が ch13、dbg[3]（読み）が ch12 に出る（配線を直せば入れ替わる）。
- **実測（2026-10-25）**: 生成 1 kHz で、生成の拍の間隔は 1.000003 ms。`--fault 7` の捨てた拍の間隔は 7.000060 ms（生成の 7 倍）。
  遅い読み手（20 ms）では、満杯の後に溢れの拍が 1 kHz で出て、読みの拍は 20 ms 間隔。ptrace のプローブ（`dut_consume`）を付けると
  アプリが止まって遅れ、プローブ自体が溢れを起こした（観測が対象を変える）。
- **JTAG の 5 本（フェーズ 1）とも未確認**: JP1（MODE3）を挿した independent の起動で PS の DAP が Primer の `jtag.scan` に見えるかは、
  利用者の立ち会い後（JP1 の位置は基板で確認してから）。

## 深いバッファのストリームを PS が DDR から直接読む（fprobe_pynq_deep の版 2、2026-10-31）

設計と実測: docs/superpowers/specs/2026-10-30-la-stream-ps-direct-design.md。窓（PL の AXI-Lite）を 32 ビットずつ読む道（1 フレーム約 34 µs、約 10 MB/s）の代わりに、
PL が DDR のリングに書いた拍を、中継が DDR から NEON で読んで送る（ケイパビリティ `la_stream_ps`。ホストの `DeepAnalyzer.stream` が自動で選ぶ。`ps_read=False` で窓経由）。

| 経路 | 持続できる速さ（RAW、16 ch、実測） |
|---|---|
| 窓経由（UDP） | 約 10.4 MB/s（約 5.2 MS/s） |
| PS 直読み + UDP | この PC では約 20 MB/s（10 MS/s）まで。それ以上は PC の NIC が受信を破棄して欠ける |
| PS 直読み + TCP（リングが Linux の管理外 = 起動パラメータ `mem=479M`） | 約 35〜40 MB/s（約 17.5〜20 MS/s）。60 秒・2,014 MB でも欠け 0 |
| **PS 直読み + TCP（リングを reserved-memory に置く。下の「起動の設定」）** | **25 MS/s（50 MB/s）を 60 秒・3 GB、欠け 0（最終の構成で 11 回中 11 回）。20 MS/s（40 MB/s）も 60 秒・2,413 MB、欠け 0。33 MS/s（66 MB/s）は 2〜3 秒で overrun**（中継の割り込みの設定・送信のまとめ・ホストの受信のまとめ取りが効いている。入れる前は 25 MS/s の 60 秒が 8 回中 3 回しか通らず、PC の受信が詰まる時間帯もあった。サンプルレートは 100 MHz の整数分周なので、指定は 20・25・33 MS/s に丸められる） |

- ホストは `Session.open_eth` で、ケイパビリティを見て TCP（中継の 63921）に自動でつなぐ（`stream_tcp=False` で UDP のまま）。制御と DAP は UDP のまま。
- 要求が持続できる速さを超えると、PL の DDR のリング（ロジアナの分 30 MiB = 31,457,280 バイト。32 MiB の領域の 1/16 = 2 MiB はイベント記録。`la_deep_buffer.vhd` の EV_SIZE・LA_SIZE）が溜め、溢れたら `overrun` で止まる（欠けや破損は出ない）。
- 入れ替えの順番: **先に中継**（版 1 と 2 の窓を受ける）、その後に PL。逆だと、古い中継は版 2 の PL を断って起動しない（安全側）。中継の元のバイナリは退避しておく。
- 壁（約 50〜54 MB/s）は、ARM の 2 コアがどちらも 85 % ほど TCP の処理（CPU0 の NIC の softirq、CPU1 の送信）と読みで埋まる分。中継は、TCP のストリームがつながっている間だけ eth0 の `napi_defer_hard_irqs` を 8、`gro_flush_timeout` を 500000 ns にして ACK の処理をまとめ、閉じたら元の値に戻す（root で動くときだけ。変えられなければ、そう記録して続ける）。送信は、読めるスロットを最大 8 つまとめて 1 回の sendmsg にしている（1 回が 6.5 KB だと 20.7、16 KB だと 15.3 ms/MB）。素の TCP の送信だけなら 1 コアで約 70 MB/s、NEON の DDR 読みだけなら約 235 MB/s（reserved-memory のとき）。内訳は、TCP が閉じたとき中継が journal に出す（`journalctl -u fprobe-relay`）。
- 中継は、読み取りスレッドを NIC（eth0）の割り込みの CPU に、送るスレッド（主）を別の CPU に固定する（割り込みの CPU は `/proc` から読む。分からなければ固定しない）。送るスレッドが割り込みと同じ CPU に載ると、約 36 MB/s に落ちて 2〜3 秒で溢れた（24 MS/s、8 秒 5 回とも。逆の配置は 5 回とも約 48 MB/s）。
- トリガ取り込みの読み出し（書き込みが止まったロジアナのリング）も、同じ仕組みで PS が DDR から読んで TCP で送る（窓の版 3、CSR `la_deep.command.read_ps`）。実測 4.8 MB を 39.1 MB/s（窓経由は 9.6 MB/s）。イベントのリングは窓経由のまま。
- 実機の試験: `FPROBE_PYNQ_HOST=<IP> pytest gateware/tests/hil/test_pynq_ps_stream_hw.py --hil`。

### 起動の設定: リングを reserved-memory に置く（読みが約 125 → 約 235 MB/s、2026-10-31）

リング（既定 0x1E000000 + 32 MB）を `mem=479M` で Linux から外すと、`/dev/mem` は最も遅い割り当てで写す（NEON の読み 約 125 MB/s）。
デバイスツリーの `reserved-memory` に置くと、カーネルは使わず（memblock の予約）、割り当てが書き込み結合になる（約 235 MB/s。同じ DDR を割り当てだけ変えて読み比べた実測）。

1. 元の `image.ub`（`/boot`）から、別の FIT を作る: `python host/pynq/fit-reserve/fit_reserve.py image.ub image-wc.ub`（元は変えない。カーネルはそのまま、dtb と sha1 だけ）。
2. `/boot` に `image-wc.ub` を置き、`/boot/uEnv.txt` に次を足す（元の `uEnv.txt` は退避しておく）。**元の `image.ub` は残す。** 新しい FIT が検証で落ちれば `bootm` が戻り、元の流れ（`mem=479M`）で起動する。
   ```
   bootargs_stock=<元の bootargs から引用符を外したもの。mem=479M を含む>
   bootargs_wc=<同じ。mem=479M を外したもの>
   fprobe_wc=if fatload mmc 0 0x10000000 image-wc.ub; then setenv bootargs ${bootargs_wc}; bootm 0x10000000; setenv bootargs ${bootargs_stock}; fi
   uenvcmd=run fprobe_pl; run fprobe_wc
   ```
3. 再起動する。起動後、`/sys/firmware/devicetree/base/reserved-memory/fprobe_ring@1e000000` がある。中継は `/proc/device-tree/reserved-memory` の 1 つのノードがリング全体を含み、`no-map`・`reusable`・`compatible` が無いときだけ、Linux のメモリと重なっていても使う（`/proc/iomem` には出ない。カーネルの `memblock/reserved` で、0x1dffff88..0x1fffffff が予約されているのを確かめた）。
4. 起動のとき、U-Boot は `/boot/fprobe.bit.bin`（古い PL）を読む。**起動後に、使う PL（`fprobe_pynq_deep`）を `fprobe-pl` の手順で書き直す。**

### ゼロコピーの道のモジュールを起動時に読み込む（`pynq ringmap`、2026-11-02）

中継は、起動のときに `/dev/fprobe_ring`（カーネルモジュール `fprobe_ringmap`、`host/pynq/fprobe-ringmap/`）があり、それが写す
デバイスツリーのノード `fprobe_ring` が深いバッファの領域と合えば、TCP のストリームをゼロコピーの道（`MSG_ZEROCOPY`、ロジアナは
最大 4096 拍のかたまり）で送る。無ければ今までの複写の道で送る（設計 docs/superpowers/specs/2026-11-01-bandwidth-observation-design.md
1.7.8・1.7.9。実測は 16 ch の RAW で 25 → 33.3 MS/s）。起動時の読み込みは利用者が 2026-11-02 に承認した。

**この PYNQ には入れてある（2026-11-02 夜）**: `pynq install` → `pynq ringmap`（PYNQ の上でビルド）→ 再起動。再起動の後の `pynq status`: モジュールの
ファイルあり（vermagic と uname -r が同じ、fprobe_src がリポジトリと同じ）、起動時の設定あり、読み込みあり（refcnt 1）、`/dev/fprobe_ring` あり、
systemd-modules-load active、中継は「ゼロコピーの道で送る」で `zc_state ready`。流して（PL は起動の版 fw72、16 ch RAW、10 秒）: 25 MS/s・33.3 MS/s
とも溢れなし、`tcp_path zerocopy`、copied 0・ENOBUFS 0。

前提: 上の「起動の設定: リングを reserved-memory に置く」で起動している（`/proc/device-tree/reserved-memory/fprobe_ring@1e000000`
がある）。無ければモジュールの読み込みは失敗し（`systemd-modules-load` が failed）、中継は複写の道で動く。

入れ方（`FPROBE_PYNQ_SUDO_PASSWORD` が要る）:

1. `python -m fprobe_gw pynq install` — 中継とサービスのファイルを新しくする。`fprobe-relay.service` は
   `After=systemd-modules-load.service`（読み込みの後に起動する。`systemd-modules-load` は `sysinit.target` が引き込むので `Wants=` は置かない）。
   新しい中継は、状態の問い合わせ（`pynq status` が別のソケットから送る）で PC の宛先を変えない。
2. `python -m fprobe_gw pynq ringmap` — ソースを `~/fprobe/ringmap` に送り、PYNQ の事実を読んでから、何を変えるかを表示し、その後に
   管理者の手順を走らせる:
   - 作り直すかは事実で決める。`/lib/modules/$(uname -r)/extra/fprobe_ringmap.ko` が無い・その `modinfo -F vermagic` の最初の語が
     `uname -r` と違う（カーネルを替えた）・`modinfo -F fprobe_src` がリポジトリのソースの sha256 と違う、のどれかなら作り直す。
     `fprobe_src` は `build.sh` が入れる印（`sha256sum fprobe_ringmap.c Makefile build.sh cc-compat.sh | cut -c1-64 | sha256sum`。
     同じ計算は `fprobe_gw.pynq_setup.ringmap_source_digest`）。
   - 作るのは `build.sh`（root は要らない。約 3 分、`/tmp/fprobe-ringmap-build` に約 110 MB。前の作業の置き場所は名前で消してから作り、
     据え付けの後にも消す）。作ったものの vermagic の版と `fprobe_src` が合わなければ据え付けない（作業の置き場所は残す）。
   - `install -D` で `/lib/modules/<版>/extra/` に置き、`depmod <版>`、`/etc/modules-load.d/fprobe-ringmap.conf`（中身は `fprobe_ringmap`）を置く。
   - **今は読み込まない・中継は止めない。** 次の起動から効く。
3. PYNQ を再起動する（`sudo reboot`）。U-Boot が `/boot` の回路を PL に書いていれば `fprobe-boot` が印を置き、中継が起動して
   `/dev/fprobe_ring` を探す。その回路が深いバッファの PS 直読みの回路（`fprobe_pynq_deep`）でなければ書き直す
   （`python -m fprobe_gw build fprobe_pynq_deep --program`。`fprobe-pl after` が中継を起動し直し、そのときにも探す）。

確かめ方（再起動の後）:

- `python -m fprobe_gw pynq status` の「ゼロコピーの道（fprobe_ringmap）」で、モジュールのファイル あり（vermagic の版 = uname -r、
  fprobe_src がリポジトリと同じ）・起動時の読み込みの設定 あり・読み込まれている あり（中継が写していれば refcnt 1 以上）・
  `/dev/fprobe_ring` あり・`systemd-modules-load` active・中継の journal の行が「…を写した…ゼロコピーの道で送る」・
  中継の状態（relay_status）が `zc_state ready`。`tcp_path` は TCP のストリームがつながっている間だけ `none` 以外になる。
- ストリームを流し、ホストの `la_stream`（`fprobe.probe_la`）の返り値の `relay` の欄で `tcp_path` が `zerocopy`、`zc_copied_calls`・
  `zc_enobufs` が 0 であること。
- **ゼロコピーの道のかたまり（4096 拍）は、`STREAM_SEG_MAX_BEATS` = 4096 のホストでしか読めない**（256 の前のホストは
  `StreamFormatError`）。モジュールを読み込んだ PYNQ には新しいホストで繋ぐ（複写の道は前のホストでも同じ）。

外し方: `python -m fprobe_gw pynq unringmap` — 何を変えるかを表示してから、`/etc/modules-load.d/fprobe-ringmap.conf` と
`/lib/modules/<版>/extra/fprobe_ringmap.ko` を消して `depmod <版>`。今読み込まれていれば、中継を止めて `rmmod`（中継が写している間は
外せない）、中継が動いていたら起動し直す（流れているストリームは切れる。起動し直した中継は複写の道）。再起動は要らない。

気をつけること:

- カーネルを替えたら `pynq ringmap` をもう一度（版の名前が変わるので作り直す。古い版の `/lib/modules/<古い版>/extra/` の .ko は残る）。
- モジュールはカーネル（GCC 12.2）と違う gcc 11.2 で作る（`cc-compat.sh`）。約 23 分の実測で落ちなかったが、ABI の一致を確かめたとは
  言えない（設計 1.7.8・1.7.9）。
- パケットの盗み見（`SO_EE_CODE_ZEROCOPY_COPIED`）で copied が出ると、中継はゼロコピーをやめ、起動し直すまで複写の道（`zc_state stopped_copied`）。
- `pynq status` の状態の問い合わせ（UDP）は、`pynq install` より前の中継では PC の宛先を移す（使っている PC の UDP の応答が届かなくなる）。
  先に `pynq install` で中継を新しくする。

**再起動（2026-10-31 に解決）**: 以前、Linux からの `reboot` は `Reboot failed -- System halted` で止まった（CPU0 が `machine_restart` の `b .`）。原因は、中継がレベルシフタの切り替えのたびに **SLCR をロックし直していた**こと。
カーネルの再起動処理は鍵を書かずに `PSS_RST_CTRL` へ書くので、ロックされていると無視される。中継は、書く前のロック状態を保つようにした（設計文書 10.15）。修正した中継では `reboot` が通る。再起動の後、約 3 分間は PYNQ の 2 コアとも起動時の処理（jupyter など）で埋まり、優先度を上げる前はストリームが溢れた。中継のサービスは `Nice=-20`（`host/pynq/fprobe-relay/fprobe-relay.service`）で、jupyter などの再起動と CPU 使い切りの中でも 25 MS/s の 40 秒が通った（設計文書 10.15。実際の再起動後も、PL を書いた直後（起動から約 2 分）から 25 MS/s の 8 秒が 11 回中 11 回通った）。
中継の古い版（ロックし直す版）が動いていて止まったときの復旧: **JTAG（PYNQ-Z2 自身の USB ケーブル）から PS のソフトリセットをかけると戻る**（OpenOCD で、`tools/openocd/pynq_z2.cfg` に `target create zynq.ahb mem_ap -dap zynq.dap -ap-num 0` を足し、
`init` の後に両コアを `resume`、`zynq.ahb mww 0xF8000008 0xDF0D`、`zynq.ahb mww 0xF8000200 0x1`。起動まで約 90 秒）。実行中のときは、先に `sync` する。

