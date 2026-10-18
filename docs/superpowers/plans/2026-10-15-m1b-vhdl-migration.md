# M1b: RTL の VHDL-2008 移行と bringup 実機確認 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:executing-plans. Steps use checkbox (`- [ ]`) syntax.

**Goal:** M1 で Amaranth により実装したボード層・UART・bringup を VHDL-2008 に置き換え、GHDL+cocotb のシミュレーション基盤と、Python のプロジェクト記述から Gowin でビルドする流れを確立し、実機 HIL（エコー・周波数申告・連続エコー無欠落）を通す。

**Architecture:** RTL は `gateware/rtl/` の VHDL-2008（ライブラリは `work`）。Python 側 `fprobe_gw` は開発ツールのみ: `toolchain/`（Gowin・GHDL の所在、ビルド、書き込み、PLL ソルバ、シミュレーション起動）、`boards/`（ボード定義: デバイス・JTAG 識別・ピン制約ファイル・クロック）、`designs.py`（デザイン = トップ entity + ソース一覧 + ボード）。Gowin 用 TCL/SDC は Python が `GowinProject` から生成する。

**Tech Stack:** VHDL-2008, GHDL 6.0 (mcode), cocotb 2.1 + pytest, Gowin EDA 1.9.11.03 (`-vhdl_std vhd2008`), Python 3.13。

**Spec:** `docs/superpowers/specs/2026-10-15-fprobe-architecture-design.md`（§10 開発・検証方式は VHDL 統一版）

## Global Constraints

- M0/M1 の安全装置（`select_jtag_location`・IDCODE 照合）・`GowinBuilder` の成果物検証はそのまま使う。弱めない。
- RTL は合成可能な VHDL-2008 のみ。`ieee.numeric_std` を使い `std_logic_arith` は使わない。リセットは同期・アクティブハイ（`rst`）。
- テスト: すべての VHDL entity は cocotb テストを持つ。pytest から起動し、GHDL の所在は `toolchain.ghdl` が解決する（PATH に依存しない）。
- 生成物（TCL/SDC）はビルドディレクトリにのみ書く。リポジトリにコミットする VHDL は手書きのみ。ボード定数パッケージと PLL ソルバの一致は pytest で検査する。
- `pytest` の `filterwarnings = error::DeprecationWarning` は維持。

## Review Focus

1. **連続受信時の取りこぼし**（HIL で実際に発覚）: ホストが隙間なく送っても bringup のエコーは欠けない（TX のフレーム周期がちょうど 10 ビット、エコー経路に FIFO）。
2. **GHDL 未導入・別の場所**: 明確なエラー（`ToolchainNotFound`）でテストが失敗し、黙ってスキップしない。
3. **ボード定数と PLL ソルバの不一致**: VHDL パッケージを手で変えたらテストが落ちる。
4. **ビルド時の VHDL 構文エラー**: gw_sh が失敗し、`BuildError` にログパスが出る（既存）。
5. **リセット解除直後の UART 線**: TX はリセット中・直後も high を保ち、受信側に偽の開始ビットを見せない。

---

## Tasks

### Task 1: 依存関係の切り替えと GHDL シミュレーション基盤
- Modify: `gateware/pyproject.toml`（dependencies から amaranth・luna-usb を削除、`cocotb~=2.1` を追加）
- Create: `gateware/fprobe_gw/toolchain/ghdl.py`: `GhdlToolchain(home: Path)` / `.ghdl: Path` / `.vpi_lib_dir: Path` / `locate(env=os.environ)`（`FPROBE_GHDL_HOME` → 既定の winget パス → PATH 上の `ghdl` の親の親）/ `environment(base_env) -> dict`（GHDL の bin・lib、cocotb libs、Python のディレクトリを PATH 先頭に追加）
- Create: `gateware/fprobe_gw/toolchain/sim.py`: `run_cocotb(*, toplevel: str, sources: Sequence[Path], test_module: str, generics: Mapping[str, object] = {}, build_dir: Path | None = None) -> None`（cocotb_tools.runner の ghdl を使い、`--std=08`、`test_dir=build_dir`、環境変数は `GhdlToolchain.environment()`。テスト失敗は例外）
- Create: `gateware/fprobe_gw/rtl.py`: `RTL_ROOT: Path`, `rtl(*relative: str) -> list[Path]`
- Tests: `tests/test_toolchain_ghdl.py`（locate の優先順位・不完全なインストールの拒否・environment が PATH 先頭に必要ディレクトリを持つ）、`tests/test_sim_smoke.py`（`tests/rtl/sim_smoke.vhd` の 8bit カウンタを cocotb で 1 本流す）

### Task 2: Gowin プロジェクト記述と TCL/SDC 生成
- Create: `gateware/fprobe_gw/toolchain/project.py`: `@dataclass(frozen=True) GowinProject(name, part, family, top, sources: tuple[Path, ...], constraints: Path, clocks: tuple[ClockConstraint, ...], options: tuple[str, ...])`, `ClockConstraint(name: str, target: str, kind: Literal["port","net"], frequency_hz: int)`, `render_tcl(project, build_dir) -> str`, `render_sdc(project) -> str`, `GowinProject.prepare(build_dir) -> Path(tcl)`
  - TCL: `add_file -type vhdl` 各ソース（絶対パス）、`add_file -type cst`、`add_file -type sdc`、`set_device -name <family> <part>`、`set_option -vhdl_std vhd2008 -top_module <top> -print_all_synthesis_warning 1 -show_all_warn 1`、追加オプション、`run all`、`file copy -force impl/pnr/project.fs <name>.fs`
- Modify: `toolchain/build.py`: `GowinBuilder.build(project: GowinProject, build_dir) -> BuildResult`（Amaranth の prepare/extract を `project.prepare(build_dir)` に置換。既存の削除・検証ロジックは不変）
- Tests: `tests/test_toolchain_project.py`（TCL の各行・SDC の周期、ソースは絶対パス、top 指定）、`tests/test_toolchain_build.py` を `GowinProject` ベースに更新

### Task 3: PLL ソルバのツール側への移設とボード定義
- Move: `platform/gowin_pll.py` のソルバ部分 → `toolchain/pll.py`（`PllConstraints, PLL_CONSTRAINTS, PllSolution, PllSolveError, solve_pll`）。Amaranth の `GowinRPLL` は削除。
- Create: `gateware/fprobe_gw/boards/__init__.py`, `boards/primer20k.py`: `@dataclass(frozen=True) Board(name, part, family, revision, jtag_identity, constraints: Path, input_clock_hz: int, sync_pll: PllSolution, options: tuple[str, ...])`, `PRIMER20K: Board`（options に `-use_done_as_gpio 1 -use_ready_as_gpio 1`）
- Create: `gateware/rtl/boards/primer20k/primer20k.cst`（clk27, led[0..5], uart_tx/rx, key[0..3]）
- Create: `gateware/rtl/boards/primer20k/primer20k_pkg.vhd`: 定数 `CLK27_HZ`, `PLL_IDIV_SEL`, `PLL_FBDIV_SEL`, `PLL_ODIV_SEL`, `SYS_CLK_HZ`
- Tests: `tests/test_toolchain_pll.py`（旧テストのソルバ部分を移設）、`tests/test_boards_primer20k.py`（**パッケージの定数がソルバの解と一致**することを VHDL を正規表現で読んで検査、cst のピンが docs と一致）

### Task 4: 共通 RTL（同期化）と VHDL UART
- Create: `rtl/common/sync_bit.vhd`（2 段 FF 同期化, generic `INIT`）, `rtl/common/reset_sync.vhd`（非同期アサート・同期解除）
- Create: `rtl/serial/uart_tx.vhd`, `rtl/serial/uart_rx.vhd`（M1 の Amaranth 版と同じ仕様 + **TX はフレーム周期がちょうど 10 ビット**（最終ビット末尾で次フレームを受理））。ポート: `clk, rst, divisor : unsigned(15 downto 0)`, ストリーム `valid/ready/data(7:0)`, `tx`/`rx`, RX は `frame_error`, `overrun`
- Tests: `tests/test_uart_vhdl.py`（cocotb）: 8N1 フレーム、連続送信周期 = 10×divisor、back-to-back 受信、グリッチ無視、フレーミングエラー後の復帰、オーバーラン、ループバック、リセット中 TX=high

### Task 5: 同期 FIFO
- Create: `rtl/common/sync_fifo.vhd`（generic `WIDTH`, `DEPTH`(2 の冪)。`in_valid/in_ready/in_data`, `out_valid/out_ready/out_data`, `level`。First-word fall-through）
- Tests: `tests/test_sync_fifo.py`: 満杯で in_ready=0、空で out_valid=0、ランダムな valid/ready で順序と内容を保持、同時 push/pop

### Task 6: bringup コア（VHDL）
- Create: `rtl/designs/bringup_core.vhd`（generic `SYS_CLK_HZ`, `BAUD`, `REVISION`。UART RX → FIFO(64) → 送信アービタ（エコー／バナー）→ UART TX。`?` でバナー `fprobe-bringup sync_hz=<SYS_CLK_HZ> rev=<REVISION>\r\n`（VHDL の関数で文字列化）。心拍・activity 出力）
- Tests: `tests/test_bringup_core.py`: エコー、バナー、バナー後のエコー継続、**95 バイト連続送信の無欠落**、心拍

### Task 7: Primer 20K の bringup トップ
- Create: `rtl/boards/primer20k/gowin_rpll.vhd`（rPLL コンポーネント宣言をラップする entity。generic で IDIV/FBDIV/ODIV、出力 `clkout, lock`）、`rtl/boards/primer20k/primer20k_bringup_top.vhd`（clk27 → rPLL → reset_sync → bringup_core。LED はアクティブロー）
- Create: `tests/rtl/sim_gowin_prims.vhd`（シミュレーション専用の rPLL 振る舞いモデル: clkout = clkin、lock は数クロック後に 1）
- Modify: `fprobe_gw/designs.py`: `Design(name, board, top, sources)`、`DESIGNS = {"bringup": ...}`、`Design.project() -> GowinProject`
- Tests: `tests/test_primer20k_top.py`（GHDL で top をシミュレーション用プリミティブと共に elaborate・短時間実行し UART エコーを確認）

### Task 8: Amaranth 実装の削除、CLI 更新、実機確認
- Delete: `fprobe_gw/platform/`, `fprobe_gw/serial/`, `fprobe_gw/designs/`（パッケージ）, 旧テスト `test_platform_*`, `test_serial_uart.py`, `test_designs_bringup.py`
- Modify: `cli.py`（`--board` 廃止しデザインがボードを持つ。`build <design> [--program] [--persistent]`、`program <fs>`、`detect`）、`README.md`
- Modify: `tests/hil/test_bringup_hw.py`（期待値は `PRIMER20K.sync_pll.fout_hz` と `revision`）
- 実行: `pytest gateware`（全シミュレーション）→ `python -m fprobe_gw build bringup`（タイミング OK）→ `pytest gateware --hil`（3 件パス）
