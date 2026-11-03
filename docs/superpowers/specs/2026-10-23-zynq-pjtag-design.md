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

## 実機の確認結果（2026-10-31）

- 構成: PYNQ-Z2 の JTAG_JP1（下の段）の右 2 ピン（ピン 1・2 = 3V3 と MODE3）を短絡。起動後の `BOOT_MODE`（0xF800025C）= 0xD（ジャンパ無しは 0x5）。PL は `fprobe_pynq_deep`（設計 ID `a036ff96d3d136bf`）。
  配線: RPi ヘッダ 23（TCK）・24（TMS）・19（TDI）・21（TDO）・25（GND）→ Primer J12 の印刷名 `J14`・`M15`・`J16`・`T11`・GND。Primer は 10/24 版の `fprobe_jtag`（SRAM）。
- **`jtag.scan`（1 MHz・200 kHz とも）で、IDCODE 0x4BA00477（Arm JTAG-DP、ADIv5、IR 長 4）が 1 台だけ見えた。TDO = ok。**
- 見えなかった間（ジャンパ位置の誤り、配線の誤り）は、装置 0 台・TDO = stuck_high。原因は配線とジャンパで、PL の回路と PS7 の PJTAG の EMIO ではなかった。
- 未確認: AP の並び、AHB-AP でシステムのメモリが読めるか（次の段）。統合後の Primer 用 `fprobe` は、面積不足（GW2A-18C で CLS 98%、185 REG が未配置）で配置できず、この確認には 10/24 版を使った。
- **DP のアクセスは未達**: `target.connect`（zynq7000、attach、core 1）が、100 kHz・500 kHz・2 MHz のどれでも `DapError: SELECT の書き込み: 応答が無い` で失敗する（`gateware/tests/hil/test_arm_a_zynq_hw.py` は 4 件中 1 件通過 = スキャンだけ）。
  IDCODE の読みは、取り込みの遅れ 0〜7 のうち確かめた範囲で 20 回中 20 回一致（`jtag.sample_delay_scan`）。つまり TAP と IR の道は安定していて、DPACC の ACK だけが返らない。原因は未確認（候補: DAP の電源・リセット、PJTAG の EMIO の有効化、IR 4 ビットの DPACC の選び方）。
- **訂正（同日）**: `DAP_EN` は devcfg.CTRL（0xF8007000）の **bit 2:0**（bit 9:7 ではない。7 = SEC_EN、8 = SEU_EN。Linux の xilinx_devcfg.c・FreeBSD の zy7_devcfg.c の定義）。
  起動直後の値は 0x4e00e17f で、DAP_EN = 111（有効）。よって「DAP が無効」は誤りで、`BOOT.BIN` の作り直しの根拠は無い。DPACC の ACK が返らない原因は未確認のまま。
  TAP と IR の道は生のビット列でも確かめた: IR = IDCODE（0xE）で IDCODE、BYPASS（0xF）で入れたパターンの 1 ビット遅れが返る（TDI も正常）。DPACC（0xA）・ABORT（0x8）の DR は全ビット 0 で返る（ACK = 000）。
- **再確認（同日・夜）: DPACC は応答する。** 前の「ACK が返らない」は試験の誤りだった。DPACC の ACK は、要求を回した次の Capture-DR で返る。生のビット列で 2 回続けて回すと ACK = 010（OK）、CTRL/STAT = 0x80000000（CSYSPWRUPACK のみ）。
  `RawDap` の経路（JTAG、`jtag_idle_cycles` 既定）でデバッグ電源が上がる（`power_up`: debug_power・system_power とも True、CTRL/STAT 0xF0000000）。1 MHz、取り込みの遅れ 0〜4 で通る（5 以上は IDCODE が崩れる）。
  `jtag_idle_cycles` を 2・4・8・16 と明示すると、SELECT の書き込みが「応答が無い」になる（既定の None では通る。理由は未確認）。
- **AP の並び（実機）**: APSEL 0〜3 は同じ IDR 0x44770001（MEM-AP、AHB3、Arm）、APSEL 4 以上は FAULT。**APB-AP が見えない**ので `target.connect`（zynq7000）は「APB-AP が無い」で失敗する。
  読みが 1 回前の値を返している疑い、または APSEL の下位 2 ビットが無視されている疑いがあるが、未確認。
- **AHB-AP でシステムのメモリが読める（実機、2026-10-31）**: `RawDap`（JTAG、`read_dpidr` → `power_up` の後）で AP 0 の `mem_read32` が、PYNQ の Linux で読んだ値と一致した: BOOT_MODE（0xF800025C）= 0xD、DEVCFG_CTRL（0xF8007000）= 0x4e00e17f、DBG_CLK_CTRL（0xF8000164）= 0xf03。DDR 0x00100000 は 0xe1a00000（ARM の NOP）。0x00010000 は FAULT（未確認: 範囲外か、読めない領域か）。
  AHB-AP は BASE = 0xFFFFFFFF（ROM テーブル無し）、CSW = 0x43800042。APSEL 0〜3 は同じ AP に見える。**止めずにシステムのメモリを読む道（MEM-AP）は使える。APB-AP は見えない（コアのデバッグレジスタ経由の停止は未達）**。
  `RawDap.start` は `read_dpidr` を先に呼ばないと、SELECT の書き込みが「応答が無い」になる（JTAG の初期化の順序。理由は未確認）。
- **コアを止める道（実機、2026-10-31 〜 11-01）**: APSEL 0〜3 は同じ AHB-AP の別名（TAR に別々の値を書いても最後の値になる）、4 以上は FAULT。APB-AP は見えない。
  ただしコアのデバッグレジスタは、AHB-AP のシステムのアドレス 0xF8890000 + 0x2000 × n で読める（DBGDIDR 0x35137030、DBGAUTHSTATUS 0xFF、DBGLSR 3 = ロック中 → V7DebugPort.unlock で解除）。
  `ArmAPart.ahb_debug_bases`（zynq7000 は 0xF8890000・0xF8892000）で、APB-AP が無いとき AHB-AP のこの番地を使う（`info["discovery"]` = "ahb_system_map"）。
  実機: `gateware/tests/hil/test_arm_a_zynq_hw.py` 4 件通過（コア 1 だけ止めて PC 側でレジスタ・メモリを読み、再開。その間 Linux は生きている）。`test_ap_map_is_recorded` は AP 1〜3 の読みで試験側の番地の誤り（ValueError）を印字する（試験は通る。直す）。
