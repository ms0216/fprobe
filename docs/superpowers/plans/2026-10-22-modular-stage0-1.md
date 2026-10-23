# モジュール化 段階 0・1 実装計画

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Primer の機能ごとの資源と速さの余裕を測って利用者に見せ（段階 0）、2 枚のメインボード（PYNQ-Z2 = 親、Primer = 子）の時刻を同期パルス + 時刻の値でそろえて 1 本の時間軸にする（段階 1）。

**Architecture:** 段階 0 はビルドの報告を読む道具（Gowin の階層別の合成の報告・Vivado の階層別の使用量・Fmax）と、機能を絞った構成の試しのビルド。段階 1 はボード非依存の RTL `sync_link`（親は周期のパルスと 64 ビットの時刻を 1 本の線に、子は受けた瞬間の自分の時刻と親の値の組を出来事の種類 10 に）と、ホストの組からの換算（既存の `fpga_obs._fit`）・2 つの記録の合成。

**Tech Stack:** VHDL-2008（GHDL 6 + cocotb 2）、Python 3.13（pytest）、Gowin EDA（Primer）、Vivado（PYNQ）。

**Spec:** `docs/superpowers/specs/2026-10-22-modular-design.md`（§3.2・§3.3・§5 段階 0・1）

## Global Constraints

- CLAUDE.md の VHDL 規約: VHDL-2008、`report` の文字列は ASCII、`ieee.numeric_std`、同期・アクティブハイの `rst`、状態名は `S_`、**すべての信号を明示的に駆動する**（EX1998 があればビルド失敗）、すべての entity に cocotb の試験。
- ボード非依存のコアとボード固有（ピン・PLL）を分ける。ベンダーのプリミティブはラッパー越し。
- 本体にヒューリスティックを入れない: 不確かさは測った事実（量子化 ±1 刻み・当てはめの残差の最大・最も近い組までの距離）だけ。配線の遅れは利用者が明示する引数（既定なし。渡さなければ「配線の遅れは含まない」と結果に書く）。
- 時刻の換算は PC でする（FPGA に傾きの推定・乗算を入れない）。子の FPGA は組を出すだけ。
- 段階 1 の合格: 同じ信号を 2 枚のロジアナに入れ、合成した記録の上の立ち上がりの時刻の差が **±2 刻み（20 ns）以内**（設計で決まる一定の遅れ = 同期化器の段数は差し引く。値は Task 3 で決める定数）。
- 書き込み: PYNQ は `fprobe_gw build/program --board pynq_z2`（シリアル `1234-TUL`・IDCODE の照合・`fprobe-pl before`）。Primer は `fprobe_gw program`（シリアル `FACTORYAIOT_PRO`）。Flash でなく SRAM（JTAG）に書く。
- 配線は利用者がする。指示には電源の順・電圧の範囲・逆流を書く（3.3 V 同士・GND を先に・電源の 3.3 V はつながない・両方の電源が入ってから信号線）。

## Review Focus

1. 親の線が来ない・途中で切れる（子の電源だけ入っている、線が抜けた）: 子は組を出さず、黙って古い換算を使い続けない。デーモンは「組が無い区間」を欠けとして返す → Task 5 の試験 `test_gap_in_pairs_is_reported`。
2. 直列の値が化けた（雑音・ジャンパの接触）: CRC が合わなければ組を捨てて数える（errors）。化けた値で換算しない → Task 3 の試験 `crc_mismatch_drops_pair`、Task 5 の `test_errors_are_counted_not_used`。
3. 親と子のクロックの速さの違い（±50 ppm 程度）で、1 ms の間に数刻ずれる: 傾きで直す。組 1 つだけでは傾きが分からないので、換算を返さず「組が 2 つ以上要る」と返す → Task 5 の `test_single_pair_has_no_slope`。
4. 48 ビットの子の時刻の折り返し（100 MHz で約 32 日）: 既存の `unwrap48` を通す → Task 4 の試験で折り返しの直前・直後の組。
5. 一方の記録だけが長い（親を先に止めた）: 合成は重なる区間だけを換算し、外は「換算できない」と数えて返す → Task 6 の `test_child_events_outside_pairs_are_counted`。

---

## 段階 0: 資源と速さの余裕を測る

### Task 1: ビルドの報告から階層別の資源と Fmax を読む

**Files:**
- Create: `gateware/fprobe_gw/toolchain/resources.py`
- Modify: `gateware/fprobe_gw/toolchain/vivado.py`（`report_utilization -hierarchical -file util_hier.rpt` を足す。:139 の後）
- Modify: `gateware/fprobe_gw/__main__.py`（サブコマンド `resources <design> [--variant pN]`）
- Test: `gateware/tests/test_resources.py`（試験用の報告の切れ端を `gateware/tests/data/` に置く）

**Interfaces:**
- Produces: `@dataclass(frozen=True) class ModuleUse: path: str; lut: int; ff: int; alu: int; bsram: int`、`read_gowin_hierarchy(rsc_xml: Path) -> list[ModuleUse]`（`impl/gwsynthesis/project_syn_rsc.xml` の `<Module>`/`<SubModule>`）、`read_vivado_hierarchy(rpt: Path) -> list[ModuleUse]`、`read_fmax(design_dir: Path) -> dict[str, float]`（クロック名 → MHz。Gowin は配置配線の時間の報告の Max Frequency の節、Vivado は WNS から `1000 / (period_ns - wns)`）。

- [ ] **Step 1: 試験** `test_gowin_hierarchy_sums_to_the_top`: 試験用の XML（2 段の SubModule）で、子の合計 + 自分の分 = 親の総計、`path` は `fprobe_core/la_front` の形。`test_fmax_from_the_timing_report`: 試験用の報告の切れ端から `{"sys_clk": 118.3}` 等。`test_vivado_hierarchy_table`。
- [ ] **Step 2: 失敗を確かめる** `.venv/Scripts/python -m pytest gateware/tests/test_resources.py -q` → FAIL（モジュールが無い）
- [ ] **Step 3: 実装**（XML は `xml.etree`。実物の報告の形は `build/fprobe/p1/impl/` の既存のファイルを見て合わせる）
- [ ] **Step 4: 通るのを確かめる**（同じコマンド → PASS）
- [ ] **Step 5: コミット** `feat(toolchain): ビルドの報告から階層別の資源と Fmax を読む`

### Task 2: 測って表にする（利用者に見せる）

**Files:**
- Modify: `gateware/fprobe_gw/designs.py`（構成 `fprobe_core_m` を足す: 案 M の機能 = SWD・JTAG・サンプラ・ロジアナ（深いバッファ）・SWO（NRZ）・UART・クロストリガ・FPGA 観測、を残し、故障注入・多段トリガ・aux・Manchester の SWO を外す。今の caps のフラグで外せないものは外さず表に「外せない」と書く）
- Create: `docs/reports/2026-10-22-primer-resources.md`

- [ ] **Step 1:** `python -m fprobe_gw build fprobe` と `python -m fprobe_gw build fprobe_core_m`（Primer。書き込まない）。
- [ ] **Step 2:** `python -m fprobe_gw resources fprobe` と `... fprobe_core_m` の出力（モジュールごとの LUT・FF・ALU・BSRAM、Fmax）を報告に表で書く: 列 = モジュール・LUT・FF・BSRAM・「メインボードに残す / サブボードに出せる / 出せない（1 クロックで連動）」（分類は仕様 §1 の Primer の余裕の節の基準）。最後に 2 つの構成の全体の使用率と sys の Fmax の比較。
- [ ] **Step 3:** 試験 `.venv/Scripts/python -m pytest gateware/tests/test_designs.py gateware/tests/test_generated.py -q` → PASS（構成を足しても生成物が一致）。
- [ ] **Step 4: コミット** `docs(report): Primer の機能ごとの資源と Fmax（モジュール化の段階 0）`
- [ ] **Step 5: 利用者に表を見せて、残す・出すを決めてもらう**（この計画の範囲はここまで。決まった内容で構成を作り直すのは別の計画）。

## 段階 1: 2 枚のメインボードの時刻をそろえる

### Task 3: RTL `sync_link`（ボード非依存）

**Files:**
- Create: `gateware/rtl/core/sync_link.vhd`
- Create: `gateware/tests/tb/sync_link_tb.py`、`gateware/tests/test_sync_link.py`（`run_cocotb(toplevel="sync_link", ...)`。`test_timebase.py` と同じ作法）

**Interfaces:**
- Produces: entity `sync_link`
  - generic: `BIT_TICKS : positive := 10`（直列の 1 ビットの刻み = 10 Mbit/s）
  - ports: `clk, rst : in std_logic`; `time_count : in unsigned(63 downto 0)`;
    `enable, master : in std_logic`; `period : in unsigned(31 downto 0)`（親のパルスの間隔の刻み。CSR から。既定 100_000 = 1 ms）;
    `sync_in : in std_logic`（非同期。中で `sync_bit` を通す）; `sync_out : out std_logic`;
    `pair_valid : out std_logic`; `pair_local : out unsigned(63 downto 0)`（子: パルスの立ち上がりを見た刻み）;
    `pair_parent : out unsigned(63 downto 0)`; `crc_errors : out unsigned(15 downto 0)`
  - 定数 `RX_LATENCY_TICKS : natural := 3`（`sync_bit` の 2 段 + 立ち上がりの検出 1 段。パッケージ `sync_link_pkg` に置き、ホストへ caps で申告）
- 線の形（親）: アイドルは 0。`period` 刻みごとに、立ち上がりの刻みの `time_count` を T として保持し、線を 1 にして `BIT_TICKS` 保つ（パルス = スタートの印）→ T の 64 ビットを下位から各 `BIT_TICKS` → CRC-8（多項式 0x07、初期値 0、T の 8 バイトを下位から）8 ビット → 0。`master = '0'` なら `sync_out` は常に 0。
- 子: 同期化した入力の立ち上がりで、その刻みの `time_count` を保持し、各ビットの中央（立ち上がりから `BIT_TICKS * (k + 1) + BIT_TICKS / 2` 刻み）で 72 ビットを読む。CRC が合えば `pair_valid` を 1 刻み、`pair_local` = 保持した刻み、`pair_parent` = T。合わなければ `crc_errors` を 1 増やし、出さない。`enable = '0'` なら何もしない。

- [ ] **Step 1: 試験**（cocotb、親と子を同じ試験の中で 2 つ置き、親の `sync_out` を子の `sync_in` に、子の `time_count` は親から一定の差 + 少し速いクロックで作る）:
  - `pair_matches_the_parent_time`: 3 回の組で、`pair_parent` = 親が立ち上げた刻みの `time_count`、`pair_local` = 子の受けた刻み（親の立ち上げから `RX_LATENCY_TICKS` 後の子の時刻）。
  - `crc_mismatch_drops_pair`: 直列の途中の 1 ビットを反転させる → `pair_valid` が出ず `crc_errors` = 1。
  - `idle_without_master_and_disabled`: `master = '0'` で `sync_out` が 0 のまま、`enable = '0'` で組が出ない。
- [ ] **Step 2: 失敗を確かめる** `.venv/Scripts/python -m pytest gateware/tests/test_sync_link.py -q` → FAIL
- [ ] **Step 3: 実装**（状態 `S_IDLE`・`S_PULSE`・`S_BITS`・`S_CRC` を送受でそれぞれ。すべての出力をリセットで駆動）
- [ ] **Step 4: 通るのを確かめる**（同じコマンド → PASS）
- [ ] **Step 5: コミット** `feat(rtl): sync_link（同期パルスと時刻の値の送受信。モジュール化の段階 1）`

### Task 4: コアへの組み込み（出来事の種類 10・CSR・ケイパビリティ・ピン）

**Files:**
- Modify: `gateware/fprobe_gw/csr/fprobe_map.py`（ブロック `sync` を 0x0F00: `CTRL`（bit0 `enable`・bit1 `master`、RW）、`PERIOD`（32 ビット、RW、既定 100000）、`STATUS`（`pairs` 16 ビット・`crc_errors` 16 ビット、RO））
- Modify: `gateware/fprobe_gw/caps.py`（`FEATURE_SYNC` = bit9、フィールド `sync: bool`、`sync_rx_latency_ticks` を HOST_CONSTANTS に）、`host/fprobe/caps.py`（:148-156 の並びで読む）
- Modify: `gateware/rtl/core/fprobe_core.vhd`（generic `SYNC : boolean`、ポート `sync_in`・`sync_out`、`sync_link` を `if SYNC generate`、無い構成は `sync_out <= '0'` と `csr_absent`。出来事の種類 10: 時刻 [47:0] = `pair_local` の下位 48、a = `pair_parent(31 downto 0)`、b = `pair_parent(63 downto 32)`。aux（:975-990）と同じ「保持 → 空いた周期に出す」形で、優先は 8・9 の後）
- Modify: ボード層: `gateware/fprobe_gw/boards/primer20k.py`（資源グループ `sync`: J6 = pmod2 の 1 番 B12 = `sync_in`・2 番 C12 = `sync_out`）、`gateware/fprobe_gw/boards/pynq_z2.py`（資源グループ `sync`: RPi ヘッダの GPIO16 = `sync_in`・GPIO20 = `sync_out`。番地は TUL の master XDC の `rpio_16_r`・`rpio_20_r` の行）、両トップ、`designs.py`（`fprobe_la` と `fprobe_pynq_deep` に `sync=True` と `pin_groups` に `sync`）
- Modify: `host/fprobe/events.py`（`KIND_SYNC = 10`）、`host/fprobe/collector.py`（`_STREAMS` / `_KINDS`: stream `sync`、kind `pair`、t = 子の時刻（`unwrap48`）、body `{"parent": a | b << 32}`）
- Test: `gateware/tests/test_fprobe_core.py`（`CONFIGURATIONS` に `sync` を持つ組を 1 つ足し、外から `sync_in` にパルスと値を入れて出来事の種類 10 が出る）、`host/tests/test_collector.py`（種類 10 の解読・`unwrap48` の折り返しの直前・直後の組）

- [ ] **Step 1: 試験を書く**（上の 2 つ）
- [ ] **Step 2: 失敗を確かめる** `.venv/Scripts/python -m pytest gateware/tests/test_fprobe_core.py -k sync host/tests/test_collector.py -q` → FAIL
- [ ] **Step 3: 実装** → `python -m fprobe_gw gen`（生成物）
- [ ] **Step 4: 通るのを確かめる** `.venv/Scripts/python -m pytest gateware -q -m "not hil"` と `pytest host -q` → PASS
- [ ] **Step 5: ビルド** `python -m fprobe_gw build fprobe_la` と `--board pynq_z2 fprobe_pynq_deep`: タイミングを満たし、EX1998 が無い（満たさなければ `-place_option` の別の版。満たす版が無ければ止めて報告）
- [ ] **Step 6: コミット** `feat(core): 同期の送受信を出来事の種類 10・CSR sync・ケイパビリティ sync で（Primer J6・PYNQ の RPi ヘッダ）`

### Task 5: ホスト: 組から親の時刻へ換算する（事実の不確かさつき）

**Files:**
- Create: `host/fprobe/timesync.py`
- Test: `host/tests/test_timesync.py`

**Interfaces:**
- Consumes: `fprobe.fpga_obs._fit(pairs) -> (x0, y0, ratio)`（最小二乗の直線。今は private なので `fpga_obs.fit_line` として公開名を足し、`_fit` はそれを呼ぶ）
- Produces:
  - `@dataclass class ParentTime: local0: int; parent0: float; ratio: float; residual_max: float; pairs: int`
  - `fit_parent(pairs: list[tuple[int, int]]) -> ParentTime | None`（組 = (子の刻み, 親の刻み)。2 組未満は None）
  - `to_parent(fit: ParentTime, local: int, pair_locals: Sequence[int]) -> dict`: `{"t": 親の刻み（四捨五入した int）, "quantization_ticks": 1, "residual_max_ticks": fit.residual_max, "nearest_pair_ticks": 最も近い組までの刻み, "cable_delay": None}`
  - `segments(pairs, gap_ticks: int | None) -> list[list[tuple[int, int]]]`: 組の間が `gap_ticks`（使う側が明示。None なら分けない）を超えたら区間を分ける（線が抜けた等）。

- [ ] **Step 1: 試験**: `test_fit_recovers_offset_and_ratio`（子 = 親 × 1.00002 + 12345 の組 10 個 → ratio・換算が ±1 刻み）、`test_single_pair_has_no_slope`（1 組 → None）、`test_uncertainty_is_facts`（返す鍵が上の 5 つだけで、`cable_delay` は None）、`test_gap_in_pairs_is_reported`（`gap_ticks` を渡すと区間が 2 つ、渡さなければ 1 つ）、`test_errors_are_counted_not_used`（CSR の `crc_errors` は換算に使われない: 換算は組だけから）
- [ ] **Step 2: 失敗を確かめる** `.venv/Scripts/python -m pytest host/tests/test_timesync.py -q` → FAIL
- [ ] **Step 3: 実装**
- [ ] **Step 4: 通るのを確かめる** → PASS、`pytest host -q` も PASS
- [ ] **Step 5: コミット** `feat(timesync): 同期の組から親の時刻へ換算（不確かさは事実だけ）`

### Task 6: 2 つの記録を 1 本の時間軸に合成する

**Files:**
- Create: `host/fprobe/merge.py`
- Modify: `host/fprobe/__main__.py`（`python -m fprobe merge <親の記録> <子の記録> --name <子の名前> --out <新しい記録> [--cable-delay-ns N] [--gap-ms M]`）
- Test: `host/tests/test_merge.py`

**Interfaces:**
- Consumes: Task 5 の `fit_parent`・`to_parent`・`segments`、`fprobe.recording.Recorder`・`Recording`
- Produces: `merge(parent: Path, child: Path, out: Path, child_name: str, cable_delay_ticks: int | None = None, gap_ticks: int | None = None) -> dict`（`{"pairs": n, "converted": n, "outside": n, "segments": n, "uncertainty": {...}}`）。新しい記録は親の出来事をそのまま、子の出来事は stream を `"<child_name>.<stream>"` にし、時刻を親の刻みに換算（配線の遅れを渡せば足す）、`uncertainty` に換算の不確かさ（量子化 + 残差の最大）。子の波形のかたまり（ロジアナ）も `t0` を換算して入れる（傾きは 1 刻みあたりの比で `ticks_per_sample` に掛ける）。
- 組（stream `sync`）は子の記録から取る。

- [ ] **Step 1: 試験**: `test_child_events_move_to_the_parent_time`（合成で、子の出来事の時刻 = 換算の値、stream に名前が付く）、`test_child_events_outside_pairs_are_counted`（組の範囲の外の出来事は換算せず `outside` に数え、入れない）、`test_waveform_chunks_are_converted`（ロジアナのかたまりの t0 と刻みの比）、`test_cable_delay_is_explicit`（渡さなければ記録の meta に「配線の遅れは含まない」）
- [ ] **Step 2: 失敗を確かめる** → FAIL
- [ ] **Step 3: 実装**
- [ ] **Step 4: 通るのを確かめる** → PASS、`pytest host -q` も PASS
- [ ] **Step 5: コミット** `feat(merge): 2 つの記録を同期の組で 1 本の時間軸に合成`

### Task 7: 実機で確かめる（利用者の配線が要る）

**Files:**
- Create: `gateware/tests/hil/test_sync_two_boards_hw.py`（`FPROBE_PYNQ_HOST`・`FPROBE_PRIMER_SERIAL` が無ければ飛ばす。`hil/test_fpga_obs_hw.py` の 2 台の作法）

- [ ] **Step 1: 利用者に配線を頼む**（文面に電源の順・電圧・逆流を書く）: 両方の電源を入れてから、GND を先に、PYNQ の RPi ヘッダ GPIO20（親の `sync_out`）→ Primer J6 の 1 番（子の `sync_in`）、GND 同士。試験の信号: Shrike の GPIO26（要求中の印。今 PYNQ の JB1 = ch10 につながっている）を分岐して Primer J14 の 7 番（la_in0）にも。どれも 3.3 V 同士、電源の 3.3 V はつながない。
- [ ] **Step 2: 書き込み**: PYNQ に `fprobe_pynq_deep`、Primer に `fprobe_la`（SRAM）。
- [ ] **Step 3: 試験** `test_two_boards_share_one_time`: 2 つのデーモン（PYNQ = Ethernet、Primer = USB）で同時に 10 秒記録（両方のロジアナを流す）→ `merge` → GPIO26 の立ち上がりの時刻の差（親の ch10 と、子の la_in0 を換算したもの、子の `RX_LATENCY_TICKS` を差し引く）がすべて ±2 刻み以内。差の分布（最小・最大・中央値）を出力。
- [ ] **Step 4: 走らせる** `FPROBE_PYNQ_HOST=192.168.77.2 FPROBE_PRIMER_SERIAL=FACTORYAIOT_PRO .venv/Scripts/python -m pytest gateware/tests/hil/test_sync_two_boards_hw.py --hil -s` → PASS（満たさなければ差の分布を報告し、原因を切り分ける）
- [ ] **Step 5: コミット** `test(hil): 2 枚のメインボードの時刻の同期（±20 ns）`、残務の表の R15 に段階 0・1 の結果を書く。
