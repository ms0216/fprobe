# Zynq DUT 環境 実装計画

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Primer の fprobe で PYNQ-Z2（Zynq: A9 + PL）の DUT を JTAG とロジアナでデバッグし、Linux アプリの動き・PL の信号・A9 の停止を同じ時間軸に並べる。

**Architecture:** 既存の PS7 ブロック設計（`ps7_bd.tcl`、PJTAG を EMIO で端子に素通し）に、新しい DUT 回路（AXI-Lite のレジスタ + サンプル生成 + FIFO + 注目信号の端子）を載せる。Primer は既存の構成（fprobe_jtag / fprobe_la）を使い、fprobe 本体は変えない。A9 は既存の `cortex_a` 系の部品表に Zynq の行を足して JTAG で扱う。Linux 側は既存の `linux.*`（ptrace）で観測する。

**Tech Stack:** VHDL-2008（GHDL + cocotb）、Vivado（ブロック設計 TCL）、Python（fprobe_gw / fprobe）、C（PYNQ 上のアプリ、gcc `-marm`）、pytest（HIL は `--hil`）。

**Spec:** docs/superpowers/specs/2026-10-24-zynq-dut-environment-design.md

## Global Constraints

- 返答・コメント・ドキュメントは日本語。VHDL は VHDL-2008、文字列リテラルは ASCII のみ、リセットは同期アクティブハイ `rst`、状態名は `S_` 接頭辞。
- **すべての信号を明示的に駆動する**（初期値は合成で無視される。EX1998 でビルドが失敗する）。使わない端子・配列要素も駆動する。
- すべての entity に cocotb テスト（`tests/tb/<name>_tb.py` + `tests/test_<name>.py` の pytest ラッパ）。
- fprobe 本体にヒューリスティックを入れない。Zynq 固有の話は DUT 側の設計と部品表の 1 行に留める。
- 3.3 V のみ。PYNQ の RPi ヘッダの 1・17 番（3V3）と 2・4 番（5V）には何もつながない。
- 実機の手順: 先に両方の USB を挿して電源を入れ、信号線は後。PL を書く前に必ず `fprobe-pl before`（`PynqLoader.program` が行う）。PYNQ の再起動は電源の入れ直しを利用者に頼む。PMODA に XIAO / RP2040 の線が付いたまま DUT 回路を書かない。
- Primer の JTAG は 2 MHz から（10 MHz は T12 の一定の線を足したとき）。止めるのはコア 1 だけ（Linux は ssh を受けるコア 0 で動かし続ける）。
- 実機で未確認の事実は「確認した」と書かない: JP1 の MODE3 のピン、Zynq の AP の並び（AHB-AP の有無）、A9 のデバッグの番地。
- 最初の計画に入れないもの（利用者の決定・私の判断）: UPROBES 付きカーネル、道 B、PTM トレース、PL の UART と nRESET（検証 1〜5 は JTAG + ロジアナで満たせるため。要るなら別計画）。

## Review Focus

1. DUT 回路を書いた直後、レベルシフタが無効のまま AXI に触ると Linux が固まる → 書いた後の有効化と、有効化前に `/dev/mem` を触らないことをローダの試験で固定する（Task 3）。
2. independent でない（MIO2 = 0、cascade）起動のとき、JTAG を繋いでも DAP が見えない → 試験は「IDCODE が見えない」を、配線の誤りでなく起動モードの案内として報告する（Task 5）。
3. A9 の停止中に Linux が応答しなくなる → コア 1 だけを止め、どの経路でも最後に再開する。再開後に ssh が通ることを試験の最後に確かめる（Task 5）。
4. FIFO が溢れたのに気づけない → 溢れは「回数」を数える固着でないカウンタで返し、読み出しでクリアしない（Task 1）。
5. 注目信号が起動の途中で浮く → 端子は PULLDOWN、回路は全信号を駆動する（Task 2）。

---

### Task 1: DUT のコア（サンプル生成 + FIFO + 不具合の仕込み）

**Files:**
- Create: `gateware/rtl/designs/dut_stream_core.vhd`
- Test: `gateware/tests/tb/dut_stream_core_tb.py`、`gateware/tests/test_dut_stream_core.py`

**Interfaces:**
- Produces: entity `dut_stream_core`（generic `FIFO_DEPTH : positive := 256`）。ポート: `clk, rst : in std_logic`、レジスタ口 `reg_wr_en : in std_logic`、`reg_addr : in unsigned(3 downto 0)`（語番地）、`reg_wdata : in std_logic_vector(31 downto 0)`、`reg_rd_en : in std_logic`、`reg_rdata : out std_logic_vector(31 downto 0)`、注目信号 `dbg : out std_logic_vector(5 downto 0)`。
- レジスタ（語番地）: 0 `CTRL`（bit0 = 生成の有効）、1 `PERIOD`（生成の間隔、クロック数、既定 1000）、2 `DATA`（読むと FIFO の先頭を 1 語取り出す。空なら 0xFFFFFFFF）、3 `LEVEL`（FIFO の語数、読み取り専用）、4 `OVERFLOWS`（溢れて捨てた数、読み取り専用、クリアしない）、5 `GENERATED`（生成した数）、6 `FAULT`（下記）、7 `ID`（0x445554_01 = "DUT" + 版 1）。
- 生成: 有効の間 `PERIOD` クロックごとに 1 語を FIFO に入れる。語の値は生成の通し番号（0 始まり）。FIFO が満杯なら捨てて `OVERFLOWS` を 1 増やす。
- 不具合の仕込み `FAULT`: bit0 = 有効、`bits[31:8]` = N。有効なら N 個に 1 個の生成を黙って捨てる（通し番号は進む = 受け取る側から見ると番号が飛ぶ）。0 なら正常。
- `dbg`: [0] 生成の拍（1 クロック）、[1] FIFO 満杯、[2] 溢れの拍、[3] DATA の読みの拍、[4] 不具合で捨てた拍、[5] 生成が有効のレベル。

- [ ] **Step 1: テストを書く** `tests/tb/dut_stream_core_tb.py` に次の cocotb テスト（assert はスペックの値で）:
  - `test_generates_sequence`: PERIOD=10、有効 → 100 クロック後 `LEVEL` = 9〜10、`DATA` を読むと 0,1,2,… の順。
  - `test_overflow_counts_and_does_not_clear_on_read`: FIFO_DEPTH=4、読まずに放置 → `OVERFLOWS` > 0、2 回読んでも同じ値。`dbg[2]` に溢れの拍が出る。
  - `test_fault_drops_every_nth`: FAULT=(N=5, 有効) → 取り出した列で 4→6 のように番号が飛ぶ（5 個に 1 個）。`dbg[4]` に拍が出る。
  - `test_empty_read_returns_all_ones`、`test_id_register`（`ID` = 0x44555401）。
  - `test_all_outputs_driven`: リセット後・停止中に `dbg` と `reg_rdata` が 'X' / 'U' にならない。
- [ ] **Step 2: 失敗を確認** Run: `.venv\Scripts\python -m pytest gateware/tests/test_dut_stream_core.py -v`（ラッパは `run_cocotb(toplevel="dut_stream_core", sources=rtl("common/util_pkg.vhd","common/sync_fifo.vhd","designs/dut_stream_core.vhd"), test_module="tests.tb.dut_stream_core_tb", generics={"FIFO_DEPTH": 4}, build_dir=tmp_path)`。既存の `tests/test_pynq_z2_blinky.py` と同じ形）。Expected: FAIL（entity が無い）。
- [ ] **Step 3: 実装** `dut_stream_core.vhd`。FIFO は既存の `common/sync_fifo.vhd` のポート（読んでから使う）。
- [ ] **Step 4: 通ることを確認** 同じコマンド。Expected: PASS。
- [ ] **Step 5: コミット** `git add gateware/rtl/designs/dut_stream_core.vhd gateware/tests/tb/dut_stream_core_tb.py gateware/tests/test_dut_stream_core.py && git commit -m "feat(dut): サンプル生成 + FIFO + 不具合の仕込みのコア"`

### Task 2: AXI-Lite の受け口 + トップ + 端子

**Files:**
- Create: `gateware/rtl/designs/dut_axil_slave.vhd`、`gateware/rtl/boards/pynq_z2/pynq_z2_dut_top.vhd`
- Modify: `gateware/fprobe_gw/boards/pynq_z2.py`（ピン群 `dut_dbg`）、`gateware/tests/test_boards_pynq_z2.py`
- Test: `gateware/tests/tb/dut_axil_slave_tb.py`、`gateware/tests/test_dut_axil_slave.py`

**Interfaces:**
- Consumes: Task 1 のレジスタ口と `dbg`。`ps7_bd_wrapper` の口（`rtl/boards/pynq_z2/pynq_z2_fprobe_top.vhd` の component 宣言をそのまま使う: `aclk, aresetn, pjtag_*, m_axi_*`）。PS の AXI は 0x40000000 から 4 KB。
- Produces: `dut_axil_slave`（AXI4-Lite のスレーブ → Task 1 のレジスタ口。語番地 = `addr(5 downto 2)`。読みの拍 `reg_rd_en` を AR の受理に 1 クロック出す。応答は常に OKAY）。`pynq_z2_dut_top`（ポート: `sys_clk_in`, `led`, `dut_dbg[5:0]`, `ps_pjtag_tck/tms/tdi/tdo`）。ボード定義にピン群 `dut_dbg`＝既存 `la_in` の 6 つの位置（W14 Y14 T11 T10 V16 W16）、プル DOWN、drive 8、ポート名 `dut_dbg[i]`。
- トップ: MMCM（`xilinx_mmcm.vhd`、fprobe のトップと同じ 125 → 100 MHz）、`ps7_bd_wrapper`、`dut_axil_slave`、`dut_stream_core`。PJTAG は `ps_pjtag_*` へ素通し。`led(0)` はハートビート、他は 0 に駆動。

- [ ] **Step 1: テストを書く** `dut_axil_slave_tb.py`: AXI-Lite の書き（AW と W を別々のクロックで）・読み（AR → R）のハンドシェイクを組み、`ID` が 0x44555401、`PERIOD` の書きが反映されること、`reg_rd_en` が `DATA` の読み 1 回につき 1 拍だけ出ることを assert。`test_boards_pynq_z2.py` に `test_dut_dbg_pins_use_the_jb_inputs`（`PYNQ_Z2.pin_groups["dut_dbg"]` の位置が `["W14","Y14","T11","T10","V16","W16"]`、プルがすべて DOWN）を足す。
- [ ] **Step 2: 失敗を確認** Run: `.venv\Scripts\python -m pytest gateware/tests/test_dut_axil_slave.py gateware/tests/test_boards_pynq_z2.py -v`。Expected: FAIL。
- [ ] **Step 3: 実装** 上記 3 ファイルとボード定義。使わない AXI の口（`awprot` 等）は読み捨てるだけでなく出力側は全部駆動する。
- [ ] **Step 4: 通ることを確認** 同じコマンド + `pytest gateware/tests/test_boards_pynq_z2.py`。Expected: PASS。
- [ ] **Step 5: コミット** `git commit -m "feat(dut): AXI-Lite の受け口・トップ・JB の注目信号の端子"`

### Task 3: デザインの登録 + ローダ（書いた後のレベルシフタ）

**Files:**
- Modify: `gateware/fprobe_gw/designs.py`（`Design.ps_axi: bool = False`、`DESIGNS["zynq_dut"]`）、`gateware/fprobe_gw/toolchain/pynq_loader.py`、`gateware/fprobe_gw/cli.py`（`_program_pynq`）、`host/pynq/fprobe-pl`、`host/pynq/fprobe-relay/main.c`（`--enable` を追加）
- Test: `gateware/tests/test_designs_pynq.py`、`gateware/tests/test_pynq_loader.py`

**Interfaces:**
- Produces: `DESIGNS["zynq_dut"]`（board `PYNQ_Z2`、top `pynq_z2_dut_top`、ピン群 `("clk125","led","dut_dbg","ps_pjtag")`、`capabilities=None`、`block_designs=(ps7_bd.tcl,)`、`ps_axi=True`、クロックは fprobe_pynq と同じ `input_clock` と `sys_clock`）。`PynqLoader.program(bitstream, *, relay: bool, design_id: str = "", shifter: bool = False)`: `shifter` が真なら書いた後に `sudo -n fprobe-pl enable` を SSH で呼ぶ。`fprobe-pl enable` = `fprobe-relay --enable`（SLCR 0xF8000900 に 0xF を書く。`--isolate` の対。解錠 0xDF0D → 書く → 施錠 0x767B）。

- [ ] **Step 1: テストを書く** `test_designs_pynq.py` に `test_zynq_dut_is_a_vivado_project_without_the_relay`: `c = DESIGNS["zynq_dut"]`、`c.capabilities is None`、`c.ps_axi`、ブロック設計名が `["ps7_bd.tcl"]`、ポートに `dut_dbg[0]`〜`[5]` と `ps_pjtag_tdo` が含まれ、ソースに `zynq_frame_link.vhd` と `fprobe_core.vhd` が**含まれない**。`test_pynq_loader.py` に `test_program_enables_the_level_shifter_after_writing_when_shifter`: 偽の `run` で ssh の呼び出しを記録し、順序が `fprobe-pl before` → vivado → `fprobe-pl enable` で、`relay=False` のとき `fprobe-pl after` が呼ばれないことを assert。`shifter=False` では `enable` を呼ばないことも assert。
- [ ] **Step 2: 失敗を確認** Run: `.venv\Scripts\python -m pytest gateware/tests/test_designs_pynq.py gateware/tests/test_pynq_loader.py -v`。Expected: FAIL。
- [ ] **Step 3: 実装** 上記。`cli._program_pynq` に `shifter=design.ps_axi` を渡す（呼び出し元 `build --program` と `program` の 2 か所を確認して揃える）。`fprobe-pl` に `enable` を足し、`main.c` に `--enable` を足す（`isolate()` と同じ形）。
- [ ] **Step 4: 通ることを確認** 同じコマンド + `pytest gateware/tests/test_cli.py gateware/tests/test_pynq_setup.py`。Expected: PASS。
- [ ] **Step 5: コミット** `git commit -m "feat(dut): zynq_dut の登録と、書いた後のレベルシフタ有効化"`

### Task 4: Vivado でビルド（実機の前の確認）

**Files:** なし（生成物は `gateware/build/zynq_dut/`）

- [ ] **Step 1: ビルド** Run: `.venv\Scripts\python -m fprobe_gw build zynq_dut`。Expected: 終了コード 0、`gateware/build/zynq_dut/top.bit` ができ、タイミング違反（WNS < 0）と EX1998 相当の「駆動されない配線」の警告が無い。失敗したら報告書（`build/zynq_dut/` の timing / drc）を読んで直す。
- [ ] **Step 2: 資源を記録** `python -m fprobe_gw resources zynq_dut` の出力を計画の実行記録（コミットメッセージ）に残す。
- [ ] **Step 3: コミット** ビルド結果は commit しない（`build/` は無視されている）。ここでのコミットは不要。

### Task 5: Zynq の部品表 + A9 の実機試験（検証 1〜3）

**Files:**
- Modify: `host/fprobe/arm_a_target.py`（`ARM_A_PARTS` に `zynq7000`）、`host/tests/test_arm_a.py`（部品表の試験）
- Create: `gateware/tests/hil/test_arm_a_zynq_hw.py`

**Interfaces:**
- Produces: `ArmAPart("zynq7000", "v7", 0x0BA00477, (0x80090000, 0x80092000), "Zynq-7000（Cortex-A9 × 2）")`。デバッグの番地は OpenOCD の zynq の設定と同じ値で、**実機で未確認**（コメントに書く。ROM テーブルが読めればそちらが優先される）。

- [ ] **Step 1: テストを書く** `test_arm_a.py` に `test_zynq7000_is_in_the_part_table`: `arm_a_part("zynq7000")` が `kind == "v7"`、`debug_bases == (0x80090000, 0x80092000)`、`idcode == 0x0BA00477`。HIL（`FPROBE_ZYNQ_JTAG=1` のときだけ走る。`test_arm_a_pi2_hw.py` と同じ形）:
  - `test_scan_finds_the_zynq_dap`: `jtag_scan`（`fprobe.jtag_chain.scan_chain`）の結果に IDCODE `0x4BA00477` が 1 台。見えなければ失敗メッセージに「PYNQ が independent（MIO2 = 1）で起動したか・DUT 回路が書かれているか・配線」を挙げる。
  - `test_ap_map_is_recorded`: DP に入り AP 0〜3 の IDR を読んで表に出す（`-s` で見る）。AHB-AP（クラス MEM-AP、バス AHB）の有無を `print` し、assert はしない（未確認の事実を調べる試験。結果は spec と docs/boards/pynq-z2.md に書き戻す）。
  - `test_halt_and_resume_core1_only`: `Probe().target_connect(target="zynq7000", frequency_hz=2_000_000, connect_mode="attach", core=1)`、`target_halt` → レジスタ読み → `target_resume`。最後に `FPROBE_ZYNQ_SSH`（`xilinx@192.168.77.2`）と `FPROBE_ZYNQ_KEY` で `uptime` が通ること。
  - `test_memory_read_while_halted`: コア 1 を止め、カーネルの線形の写しの頭（`0xC0008000`）を 64 語読めること。
- [ ] **Step 2: 失敗を確認** Run: `.venv\Scripts\python -m pytest host/tests/test_arm_a.py -v`。Expected: 新しい試験が FAIL。
- [ ] **Step 3: 実装** 部品表に 1 行。
- [ ] **Step 4: 通ることを確認** 同じコマンド。Expected: PASS。
- [ ] **Step 5: 実機（利用者の立ち会いが要る）**
  1. 利用者が配線（仕様書の「フェーズ 1」の 5 本）と JP1（MODE3、電源を切って）を済ませ、PYNQ を起動する。
  2. `python -m fprobe_gw program gateware/build/fprobe_jtag/top.fs`（Primer を JTAG の構成に。**明示して書く**）。
  3. `FPROBE_ZYNQ_JTAG=1 FPROBE_ZYNQ_SSH=xilinx@192.168.77.2 FPROBE_ZYNQ_KEY=~/.ssh/fprobe_pynq .venv\Scripts\python -m pytest gateware/tests/hil/test_arm_a_zynq_hw.py --hil -s -v`
  4. `python -m fprobe_gw build zynq_dut --program` で DUT 回路を書き、同じ試験を再実行する。
  Expected: PASS。AP の表と AHB-AP の有無を spec の「検証の基準 2」に書き戻す。
- [ ] **Step 6: コミット** `git commit -m "feat(arm_a): Zynq-7000 の部品表と実機試験"`

### Task 6: PYNQ 上のアプリ（観測される側の Linux プロセス）

**Files:**
- Create: `host/pynq/dut/dut_app.c`、`host/pynq/dut/Makefile`
- Test: `host/tests/test_dut_app_build.py`

**Interfaces:**
- Produces: C のアプリ `dut_app`。`/dev/mem` を 0x40000000 から 4 KB 写し、`CTRL` で生成を有効にし、`dut_consume(uint32_t v)`（`__attribute__((noinline))`。引数 = 取り出した語。ptrace のプローブを付ける関数）を FIFO から 1 語ずつ呼ぶ。引数: `--period <clk>`（既定 1000）、`--delay-us <n>`（1 語ごとの待ち。既定 0）、`--fault <n>`（FAULT の N。既定 0 = 正常）、`--seconds <n>`（実行時間）。終了時に `GENERATED`・読んだ数・`OVERFLOWS` を 1 行（`generated=%u consumed=%u overflows=%u`）で出す。`-marm` でビルドする（uprobe の将来の制約を避ける）。レジスタの番地は Task 1 の表と同じ（定数の源は一つ: `host/pynq/dut/dut_regs.h`）。

- [ ] **Step 1: テストを書く** `test_dut_app_build.py`: `gcc`（またはクロスの `arm-none-eabi-gcc` を `-fsyntax-only`）で構文を通すこと。ホストに適当な gcc が無ければ skip。`dut_regs.h` の語番地が `dut_stream_core.vhd` のコメントの表と一致することを、両方を読む文字列照合で assert（`ID = 0x44555401`、`OVERFLOWS` = 4 等）。
- [ ] **Step 2: 失敗を確認** Run: `.venv\Scripts\python -m pytest host/tests/test_dut_app_build.py -v`。Expected: FAIL。
- [ ] **Step 3: 実装** 上記。`--delay-us` を大きくすると読み手が遅れて `overflows > 0` になるのが、PL との競合の題材。
- [ ] **Step 4: 通ることを確認** 同じコマンド。Expected: PASS。
- [ ] **Step 5: 実機** PYNQ に `scp` して `make`（`-marm`）。`sudo ./dut_app --seconds 5` で `overflows=0`、`--delay-us 20000 --seconds 5` で `overflows>0` になること。
- [ ] **Step 6: コミット** `git commit -m "feat(dut): PYNQ 上の観測される側のアプリ"`

### Task 7: アプリと PL と A9 を同じ記録に並べる（検証 4・5）

**Files:**
- Create: `gateware/tests/hil/test_zynq_dut_correlation_hw.py`

**Interfaces:**
- Consumes: `linux.connect` / `linux.agent.deploy` / `linux.agent.start` / `linux.probe.add` / `linux.sync` / `linux.trace.start` / `linux.events.read`（docs/linux.md）、`fprobe.la.LogicAnalyzer`、`fprobe.xtrig.CrossTrigger`、Task 5 の `Probe` 接続。Primer の構成は fprobe_jtag（ロジアナ ch は JTAG の線と空きの端子）。
- 前提（Task 0 に相当、この試験の最初の確認として実施）: **JTAG の構成のロジアナ ch が、PL の注目信号を受けられる本数を持つか。** 持たなければ「J12 の T12 に DUT の `dbg[3]`（読みの拍）を 1 本だけつなぐ」へ縮め、残りの観測は fprobe_la の別実行で行う。結果を spec のリスクの節に書き戻す。

- [ ] **Step 1: 試験を書く**（`FPROBE_ZYNQ_JTAG=1` と `FPROBE_ZYNQ_LINUX=1` のときだけ走る。assert は事実だけ）:
  - `test_linux_calls_and_pl_reads_are_on_one_timeline`: `dut_consume` に ptrace のプローブを付け、アプリを `--seconds 3` で走らせる。同時にロジアナで `dbg[3]`（DATA の読みの拍）を捕捉し、Linux の出来事の数と PL の拍の数の差、および各 n 番目の時刻の差（時計の不確かさの範囲）を assert 付きで測る。不確かさ以上のずれが出たら、数字を出して失敗。
  - `test_overflow_is_found_from_facts_only`: `--delay-us 20000` で走らせ、PL の `dbg[2]`（溢れの拍）の回数が `OVERFLOWS` レジスタの値と一致すること。fprobe の出す事実（回数・時刻・間隔）から「アプリの呼び出しの間隔が長いときに溢れた」を人が読めること（試験は間隔の最大値と溢れの時刻が近いことを数字で assert。判定はしない）。
  - `test_cross_trigger_pl_to_a9`: ロジアナのトリガ（`dbg[2]` の立ち上がり）で `CrossTrigger` に A9 の停止要求を出せるか。出せない（Zynq の DAP は SWD の注入経路でない）場合は、この試験を `xfail(reason=...)` にして「JTAG の注入経路は未実装」を spec に書き戻す。実装する場合は別の計画にする。
  - `test_a9_breakpoint_to_la`: `dut_consume` のアドレスにハードのブレークポイント → 止まった時刻（A9 の停止の記録）と、止まる直前の PL の `dbg[3]` の拍が時刻順で並ぶこと。
- [ ] **Step 2: 実機で実行して失敗/結果を確認** Run: `FPROBE_ZYNQ_JTAG=1 FPROBE_ZYNQ_LINUX=1 ... pytest gateware/tests/hil/test_zynq_dut_correlation_hw.py --hil -s -v`。
- [ ] **Step 3: 足りない所を直す**（時刻の換算・ch の割り当て。fprobe 本体は変えない。Zynq 固有の処理は試験側に置く）。
- [ ] **Step 4: 通ることを確認** 同じコマンド。Expected: PASS（`xfail` の試験を除く）。測った時刻の誤差を spec の検証 4 に書き戻す。
- [ ] **Step 5: コミット** `git commit -m "test(dut): アプリ・PL・A9 を同じ記録に並べる実機試験"`

### Task 7b: PL の内部を fprobe で見る道の調査（利用者の指摘、2026-10-24）

PS の DAP（Task 5）は ARM 側だけ。PL の中は、基板の USB-JTAG（Vivado）でなく fprobe 自身の道（`fpga_obs`: 対象の PL に `target_ip/fprobe_obs` を入れ、RPi ヘッダ 15・16・18・22 番の JTAG で読む）で見る。**Primer の製品デザインには `fpga_obs` の端子・エンジンの受け口が無い**（`designs.py` の Primer 側は `obs_jtag` を持たない）ので、可能かどうかを先に決める。

**Files:** Modify: `docs/superpowers/specs/2026-10-24-zynq-dut-environment-design.md`（結論を書き戻す）。結論が「可能」なら別計画に切り出す。

- [ ] **Step 1: 調べる** `gateware/fprobe_gw/designs.py` の `fpga_obs` と `obs_jtag`（PYNQ のみ）、`gateware/rtl/core/fprobe_core.vhd` の観測エンジン（`obs_jtag_engine.vhd`）、Primer の空き端子（J12 の T12・R11・P11・M14）と資源（CLS 90% 台）を読み、次の 3 案の可否と費用を表にする: (a) Primer の JTAG 構成に観測の端子を 4 本足す、(b) JTAG の口を時分割で PS の DAP と PL の観測で使い分ける、(c) PL の中の信号は DUT 回路が端子へ出してロジアナで見る（今の計画）。
- [ ] **Step 2: 結論を利用者に報告** 採用案を spec に書く。(a)(b) を採るなら DUT 回路に `obs_core`/`obs_soft_tap` を入れる作業と、Primer の配線・資源の作業を別計画で立てる。

### Task 8: 仕込んだ不具合を特定する試験（検証 5）と文書

**Files:**
- Create: `gateware/tests/hil/test_zynq_dut_fault_hw.py`
- Modify: `docs/boards/pynq-z2.md`（DUT の節: 配線・JP1・手順・分かった事実）、`docs/linux.md`（Zynq の節）、`docs/superpowers/specs/2026-10-24-zynq-dut-environment-design.md`（実測の書き戻し）

- [ ] **Step 1: 試験を書く** `dut_app --fault 7`（7 個に 1 個を黙って捨てる）で走らせ、Linux の呼び出しの引数 `v` の列（ptrace の記録）と PL の生成の拍（`dbg[0]`）・捨てた拍（`dbg[4]`）から、事実だけで次を assert: 取り出した番号の欠けた位置の間隔が 7 の倍数であること、`dbg[4]` の拍の回数が欠けた回数と一致すること。判定ロジックは試験側（利用者・AI 側の仕事）に置き、fprobe 本体には入れない。
- [ ] **Step 2: 実行** 実機で実行し、通ること。
- [ ] **Step 3: 文書** 配線表・JP1 の確認結果・AP の並び・時刻の誤差・ロジアナ ch の割り当て・Primer の構成の結論を、実測値で書く。未確認のものは未確認のまま残す。
- [ ] **Step 4: コミット** `git commit -m "docs(dut): Zynq DUT の手順と実測"`

---

## Self-Review

- **仕様の網羅:** 目標 1（JTAG で A9）= Task 5、目標 2・3（ロジアナ・クロストリガ）= Task 7、目標 4（アプリと PL の突き合わせ）= Task 7・8、DUT 回路 = Task 1〜4、検証 1〜3 = Task 5、4 = Task 7、5 = Task 8。リスク「Primer の資源（JTAG + ロジアナの両立）」= Task 7 の前提。後の作業（UPROBES・道 B）は意図して外した。PL の UART と nRESET は仕様のフェーズ 2 の表にあるが、この計画では外した（検証を満たすのに不要。利用者に報告する）。
- **未確認の事実の扱い:** AHB-AP の有無、JP1、A9 のデバッグの番地は Task 5 で実測し、文書に書き戻す。
- **型の一貫性:** レジスタの語番地は Task 1 の表が源で、Task 2・6 は同じ番号を使う（Task 6 に照合の試験）。`ps_axi` / `shifter` / `fprobe-pl enable` は Task 3 で定義し Task 5 の手順が使う。
