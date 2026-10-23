# FPGA 観測の PC ツール（U1）実装計画

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** FPGA の観測の値を、既存の共通の時間軸に信号の名前・列挙の名前付きで並べ、画面のパネルと MCP から操作できるようにする。

**Architecture:** 記録には組の生の値（stream `fpga`: value / gap / group）だけを入れ、信号の定義は記録の meta と ui.state に置き、時間軸は見るときに定義から信号ごとの段へ展開する（生・要約の SQL・持ち込みの 3 経路）。取り込みは状態を持つ復号器 `ObsDecoder` で少しずつ。組の番号は観測の回路が記録の列に入れる（種類 1 の bit 29 = 制御の記録）。

**Tech Stack:** VHDL-2008（GHDL + cocotb）、Python 3.13（pytest）、Svelte 5 + TypeScript（vitest）。

**Spec:** docs/superpowers/specs/2026-10-20-fpga-observation-ui-design.md

## Global Constraints

- 記録の行: stream `fpga`。`value`（t = fprobe の時刻、a = 組、b = 16 ビットの値）、`gap`（a = 失った数）、`group`（a = 組）。
- 制御の記録: 種類 1 の bit 29 = 1、bits 28:24 = 制御の種類（0 = 組）、bits 7:0 = 組の番号。時間の記録は bit 29 = 0。
- 再同期の順: （溢れ）→ 組 → 絶対 → 値。FIFO の空きは 4 語を待つ。
- 信号の定義: `{"groups": {"<組>": [{"name", "lsb", "width", "format": "hex"|"dec"|"bin"|"enum", "enum": {"<値>": "<名前>"}}]}}`。
  置き場所は記録の meta `fpga.signals` と ui.state の `fpga_signals`。
- VHDL の規約（CLAUDE.md）。画面の文言は i18n（ja・en）。
- 観測の回路の VERSION は 1 のまま（まだ出していない）。

## Review Focus

1. 記録中に観測を始める・止める・組を変える: Collector が止まらず、組の切り替えの前後の値が正しい組に入る。→ Task 2・3
2. 溢れの後: 失った数が gap に入り、その後の値の時刻が正しい。→ Task 2
3. 信号の定義を記録の後で変える: 過去の記録の段の名前・値が変わる（生の値は変わらない）。→ Task 4
4. 定義の無い組・定義が範囲外: 組の値の段 1 本になる・保存のときに断る。→ Task 4・5
5. 時刻合わせが来る前の値: 仮の傾きで並び、後の値と飛ばない。→ Task 2

---

### Task 1: 観測の回路が組の番号を記録の列に入れる（制御の記録）

**Files:** Modify `target_ip/fprobe_obs/rtl/obs_core.vhd`・`README.md`、`gateware/tests/tb/obs_core_tb.py`

- [ ] 失敗する試験: `the_group_is_recorded_at_each_resync`（開始で 組 0、SELECT = 1 で 組 1 の制御の記録が絶対の記録の前に来る）、
  `select_switches_the_group` と `overflow_is_reported_with_the_lost_count` を新しい順（溢れ → 組 → 絶対 → 値）に直す。
- [ ] 実装: 状態 S_GROUP を S_OVERFLOW と S_ABS の間に。S_RESYNC は空き 4 を待つ。
- [ ] 試験を通す・コミット `feat(target_ip): 再同期で組の番号を記録する`。

### Task 2: 状態を持つ復号器 `ObsDecoder` と Collector の取り込み

**Files:** Modify `host/fprobe/fpga_obs.py`・`host/fprobe/collector.py`・`host/fprobe/recording_stream.py`、Test `host/tests/test_fpga_obs.py`・`host/tests/test_collector.py`

**Interfaces:** Produces `ObsDecoder(latch_delay=0.0, sample_delay=0.0, nominal_ratio=None)`、`.feed(events) -> list[ObsSample]`。
`ObsSample` に `group: int` を足す。`samples(events, …)` は `ObsDecoder().feed(events)`。`add_events(rec, events, reference, carry, obs=None)` は
`obs` の復号器があれば種類 8・9 を `fpga` の行にする（無ければ捨てる）。

- [ ] 失敗する試験: `feeding_in_pieces_gives_the_same_samples`、`samples_carry_the_group_from_the_control_record`、
  `before_two_syncs_the_nominal_ratio_is_used`、`the_collector_does_not_stop_on_obs_events`（今は KeyError）、
  `obs_events_become_fpga_rows`（value・gap・group）。
- [ ] 実装・通す・コミット `feat(host): ObsDecoder と記録への取り込み（stream fpga）`。

### Task 3: 信号の定義 `FpgaSignals` と API

**Files:** Create `host/fprobe/fpga_signals.py`、Modify `host/fprobe/mcp_server.py`・`host/fprobe/probe_ui_state.py`・`host/api/fprobe_api.json`、Test `host/tests/test_fpga_signals.py`

**Interfaces:** `FpgaSignals.from_dict(d) -> FpgaSignals`（検査: 名前の重複・ビットの重なり・16 ビットの外・列挙の値が幅の外 → ValueError）、
`.to_dict()`、`.fields(group, value) -> dict[str, int | str]`、`.signals(group) -> list[Signal]`。
API `fpga_obs.signals_get` / `fpga_obs.signals_set`（ui.state の `fpga_signals` と、記録中なら meta `fpga.signals`）。
`recording.start` は ui.state の定義を meta に写す。`fpga_obs.read` は定義があれば `fields`。

- [ ] 失敗する試験 → 実装 → 通す・`python -m fprobe.api gen`・コミット `feat(host): FPGA の信号の定義（名前・ビット・列挙）`。

### Task 4: 時間軸の段の展開 `timeline_fpga`

**Files:** Create `host/fprobe/timeline_fpga.py`、Modify `host/fprobe/timeline.py`・`host/fprobe/timeline_rows.py`、Test `host/tests/test_timeline_fpga.py`

**Interfaces:** `expand(events, signals) -> list[Event]`（`value` を信号ごとの仮の出来事 kind `signal` に、label = 書式・列挙の文字、
key = `<組>.<名前>`）、`summary_sql(...)`・`carry_in(...)` の fpga 版。規則: `signal`（1 ビット・列挙は state、幅のある数は values）、
`gap`（points、bad）、`group`（points）。定義の無い組は `<組>` の段 1 本（16 進）。

- [ ] 失敗する試験: 生と要約で同じ段と値、記録の後で定義を変えると段が変わる、定義の無い組。→ 実装・通す・コミット。

### Task 5: VCD の書き出しに FPGA の信号

**Files:** Modify `host/fprobe/recording.py`、Test `host/tests/test_recording_export.py`（または既存の書き出しの試験）

- [ ] 失敗する試験: 信号ごとに `$var wire <幅> … <名前>` と値の変化。→ 実装・通す・コミット。

### Task 6: 画面のパネル「FPGA 観測」と時間軸の文言

**Files:** Create `host/ui/src/components/panels/FpgaObsPanel.svelte`・`host/ui/src/lib/fpgaobs.ts`、Modify `registry.ts`・`i18n.ts`・
`timeline/titles.ts`（必要なら）、Test `host/ui/tests/fpgaobs.test.ts`

**Interfaces:** `parseEnum("0=IDLE, 1=RUN") -> Record<string,string>`、`validateSignals(groups) -> string[]`（誤りの文言）、
`tckToHalfPeriod(mhz, sysHz) -> {half, actualMhz}`、`statusText(status) -> string`。

- [ ] 失敗する試験（vitest）→ 実装 → `npm test`・`npm run check`・`npm run build` が通る・コミット。

### Task 7: 実機（記録しながら観測、Primer の代役）

**Files:** Modify `gateware/tests/hil/test_fpga_obs_hw.py`

- [ ] `the_timeline_shows_named_fpga_signals`: 定義（カウンタの bit 0 = `mark`、bits 3:0 を列挙）を置き、記録しながら観測 →
  時間軸のタイルに `fpga` の段 `0.mark` があり、値が変わる。Primer の回路を書き直して（Task 1）PYNQ から読む。
