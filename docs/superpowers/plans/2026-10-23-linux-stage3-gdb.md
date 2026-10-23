# 組み込み Linux のアプリのデバッグ 段階 3（gdb） Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:executing-plans. Steps use checkbox (`- [ ]`) syntax.

**Goal:** 止めて調べるデバッグを、AI と人の両方から使えるようにする（仕様 §3.3 の 3「linux.gdb.*」）。

**Architecture:**
- AI の道 `linux.gdb.batch`: ターゲットの gdb を SSH の中で `-batch -nx` で走らせる。PID につけるか、core を読む。使う側が明示した gdb の命令の列を順に実行し、出力を事実のまま返す。止める時間は gdb がつけている間だけ。
- 人の道 `linux.gdb.serve` / `linux.gdb.stop`: ターゲットで `gdbserver --once --attach - PID`（標準入出力）を SSH の中で起動する。デーモンは PC の 127.0.0.1 の、使う側が明示したポートで gdb の接続を 1 つ待ち、バイト列を中継する（RSP は解さない）。PC のポートは 127.0.0.1 だけで開く（仕様 §4 の「PC のネットワークの設定を変えない」）。

**Spec:** docs/superpowers/specs/2026-10-22-linux-app-debug-design.md

## Global Constraints

- 暗黙の値を入れない。命令の列・ポート・PID は使う側が明示する。時間切れ（timeout_s）も明示する。
- 出力は gdb の文字のまま返す。解析して判定しない（呼び出しの履歴の行をそのまま）。
- 道具が無ければ（gdb / gdbserver が caps に無い）、事実で断る。

## Review Focus

1. **gdb の命令に、シェルの特殊な文字（`'`・`;`・`$`）が入る**: 安全に渡すこと。命令は 1 つずつ `-ex` の引数にし、シェルの単語として引用する。→ `test_batch_quotes_commands`
2. **中継の途中で gdb か SSH が切れた**: もう片方も閉じること。`linux.gdb.stop` と status に事実を出すこと。→ `test_serve_relays_and_closes_both_sides`
3. **ポートが使用中**: 事実で断ること。→ `test_serve_port_in_use`

### Task 1: linux.gdb.batch

**Files:** Modify `host/fprobe/probe_linux.py`・`host/api/fprobe_api.json`; Test `host/tests/test_probe_linux.py`

- 引数: `linux.gdb.batch {target, commands: [str], pid?: int, core?: str, binary?: str, timeout_s: number}`
  - pid か core のどちらか 1 つ。core は core のファイルで、binary が要る。
- 実行するもの:
  - `gdb -batch -nx -p PID -ex <c1> -ex <c2> ...`
  - core のときは `gdb -batch -nx BINARY CORE -ex ...`
  - sudo なら `sudo -n` を付ける。
- 返す形: `{exit_code, stdout, stderr, command}`（command は実際に走らせた 1 行）。
- [ ] 試験を書く。
  - `test_batch_quotes_commands`: 偽の ssh が受けた命令の文字に、`'bt'` と `'print '"'"'x'"'"''`（引用済み）が入る。
  - `test_batch_refuses_without_gdb`: caps の tools.gdb が None なら RuntimeError。
- [ ] 落ちる → 作る → 通る → コミット。

### Task 2: linux.gdb.serve / stop

**Files:** Create `host/fprobe/linux/gdb_relay.py`（`GdbRelay(ssh, remote_cmd, port)`: listen → accept 1 → 2 本のスレッドで中継 → どちらかが閉じたら両方閉じる。`status()`）; Modify probe_linux.py・json; Test `host/tests/test_linux_gdb_relay.py`

- `linux.gdb.serve {target, pid, port}` → `{port, command}`
- `linux.gdb.stop {target}` → `{bytes_to_target, bytes_from_target, closed_by}`
- [ ] 試験を書く（偽の ssh は、受けたバイトを大文字にして返す偽物）。
  - `test_serve_relays_and_closes_both_sides`
  - `test_serve_port_in_use`
- [ ] 落ちる → 作る → 通る → コミット。

### Task 3: 実機（Pi 2B）

**Files:** Create `gateware/tests/hil/test_linux_gdb_pi_hw.py`

- [ ] batch: 題材のアプリを走らせ、`linux.gdb.batch(pid, ["bt"])` の出力に `nanosleep` か `main` が含まれ、アプリが終了コード 0 で終わる。
- [ ] serve: 中継のポートに Python のソケットでつなぐ。RSP の `$qSupported#37` を送り、`$` で始まる返事が来る。切った後にアプリが終了コード 0 で終わる。
- [ ] docs/linux.md に事実と使い方を書く → コミット。
