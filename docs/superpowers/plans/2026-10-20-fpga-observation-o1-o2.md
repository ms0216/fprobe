# 対象の FPGA の観測（O1・O2）Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 対象の FPGA に入れる観測の回路の値の変化を、fprobe（PYNQ-Z2）が JTAG で途切れずに吸い出し、fprobe の時刻に直して MCU・ロジアナと同じ時間軸に並べる。

**Architecture:** 対象側は Apache-2.0 の `target_ip/fprobe_obs/`（ベンダー非依存の本体 `obs_core` + JTAG の口の包み。O1 は
ふつうの端子で JTAG を受ける `obs_soft_tap`）。fprobe 側は `fprobe_core` の中の `obs_jtag_engine`（generic `OBS_PORT`）が
専用の 4 本で JTAG を回し、受けた記録と時刻合わせの組を既存のイベント記録（種類 8・9）に入れる。PC は `fprobe.fpga_obs` が
イベントから値の列を組み立て、時刻を fprobe の時刻に直す。

**Tech Stack:** VHDL-2008（GHDL 6 + cocotb 2）、Vivado（PYNQ）、Gowin（確かめの相手の Primer）、Python 3.13。

**Spec:** docs/superpowers/specs/2026-10-20-fpga-observation-design.md（§10b・§11 も読む）

## Global Constraints

- `target_ip/fprobe_obs/` は **Apache-2.0**（LICENSE を置く）。gateware/（GPL-3.0）のファイルを使わない・写さない（非同期 FIFO 等は自前）。
- 機能は PYNQ-Z2 の構成だけ（`fprobe_pynq`・`fprobe_pynq_deep`）。Tang 系の構成の資源・タイミング・ピンを変えない（`OBS_PORT = false` の既定で回路が無い）。
- ケイパビリティ: FEATURES の新しいビット `FEATURE_FPGA_OBS = 1 << 7`。無い構成では PC の API・MCP が「この構成に無い」と答える。
- 対象の JTAG に出す命令は、選んだ装置の USER の命令・BYPASS・IDCODE だけ（コンフィグ用の命令・EXTEST を出さない）。
- VHDL の規約は CLAUDE.md のとおり（VHDL-2008、ASCII の文字列、numeric_std、同期のアクティブハイの `rst`、`S_` の状態名、すべての信号を明示的に駆動、すべての entity に cocotb の試験）。
- 目標の精度: fprobe の時刻への直しは 100 ns 以内（TCK 30 MHz のとき）。TCK は相手ごとに設定で落とせる。

## 決めた形（O1・O2。仕様 §12 で作る人に任された点の、この計画での決定）

**DR の形式**（対象の 1 つの USER の DR。下位から送る。長さはホストが毎回決める）:
- TDI の先頭 64 ビット = 命令 `op(8) | addr(8) | data(32) | 予約(16)`。**Update-DR で実行**する。
  op: 0 = NOP、1 = レジスタの書き（addr・data）、2 = レジスタの読み（結果は次のスキャンの見出し）、3 = 時刻の写し取り。
- TDO = 見出し 4 語 + 記録 M 語（**Capture-DR の時点で決まる**）:
  - 語 0: `count(16) | flags(16)`（count = この後の有効な記録の数、flags bit0 = FIFO が溢れたことがある）
  - 語 1: 直前の読みの結果（op 2）。語 2・3: 直前に写し取った対象の時刻（48 ビット: 語 2 = 下位 32、語 3 の下位 16 = 上位）
    その後に記録 M 語（有効でない語は 0）。
- **記録（32 ビット）**: `type(2) | 残り(30)`。type 0 = 値 `delta(14) | value(16)`（前の記録から delta 周期後に value に
  なった）、type 1 = 時刻だけ `delta(30)`（delta が 14 ビットに収まらないとき先に入れる）、type 2 = 溢れ `lost(30)`
  （FIFO が一杯で捨てた変化の数。次に入れられたときに 1 つ入れる）。
- **レジスタ**（op 1・2 の addr）: 0x00 ID（読み = 0x46_4F_42_53 "FOBS"）、0x01 VERSION（1）、0x02 WIDTH（16）、
  0x03 GROUPS（generic）、0x04 FIFO_DEPTH、0x05 CLOCK_HZ（generic、0 = 不明）、0x10 CONTROL（bit0 = 流し続けを始める）、
  0x11 SELECT（流し続ける組の番号）。

**fprobe 側のイベント**（既存のイベント記録の 128 ビット。`events.decode_events` の欄の名前で）:
- 種類 8（KIND_OBS_RECORD）: `data` = 記録 32 ビット、`address` = スキャンの番号（32 ビット、記録の並びの照合用）、`time` = そのスキャンの Update-DR の fprobe の時刻。
- 種類 9（KIND_OBS_SYNC）: `data` = 写し取った対象の時刻の下位 32、`address` = 上位 16（下位に）、`time` = 写し取りを命じた Update-DR の fprobe の時刻。

## Review Focus

1. 対象の回路が書き直されて消えた・JTAG の線が外れた（TDO が 0 か 1 に張り付く）: 申告（ID）が読めなくなったことで止め、CSR の状態で知らせる（黙って 0 の記録を流さない）。→ Task 3 `engine_stops_when_the_core_disappears`
2. 対象のクロックが止まる・遅い（変化の delta が長い・時刻だけの記録が続く）: 時刻だけの記録で途切れずに時間がつながる。→ Task 1 `long_gaps_insert_time_records`
3. FIFO の溢れ: 溢れの記録で数が分かり、その後の値も正しい。→ Task 1 `overflow_is_reported_with_the_lost_count`
4. スキャンの途中で fprobe が止まる（CONTROL を切る・リセット）: 対象の TAP を Test-Logic-Reset に戻し、線を手放す（Hi-Z）。→ Task 3 `disable_returns_the_tap_to_reset_and_releases_the_pins`
5. 時刻合わせで対象のカウンタが巻き戻る・クロックが変わる: 連続しない組で区間を分け、区間ごとに直線を当てる。→ Task 5 `sync_segments_split_on_discontinuity`

---

### Task 1: 観測の回路の本体 `obs_core`（対象側、Apache-2.0）

**Files:**
- Create: `target_ip/fprobe_obs/LICENSE`（Apache-2.0 の全文）、`target_ip/fprobe_obs/README.md`（入れ方・DR の形式・レジスタ）
- Create: `target_ip/fprobe_obs/rtl/obs_core.vhd`、`target_ip/fprobe_obs/rtl/obs_async_fifo.vhd`（自前の非同期 FIFO、グレイ符号）
- Test: `gateware/tests/tb/obs_core_tb.py`、`gateware/tests/test_obs_core.py`（ユーザーの DR の口を Python で駆動）
- Modify: `gateware/fprobe_gw/rtl.py`（`TARGET_IP_ROOT` = リポジトリの `target_ip/`）

**Interfaces:**
- Produces: entity `obs_core`（generic `GROUPS : positive := 1`、`FIFO_DEPTH : positive := 512`、`CLOCK_HZ : natural := 0`）。
  口: `clk`・`rst`（対象のクロックのドメイン）、`probes : in std_logic_vector(16 * GROUPS - 1 downto 0)`、ユーザーの DR
  `tck`・`tdi`・`tdo : out`・`sel`（この DR が選ばれている）・`capture`・`shift`・`update`（TCK のドメインの 1 周期の印、
  Xilinx BSCANE2 と同じ意味）・`tlr`（Test-Logic-Reset）。上の「決めた形」の DR とレジスタ。

- [ ] **Step 1: 失敗する試験を書く**: `id_and_registers`（op 2 で ID = 0x464F4253・VERSION 1・WIDTH 16）、
  `value_changes_become_records`（probes を 3 回変える → 記録 3 つ、delta と value が合う）、`long_gaps_insert_time_records`
  （20000 周期変えない → type 1 の記録が先に入り、delta の合計が周期数と一致）、`overflow_is_reported_with_the_lost_count`
  （FIFO_DEPTH = 8 で 20 回変えてから吸い出す → type 2 の lost = 捨てた数、その後の値が正しい）、`select_switches_the_group`
  （GROUPS = 2、SELECT = 1 で上の 16 ビットの変化だけ）、`time_latch_reports_the_target_time`（op 3 → 次の見出しの語 2・3）。
  TCK と clk は非同期（例: 10 MHz と 50 MHz）。
- [ ] **Step 2: 失敗を確かめる** — `pytest gateware/tests/test_obs_core.py -q`
- [ ] **Step 3: 実装**（clk 側で変化を記録に → `obs_async_fifo` → TCK 側で Capture-DR に見出し + M 語を詰める。M はスキャンの
  長さから: 送られた分だけ取り出す = Shift で 32 ビット送り終えるたびに次の語を FIFO から取る。取り出した語はスキャンが
  最後まで行かなくても失う（ホストは常に決めた長さで回す））
- [ ] **Step 4: 通す・コミット** — `feat(target_ip): 観測の回路 obs_core（Apache-2.0）`

### Task 2: ふつうの端子で JTAG を受ける `obs_soft_tap`（対象側）と、試験用の TAP の口

**Files:**
- Create: `target_ip/fprobe_obs/rtl/obs_soft_tap.vhd`（IEEE 1149.1 の TAP の状態機械・IR 4 ビット（USER = 0x2、IDCODE = 0x1、BYPASS = 0xF）・IDCODE の値は generic。TCK でそのまま動く）
- Test: `gateware/tests/tb/obs_soft_tap_tb.py`、`gateware/tests/test_obs_soft_tap.py`

**Interfaces:**
- Consumes: Task 1 の `obs_core` のユーザーの DR の口（そのまま差す）
- Produces: entity `obs_soft_tap`（generic `IDCODE : std_logic_vector(31 downto 0) := x"0F0B5001"`）。口: `tck`・`tms`・`tdi`・`tdo`・`tdo_oe`（端子）と、`obs_core` 用のユーザーの DR の口。

- [ ] **Step 1: 失敗する試験**: `idcode_after_reset`（TLR → Shift-DR で IDCODE）、`bypass_is_one_bit`、`user_dr_reaches_the_core`（IR = USER で obs_core の ID が読める）
- [ ] **Step 2〜4: 失敗を確かめる → 実装 → 通す・コミット** — `feat(target_ip): obs_soft_tap（ふつうの端子の JTAG）`

### Task 3: fprobe 側の `obs_jtag_engine`（gateware、GPL）

**Files:**
- Create: `gateware/rtl/debug/obs_jtag_engine.vhd`
- Modify: `gateware/fprobe_gw/csr/fprobe_map.py`（ブロック `obs` 0x0E00、0x40）、`gateware/rtl/core/fprobe_core.vhd`（generic `OBS_PORT : boolean := false`、口 `obs_tck`・`obs_tms`・`obs_tdi`・`obs_tdo`・`obs_oe`、イベント 8・9）
- Create: `gateware/tests/rtl/obs_harness.vhd`（engine ↔ `obs_soft_tap` ↔ `obs_core`。TDO の張り付きを入れられる）
- Test: `gateware/tests/tb/obs_engine_tb.py`、`gateware/tests/test_obs_engine.py`

**Interfaces:**
- Consumes: Task 1・2 の entity と DR の形式
- Produces: CSR `obs`: `CONTROL`（bit0 enable、bit1 = 1 で線を手放す）、`HALF_PERIOD`（TCK の半周期、sys の周期数）、
  `CHAIN`（`ir_len(6) | ir_before(6) | ir_after(6) | dr_before(3) | dr_after(3)`）、`USER_IR`（対象の USER の命令の値）、
  `WORDS`（1 スキャンの記録の語数 M、1〜256）、`SYNC_PERIOD`（時刻合わせの間隔、スキャンの数）、`CMD_LO`・`CMD_HI`
  （次のスキャンに載せる命令。書くと 1 つ積む、深さ 4）、`STATUS`（`running | id_ok | stuck | cmd_pending | overflow_seen`）、
  `SCANS`、`READ_VALUE`（最後の op 2 の結果）、`ID`（最後に読めた ID）。イベント 8・9（上の形）。

- [ ] **Step 1: 失敗する試験**: `records_flow_into_events`（対象の probes を変える → イベント 8 の data が記録の列、
  address が連番）、`sync_pairs_are_recorded`（SYNC_PERIOD ごとにイベント 9、対象の時刻が増える）、`register_read_via_cmd`
  （CMD で op 2 SELECT → READ_VALUE）、`engine_stops_when_the_core_disappears`（TDO を 1 に張り付け → `stuck`・止まる・
  イベントを出さない）、`disable_returns_the_tap_to_reset_and_releases_the_pins`（CONTROL = 0 → TMS 1 を 5 回以上 → `obs_oe` = 0）
- [ ] **Step 2〜4: 失敗を確かめる → 実装（開始時に TLR → IR = USER（鎖の位置で BYPASS を詰める）→ ID を読む → 合えば
  スキャンを繰り返す。TCK のタイミングは `jtag_phy` と同じ）→ 通す・コミット** — `feat(debug): obs_jtag_engine`

### Task 4: PYNQ の構成と、ケイパビリティ

**Files:**
- Modify: `gateware/fprobe_gw/caps.py`（`FEATURE_FPGA_OBS`、`Capabilities.fpga_obs: bool = False`、生成パッケージに `OBS_PORT`）、
  `gateware/fprobe_gw/boards/pynq_z2.py`（ピンの組 `obs_jtag`: 4 本。Raspberry Pi のヘッダの空きから、基板の抵抗を回路図で
  確かめて選ぶ。docs/boards/pynq-z2.md に表を足す）、`gateware/fprobe_gw/designs.py`（`_pynq` の 2 構成で `fpga_obs=True`）、
  `gateware/rtl/boards/pynq_z2/pynq_z2_fprobe_top.vhd`、`host/fprobe/caps.py`・`generated.py`（gen）
- Test: `gateware/tests/test_designs_pynq.py`（`fpga_obs` が PYNQ の構成だけ真）、`gateware/tests/test_caps.py`

- [ ] **Step 1〜3: 失敗する試験 → 実装 → 通す**（Tang の構成は `fpga_obs = False` のまま、生成パッケージの `OBS_PORT = false`）
- [ ] **Step 4: ビルド** — `python -m fprobe_gw build fprobe_pynq`（タイミングを満たす。資源を台帳に）。Primer の `fprobe` もビルドして変わらないこと。
- [ ] **Step 5: コミット** — `feat(pynq): FPGA 観測の口（ケイパビリティ FPGA_OBS）`

### Task 5: PC 側 `fprobe.fpga_obs`

**Files:**
- Create: `host/fprobe/fpga_obs.py`、`host/tests/test_fpga_obs.py`
- Modify: `host/fprobe/events.py`（`KIND_OBS_RECORD = 8`、`KIND_OBS_SYNC = 9`）、`host/api/fprobe_api.json` と MCP（`fpga_obs.*`: open・select・start・stop・read・register_read・register_write）

**Interfaces:**
- Produces: `class FpgaObs(device)`: `open(chain: dict, user_ir: int, half_period: int, words: int = 64, sync_period: int = 100) -> dict`（申告を返す）、
  `register_read(addr) -> int`、`register_write(addr, value)`、`select(group)`、`start()`・`stop()`、
  `samples(events) -> list[ObsSample]`（`ObsSample(time_fprobe: float, target_cycles: int, value: int)`）、
  `fit_sync(sync_events) -> list[SyncSegment]`（区間ごとの `(target0, fprobe0, ratio)`）。

- [ ] **Step 1: 失敗する試験**（偽のイベントで）: `records_rebuild_the_value_sequence`（type 0・1 の delta の積み上げ）、
  `overflow_records_are_reported_as_gaps`、`sync_fit_maps_target_time_to_fprobe_time`（ずれ 50 ppm の組から直線、誤差 < 1 周期）、
  `sync_segments_split_on_discontinuity`（対象の時刻が巻き戻る組で区間が分かれる）、`missing_feature_is_a_clear_error`
  （FEATURE_FPGA_OBS が無い → 「この構成に無い」）
- [ ] **Step 2〜4: 失敗を確かめる → 実装 → 通す・コミット** — `feat(host): fpga_obs（FPGA の観測の値を fprobe の時刻で）`

### Task 6: 実機の確かめ（O1・O2）— 相手は Primer の J12 の `obs_soft_tap`

**Files:**
- Create: `target_ip/fprobe_obs/examples/primer20k_counter/`（Primer の試験の回路: カウンタ・状態機械を probes に、
  `obs_soft_tap` を J12 の 4 本に、カウンタの 1 ビットを J14 の 1 本にも出す）と、そのビルドの構成（`fprobe_gw` の構成
  `obs_target_primer`）
- Create: `gateware/tests/hil/test_fpga_obs_hw.py`
- Modify: `docs/boards/pynq-z2.md`（FPGA 観測の口の配線）、台帳

- [ ] **Step 1: 配線（利用者）**: PYNQ の FPGA 観測の口（Task 4 で決めた 4 本 + GND）→ Primer の J12。Primer のカウンタの
  1 ビットの J14 の端子 → PYNQ の PMOD JB の 1 本（ロジアナの専用の入力、時刻の照合用）。両方 3.3 V。電源の順は CLAUDE.md。
- [ ] **Step 2: O1**: `obs_target_primer` を Primer に（SRAM）→ `FpgaObs.open` で ID → 流し続けて 10 秒、カウンタの値の列に欠けが無い（溢れ 0）。TCK を 1・10・30 MHz で、流せた量を記録。
- [ ] **Step 3: O2**: 同じカウンタのビットを、fprobe のロジアナ（JB）と観測の回路の両方で見て、fprobe の時刻に直した立ち上がりの差が 100 ns 以内（TCK 30 MHz）。
- [ ] **Step 4: MCU と並べる**: XIAO の DAP の出来事（サンプラ）と FPGA の観測を同じ記録に入れて、時間軸に並ぶ（PC の記録の API）。
- [ ] **Step 5: 台帳・資料を更新してコミット** — `docs: FPGA 観測（O1・O2）の実機の結果`

---

## 自分で確かめたこと（Self-Review）

- 仕様の対応: ゴール 1（Task 1・3・6）、2（O3、別の計画）、3（Task 3・5・6）、4（Task 1 の SELECT。保存の条件などは O3）、
  5（O3）、6（Task 5）。§6（Task 3・4）、§7（Task 3・5・6）、§8（Task 6 Step 4。線なしのクロストリガは O3）、§10b の鎖の
  位置・BYPASS・ID の照合（Task 3）、線を手放す（Task 3）、§11 のライセンス（Task 1）。
- 名前の一貫: `obs_core`・`obs_soft_tap`・`obs_jtag_engine`・`FEATURE_FPGA_OBS`・`KIND_OBS_RECORD/SYNC`・`FpgaObs`。
- 量: 仕様と同程度。DR の形式とレジスタは、試験と実装の両方の口になるので決めて書いた。
