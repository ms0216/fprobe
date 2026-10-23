# PYNQ の Zynq の CPU（PS）に fprobe の JTAG で届く（R26 案 1）— 仕様の案

状態: **案（利用者の判断待ち）**。2026-10-23 夜に調べた事実と、決めてほしい点をまとめた。

## 1. 目的

Linux のアプリのデバッグの道 B（CPU を止めずに、メモリのリングを JTAG の MEM-AP で読む）を、**fprobe 自身の JTAG で**
実機で確かめる。Pi 2B の DAP には APB-AP しか無く、止めずには読めない（2026-10-22 に確認）。止めずに読める AP を持つ
手持ちの SoC は PYNQ-Z2 の Zynq-7020 だけ。今は Vivado の hw_server（xsdb の memap）経由で読んでおり、fprobe の JTAG では
届いていない。

## 2. 調べた事実

- **JTAG のつなぎ方は 2 通り**（UG585 v1.14、6.5 節「JTAG and DAP Subsystem」、表 6-4）:
  - cascade（ふつう）: 基板の JTAG の端子に、PS の DAP と PL の TAP が 1 本の鎖でつながる。
  - independent: 基板の JTAG の端子には PL の TAP だけ。PS の DAP は **EMIO の PJTAG**（PL を通る）か MIO の PJTAG で
    つなぐ。EMIO を使うときは、PL に PJTAG を端子へ回す回路を書いてから DAP が使える。
  - どちらにするかは、電源を入れたときの **MIO[2]（BOOT_MODE[3]）** で決まる。0 = cascade、1 = independent。
- **PYNQ-Z2 の今の状態**: SLCR の BOOT_MODE（0xF800025C）= 0x5。SD から起動していて、MIO[2] = 0 なので **cascade**
  （2026-10-23、PYNQ の Linux の /dev/mem から読んだ）。
- **PYNQ-Z2 の回路図**（TUL_PYNQ_Schematic_R12）: MIO[2] の線は SPI_DQ0/MODE3 で、20 kΩ で 0 に引かれている。
  4 ピンのヘッダ **JTAG_JP1** に、1 kΩ を通して 3.3 V に引く形でつながっているように読める。同じヘッダには MODE4
  （MIO[6] = PLL を通さない）もつながっているように読める。**どのピンの組が MODE3 かは、まだ基板で確かめていない。**
  （回路図の PDF から抜き出した文字の配置が崩れていて、ここは読みに推測が入る。PYNQ のフォーラムにも「JP1 のそばの
  4 ピンで independent にできるらしい」という書き込みがある。）
- **Vivado の PS7 の部品**: `PCW_PJTAG_PERIPHERAL_ENABLE 1`・`PCW_PJTAG_PJTAG_IO EMIO` にすると、部品の端子に
  PJTAG_TCK・PJTAG_TMS・PJTAG_TDI・PJTAG_TDO が出る（2026-10-23、Vivado 2025.2 で試しに作って確かめた）。
- **DAP の有効化**: devcfg.CTRL[DAP_EN] = 111 で DAP が有効になる。セキュアでない起動では BootROM が有効にする。
- **確かめていないこと**:
  - SD から起動して independent のとき、Linux がふつうに上がるか。UG585 の「PL が 90 秒以内に書かれないとロックする」は
    JTAG 起動の場合の記述で、SD 起動のときにどうなるかは読み取れなかった。
  - Linux が上がった後に PL を書き直したとき、EMIO の PJTAG がすぐ使えるか。
  - Zynq の DAP の AP の並び（AHB-AP がシステムのメモリにつながるか）。fprobe の JTAG でつながったら、最初に読んで確かめる。

## 3. どう作るか（案）

fprobe の本体には何も足さない。**ターゲットの側（PYNQ の PL の回路）で、PS の PJTAG を PL の空いている端子に出し、
ジャンパ線で fprobe の汎用の JTAG の口につなぐ。** CLAUDE.md の「ターゲット側を改造して、デバッガの汎用の機能に合わせる」に
従う形。

- PYNQ のボードの定義に、PJTAG を出す端子の資源のグループ `ps_pjtag` を足す（Raspberry Pi のヘッダの GPIO8〜11 =
  23・24・19・21 番、GND 25 番。docs/boards/pynq-z2.md）。fprobe_pynq・fprobe_pynq_deep は常に素通しする（選択にしない。
  cascade のときは PS が線を見ないので害が無く、設定を増やさないため。Claude の判断）。2026-10-23 夜に実装した（ブランチ
  feat/zynq-pjtag。実機はまだ）。
- つなぐ先（fprobe の JTAG の口）は次のどちらか。
  - **(i) Primer の fprobe**: 本命の機器で確かめられる。ただし Primer の JTAG の口は今、Pi 2B の JTAG（DVP のヘッダ）に
    使っている。
  - **(ii) PYNQ 自身の fprobe の口（PMODA）**: 1 枚で済む。ただし PMODA には今 XIAO の SWD がつながっている。
- 確かめの順:
  1. JTAG_JP1 の MODE3 のピンを基板で確かめる（利用者）。
  2. 電源を切って MODE3 のジャンパを挿し、起動する。Linux が上がること、BOOT_MODE が 0xD になることを確かめる。
     上がらなければ、ジャンパを外せば元に戻る。
  3. PJTAG を出す回路を書き、fprobe の jtag.scan で Zynq の DAP（IDCODE 0x4BA00477）が 1 台だけ見えることを確かめる。
  4. AP の並びを読み、止めずに読める AP があればリングを読む（道 B の既存の API で）。

## 4. 代わりに失うもの

- independent にすると、基板の USB の JTAG（hw_server）から PS の DAP が見えなくなる。今の xsdb の memap の道（PYNQ で
  止めずに読む今の方法）は、ジャンパを挿している間は使えない。PL の書き込みは今までどおりできる（TAP は残る）。

## 5. 決めてほしいこと

1. この方向（PS の PJTAG を PL の端子に出し、配線で fprobe の汎用の JTAG の口へつなぐ）でよいか。
2. つなぐ先は (i) Primer か (ii) PYNQ 自身の PMODA か。
3. ジャンパを挿す作業（電源を切って JTAG_JP1。どのピンかを基板の印字で確かめてから）を、いつしてもらえるか。

## 出典

- UG585 Zynq 7000 SoC Technical Reference Manual v1.14（6.3.6・6.5 節、表 6-4・6-17、付録 A の devcfg.CTRL）
- TUL PYNQ-Z2 回路図 R12（https://dpoauwgwqsy2x.cloudfront.net/Download/TUL_PYNQ_Schematic_R12.pdf）
- PYNQ のフォーラム「PYNQ-Z2 board JTAG jumper link query」（https://discuss.pynq.io/t/pynq-z2-board-jtag-jumper-link-query/1644）
