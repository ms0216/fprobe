# Shrike-Lite の C のデモ 実装計画

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Shrike-Lite を、MCU（Zephyr の C、ELF つき）と FPGA の連携の不具合 4 種を実行中に切り替えて fprobe で調べられる題材にする。

**Architecture:** FPGA の演算器に MODE 等のレジスタを足し、題材の FPGA 側の仕込みを実行中に入れ外しする。MCU は Zephyr 4.1 の
RP2040 のアプリで、FPGA の回路を埋め込んで起動時に SPI で送り、要求のスレッド・タイマ割り込み・RTT のログで動く。ビルドは
`build.py` が FPGA → ファームウェアの順にし、書き込みは fprobe の SWD（`flash.*`）。

**Tech Stack:** VHDL-2008（GHDL 6 + cocotb 2）、ForgeFPGA（yosys・eda-placer、`fprobe_gw.toolchain.forgefpga`）、Zephyr 4.1・
Zephyr SDK 0.17.0 の GCC・west（`~/zmk`）、Python 3.13（pytest）、fprobe のデーモン（PYNQ-Z2）。

**Spec:** `docs/superpowers/specs/2026-10-21-shrike-c-demo-design.md`

## Global Constraints

- `~/zmk/ws`（hhkb-split の作業場所）は**書き換えない**。ビルドの出力はこのリポジトリの `target_ip/fprobe_obs/examples/shrike_lite/build/` の下。
- 道具の場所は環境変数で差し替えられる: `FPROBE_ZEPHYR_WS`（既定 `~/zmk/ws`）・`FPROBE_ZEPHYR_SDK`（既定 `~/zmk/sdk/zephyr-sdk-0.17.0`）・`FPROBE_WEST`（既定 `~/zmk/zmkenv/Scripts/west.exe`）。
- fprobe 本体（FPGA・デーモン）にこのデモ専用の機能を足さない。題材の切り替えは fprobe の汎用の操作（メモリの書き込み）で。
- VHDL は CLAUDE.md の規約（VHDL-2008・ASCII の文字列・同期リセット `rst`・`S_` の状態名・全信号を駆動・entity ごとに cocotb の試験）。
- Shrike のフラッシュの書き込みは必要なときだけ。題材の切り替え・頻度の調整でファームウェアを書き直さない。書く前に Windows で D: を取り出す。
- Shrike の FPGA の送り方: GPIO12（PWR）・GPIO13（EN）・CS（GPIO1）を下げて 100 ms → PWR・EN を上げて 100 ms → 500 ms → CS を上げて 2 ms → CS を下げる → SPI モード 0・MSB 先頭で回路のデータをそのまま送る → CS を上げる（vicharak-in/shrike_flash）。
- SPI（RP2040 側）: GPIO0 = MISO・GPIO1 = CS・GPIO2 = SCK・GPIO3 = MOSI、1 MHz、モード 0、3 バイトのフレーム（書き: addr・上位・下位、読み: 0x80|addr・0・0）。
- マーカー: GPIO26 = 要求中 1（ロジアナ ch 10）、GPIO27 = 題材の印（JB 7 = ch 14。2026-10-21 に配線済み）。
- 名前つきの大域変数: `g_requests`・`g_errors`・`g_retries`・`g_last_a`・`g_last_b`・`g_last_result`・`g_scenario`・`g_poll_limit`・`g_s2_rate`・`g_log_every`・`g_reset`・`g_fpga_id`。
- Zephyr のスレッドの表示に要る CONFIG: THREAD_MONITOR・THREAD_NAME・THREAD_STACK_INFO・INIT_STACKS（host/fprobe/rtos/zephyr.py）。
- 文書・コメント・コミットは日本語。

## Review Focus

1. 題材 0（不具合なし）で長く回しても `g_errors` = 0（S2 のマスク・S4 の固まりが既定で無効であること）→ Task 2 の試験と Task 6 の実機の試験。
2. 実行中に `g_scenario` を切り替えたとき、前の題材の仕込みが残らない（MODE を書き直し、固まりは `g_reset` なしでも題材 0 に戻せば解ける）→ Task 2・Task 6。
3. FPGA の回路を送れなかった・ID が違うときに、黙って動き続けない（`g_fpga_id` に読んだ値、RTT に誤り、要求を止める）→ Task 3。
4. 長く止めた（10 分）後に再開しても動き続ける（MicroPython の固まりの再発がない）→ Task 7。
5. fprobe の SWD でのフラッシュの書き込みが、PYNQ の中継越しでも検証まで通る（通らなければ直す）→ Task 5。

---

### Task 1: Zephyr の道具の場所（fprobe_gw.toolchain.zephyr）

**Files:**
- Create: `gateware/fprobe_gw/toolchain/zephyr.py`
- Test: `gateware/tests/test_zephyr_toolchain.py`

**Interfaces:**
- Produces: `ZephyrToolchain.locate(env: Mapping[str, str] = os.environ) -> ZephyrToolchain`（見つからなければ `ToolchainNotFound`。forgefpga.py と同じ例外）、属性 `ws: Path`・`sdk: Path`・`west: Path`、`build(app: Path, board: str, out: Path, extra: Sequence[str] = ()) -> Path`（`out/zephyr/zephyr.elf` を返す。west build を `ZEPHYR_SDK_INSTALL_DIR`・`ZEPHYR_TOOLCHAIN_VARIANT=zephyr`・`PYTHONUTF8=1` で、cwd = ws で呼ぶ）、`version() -> str`（ws/zephyr/VERSION から "4.1.0"）。

- [ ] **Step 1: 失敗する試験を書く**: `test_locate_uses_the_environment`（tmp に ws/zephyr/VERSION・sdk・west.exe を作り env で指すと見つかる）、`test_locate_reports_what_is_missing`（無ければ ToolchainNotFound で、文言に 3 つの環境変数名）、`test_version_reads_the_zephyr_version_file`（"VERSION_MAJOR = 4\nVERSION_MINOR = 1\nPATCHLEVEL = 0" → "4.1.0"）、`test_build_runs_west_without_touching_the_workspace`（`subprocess.run` を差し替え、引数に `-s <app> -d <out> -b rpi_pico` と cwd = ws、env の 3 つ）。
- [ ] **Step 2: 実行して失敗を確かめる**: `pytest gateware/tests/test_zephyr_toolchain.py -q` → 期待: ImportError。
- [ ] **Step 3: 実装する**（forgefpga.py の locate の形に合わせる）。
- [ ] **Step 4: 実行して通す** → 期待: 4 passed。
- [ ] **Step 5: コミット** `feat(toolchain): Zephyr（west・SDK・作業場所）の場所と build`

### Task 2: FPGA の演算器に題材の仕込み（MODE・S2・S4・回数）

**Files:**
- Modify: `target_ip/fprobe_obs/examples/shrike_lite/rtl/demo_accel.vhd`
- Modify: `gateware/tests/tb/demo_accel_tb.py`, `gateware/tests/test_shrike_demo.py`

**Interfaces:**
- Produces（レジスタ。既存の 0x00〜0x07 は変えない）: `0x08 MODE`（bit0 = S1: 計算中の開始で和を 0 に戻さない、bit1 = S2: 書き込みの取りこぼし、bit2 = S4: 固まり。リセット後 0 = 不具合なし。MODE を書くと固まりは解ける）、`0x09 S2_MASK`（S2 の頻度。16 ビットの LFSR の下位 `S2_MASK` ビットがすべて 0 の周期に来た A・B の書き込みを捨てる。S2_MASK = 0 は「捨てない」（0 ビットのマスクを「毎回一致」とは扱わない）、`0x0A EVENTS`（読み: bit3:0 = 捨てた回数の下位、bit7:4 = 固まった回数の下位。書きで 0 に）。
- 観測用の出力を足す: `dropped : out std_logic`（捨てた周期に 1 周期）、`stuck : out std_logic`（固まっている間 1）、`mode : out std_logic_vector(2 downto 0)`。
- generic `CLEAR_ON_RESTART` は残す（true なら MODE の bit0 を無視。`build.py --fixed` 互換）。
- S4 の条件: MODE bit2 かつ A = 0xFF のときに計算中の開始（restart）が来たら、S_BUSY のまま残りの回数を減らさない（stuck = 1）。

- [ ] **Step 1: 失敗する試験を書く**（demo_accel_tb.py）: `mode_zero_restart_clears_the_sum`（MODE = 0 で計算中に開始しても結果 = A×B）、`mode_s1_keeps_the_partial_sum`（MODE = 1 で今の不具合と同じ値）、`mode_s2_drops_writes_when_the_lfsr_matches`（S2_MASK = 1 で B の書き込みを何十回もし、EVENTS の下位 4 ビットが増え、捨てた回は B が前の値のまま）、`s2_mask_zero_never_drops`（MODE = 2・S2_MASK = 0 で 1000 回書いて EVENTS = 0）、`mode_s4_sticks_only_for_a_ff_with_a_restart`（A = 0xFE では固まらず、0xFF で固まり STATUS bit0 = 1 のまま、EVENTS の上位が 1 増える）、`writing_mode_releases_a_stuck_engine`（MODE を書くと S_IDLE に戻る）。既存の試験はそのまま通ること（既定 MODE = 0 なので「planted_bug」の試験は MODE = 1 を書いてから行うよう直す）。
- [ ] **Step 2: 実行して失敗を確かめる**: `pytest gateware/tests/test_shrike_demo.py -q` → 期待: 新しい試験が失敗。
- [ ] **Step 3: 実装する**。LFSR は x^16 + x^14 + x^13 + x^11 + 1、リセット値 0xACE1、毎周期進める。
- [ ] **Step 4: 実行して通す** → 期待: 全件 pass。
- [ ] **Step 5: コミット** `feat(shrike_demo): 演算器に題材の仕込み（MODE・S2 の取りこぼし・S4 の固まり・回数）`

### Task 3: 観測の信号と資源（記録型を試し、標本型 + 回数に）

**Files:**
- Modify: `target_ip/fprobe_obs/examples/shrike_lite/rtl/shrike_demo_core.vhd`, `gateware/tests/tb/shrike_demo_core_tb.py`
- Modify: `target_ip/fprobe_obs/examples/shrike_lite/build.py`（資源の表示はそのまま）

**Interfaces:**
- Consumes: Task 2 の `dropped`・`stuck`・`mode`。
- Produces: `probes`（16 ビット）の割り当て = 今の 12 ビット（`state` 1:0・`active` 2・`re_tog` 3・`wr_tog` 4・`rd_addr` 7:5・`left_hi` 11:8）+ `drop_tog` 12（捨てるたびに反転。標本型でも数え落とさない）・`stuck` 13・`mode(1 downto 0)` 15:14。README の信号の定義もこれに。

- [ ] **Step 1: 記録型の資源を測る**: `python target_ip/fprobe_obs/examples/shrike_lite/build.py --obs stream` → rbbs が 140 以下なら記録型を既定にする（以下の割り当ては同じ）。超えたら標本型のまま。結果（rbbs・LUT・FF）を README とコミットに残す。
- [ ] **Step 2: 失敗する試験を書く**（shrike_demo_core_tb.py）: `probes_carry_drop_toggle_stuck_and_mode`（SPI で MODE = 0b110・S2_MASK = 1 を書き、B を書き続けて probes の bit12 が反転、A = 0xFF で restart させて bit13 = 1、bit15:14 = "10"）。
- [ ] **Step 3: 実行して失敗を確かめる** → 期待: 失敗。
- [ ] **Step 4: 実装し、通す** → 期待: pass。`build.py`（既定の観測）で rbbs ≤ 140 を確かめる。
- [ ] **Step 5: コミット** `feat(shrike_demo): 観測の信号に取りこぼし・固まり・MODE（資源 <数値>）`

### Task 4: Zephyr のファームウェア（起動・FPGA の送り込み・要求の流れ・RTT）

**Files:**
- Create: `target_ip/fprobe_obs/examples/shrike_lite/firmware_c/`（`CMakeLists.txt`・`prj.conf`・`boards/rpi_pico.overlay`・`src/main.c`・`src/fpga_load.c`・`src/fpga_load.h`・`src/accel.c`・`src/accel.h`・`src/demo.h`）
- Modify: `target_ip/fprobe_obs/examples/shrike_lite/build.py`（`--firmware`）
- Test: `gateware/tests/test_shrike_firmware_build.py`

**Interfaces:**
- Consumes: Task 1 の `ZephyrToolchain`、Task 2 のレジスタ。
- Produces（C）: `int fpga_load(const uint8_t *image, size_t len)`（0 = 成功。Global Constraints の手順）、`void accel_write(uint8_t addr, uint16_t v)`・`uint16_t accel_read(uint8_t addr)`（SPI の 3 バイト。CS は GPIO を手で）、大域変数（Global Constraints の一覧。`volatile`、`__attribute__((used))`）。回路のデータは CMake の `generate_inc_file_for_target` で `build/<名前>/fpga_mcu.bin` から埋め込む。
- Produces（Python）: `build.py` の `build_firmware(fpga_image: Path, out: Path) -> Path`（ELF のパス。`zephyr.uf2` も出る）、CLI `--firmware`（FPGA を作ってからファームウェア）。出力の JSON に、使った Zephyr の版（`ZephyrToolchain.version()`）と SDK の場所を入れる（仕様 §7）。
- 要求の流れ（要求のスレッド `req`、優先度 5）: 起動で `fpga_load` → `g_fpga_id = accel_read(0)`、0x5D01 でなければ RTT に誤りを出して要求しない → ループ: A・B（乱数）→ GPIO26 = 1 → A・B・CTRL を書く → STATUS を `g_poll_limit` 回（既定 8）まで読む → 足りなければ `g_retries++`・待ち回数を倍・GPIO26 を一度 0 に戻して CTRL を書き直す → RESULT を読み A×B と比べ `g_errors`・`g_last_*` → GPIO26 = 0 → `g_log_every` 回ごとに RTT に 1 行 → 5 ms 待つ。`g_scenario` の変化で MODE を書く（Task 6 で中身）。
- prj.conf: SPI・GPIO・USE_SEGGER_RTT・RTT_CONSOLE・THREAD_MONITOR・THREAD_NAME・THREAD_STACK_INFO・INIT_STACKS・DEBUG_OPTIMIZATIONS（ソース行で追いやすく）。

- [ ] **Step 1: 失敗する試験を書く**（Zephyr が無ければ skip）: `test_firmware_builds_with_the_fpga_image_embedded`（`build.py --firmware` で ELF ができ、ELF のシンボルに `g_errors`・`g_scenario`・`_SEGGER_RTT`・`fpga_load` があり、.rodata に fpga_mcu.bin の先頭 64 バイトが含まれる）。
- [ ] **Step 2: 実行して失敗を確かめる**: `pytest gateware/tests/test_shrike_firmware_build.py -q` → 期待: 失敗（firmware_c が無い）。
- [ ] **Step 3: 実装する**。overlay は spi0 の pinctrl（GPIO0・2・3）、GPIO1・12・13・26・27 は gpio として。
- [ ] **Step 4: 実行して通す** → 期待: pass。ZMK の作業場所の `git status --short` の行数が前後で同じ。
- [ ] **Step 5: コミット** `feat(shrike_demo): Zephyr の C のファームウェア（FPGA の送り込み・要求の流れ・RTT）`

### Task 5: fprobe の SWD で書き込み・起動の確認（実機）

**Files:**
- Create: `gateware/tests/hil/test_shrike_c_demo_hw.py`（`--hil` のときだけ。既存の HIL の試験の形）
- Modify（必要なら）: `host/fprobe/flash.py` 等（書き込みが通らなかったときの修正。回帰の試験つき）

**Interfaces:**
- Consumes: Task 4 の ELF。デーモン（PYNQ・LAN）の `flash`・`target.reset`・`memory.read`（ELF のシンボルで番地を引く）。

- [ ] **Step 1: 試験を書く**: `test_flash_boots_and_runs`（D: の取り出しを利用者に確かめてから。`flash` で ELF を書き検証 → リセット → 3 秒で `g_fpga_id` = 0x5D01・`g_requests` が増える・`g_errors` = 0。書き込みの時間をログに出す）。
- [ ] **Step 2: 実機で実行**: `pytest gateware/tests/hil/test_shrike_c_demo_hw.py --hil -k boots -s`。期待: pass。通らなければ superpowers:systematic-debugging で原因を突き止め、fprobe 側を直し、回帰の試験を足す（仕様 §6.1）。
- [ ] **Step 3: コミット** `test(hil): Shrike の C のデモを fprobe の SWD で書いて起動を確かめる（書き込み <秒>）`

### Task 6: 題材 S1〜S4 の切り替え（ファームウェア）

**Files:**
- Create: `target_ip/fprobe_obs/examples/shrike_lite/firmware_c/src/scenario.c`, `src/scenario.h`
- Modify: `src/main.c`, `gateware/tests/hil/test_shrike_c_demo_hw.py`

**Interfaces:**
- Consumes: Task 2 のレジスタ、Task 4 の `accel_*`。
- Produces: `void scenario_apply(uint32_t s)`（MODE・S2_MASK・`g_poll_limit`・割り込みの有効を設定）。題材ごとの設定: S0 = MODE 0・割り込みなし、S1 = MODE 1・`g_poll_limit` 8、S2 = MODE 2・S2_MASK = `g_s2_rate`（既定 10）・`g_poll_limit` 64（リトライしない）、S3 = MODE 0・タイマ割り込み（周期 `g_s3_period_us`、既定 997 µs）が ISR の中で SPI のレジスタを直接たたいて STATUS を読む（排他なし）・GPIO27 = 割り込み中、S4 = MODE 5（S1 + 固まり）・GPIO27 = タイムアウトを検知した瞬間。`g_reset` に 1 を書くと FPGA を送り直し `g_reset` = 0。
- 大域変数の追加: `g_s3_period_us`。

- [ ] **Step 1: 実機の試験を書く**: `test_scenario_zero_has_no_errors`（60 秒で `g_errors` 増えない）、`test_each_scenario_produces_errors`（S1〜S3 は 30 秒で `g_errors` が増える、S4 は `g_s2_rate` 等の調整で 2 分以内に固まり `g_requests` が止まり、`g_scenario` = 0 + `g_reset` = 1 で戻る）、`test_switching_back_to_zero_clears_the_scenario`（S1 → S0 の後 60 秒で誤り 0）。題材の切り替えは `memory.write` で（フラッシュを書き直さない）。
- [ ] **Step 2: 実行して失敗を確かめる**（題材が未実装）。
- [ ] **Step 3: 実装し、実機で通す**。各題材の誤りの頻度をログに出す。
- [ ] **Step 4: コミット** `feat(shrike_demo): 題材 S1〜S4 を実行中に切り替え（頻度は g_* で調整）`

### Task 7: 単体で見られることの確認（MCU・FPGA）

**Files:**
- Modify: `gateware/tests/hil/test_shrike_c_demo_hw.py`

- [ ] **Step 1: 実機の試験を書く**: `test_mcu_alone`（ソース行（`main.c` の結果の比較の行）にブレークポイント → 止まり、`g_last_a` を名前で読める・スレッドの一覧に `req` がある・RTT にログの行が来る・10 分止めて再開して `g_requests` が増え続ける）、`test_fpga_alone`（観測の回路に接続し、信号の定義（Task 3 の割り当て）で `state` が BUSY/DONE を行き来し、条件 `state changed and state=BUSY` の回数が毎秒 100 前後）。
- [ ] **Step 2: 実機で実行して通す**（10 分の停止の試験は `-k mcu_alone` で個別に）。通らない項目は原因を突き止めて直す（fprobe 側なら回帰の試験つき）。
- [ ] **Step 3: コミット** `test(hil): Shrike の C のデモを MCU・FPGA それぞれ単体で見る`

### Task 8: 題材ごとの解析と連動の実測、README

**Files:**
- Modify: `target_ip/fprobe_obs/examples/shrike_lite/README.md`（C のデモに書き直す。MicroPython の手順は「戻し方」だけ残す）
- Create（実測のスクリプト。リポジトリに残す）: `target_ip/fprobe_obs/examples/shrike_lite/analyze.py`（題材を切り替えて記録し、`recording.correlate`・`transactions`・`fpga_obs.trigger_set` 等で各題材の数値を出す）

- [ ] **Step 1: 題材ごとに、仕様 §4 の「捕まえ方」で原因に行き着けることを実機で確かめ、数値（先行率・区間の比較・止めた位置のソース行・変数）を README に書く。** 片側だけでは分からないことも、それぞれ 1 文で書く。
- [ ] **Step 2: 連動の遅れを実測する**: FPGA の条件 → MCU の停止（SWD 1 MHz・4 MHz）、MCU のブレークポイント → ロジアナの捕捉。README に。
- [ ] **Step 3: 配線の節に GPIO27 → JB 7（ch 14）を書く**（電源の順・電圧の範囲・逆流）。線が無い間の代わりの見方も。
- [ ] **Step 4: コミット** `docs(shrike_demo): C のデモの README（題材ごとの解析の実測・連動の遅れ・配線）`

### Task 9: AI の試験と、不具合なしの長時間

- [ ] **Step 1: AI 役（ソースを読まず、手引き・API・README の「題材の伏せた版」だけ）に S2・S3・S4 のどれか 1 つを伏せて調べさせ、原因に行き着いたかとつまずきを記録する。** つまずきは直す（fprobe の汎用の部分なら回帰の試験つき）。
- [ ] **Step 2: `g_scenario` = 0 で 30 分回して `g_errors` = 0**（HIL の試験に `test_soak_30min`、`-k soak` で個別に）。
- [ ] **Step 3: 記録の試験の後片付け**（試験の記録は `claude-` の名前で作り、名前で消す）。
- [ ] **Step 4: コミット** `test(shrike_demo): AI の試験と 30 分の誤りなしの確認`
