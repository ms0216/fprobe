# 組み込み Linux のアプリのデバッグ 段階 1（道 A）Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** SSH でつないだ Linux のターゲットの、アプリの関数の実行を（アプリを作り直さずに）時刻付きで受け、fprobe の記録の時間軸に並べる。画面と AI（MCP）から使える。

**Architecture:** デーモンは Windows の OpenSSH（`ssh`）を子のプロセスで使う。ターゲットの上の小さな C のエージェントと、JSON の行（1 行 1 つ）でやりとりする（道 A）。
関数を取る仕組みは 2 つ。1 つは tracefs の uprobes で、ARM 命令の関数と 64 ビットだけ。もう 1 つは ptrace のソフトのブレークポイントで、全スレッドを止める方式。
時刻は 2 段で換算する。ターゲットの CLOCK_MONOTONIC_RAW から PC の単調時計へは、往復で取った組の内挿で換算する。PC から fprobe のタイムベースへは、記録の ClockMap で換算する。
記録中は `<名前>.uprobe` / `<名前>.ptrace` の stream に入れる。記録していなくても、メモリに置いて `linux.events.read` で読める。

**Tech Stack:** Python 3.13（subprocess・threading・pyelftools・capstone、どれも既存）、C（ターゲットの gcc で作る）、Svelte 5 + vitest。

**Spec:** docs/superpowers/specs/2026-10-22-linux-app-debug-design.md

## Global Constraints

- 本体にヒューリスティックを入れない。取れない・分からないは「無い／不明」と事実で返す。境界の値は使う側が明示して渡す（CLAUDE.md）。
- 新しい依存を足さない。SSH は `ssh` / `scp` を子のプロセスで使い、利用者の ~/.ssh と ssh-agent を使う。鍵はリポジトリに置かない。PC のポートを開かない（仕様 §3.6）。
- エージェントはターゲットの gcc で作る。どの gcc で作ったか（`gcc --version` の 1 行目）と、元のソースの sha256 を記録する（§3.3・§3.6）。
- uprobes の時計は trace_clock を `mono_raw` にする。無ければ事実で返す。fetch-arg は使う側が明示した文字列をそのまま使う。作った uprobe_events の行は API の結果に出す（§3.6）。
- ネットワークの時刻合わせの不確かさ（往復の半分）と、ClockMap の不確かさは、出来事の body に別々に出す（§3.6）。
- perf のハードのブレークポイントは段階 1 に入れない（2026-10-22、PYNQ で固まった。仕様 §3.3）。
- 操作はデーモンの調停と記録を通る。API は `host/api/fprobe_api.json` が正。変えたら `python -m fprobe.api gen` で生成物を作り直す。
- 文言は日本語。英語の UI の文言に CJK を入れない（i18n の試験）。

## Review Focus

1. **32 ビットの ARM で Thumb の関数に uprobe を付ける**: 32 ビットの ARM の uprobes は ARM 命令だけに対応する。Thumb の関数に付けると、カーネルが命令を読み違えてアプリを壊しうる。シンボルの bit0 が 1 のとき、または `isa="thumb"` のときは、付ける前に断る。ターゲットが 64 ビット（aarch64）なら断らない。→ Task 4 の `test_uprobe_refuses_thumb_on_arm32`。
2. **ptrace の最初の命令が PC を書く**: 元の命令に戻し、次の命令に仮のブレークポイントを置いて 1 歩進める方式は、分岐の命令では成り立たない。capstone で PC を書く命令だと分かったら、付ける前に断る。→ Task 5 の `test_plan_refuses_pc_writing_first_insn`。
3. **時刻合わせの組が 2 つ未満、または出来事が組の範囲の外**: 換算しない（または `outside: true` を付ける）。推測で延ばさない。→ Task 3 の `test_to_host_outside_and_too_few`。
4. **SSH が切れた・エージェントが落ちた**: 読む側のスレッドが止まる。`linux.agent.status` は終了コードと stderr の最後を事実で返す。調停を止めない。→ Task 6 の `test_agent_exit_is_reported`。
5. **記録の開始より前・途中で記録を止めた**: 出来事はメモリには残す。記録に入るのは、そのとき記録が開いていた分だけ。→ Task 7 の `test_events_without_recording_stay_in_memory`。

---

## ファイル

- 作る:
  - `host/fprobe/linux/__init__.py`
  - `ssh.py`: SSH と scp の子のプロセス。
  - `caps.py`: 能力の事実の採取の台本と、その解析。
  - `clock.py`: ターゲットの時計から PC の時計への換算。
  - `uprobe.py`: ELF のオフセット、uprobe_events の行、trace_pipe の行の解析。
  - `ptrace_plan.py`: ブレークポイントの計画（capstone）。
  - `agent.py`: 導入（作る）と、子のプロセス・JSON の行のやりとり。
  - `agent/fprobe_agent.c`: エージェント。
- 作る: `host/fprobe/probe_linux.py`（`LinuxTools` の mixin）
- 変える:
  - `host/fprobe/mcp_server.py`（Probe の基底・初期化・解放）
  - `host/api/fprobe_api.json`
  - 生成物
  - `host/pyproject.toml`（package-data に `linux/agent/*.c`）
- 作る:
  - UI: `host/ui/src/lib/linux.ts`、`host/ui/src/components/panels/LinuxPanel.svelte`、`host/ui/tests/linux.test.ts`
  - 変える: registry.ts、i18n.ts
- 試験:
  - `host/tests/test_linux_ssh.py`、`test_linux_caps.py`、`test_linux_clock.py`、`test_linux_uprobe.py`、`test_linux_ptrace_plan.py`、`test_linux_agent.py`、`test_probe_linux.py`
  - 偽のエージェント: `host/tests/linux_fake_agent.py`
  - 実機: `gateware/tests/hil/test_linux_pynq_hw.py`

---

### Task 1: SSH の子のプロセス

**Files:** Create `host/fprobe/linux/__init__.py`（空）・`host/fprobe/linux/ssh.py`; Test `host/tests/test_linux_ssh.py`

**Interfaces:**
- Produces:
  - `@dataclass(frozen=True) class SshTarget: host: str; user: str; port: int = 22; key: str | None = None`
  - `SshTarget.argv(remote_cmd: str) -> list[str]`
  - `SshTarget.run(remote_cmd: str, input: bytes | None = None, timeout: float = 30) -> subprocess.CompletedProcess`（bytes）
  - `SshTarget.open(remote_cmd: str) -> subprocess.Popen`（stdin・stdout・stderr は PIPE、bytes）
  - `SshTarget.put(local: Path, remote: str)` / `get(remote: str, local: Path)`
- 実行するファイルは環境変数 `FPROBE_SSH` / `FPROBE_SCP` で差し替えられる（既定は `ssh` / `scp`）。

- [ ] **Step 1: 試験を書く**:
  - `test_argv_has_batchmode_and_key`: `SshTarget("h", "u", 2222, "k").argv("ls")` が `["ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=10", "-p", "2222", "-i", "k", "u@h", "ls"]`。
  - `test_run_uses_env_ssh`: `FPROBE_SSH` に、tests の中の Python の小さな台本を指す。その台本は、受けた argv の最後を stdout に返す。`run("echo x").stdout == b"echo x"` を確かめる。
- [ ] **Step 2: 走らせて落ちることを見る** `pytest tests/test_linux_ssh.py -v` → ModuleNotFoundError。
- [ ] **Step 3: 作る**（`ConnectTimeout=10` は固定の値。判定ではなく接続の待ちの上限で、argv に見えるので隠れない）。
  - `FPROBE_SSH` に `.py` で終わる値が入っていたら、`[sys.executable, path]` の形で起動する（試験の偽物のため）。
- [ ] **Step 4: 通る** `pytest tests/test_linux_ssh.py -v` → PASS。
- [ ] **Step 5: コミット** `feat(linux): SSH の子のプロセス（ssh・scp。鍵は利用者の ~/.ssh）`

### Task 2: 能力の事実

**Files:** Create `host/fprobe/linux/caps.py`; Test `host/tests/test_linux_caps.py`（PYNQ で採った出力を試験の中の文字列に置く）

**Interfaces:**
- Produces:
  - `CAPS_SCRIPT: str`: ターゲットの /bin/sh で走らせる台本。各節を `@@<名前>` の行で区切って出す。
  - `parse_caps(text: str) -> dict`
- 返す形（値は採った事実のまま）:
  - 基本: `{"kernel": "6.6.10-xilinx", "machine": "armv7l", "config_source": "/proc/config.gz" | "/boot/config-..." | None,`
  - カーネルの設定: `"config": {"CONFIG_UPROBE_EVENTS": "y" | "n" | "m" | None, ...}`（None = 設定が読めず不明）
  - tracefs: `"tracefs": "/sys/kernel/tracing" | "/sys/kernel/debug/tracing" | None, "uprobe_events": bool, "trace_clocks": ["local", "mono_raw", ...] | None,`
  - perf・ptrace: `"perf_event_paranoid": int | None, "ptrace_scope": int | None,`
  - 道具: `"tools": {"gcc": path | None, "gdbserver": ..., "gdb": ..., "perf": ..., "bpftrace": ...},`
  - 権限: `"uid": int, "sudo_nopasswd": bool}`
- 採る設定の名前: `CONFIG_UPROBES CONFIG_UPROBE_EVENTS CONFIG_FTRACE CONFIG_KPROBES CONFIG_BPF_SYSCALL CONFIG_BPF_JIT CONFIG_DEBUG_INFO_BTF CONFIG_PERF_EVENTS CONFIG_HAVE_HW_BREAKPOINT CONFIG_DEVMEM CONFIG_STRICT_DEVMEM CONFIG_DEBUG_FS`

- [ ] **Step 1: 試験**:
  - `test_parse_pynq_sample`: 2026-10-22 の PYNQ の事実と同じ値になること。CONFIG_UPROBE_EVENTS は None でなく "n"（設定の行が `# CONFIG_UPROBE_EVENTS is not set`）。BPF_SYSCALL は "y"。gdbserver は None。gcc はパス。
  - `test_unreadable_config_is_unknown`: config の節が空なら、すべて None で、`config_source` も None。
- [ ] **Step 2: 落ちる**ことを見る。
- [ ] **Step 3: 作る**。
  - 台本は `zcat /proc/config.gz 2>/dev/null || cat /boot/config-$(uname -r) 2>/dev/null` の結果を節に入れる。どちらを読んだかも出す。
  - tracefs は `/sys/kernel/tracing` と `/sys/kernel/debug/tracing` のうち、`uprobe_events` があるものを選ぶ。
  - `trace_clock` の中身（`[local] global ... mono_raw` の形）は、角かっこを外して名前の並びにする。
- [ ] **Step 4: 通る**。
- [ ] **Step 5: 実機の出力を採り直す**（PYNQ が戻っていれば）: `ssh ... sh -s < CAPS_SCRIPT` の結果を試験の文字列と比べる。違えば試験の文字列を実機の出力に替える。
- [ ] **Step 6: コミット** `feat(linux): ターゲットの能力の事実（カーネルの設定・tracefs・道具・権限）`

### Task 3: ターゲットの時計から PC の時計へ

**Files:** Create `host/fprobe/linux/clock.py`; Test `host/tests/test_linux_clock.py`

**Interfaces:**
- Produces:
  - `class TargetClock`:
    - `add(target_ns: int, host_s: float, half_rtt_s: float) -> None`
    - `pairs: list[tuple[int, float, float]]`（target_ns の順）
  - `TargetClock.to_host(target_ns: int) -> dict | None`
    - 組が 2 つ未満なら None。
    - それ以外は `{"host_s": float, "half_rtt_s": float, "nearest_pair_ns": int, "outside": bool}`。
- 換算: 隣り合う 2 つの組の間を直線で内挿する（timesync.interpolator と同じ考え。時計の比のゆれに追う）。
  - 範囲の外は、端の 2 つの組の直線で延ばし、`outside: True` を付ける。
  - `half_rtt_s` は、挟む 2 つの組の大きい方（外なら端の 2 つの大きい方）。

- [ ] **Step 1: 試験**:
  - `test_interpolates_between_neighbours`: 組 (0, 10.0, 1e-4)・(1_000_000_000, 11.0, 2e-4)・(2_000_000_000, 12.5, 1e-4) を足す。
    - `to_host(1_500_000_000)` の host_s が 11.75、half_rtt_s が 2e-4、nearest_pair_ns が 500_000_000、outside が False。
  - `test_to_host_outside_and_too_few`:
    - 組が 1 つなら None。
    - 上の組で `to_host(3_000_000_000)` は host_s が 14.0、outside が True。
  - `test_pairs_kept_sorted`: 順不同に足しても、target_ns の順に並ぶ。
- [ ] **Step 2: 落ちる**。 **Step 3: 作る**（`bisect`）。 **Step 4: 通る**。
- [ ] **Step 5: コミット** `feat(linux): ターゲットの時計（CLOCK_MONOTONIC_RAW）から PC の時計への内挿`

### Task 4: uprobes の行と trace_pipe の解析

**Files:** Create `host/fprobe/linux/uprobe.py`; Test `host/tests/test_linux_uprobe.py`

**Interfaces:**
- Produces:
  - `@dataclass(frozen=True) class Site: path: str; offset: int; vaddr: int; thumb: bool; machine: str`
    - machine は ELF の e_machine 名（"EM_ARM" / "EM_AARCH64" / ...）。
  - `site_of(elf: Path, remote_path: str, symbol: str | None = None, address: int | None = None, isa: str | None = None) -> Site`
    - symbol か address のどちらか 1 つ。
    - address のときは、ARM の 32 ビットなら isa（"arm" / "thumb"）が必須。
    - vaddr から、PT_LOAD を使ってファイルのオフセットに換える。
  - `uprobe_line(name: str, site: Site, fetchargs: list[str], ret: bool = False) -> str`
    - 形: `p:fprobe/<name> <path>:0x<offset> <fetchargs...>`（ret なら `r:`）。
  - `UprobeRefused(ValueError)`
  - `parse_trace_line(line: str) -> dict | None`
    - 返す形: `{"comm": str, "pid": int, "cpu": int, "t_ns": int, "event": str, "args": dict[str, str], "raw": str}`
    - fprobe の uprobe の行でなければ None。
- 名前は `^[A-Za-z_][A-Za-z0-9_]{0,63}$`。合わなければ ValueError。
- trace_pipe の時刻は「秒.マイクロ秒」で、分解能は 1 µs。`t_ns` は 1000 の倍数になる（事実として、そのまま）。

- [ ] **Step 1: 試験**（ELF は、試験の中で pyelftools で読める最小の ELF を組むか、`tests/data/` の小さな ARM の ELF を使う）:
  - `test_uprobe_line_text`: `uprobe_line("enter", Site("/usr/bin/app", 0x1234, 0x11234, False, "EM_ARM"), ["a=%r0:s32"])` が `"p:fprobe/enter /usr/bin/app:0x1234 a=%r0:s32"`。
  - `test_uprobe_refuses_thumb_on_arm32`:
    - Site の thumb が True で、machine が "EM_ARM" のとき、`uprobe_line` が UprobeRefused。
    - 文に「32 ビットの ARM の uprobes は ARM 命令だけ」を含む。
  - `test_bad_name`: `uprobe_line("a b", ...)` が ValueError。
  - `test_parse_trace_line`: `"            app-812     [001] d..1.  1234.567890: enter: (0x10450) a=5"` が次の値になる。
    - `comm="app", pid=812, cpu=1, t_ns=1234567890000, event="enter", args={"a": "5"}`
  - `test_parse_ignores_other`: `"# tracer: nop"` と空の行は None。
- [ ] **Step 2: 落ちる**。 **Step 3: 作る**。 **Step 4: 通る**。
- [ ] **Step 5: コミット** `feat(linux): uprobe_events の行・trace_pipe の解析（32 ビット ARM の Thumb は断る）`

### Task 5: ptrace のブレークポイントの計画

**Files:** Create `host/fprobe/linux/ptrace_plan.py`; Test `host/tests/test_linux_ptrace_plan.py`

**Interfaces:**
- Consumes: Task 4 の `Site`・`site_of`。
- Produces:
  - `plan_breakpoint(elf: Path, site: Site) -> dict`
    - 返す形: `{"path": site.path, "offset": site.offset, "thumb": bool, "length": 2 | 4, "insn": "<mnemonic op_str>"}`
  - `BreakpointRefused(ValueError)`
- 作り:
  - 最初の命令を capstone で 1 つ読む（ARM / Thumb、aarch64）。
  - 次の場合は BreakpointRefused で断る。
    - PC を書く（`insn.regs_access()` の書く側に pc がある、または group が jump / call / ret / branch_relative）。
    - 読めない。
- 32 ビットでない（aarch64 等）は、段階 1 では BreakpointRefused で断る（エージェントのブレークポイントの命令の符号は ARM の 32 ビットだけ作る）。

- [ ] **Step 1: 試験**（機械語のバイトを ELF の代わりに渡せるよう、`plan_from_bytes(code: bytes, site: Site) -> dict` を `plan_breakpoint` の中身として公開する）:
  - `test_plan_thumb_push`: Thumb の `push {r4, lr}`（`10 b5`）なら length 2・thumb True。
  - `test_plan_arm_32bit`: ARM の `push {r4, lr}`（`10 40 2d e9`）なら length 4・thumb False。
  - `test_plan_refuses_pc_writing_first_insn`: Thumb の `b.n`（`fe e7`）は BreakpointRefused。
  - `test_plan_thumb2_wide`: Thumb-2 の 32 ビットの `push.w {r4-r8, lr}`（`2d e9 f0 41`）なら length 4。
- [ ] **Step 2: 落ちる**。 **Step 3: 作る**。 **Step 4: 通る**。
- [ ] **Step 5: コミット** `feat(linux): ptrace のブレークポイントの計画（最初の命令が PC を書くなら断る）`

### Task 6: エージェント（C）と、その導入・やりとり

**Files:** Create `host/fprobe/linux/agent/fprobe_agent.c`・`host/fprobe/linux/agent.py`・`host/tests/linux_fake_agent.py`; Modify `host/pyproject.toml`（package-data に `"linux/agent/*.c"`）; Test `host/tests/test_linux_agent.py`

**Interfaces:**
- Consumes: Task 1 の `SshTarget`。
- やりとり（1 行 1 つの JSON）:
  - 要求は `{"id": n, "op": ...}`。返事は `{"id": n, "ok": true, ...}` か `{"id": n, "ok": false, "error": "..."}`。
  - 出来事は `{"ev": ...}`（id 無し）。
  - op の一覧:
    - `hello` → `{version: 1, clock: "CLOCK_MONOTONIC_RAW", pid}`
    - `time` → `{t_ns}`
    - `trace_open {path}`: trace_pipe を読み始める。出来事 `{"ev":"trace","line":...,"rx_ns":...}`。
    - `trace_close`
    - `bp_add {id, pid, path, offset, thumb, length, regs: ["r0", ...]}`:
      - /proc/<pid>/maps で path と offset から番地を出す。
      - 全スレッドを PTRACE_SEIZE し、命令を BKPT（Thumb `0xde01`、ARM `0xe7f001f0`）に替える。
      - 当たったら出来事 `{"ev":"bp","bp","tid","t_ns","regs":{...}}`。
      - 処理: 全スレッドを止める → 元の命令に戻す → 番地 + length に仮の BKPT を置く → そのスレッドだけ進める → 仮に当たったら戻して元の BKPT を置き直す → 全スレッドを続ける。
    - `bp_remove {id}`（元の命令に戻して detach）
    - `quit`
  - 落ちる前、または失敗のときは `{"ev":"error","error":...}`。
- Produces:
  - `deploy(ssh: SshTarget, sudo: bool) -> dict`
    - 源を `~/.fprobe/fprobe_agent.c` に置き、`gcc -O2 -Wall -o ~/.fprobe/fprobe_agent` で作る。
    - 返す形: `{"binary": "~/.fprobe/fprobe_agent", "compiler": "<gcc --version の 1 行目>", "source_sha256": str}`
    - gcc が無ければ RuntimeError（事実の文で）。
  - `class Agent`:
    - `__init__(ssh: SshTarget, binary: str, sudo: bool, on_event: Callable[[dict], None])`
    - `start() -> dict`（hello の返事）
    - `request(op: str, timeout: float = 10, **args) -> dict`（失敗は RuntimeError）
    - `status() -> dict`: `{"running": bool, "exit_code": int | None, "stderr_tail": str}`
    - `stop() -> None`
  - sudo なら `sudo -n <binary>` で起動する。

- [ ] **Step 1: 試験**（`FPROBE_SSH` に偽の ssh を指す。偽の ssh は、受けた命令の代わりに `tests/linux_fake_agent.py` を走らせる）:
  - `test_hello_and_time`: start の返事の clock が "CLOCK_MONOTONIC_RAW"。`request("time")` に t_ns がある。
  - `test_events_are_delivered`: 偽のエージェントに `trace_open` すると、決まった 2 行を `ev: trace` で出す。on_event が 2 回呼ばれる。
  - `test_error_reply_raises`: 未知の op は RuntimeError。文にエージェントの error を含む。
  - `test_agent_exit_is_reported`: 偽のエージェントに `crash` を送ると、終了コード 3 で終わり、stderr に "boom" を書く。status が running False・exit_code 3・stderr_tail に "boom" を含む。待っている request は RuntimeError で戻る。
- [ ] **Step 2: 落ちる**。 **Step 3: agent.py と偽物を作る**。 **Step 4: 通る**。
- [ ] **Step 5: C のエージェントを書く**。
  - poll で stdin・trace_pipe・waitpid（`__WALL`）を回す、単一のスレッドにする。
  - JSON は、決まった形の小さな手書きの読み書きで扱う（依存を足さない）。
- [ ] **Step 6: 実機で作れることを確かめる**（PYNQ が戻っていれば）:
  - `python -c "from fprobe.linux.agent import deploy; ..."` で作る。
  - `echo '{"id":1,"op":"time"}' | ~/.fprobe/fprobe_agent` が t_ns を返す。
- [ ] **Step 7: コミット** `feat(linux): ターゲットのエージェント（C）と導入・JSON の行のやりとり`

### Task 7: LinuxTools（デーモンの API）

**Files:**
- Create: `host/fprobe/probe_linux.py`
- Modify:
  - `host/fprobe/mcp_server.py`（import・Probe の基底・`_init_linux()`・`_release_device` の `_close_linux()`）
  - `host/api/fprobe_api.json`
  - 生成物（`python -m fprobe.api gen`）
- Test: `host/tests/test_probe_linux.py`

**Interfaces:**
- Consumes: Task 1〜6 の全部。
- Produces: API（すべて引数に `target: str`。つないだ時の名前で、複数のターゲットを同時に持てる）
  - `linux.connect {target, host, user, port=22, key?, sudo=false}` → caps（Task 2 の形）
  - `linux.disconnect {target}`
  - `linux.capabilities {target}` → caps
  - `linux.agent.deploy {target}` → deploy の結果
  - `linux.agent.start {target}` / `linux.agent.status {target}` / `linux.agent.stop {target}`
  - `linux.probe.add`:
    - 引数: `{target, name, binary, symbol? , address? , isa?, method: "uprobe"|"ptrace", fetchargs?: [str], regs?: [str], ret=false, pid?}`
    - binary をターゲットから `$FPROBE_HOME/linux/<target>/sysroot/<binary>` に取り寄せる。
    - uprobe は uprobe_events に書いて有効にする。
    - ptrace は pid が必須。
    - 返す形: `{name, method, site, line? (uprobe), plan? (ptrace), sysroot_file, sha256}`
  - `linux.probe.list {target}` / `linux.probe.remove {target, name}`
  - `linux.sync {target, count}`:
    - time を count 回問う。往復の前後の monotonic の中点と、往復の半分を TargetClock に足す。
    - 返す形: `{pairs: [...], half_rtt_s: {min, max}}`
  - `linux.trace.start {target, sync_interval_s}`:
    - uprobe があれば、trace_clock を mono_raw にして trace_open する。
    - sync_interval_s ごとに 1 回 linux.sync をする（使う側が明示）。
  - `linux.trace.stop {target}`
  - `linux.events.read {target, since=0, limit=200}` → `{records, next, dropped}`
- 出来事の 1 件:
  - `{seq, target_ns, method: "uprobe"|"ptrace", probe, pid|tid, args|regs, host_s?, sync?}`
  - メモリに 100_000 件まで持つ（あふれた数は dropped）。
  - 記録中なら `rec.add(f"{target}.{method}", probe, t, body=件, uncertainty=...)`。
    - t は `rec.clock.to_ticks(host_s)` で出す。
    - uncertainty = 往復の半分（ticks）+ ClockMap の不確かさ。
    - 2 つは body の `sync` に `{"method": "network", "half_rtt_ns", "clockmap_uncertainty_ticks", "nearest_pair_ns", "outside"}` として別々に入れる。
  - `to_host` が None（組が 2 つ未満）なら、記録には入れない。メモリには target_ns だけで残す（事実）。
- json の summary に書くこと:
  - uprobe は「32 ビットの ARM では ARM 命令の関数だけ」。
  - ptrace は「当たるたびに全スレッドを止める（振る舞いが変わる）」。
  - perf のハードのブレークポイントは使わない理由（PYNQ で固まった）は書かない（段階 1 に無い）。
- 調停: connect・deploy・probe.add/remove・trace.start/stop・sync は control。capabilities・status・list・events.read は read。

- [ ] **Step 1: 試験**（偽の ssh・偽のエージェントで。偽の ssh は caps の台本には PYNQ の見本の出力を返す）:
  - `test_connect_returns_caps`
  - `test_probe_add_ptrace_sends_bp_add`（偽のエージェントが受けた bp_add の offset・thumb・length が、試験の ELF の値と同じ）
  - `test_probe_add_uprobe_refuses_thumb`
  - `test_trace_event_goes_to_recording`:
    - 記録を始め、`rec.clock` に点を足す。
    - linux.sync で組を 2 つ作る。偽のエージェントの bp の出来事を流す。
    - 記録に stream `pynq.ptrace` の出来事が 1 つ入り、body の sync に half_rtt_ns と clockmap_uncertainty_ticks が別々にある。
  - `test_events_without_recording_stay_in_memory`: 記録無しで出来事が流れても、events.read に出る。
  - `test_two_targets_are_independent`
- [ ] **Step 2: 落ちる**。 **Step 3: 作る**（json の項目も）。 **Step 4: 通る**。
- [ ] **Step 5: 生成物と全体** `python -m fprobe.api gen` → `pytest tests -q`（test_api_spec・test_api_mcp を含めて全部通る）。
- [ ] **Step 6: コミット** `feat(linux): デーモンの linux.* の API（つなぐ・能力・エージェント・プローブ・時刻合わせ・記録）`

### Task 8: 画面の Linux のパネル

**Files:** Create `host/ui/src/lib/linux.ts`・`host/ui/src/components/panels/LinuxPanel.svelte`・`host/ui/tests/linux.test.ts`; Modify `registry.ts`（group "trace"、log の後）・`i18n.ts`

**Interfaces:**
- Consumes: Task 7 の API。
- Produces:
  - `connectParams(form) -> Record<string, unknown>`（書いた欄だけ。port は数として確かめる）
  - `probeParams(form) -> {params} | {error: field}`（symbol と address のどちらか 1 つ。address は 0x の数。fetchargs と regs は空白で分ける）
  - `class LinuxEvents`（logview.ts の LogView と同じ形の読み足し。`linux.events.read`）

画面の中身。設定はすべて見えて変えられるようにする（仕様 §2）。
- つなぐの欄: target・host・user・port・key・sudo。
- 能力の事実: caps をそのままの表で出す。None は「不明」。
- エージェント: 導入・起動・状態。compiler と sha256 も出す。
- プローブの追加の欄: すべての引数。作った uprobe の行、または ptrace の計画を出す。
- プローブの一覧と削除。
- 記録の開始・停止: sync_interval_s を入れる欄がある。
- 出来事の一覧: target_ns・プローブ・引数・sync の 2 つの不確かさ。

- [ ] **Step 1: 試験** `linux.test.ts`:
  - `connectParams` が空の key を入れない。
  - `probeParams` は、symbol と address の両方が入っていればエラーにする。`"0x10"` は 16 になる。
  - LinuxEvents が next から読み足す。
- [ ] **Step 2: 落ちる** `npx vitest run tests/linux.test.ts`。 **Step 3: 作る**。 **Step 4: 通る**。そのうえで `npx vitest run` と `npx svelte-check` が通ること。
- [ ] **Step 5: コミット** `feat(ui): Linux のパネル（つなぐ・能力の事実・エージェント・プローブ・出来事）`

### Task 9: 実機の確かめ（PYNQ の Linux）

**Files:** Create `gateware/tests/hil/test_linux_pynq_hw.py`; `docs/linux.md`（使い方。事実の表: PYNQ の能力、32 ビットの ARM の uprobes の制限、perf のハードのブレークポイントで固まった件）

- [ ] **Step 1: 試験を書く**（`FPROBE_PYNQ_HOST` が無ければ skip）:
  1. デーモンの Probe に connect（鍵 `~/.ssh/fprobe_pynq`）する。
  2. agent.deploy・start をする。
  3. 試験のアプリをターゲットの gcc で作る。`-marm` と `-mthumb` の 2 つで、`tick(int)` を 10 ms ごとに 300 回呼ぶ。
  4. ptrace のプローブを両方に付け、regs ["r0"] を取る。
  5. linux.sync(count=8)・trace.start(sync_interval_s=1.0) をする。
  6. 4 秒後に stop する。
- [ ] **Step 2: 合格の条件**:
  - 出来事の数は 300。r0 は 0..299 の連番。
  - 隣り合う target_ns の差は、すべて 10 ms 以上。
  - half_rtt_ns の最大を事実として印字する（合否には使わない）。
  - アプリの終了コードは 0（ptrace で壊していない）。
- [ ] **Step 3: 走らせる** `pytest gateware/tests/hil/test_linux_pynq_hw.py -v -s`（PYNQ が戻ってから。戻っていなければ ledger に記して次へ）。
- [ ] **Step 4: Pi 2B**（準備ができていれば）: 同じ試験を `FPROBE_LINUX_HOST=fprobe-pi2.local` で走らせる。uprobes があれば `-marm` のアプリに uprobe も付けて、数が同じことを確かめる。
- [ ] **Step 5: コミット** `test(hil): PYNQ の Linux で ptrace のプローブと時刻合わせを確かめる`
