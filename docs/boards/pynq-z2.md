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

## PL の書き込み

PYNQ の Linux 経由（`pynq.Bitstream`・fpga_manager）で書くと Linux が固まった（2026-10-19）。JTAG（Vivado の hw_server）で
書く: `fprobe_gw.toolchain.pynq_loader`。口はシリアル `1234-TUL` の 1 本だけを選び、IDCODE が Zynq-7020（x3727093）である
ことを照合する。手順:

1. `sudo -n /usr/local/bin/fprobe-pl before`（SSH）: 中継を止め、「fprobe の回路が載っている」印 `/run/fprobe/pl-design` を
   消し、`fprobe-relay --isolate` で PS と PL の間のレベルシフタ（SLCR 0xF8000900）を 0 にする。
2. JTAG で書く。
3. fprobe の回路（PS7 と zynq_frame_link を持つ）なら `fprobe-pl after <id>`: 印を置いて中継を起動する。中継はシフタを
   0xF にし、ID（"FPLK"）と版を確かめてから受け渡しを始める。印が無ければ PL を読まずに終わる。

**Linux が固まった条件**（2026-10-20）: シフタ有効のまま PL を書き直した／PS7 を持たない回路でシフタを有効にした。
PS7 を持つ回路では固まらなかった。応答の無い PL の番地を読んでも固まる（中継は印が無ければ読まない）。

## 起動と電源

- `/boot/boot.py` は空（元は `boot.py.orig`）。PYNQ の標準の回路（base.bit）は PMODA を駆動してキーを押したので、起動時に
  書かない。起動直後の PL は未構成で、中継は印が無いので動かない。
- 起動の途中（PL が未構成の間）の端子の状態は未確認。キーの線は基板の引き下げの穴にあるので入らない見込み（要確認）。
- P3 の深いバッファ: `host/pynq/uEnv.txt`（今の起動の引数 + `mem=480M`）を /boot に、`host/pynq/fprobe-mem` を
  /etc/fprobe/mem に置いて再起動すると、DDR の上端 32 MB（0x1E000000〜）を Linux が使わなくなり、中継が MEM_BASE に書く。
  元に戻すには /boot/uEnv.txt を消す。

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
