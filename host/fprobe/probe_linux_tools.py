"""Linux のターゲットの道具（読む・設定する系）: ftrace・journal ログ・core dump・perf・ターゲットの gdb。

LinuxService（fprobe.probe_linux）に混ぜ込む部品（LinuxToolsMixin）。ターゲットへの命令の送り方（_lt・_sh・_sudo）と
出来事の入れ方（_linux_keep・_linux_flush）は LinuxService の側にあり、ここはそれを使う。API の名前の一覧は LinuxService.API。
"""
from __future__ import annotations

import hashlib
import json
import re
from pathlib import Path

from .linux.gdb_relay import EMPTY_STATUS, GdbRelay
from .linux.perf import parse_perf_sample, parse_perf_stat
from .linux.shell import perf_where, sh_quote, text_of
from .linux.ssh import SshTimeout
from .linux.target import KEEP
from .linux.target import Target as _Target

BATCH_BACKSTOP_S = 10                                # gdb.batch: ターゲットの timeout が効かないときの PC 側の待ちの上乗せ
FTRACE_EVENT = re.compile(r"[a-z0-9_-]+/[a-z0-9_]+")       # tracefs の events/<グループ>/<名前>（fullmatch で使う）
_PERF_EVENT = re.compile(r"^[A-Za-z0-9_:\-./]+$")           # perf のイベントの名前（シェルに渡すので形を限る）


class LinuxToolsMixin:
    @staticmethod
    def _ftrace_paths(t: _Target, events: list[str]) -> list[str]:
        """tracefs のイベントの名前 "グループ/名前" を確かめて返す（ターゲットに tracefs が無ければ誤り）。"""
        if not t.caps.get("tracefs"):
            raise RuntimeError("ターゲットに tracefs が無い（linux.capabilities の tracefs）")
        for e in events:
            if not FTRACE_EVENT.fullmatch(e):
                raise ValueError(f"イベントの名前 {e!r} は「グループ/名前」（英小文字・数字・_）")
        return list(events)

    def linux_ftrace_enable(self, target: str, events: list[str]) -> dict:
        """tracefs のイベント（例 "sched/sched_switch"）を有効にする。出来事は linux.trace.start の trace_pipe で受け、
        method "ftrace"・stream "<target>.ftrace" に入る（時刻は trace_clock）。"""
        t = self._lt(target)
        for path in self._ftrace_paths(t, events):
            self._sh(t, f"echo 1 > {t.caps['tracefs']}/events/{path}/enable")
            t.ftrace.add(path)
        return {"enabled": sorted(t.ftrace), "tracefs": t.caps["tracefs"]}

    def linux_ftrace_disable(self, target: str, events: list[str]) -> dict:
        t = self._lt(target)
        for path in self._ftrace_paths(t, events):
            self._sh(t, f"echo 0 > {t.caps['tracefs']}/events/{path}/enable")
            t.ftrace.discard(path)
        return {"enabled": sorted(t.ftrace)}

    def linux_log_read(self, target: str, lines: int, unit: str | None = None, since_cursor: str | None = None) -> dict:
        """journal を journalctl -o json で読む（新しい方から lines 行。since_cursor の後だけ、unit だけ）。各行は JSON のまま。
        時刻 __MONOTONIC_TIMESTAMP は CLOCK_MONOTONIC（µs）で、出来事の CLOCK_MONOTONIC_RAW とは別の時計（換算しない）。"""
        t = self._lt(target)
        pre, stdin = self._sudo(t)
        command = f"{pre}journalctl -o json --no-pager -n {int(lines)}"
        if unit:
            command += f" -u {sh_quote(unit)}"
        if since_cursor:
            command += f" --after-cursor {sh_quote(since_cursor)}"
        r = t.ssh.run(command, input=stdin, timeout=60)
        entries, bad = [], 0
        for line in text_of(r.stdout).splitlines():
            try:
                entry = json.loads(line)
            except ValueError:
                entry = None
            if isinstance(entry, dict):
                entries.append(entry)
            else:
                bad += 1
        return {"entries": entries, "next_cursor": entries[-1].get("__CURSOR") if entries else None,
                "clock": "CLOCK_MONOTONIC", "exit_code": r.returncode, "stderr": text_of(r.stderr),
                "bad_lines": bad, "command": command}

    # --- core dump（段階 4。仕様 §3.3 の linux.core.*）-----------------------------------------

    def linux_core_status(self, target: str, pid: int | None = None) -> dict:
        """core dump の設定の事実: pattern（/proc/sys/kernel/core_pattern。pipe = "|" で始まる = 別のプログラムに渡す）・
        uses_pid・suid_dumpable、pid を渡せばそのプロセスの上限（limit_soft・limit_hard、バイト。null = unlimited）・cwd・exe
        （pattern が相対なら core は cwd に置かれる）。"""
        t = self._lt(target)
        script = ("printf 'pattern=%s\n' \"$(cat /proc/sys/kernel/core_pattern)\"; "      # echo は \ を解釈しうる
                  "printf 'uses_pid=%s\n' \"$(cat /proc/sys/kernel/core_uses_pid)\"; "
                  "printf 'suid_dumpable=%s\n' \"$(cat /proc/sys/fs/suid_dumpable)\"")
        if pid is not None:
            p = int(pid)
            script += (f"; grep 'Max core file size' /proc/{p}/limits; echo \"cwd=$(readlink /proc/{p}/cwd)\"; "
                       f"echo \"exe=$(readlink /proc/{p}/exe)\"")
        out = self._sh(t, script)
        facts, process = {}, None
        for line in out.splitlines():
            if line.startswith("Max core file size") and pid is not None:
                soft, hard = line.split()[4:6]
                process = {"pid": int(pid), "limit_soft": None if soft == "unlimited" else int(soft),
                           "limit_hard": None if hard == "unlimited" else int(hard)}
            elif "=" in line:
                k, v = line.split("=", 1)
                facts[k] = v
        if process is not None:
            process.update(cwd=facts.get("cwd"), exe=facts.get("exe"))
        pattern = facts.get("pattern", "")
        return {"pattern": pattern, "pipe": pattern.startswith("|"), "uses_pid": int(facts.get("uses_pid", 0)),
                "suid_dumpable": int(facts.get("suid_dumpable", 0)), "process": process}

    def linux_core_pattern_set(self, target: str, pattern: str) -> dict:
        """/proc/sys/kernel/core_pattern を pattern にする（システム全体の設定。sudo が要る。戻すには previous を渡し直す）。
        例 "/tmp/core.%e.%p"（%e = 実行ファイルの名前、%p = pid）。previous = 前の値、pattern = 書いた後に読み返した値。"""
        t = self._lt(target)
        if not pattern or "\n" in pattern:
            raise ValueError("pattern は 1 行の空でない文字")
        if not t.sudo:
            raise RuntimeError("core_pattern を書くには sudo が要る（linux.connect の sudo）")
        previous = self.linux_core_status(target=target)["pattern"]
        self._sh(t, f"printf %s {sh_quote(pattern)} > /proc/sys/kernel/core_pattern")
        return {"previous": previous, "pattern": self.linux_core_status(target=target)["pattern"]}

    def linux_core_limit_set(self, target: str, pid: int, soft: int | str, hard: int | str) -> dict:
        """プロセス pid の core の大きさの上限（RLIMIT_CORE）を変える（prlimit。soft・hard はバイトか "unlimited"）。
        そのプロセスの後の落ち方で core が書かれる。戻り値は変えた後の linux.core.status。"""
        t = self._lt(target)
        for v in (soft, hard):
            if not (v == "unlimited" or (isinstance(v, int) and not isinstance(v, bool) and v >= 0)):
                raise ValueError(f"soft・hard はバイト（0 以上の整数）か \"unlimited\"（{v!r}）")
        self._sh(t, f"prlimit --pid {int(pid)} --core={soft}:{hard}")
        return self.linux_core_status(target=target, pid=pid)

    def linux_core_fetch(self, target: str, path: str, local_path: str) -> dict:
        """ターゲットのファイル path（core 等）を PC の local_path に写す（sudo なら sudo で読む）。bytes・sha256 を返す。
        中身の解析はターゲットの gdb で（linux.gdb.batch の core・binary）。"""
        t = self._lt(target)
        pre, stdin = self._sudo(t)
        r = t.ssh.run(f"{pre}cat -- {sh_quote(path)}", input=stdin, timeout=300)
        if r.returncode:
            raise RuntimeError(f"{path} を読めない（{r.returncode}）: {text_of(r.stderr).strip()}")
        local = Path(local_path)
        local.parent.mkdir(parents=True, exist_ok=True)
        local.write_bytes(r.stdout)
        return {"path": path, "local_path": str(local), "bytes": len(r.stdout),
                "sha256": hashlib.sha256(r.stdout).hexdigest()}

    # --- perf（段階 4。仕様 §3.3 の linux.perf.*）-------------------------------------------

    @staticmethod
    def _need_perf(t: _Target) -> None:
        if not t.caps.get("tools", {}).get("perf"):
            raise RuntimeError("ターゲットに perf が無い（linux.capabilities の tools.perf。入れた後は linux.connect し直す）")

    def linux_perf_stat(self, target: str, events: list[str], duration_s: float, pid: int | None = None) -> dict:
        """perf stat で events（例 "cycles"・"instructions"・"task-clock"）を duration_s 秒数える（pid か、無ければ全体 -a）。
        rows = 行ごとの値（--no-scale: 数えた生の値。カウンタを共用して数えた時間が一部なら run_pct < 100 で、引き延ばさない）・
        単位・走った時間と割合（数えられなければ value = null と counted）。perf_metric は perf が計算した比。"""
        t = self._lt(target)
        self._need_perf(t)
        if not events or any(not _PERF_EVENT.fullmatch(e) for e in events):
            raise ValueError("events は 1 つ以上の perf のイベントの名前（英数字と _ : - . / だけ）")
        if not 0 < duration_s <= 3600:
            raise ValueError("duration_s は 0 より大きく 3600 以下")
        pre, stdin = self._sudo(t)
        where = perf_where(pid)
        command = f"{pre}env LC_ALL=C perf stat --no-scale -x, -e {','.join(events)} {where} -- sleep {duration_s:.6f}"
        r = t.ssh.run(command, input=stdin, timeout=duration_s + 60)
        err = text_of(r.stderr)
        return {"rows": parse_perf_stat(err), "exit_code": r.returncode, "stderr": err, "command": command}

    def linux_perf_record(self, target: str, duration_s: float, frequency_hz: int, max_samples: int,
                          pid: int | None = None) -> dict:
        """perf record で duration_s 秒、frequency_hz で標本を取り（pid か、無ければ全体 -a。時計は CLOCK_MONOTONIC_RAW、
        CPU の番号つき）、標本を出来事（method "perf"、probe = 関数の名前、ip・dso・pid・tid・cpu・period・event。時刻は
        µs の精度 = resolution_ns 1000）にして、ほかの出来事と同じ時間軸に入れる。PC に持ってくるのは perf script の頭から
        max_samples 行まで（呼び手が明示。script_lines = perf script の全行数）。record_exit_code・script_exit_code、
        samples = 読めた標本の数、perf_reported_samples = perf が書いたと言った数、unparsed = 読めなかった行の数と
        unparsed_examples（頭から 5 行まで）、evicted = 入れたことでメモリから押し出したほかの出来事の数。"""
        t = self._lt(target)
        self._need_perf(t)
        if not 0 < duration_s <= 600 or not 1 <= frequency_hz <= 100_000 or not 1 <= max_samples <= KEEP:
            raise ValueError(f"duration_s は 0 より大きく 600 以下、frequency_hz は 1〜100000、max_samples は 1〜{KEEP}")
        pre, stdin = self._sudo(t)
        where = perf_where(pid)
        script = (f'd=$(mktemp); trap \'rm -f "$d" "$d.old" "$d.txt"\' EXIT; '
                  f'perf record --clockid monotonic_raw --sample-cpu -F {int(frequency_hz)} {where} -o "$d" '
                  f'-- sleep {duration_s:.6f} >/dev/null; rr=$?; '
                  f'perf script -i "$d" -F comm,pid,tid,cpu,time,period,event,ip,sym,dso > "$d.txt"; rs=$?; '
                  f'echo "fprobe_perf record=$rr script=$rs lines=$(wc -l < "$d.txt")" >&2; '
                  f'head -n {int(max_samples)} "$d.txt"')
        command = f"{pre}env LC_ALL=C sh -c {sh_quote(script)}"
        r = t.ssh.run(command, input=stdin, timeout=duration_s + 120)
        err = text_of(r.stderr)
        dropped_before = t.dropped
        samples, unparsed, examples = 0, 0, []
        for line in text_of(r.stdout).splitlines():
            s = parse_perf_sample(line)
            if s is None:
                unparsed += 1
                if len(examples) < 5:
                    examples.append(line)
                continue
            samples += 1
            self._linux_keep(target, t, {"method": "perf", "probe": s["sym"], "pid": s["pid"], "tid": s["tid"],
                                         "cpu": s["cpu"], "comm": s["comm"], "period": s["period"], "event": s["event"],
                                         "ip": s["ip"], "dso": s["dso"], "target_ns": s["t_ns"],
                                         "clock": "CLOCK_MONOTONIC_RAW", "resolution_ns": 1000}, flush=False)
        self._linux_flush(target, t, final=False)                    # まとめて 1 回（標本ごとに全部を見直さない）
        m = re.search(r"\((\d+) samples\)", err)
        x = re.search(r"fprobe_perf record=(\d+) script=(\d+) lines=\s*(\d+)", err)
        return {"samples": samples, "unparsed": unparsed, "unparsed_examples": examples,
                "perf_reported_samples": int(m.group(1)) if m else None,
                "record_exit_code": int(x.group(1)) if x else None, "script_exit_code": int(x.group(2)) if x else None,
                "script_lines": int(x.group(3)) if x else None, "evicted": t.dropped - dropped_before,
                "exit_code": r.returncode, "stderr": err, "command": command}

    # --- gdb（止めて調べる。段階 3）--------------------------------------------------------

    def linux_gdb_batch(self, target: str, commands: list[str], timeout_s: float, pid: int | None = None,
                        core: str | None = None, binary: str | None = None) -> dict:
        """ターゲットの gdb を -batch -nx で走らせ、commands を順に実行した出力をそのまま返す（pid につける間、プロセスは止まる。
        core なら binary と core のファイルを読む）。命令は 1 つずつ -ex の引数にし、シェルの単語として引用する。"""
        t = self._lt(target)
        if (pid is None) == (core is None):
            raise ValueError("pid か core のどちらか 1 つを渡す")
        if core is not None and not binary:
            raise ValueError("core を読むには binary（そのプロセスの実行ファイル）が要る")
        if not t.caps.get("tools", {}).get("gdb"):
            raise RuntimeError("ターゲットに gdb が無い（linux.capabilities の tools.gdb）")
        where = f"-p {int(pid)}" if pid is not None else f"{sh_quote(binary)} {sh_quote(core)}"
        # ターゲットの timeout で gdb を終わらせる（SIGTERM。つけたプロセスからは外れる）: PC の ssh を止めるだけだと
        # ターゲットの gdb が残ってプロセスにつきっぱなしになる
        pre, stdin = self._sudo(t)
        command = " ".join([f"{pre}timeout -s TERM {timeout_s:g} gdb -batch -nx {where}",
                            *(f"-ex {sh_quote(c)}" for c in commands)])
        try:
            r = t.ssh.run(command, input=stdin, timeout=timeout_s + BATCH_BACKSTOP_S)
        except SshTimeout as e:                              # 途中までの出力は事実として返す
            return {"exit_code": None, "timed_out": True, "stdout": text_of(e.stdout),
                    "stderr": text_of(e.stderr), "command": command}
        return {"exit_code": r.returncode, "timed_out": r.returncode == 124,     # 124 = timeout が止めた
                "stdout": text_of(r.stdout), "stderr": text_of(r.stderr), "command": command}

    def linux_gdb_serve(self, target: str, pid: int, port: int) -> dict:
        """ターゲットで gdbserver を pid につけ（標準入出力。sudo なら sudo -n）、PC の 127.0.0.1:port で gdb の接続を 1 つ待って
        中継する（gdbserver は gdb がつないだときに起動する。止めて調べる間、プロセスは止まる）。"""
        t = self._lt(target)
        if not t.caps.get("tools", {}).get("gdbserver"):
            raise RuntimeError("ターゲットに gdbserver が無い（linux.capabilities の tools.gdbserver。入れた後は linux.connect し直す）")
        if t.gdb is not None:
            t.gdb.stop()
        pre, stdin = self._sudo(t)
        command = f"{pre}gdbserver --once --attach - {int(pid)}"
        t.gdb = GdbRelay(t.ssh, command, port, stdin_prefix=stdin or b"")
        return {"port": port, "command": command, "connect": f"target remote 127.0.0.1:{port}"}

    def linux_gdb_status(self, target: str) -> dict:
        t = self._lt(target)
        return t.gdb.status() if t.gdb is not None else dict(EMPTY_STATUS)

    def linux_gdb_stop(self, target: str) -> dict:
        t = self._lt(target)
        if t.gdb is None:
            return dict(EMPTY_STATUS)
        st, t.gdb = t.gdb.stop(), None
        return st
