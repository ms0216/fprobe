# 組み込み Linux のアプリのデバッグ 段階 4（の一部: ftrace・journal） Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:executing-plans. Steps use checkbox (`- [ ]`) syntax.

**Goal:** カーネルの出来事（tracefs のイベント）と、システムのログ（journal）を、アプリの出来事と同じ道具・同じ時間軸で見る（仕様 §3.3 の 3「linux.ftrace.*」「linux.log」）。perf（道具が無い）と core dump の回収（core の置き場所の設定が要る）は、この計画に入れない。

**Architecture:**
- `linux.ftrace.enable {target, events: ["sched/sched_switch", ...]}` / `disable`: tracefs の `events/<group>/<name>/enable` に書く（使う側が名前を明示）。有効にしたイベントの名前は、出来事の解析で受け入れる。
- trace_pipe は `linux.trace.start` で開く（uprobe か ftrace のイベントがあれば）。出来事は `method: "ftrace"`、stream `<target>.ftrace`、引数は行の `k=v` をそのまま。時刻は trace_clock（mono_raw にそろえる）。
- `linux.log.read {target, lines, unit?, since_cursor?}`: `journalctl -o json --no-pager -n lines [-u unit] [--after-cursor C]` の各行を JSON のまま返す（`__CURSOR`・`__MONOTONIC_TIMESTAMP`（CLOCK_MONOTONIC の µs）・MESSAGE 等）。journal の時刻は MONOTONIC（MONOTONIC_RAW とは別の時計）なので、時刻合わせの換算はしない（事実として時計の名前を返す）。

**Spec:** docs/superpowers/specs/2026-10-22-linux-app-debug-design.md

## Global Constraints

- イベントの名前・行数・ユニットは使う側が明示する。既定の暗黙の選び方をしない。
- 解析は行の形（`k=v`）をそのまま。意味づけ（どれが「怪しい」等）はしない。

## Review Focus

1. **イベントの名前にパスの文字（`..`・`/` の数）**: tracefs の外を書かないこと。`^[a-z0-9_]+/[a-z0-9_]+$` だけを受ける。→ `test_ftrace_event_name_is_checked`
2. **trace_pipe に uprobe と ftrace の行が混ざる**: 方式を取り違えないこと。→ `test_trace_lines_are_split_by_kind`
3. **journalctl が無い・権限が無い**: stderr と終了コードを事実で返す。→ `test_log_read_reports_failure`

### Task 1: linux.ftrace.enable / disable と出来事

**Files:** Modify `host/fprobe/probe_linux.py`・`host/fprobe/linux/uprobe.py`（`parse_trace_line` は kprobe・tracepoint の行も読む: `(0x…)` が無い行）・json; Test `host/tests/test_probe_linux.py`・`test_linux_uprobe.py`

- [ ] 試験:
  - `test_parse_tracepoint_line`: `"  kworker/0:1-12 [000] d..2. 100.000002: sched_switch: prev_comm=a prev_pid=1 ==> next_comm=b next_pid=2"` → event sched_switch、args に prev_pid=1・next_pid=2。
  - `test_ftrace_event_name_is_checked`
  - `test_trace_lines_are_split_by_kind`
- [ ] 作る → 通る → コミット。

### Task 2: linux.log.read

- [ ] 試験: `test_log_read_returns_json_lines`（偽の ssh が JSON の行を返す）、`test_log_read_reports_failure`。
- [ ] 作る → 通る → コミット。

### Task 3: 実機（Pi 2B）

- [ ] `gateware/tests/hil/test_linux_ftrace_pi_hw.py`:
  - sched/sched_switch を有効にし、ptrace のプローブと一緒に trace.start する。
  - 出来事に ftrace の sched_switch と ptrace の tick の両方があり、時刻（target_ns）が同じ時計（CLOCK_MONOTONIC_RAW / mono_raw）で並ぶ。
  - log.read で journal の行が返る。
- [ ] docs/linux.md → コミット → レビュー → 修正。
