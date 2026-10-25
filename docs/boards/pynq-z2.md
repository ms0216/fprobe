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
`python -m fprobe_gw pynq install | status | reserve | unreserve`: 中継と補助の据え付け（`FPROBE_PYNQ_SUDO_PASSWORD` が要る）・
状態・深いバッファの予約。

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

## 性能（2026-10-20、XIAO nRF52840）

- CONTROL の往復 約 290 µs（PYNQ が UDP をそのまま返すだけで約 250 µs。中継と PL の分は小さい: PL の窓の読み 0.15 µs）。
- pyOCD の read32 の往復 約 380 µs、メモリの読み 297 KB/s（SWD 4 MHz）・532 KB/s（8 MHz）。
- 資源（fprobe_pynq）: LUT 約 11.5%・BRAM 6 タイル。sys 100 MHz を満たす。
- ストリームと制御は同じ中継を通る: ロジアナの深いバッファのストリームが約 1〜2 MB/s を超えると、制御の応答が
  その後ろで待たされる（2026-10-21、Shrike-Lite のシステム解析の記録。DAP サンプラ 1000 回/秒 + ピンのストリームで
  2.3 MB/s → CSR の往復 0.4〜1.4 秒、画面と AI の問い合わせが遅れ、記録の取り込みも数分遅れた。サンプラの SWD の
  転送で内部の ch（SWDIO 駆動・DAP 処理中）が細かく変わるのが流量の大半）。200 回/秒なら 0.7 MB/s・2 ms。
  システム解析の画面は、ピンも流すとき Ethernet ではメモリの読みを 200 回/秒にする。USB（Primer）はエンドポイントが
  別なので起きない。

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
