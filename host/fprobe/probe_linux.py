"""組み込み Linux のアプリのデバッグ（R26 段階 1、道 A）: linux.* の API。

SSH でつないだターゲットごとに、能力の事実・エージェント・プローブ・時刻合わせ・出来事を持つ。出来事はメモリに置き
（linux.events.read）、記録中なら `<ターゲット>.<方式>` の stream に入れる。仕様 docs/superpowers/specs/2026-10-22-linux-app-debug-design.md。"""
from __future__ import annotations

import collections
import hashlib
import itertools
import json
import os
import re
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path

from .linux.agent import BINARY, Agent, deploy
from .linux.caps import CAPS_SCRIPT, parse_caps
from .linux.clock import TargetClock
from .linux.gdb_relay import EMPTY_STATUS, GdbRelay
from .linux.memap import XsdbMemAp
from .linux.ptrace_plan import plan_breakpoint
from .linux.perf import parse_perf_sample, parse_perf_stat
from .linux.ring import HEADER, RECORD, RingError, RingReader, parse_header
from .linux.ssh import SshTarget, SshTimeout
from .linux.uprobe import GROUP, NAME, parse_lost, parse_trace_line, site_of, uprobe_line

KEEP = 100_000                                       # メモリに持つ出来事の数（あふれた数は dropped）
BATCH_BACKSTOP_S = 10                                # gdb.batch: ターゲットの timeout が効かないときの PC 側の待ちの上乗せ
METHODS = ("uprobe", "ptrace")
FTRACE_EVENT = re.compile(r"[a-z0-9_-]+/[a-z0-9_]+")       # tracefs の events/<グループ>/<名前>（fullmatch で使う）
_PERF_EVENT = re.compile(r"^[A-Za-z0-9_:\-./]+$")           # perf のイベントの名前（シェルに渡すので形を限る）


def _ring_stats() -> dict:
    return {"reads": 0, "halted_ms_total": 0.0, "halted_ms_max": 0.0, "records": 0, "lost": 0, "torn": 0, "corrupt": 0,
            "header_errors": 0, "error": None}


def _count_ring_read(stats: dict, out: dict, records: list[dict], halted_ms: float, header_error: bool) -> None:
    """1 回の linux.ring.read の結果を ring_stats に足す（t.lock の中で）。"""
    stats["reads"] += 1
    stats["halted_ms_total"] += halted_ms
    stats["halted_ms_max"] = max(stats["halted_ms_max"], halted_ms)
    stats["records"] += sum(not r["duplicate"] for r in records)
    stats["lost"] += sum(b - a for a, b in out["lost"])
    stats["torn"] += len(out["torn"])
    stats["corrupt"] += len(out["corrupt"])
    stats["header_errors"] += header_error


def _text(data: bytes) -> str:
    """ターゲットの出力（バイト列）を文字に（読めないバイトは置き換える）。"""
    return data.decode(errors="replace")


def _where(pid: int | None) -> str:
    """perf の対象: プロセス pid か、無ければ全体 -a。"""
    return f"-p {int(pid)}" if pid is not None else "-a"


def _home() -> Path:
    return Path(os.environ.get("FPROBE_HOME", Path.home() / ".fprobe")) / "linux"


@dataclass
class _Target:
    ssh: SshTarget
    sudo: bool
    caps: dict
    sudo_password_env: str | None = None             # sudo のパスワードを入れた環境変数の名前（パスワードは持たない）
    agent: Agent | None = None
    deployed: dict | None = None
    probes: dict[str, dict] = field(default_factory=dict)
    by_bp: dict[int, str] = field(default_factory=dict)
    next_bp: int = 0
    clock: TargetClock = field(default_factory=TargetClock)
    events: collections.deque = field(default_factory=lambda: collections.deque(maxlen=KEEP))
    seq: int = 0
    pending: list = field(default_factory=list)   # (出来事, 受けたときの記録): 後ろに組ができるのを待つ（記録中に受けた分だけ）
    examined: int = 0                                # pending の先頭のうち、組の最後の時刻 examined_last で調べ済み（まだ待つ）の数
    examined_last: int | None = None
    not_recorded: int = 0                            # 記録中に受けたのに記録に入れられなかった数（組が足りない・記録が替わった）
    dropped: int = 0
    trace_clock: str | None = None                   # uprobes の出来事の時計（trace_clock の名前）
    sync_stop: threading.Event | None = None
    sync_error: str | None = None
    by_seq: collections.OrderedDict = field(default_factory=collections.OrderedDict)   # 通しの番号 → 出来事（A と B を 1 つに）
    ring: dict | None = None                         # ring_open の結果（phys・size・capacity・first）
    ring_reader: RingReader | None = None
    ring_stats: dict = field(default_factory=_ring_stats)
    ring_stop: threading.Event | None = None
    gdb: GdbRelay | None = None                     # linux.gdb.serve の中継
    memap: object | None = None                      # linux.memap.connect の止めずに読む口（read_words・info・close）
    ftrace: set = field(default_factory=set)         # 有効にした tracefs のイベント（group/name）
    trace_unparsed: int = 0                          # trace_pipe の、形の分からない行の数（捨てた）
    trace_unrouted: int = 0                          # 有効にしていないイベントの行の数（捨てた。ほかの人の tracefs の使用等）
    lock: threading.Lock = field(default_factory=threading.Lock)


class LinuxTools:
    def _init_linux(self) -> None:
        self._linux: dict[str, _Target] = {}

    def _close_linux(self) -> None:
        for name in list(self._linux):
            self.linux_disconnect(target=name)

    def _lt(self, target: str) -> _Target:
        t = self._linux.get(target)
        if t is None:
            raise ValueError(f"Linux のターゲット {target} につないでいない（linux.connect）")
        return t

    @staticmethod
    def _sudo(t: _Target) -> tuple[str, bytes | None]:
        """(命令の頭, stdin に先に送るもの)。パスワードの要る sudo は sudo -S で、環境変数のパスワードを最初の行で渡す。"""
        if not t.sudo:
            return "", None
        if not t.sudo_password_env:
            return "sudo -n ", None
        password = os.environ.get(t.sudo_password_env)
        if password is None:
            raise RuntimeError(f"sudo のパスワードの環境変数 {t.sudo_password_env} が無い（linux.connect の sudo_password_env）")
        return "sudo -S -k -p '' ", (password + "\n").encode()     # -k: 覚えた資格を使わず必ず stdin の行を読む

    def _sh(self, t: _Target, cmd: str, timeout: float = 30) -> str:
        pre, stdin = self._sudo(t)
        r = t.ssh.run(f"{pre}sh -c {_quote(cmd)}" if t.sudo else cmd, input=stdin, timeout=timeout)
        if r.returncode:
            raise RuntimeError(f"ターゲットで失敗した（{r.returncode}）: {cmd}: {_text(r.stderr).strip()}")
        return _text(r.stdout)

    # --- つなぐ・能力 ----------------------------------------------------------------

    def linux_connect(self, target: str, host: str, user: str, port: int = 22, key: str | None = None,
                      sudo: bool = False, sudo_password_env: str | None = None) -> dict:
        if target in self._linux:
            self.linux_disconnect(target=target)
        if sudo_password_env and not sudo:
            raise ValueError("sudo_password_env は sudo=true のときだけ（パスワードの要る sudo のターゲット）")
        ssh = SshTarget(host, user, port, key)
        if sudo_password_env:                                # パスワードを先に確かめる（違えば後の命令の stdin を食わせない）
            password = os.environ.get(sudo_password_env)
            if password is None:
                raise RuntimeError(f"sudo のパスワードの環境変数 {sudo_password_env} が無い")
            v = ssh.run("sudo -S -k -v -p ''", input=(password + "\n").encode(), timeout=30)
            if v.returncode:
                raise RuntimeError(f"sudo のパスワードの確かめに失敗した（{v.returncode}）: {_text(v.stderr).strip()}")
        r = ssh.run("sh -s", input=CAPS_SCRIPT.encode(), timeout=60)
        if r.returncode:
            raise RuntimeError(f"{user}@{host} に入れない（{r.returncode}）: {_text(r.stderr).strip()}")
        caps = parse_caps(_text(r.stdout))
        self._linux[target] = _Target(ssh, sudo, caps, sudo_password_env)
        return caps

    def linux_disconnect(self, target: str) -> dict:
        t = self._lt(target)
        self._linux_stop_sync(t)
        if t.ring_stop is not None:
            t.ring_stop.set()
        if t.gdb is not None:
            t.gdb.stop()
        if t.memap is not None:
            t.memap.close()
        if t.ftrace:
            try:                                             # 有効にしたカーネルのイベントを残さない
                self.linux_ftrace_disable(target=target, events=sorted(t.ftrace))
            except (RuntimeError, ValueError):
                pass
        for name in list(t.probes):
            try:
                self.linux_probe_remove(target=target, name=name)
            except (RuntimeError, OSError):
                pass                                 # 切れていても外す（残ったものは list で見えない。エージェントの終わりで ptrace は外れる）
        if t.agent is not None:
            t.agent.stop()
        del self._linux[target]
        return {"target": target, "disconnected": True}

    def linux_capabilities(self, target: str) -> dict:
        return self._lt(target).caps

    # --- エージェント ----------------------------------------------------------------

    def linux_agent_deploy(self, target: str) -> dict:
        t = self._lt(target)
        t.deployed = deploy(t.ssh)
        return t.deployed

    def linux_agent_start(self, target: str, watchdog_s: float | None = None) -> dict:
        t = self._lt(target)
        if self._running(t):
            return {"hello": None, "already_running": True, "watchdog_s": t.agent.watchdog_s}   # 今効いている値（求めた値ではない）
        with t.lock:                                         # 新しいエージェントの seq は 0 から（前の番号と取り違えない）
            t.by_seq.clear()
            t.ring, t.ring_reader = None, None
        _, stdin = self._sudo(t)
        t.agent = Agent(t.ssh, BINARY, t.sudo, lambda ev: self._linux_event(target, ev), sudo_input=stdin)
        return {"hello": t.agent.start(watchdog_s=watchdog_s), "already_running": False, "watchdog_s": watchdog_s}

    def linux_agent_status(self, target: str) -> dict:
        t = self._lt(target)
        st = t.agent.status() if t.agent else {"running": False, "exit_code": None, "stderr_tail": ""}
        return {**st, "deployed": t.deployed, "sync_error": t.sync_error, "ring": t.ring, "ring_stats": dict(t.ring_stats),
                "trace_unparsed": t.trace_unparsed, "trace_unrouted": t.trace_unrouted}

    def linux_agent_stop(self, target: str) -> dict:
        t = self._lt(target)
        self._linux_stop_sync(t)
        self.linux_ring_unwatch(target=target)               # 止めたエージェントのリングを読み続けない
        if t.agent is not None:
            t.agent.stop()                           # エージェントは止まる前に ptrace の bp を元に戻して外す
        cleared = [n for n, p in t.probes.items() if p["method"] == "ptrace"]
        for n in cleared:
            t.by_bp.pop(t.probes.pop(n).get("bp"), None)
        return {**self.linux_agent_status(target=target), "cleared_probes": cleared}

    @staticmethod
    def _running(t: _Target) -> bool:
        return t.agent is not None and t.agent.status()["running"]

    def _agent(self, t: _Target) -> Agent:
        if not self._running(t):
            raise RuntimeError("エージェントが動いていない（linux.agent.start）")
        return t.agent

    # --- プローブ --------------------------------------------------------------------

    def linux_probe_add(self, target: str, name: str, binary: str, method: str, symbol: str | None = None,
                        address: int | None = None, isa: str | None = None, fetchargs: list[str] | None = None,
                        regs: list[str] | None = None, ret: bool = False, pid: int | None = None) -> dict:
        t = self._lt(target)
        if method not in METHODS:
            raise ValueError(f"method は {' / '.join(METHODS)}")
        if not NAME.match(name):
            raise ValueError(f"プローブの名前 {name!r} は英数字と _（先頭は数字以外、64 文字まで）")
        if name in t.probes:
            raise ValueError(f"プローブ {name} はもうある")
        local = _home() / target / "sysroot" / binary.lstrip("/")
        t.ssh.get(binary, local)                     # シンボルのため、ターゲットのバイナリをそのまま取り寄せる
        out = {"name": name, "method": method, "binary": binary, "sysroot_file": str(local),
               "sha256": hashlib.sha256(local.read_bytes()).hexdigest()}
        site = site_of(local, binary, symbol=symbol, address=address, isa=isa)
        out["site"] = {"offset": site.offset, "vaddr": site.vaddr, "thumb": site.thumb, "machine": site.machine}
        if method == "uprobe":
            line = uprobe_line(name, site, fetchargs or [], ret)       # Thumb 等を先に断る（ELF の事実）
            if not t.caps.get("uprobe_events"):
                raise RuntimeError("このカーネルに uprobe_events が無い（CONFIG_UPROBE_EVENTS: "
                                   f"{t.caps['config'].get('CONFIG_UPROBE_EVENTS')}）。ptrace を使う")
            events = t.caps["tracefs"]
            self._sh(t, f"echo {_quote(line)} >> {events}/uprobe_events && echo 1 > {events}/events/{GROUP}/{name}/enable")
            out["line"] = line
        else:
            if pid is None:
                raise ValueError("ptrace は pid が要る")
            plan = plan_breakpoint(local, site)
            agent = self._agent(t)
            t.next_bp += 1
            bp = t.next_bp
            t.by_bp[bp] = name                       # 返事より先に届く当たりのため、送る前に名前を置く
            try:
                reply = agent.request("bp_add", bp=bp, pid=pid, path=plan["path"], offset=plan["offset"],
                                      thumb=plan["thumb"], length=plan["length"], regs=regs or [])
            except RuntimeError:
                t.by_bp.pop(bp, None)
                raise
            out.update(plan=plan, pid=pid, regs=regs or [], bp=bp, address=reply.get("address"),
                       threads=reply.get("threads"))
        t.probes[name] = out
        return out

    def linux_probe_list(self, target: str) -> dict:
        return {"probes": list(self._lt(target).probes.values())}

    def linux_probe_remove(self, target: str, name: str) -> dict:
        t = self._lt(target)
        p = t.probes.get(name)
        if p is None:
            raise ValueError(f"プローブ {name} は無い")
        if p["method"] == "uprobe":
            events = t.caps["tracefs"]
            self._sh(t, f"echo 0 > {events}/events/{GROUP}/{name}/enable; echo -:{GROUP}/{name} >> {events}/uprobe_events")
        elif not p.get("ended") and self._running(t):
            try:
                t.agent.request("bp_remove", bp=p["bp"])
            except RuntimeError as e:
                if "no breakpoint" not in str(e):
                    raise
                p["ended"] = {"agent": str(e)}           # エージェントにはもう無い（事実として返す）
        t.by_bp.pop(p.get("bp"), None)
        del t.probes[name]
        out = {"removed": name}
        if p.get("ended"):
            out["ended"] = p["ended"]
        elif p["method"] == "ptrace" and not self._running(t):
            out["agent_running"] = False                  # エージェントが先に終わった: ターゲットに BKPT が残っているかもしれない
        return out

    # --- 時刻合わせ・記録 ------------------------------------------------------------

    def linux_sync(self, target: str, count: int) -> dict:
        """エージェントの時計を count 回問い、（ターゲットの時刻、PC の往復の中点、往復の半分）の組を足す。"""
        t = self._lt(target)
        agent = self._agent(t)
        pairs = []
        for _ in range(count):
            h0 = time.monotonic()
            t_ns = agent.request("time")["t_ns"]
            h1 = time.monotonic()
            with t.lock:
                t.clock.add(t_ns, (h0 + h1) / 2, (h1 - h0) / 2)
            pairs.append({"target_ns": t_ns, "host_s": (h0 + h1) / 2, "half_rtt_s": (h1 - h0) / 2})
        self._linux_flush(target, t, final=False)
        halves = [p["half_rtt_s"] for p in pairs]
        return {"pairs": pairs, "half_rtt_s": {"min": min(halves), "max": max(halves)} if halves else None}

    def linux_trace_start(self, target: str, sync_interval_s: float) -> dict:
        """uprobes の出来事を読み始め（uprobe があれば）、sync_interval_s ごとに時刻合わせを続ける。"""
        t = self._lt(target)
        agent = self._agent(t)
        out = {"trace_clock": None, "trace_pipe": False}
        if any(p["method"] == "uprobe" for p in t.probes.values()) or t.ftrace:
            tracefs = t.caps["tracefs"]
            if "mono_raw" in (t.caps.get("trace_clocks") or []):
                self._sh(t, f"echo mono_raw > {tracefs}/trace_clock")
            t.trace_clock = self._sh(t, f"cat {tracefs}/trace_clock").split("[", 1)[-1].split("]", 1)[0]
            self._sh(t, f"echo 1 > {tracefs}/tracing_on")
            agent.request("trace_open", path=f"{tracefs}/trace_pipe")
            out.update(trace_clock=t.trace_clock, trace_pipe=True)
        self._linux_stop_sync(t)
        t.sync_stop = threading.Event()
        t.sync_error = None
        threading.Thread(target=self._linux_sync_loop, args=(target, t, sync_interval_s), daemon=True,
                         name=f"linux-sync-{target}").start()
        return out

    def _linux_sync_loop(self, target: str, t: _Target, interval: float) -> None:
        stop = t.sync_stop
        while not stop.wait(interval):
            try:
                self.linux_sync(target=target, count=1)
            except (RuntimeError, ValueError) as e:
                t.sync_error = str(e)                # status に事実で出す（調停を止めない）
                return

    def _linux_before_recording_stop(self) -> None:
        """記録を止める前に、組を待っている出来事を入れる（後ろに組を 1 つ置いてから。置けなければ outside のまま）"""
        for name, t in list(self._linux.items()):
            if not t.pending:
                continue
            if self._running(t):
                try:
                    self.linux_sync(target=name, count=1)
                except RuntimeError:
                    pass                             # 組を置けなくても、残りは outside=true で入る
            self._linux_flush(name, t, final=True)

    # --- カーネルの出来事・ログ（段階 4）------------------------------------------------------

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
            command += f" -u {_quote(unit)}"
        if since_cursor:
            command += f" --after-cursor {_quote(since_cursor)}"
        r = t.ssh.run(command, input=stdin, timeout=60)
        entries, bad = [], 0
        for line in _text(r.stdout).splitlines():
            try:
                entry = json.loads(line)
            except ValueError:
                entry = None
            if isinstance(entry, dict):
                entries.append(entry)
            else:
                bad += 1
        return {"entries": entries, "next_cursor": entries[-1].get("__CURSOR") if entries else None,
                "clock": "CLOCK_MONOTONIC", "exit_code": r.returncode, "stderr": _text(r.stderr),
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
        self._sh(t, f"printf %s {_quote(pattern)} > /proc/sys/kernel/core_pattern")
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
        r = t.ssh.run(f"{pre}cat -- {_quote(path)}", input=stdin, timeout=300)
        if r.returncode:
            raise RuntimeError(f"{path} を読めない（{r.returncode}）: {_text(r.stderr).strip()}")
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
        where = _where(pid)
        command = f"{pre}env LC_ALL=C perf stat --no-scale -x, -e {','.join(events)} {where} -- sleep {duration_s:.6f}"
        r = t.ssh.run(command, input=stdin, timeout=duration_s + 60)
        err = _text(r.stderr)
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
        where = _where(pid)
        script = (f'd=$(mktemp); trap \'rm -f "$d" "$d.old" "$d.txt"\' EXIT; '
                  f'perf record --clockid monotonic_raw --sample-cpu -F {int(frequency_hz)} {where} -o "$d" '
                  f'-- sleep {duration_s:.6f} >/dev/null; rr=$?; '
                  f'perf script -i "$d" -F comm,pid,tid,cpu,time,period,event,ip,sym,dso > "$d.txt"; rs=$?; '
                  f'echo "fprobe_perf record=$rr script=$rs lines=$(wc -l < "$d.txt")" >&2; '
                  f'head -n {int(max_samples)} "$d.txt"')
        command = f"{pre}env LC_ALL=C sh -c {_quote(script)}"
        r = t.ssh.run(command, input=stdin, timeout=duration_s + 120)
        err = _text(r.stderr)
        dropped_before = t.dropped
        samples, unparsed, examples = 0, 0, []
        for line in _text(r.stdout).splitlines():
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
        where = f"-p {int(pid)}" if pid is not None else f"{_quote(binary)} {_quote(core)}"
        # ターゲットの timeout で gdb を終わらせる（SIGTERM。つけたプロセスからは外れる）: PC の ssh を止めるだけだと
        # ターゲットの gdb が残ってプロセスにつきっぱなしになる
        pre, stdin = self._sudo(t)
        command = " ".join([f"{pre}timeout -s TERM {timeout_s:g} gdb -batch -nx {where}",
                            *(f"-ex {_quote(c)}" for c in commands)])
        try:
            r = t.ssh.run(command, input=stdin, timeout=timeout_s + BATCH_BACKSTOP_S)
        except SshTimeout as e:                              # 途中までの出力は事実として返す
            return {"exit_code": None, "timed_out": True, "stdout": _text(e.stdout),
                    "stderr": _text(e.stderr), "command": command}
        return {"exit_code": r.returncode, "timed_out": r.returncode == 124,     # 124 = timeout が止めた
                "stdout": _text(r.stdout), "stderr": _text(r.stderr), "command": command}

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

    # --- 道 B: リング ------------------------------------------------------------------

    def linux_ring_open(self, target: str, phys: int, size: int, cached: bool, linger: bool) -> dict:
        """エージェントに、物理番地 phys から size バイトをリングとして開かせる（/dev/mem。cached = false なら O_SYNC）。
        linger = true なら、SSH が切れても ptrace しているプロセスが終わるまで、ptrace とリングへの書き込みを続ける。"""
        t = self._lt(target)
        reply = self._agent(t).request("ring_open", phys=phys, size=size, cached=cached, linger=linger)
        with t.lock:
            t.ring = {"phys": phys, "size": size, "cached": cached, "linger": linger, "capacity": reply["capacity"],
                      "first": reply["first"]}
            t.ring_reader = None
            t.ring_stats = _ring_stats()
        return t.ring

    def linux_memap_connect(self, target: str, xsdb: str, cable_serial: str, chain_idcode: int, timeout_s: float) -> dict:
        """止めずに読む口（memap）を開く: Vivado の xsdb（hw_server）で、ボードの USB の JTAG から DAP の AHB-AP で物理メモリを読む。
        xsdb の場所・JTAG のケーブルのシリアル・チェーンにあるはずの装置の IDCODE（違うボードを読まない）を明示する。"""
        t = self._lt(target)
        if t.memap is not None:
            t.memap.close()
            t.memap = None
        t.memap = XsdbMemAp(xsdb, cable_serial=cable_serial, chain_idcode=chain_idcode, timeout_s=timeout_s)
        return t.memap.info

    def linux_memap_disconnect(self, target: str) -> dict:
        t = self._lt(target)
        if t.memap is not None:
            t.memap.close()
            t.memap = None
        return {"memap": None}

    def _linux_ring_cpu(self):
        """止めて読む CPU（target.connect で Cortex-A のコアにつないだもの）"""
        target = getattr(self._debug, "target", None) if self._debug is not None else None
        if target is None or not hasattr(target, "core") or not hasattr(target.core, "read_memory_fast"):
            raise RuntimeError("Cortex-A のコアに target.connect していない（道 B の access=halt はそのコアに読ませる）")
        return _ArmACpu(target)

    def _linux_ring_reader(self, t: _Target, access: str):
        """access に合う読み手（halt = 止めて読む CPU、memap = 止めずに読む口）。リングのキャッシュの開き方と合わなければ誤り。"""
        if access == "halt":
            if t.ring is not None and not t.ring["cached"]:
                raise ValueError("cached=false で開いたリングは access=halt で読まない: エージェントは O_SYNC（キャッシュしない）で書き、"
                                 "止めたコアはキャッシュする線形の写しで読むので、古い値を読みうる（cached=true で開き直す）")
            return self._linux_ring_cpu()
        if access == "memap":
            if t.memap is None:
                raise RuntimeError("止めずに読む口が無い（linux.memap.connect）")
            if t.ring is not None and t.ring["cached"]:
                raise ValueError("cached=true で開いたリングは access=memap で読まない: memap は CPU のキャッシュを通らずに物理メモリを"
                                 "読むので、キャッシュに残った書き込みが見えない（cached=false で開き直す）")
            return _MemapReader(t.memap)
        raise ValueError(f"access は halt / memap（{access!r}）")

    def linux_ring_read(self, target: str, address: int, access: str, max_records: int) -> dict:
        """リングの新しい記録を、古い方から max_records 個まで読む。address はリングの頭の、読み手から見た番地。
        access = "halt": target.connect でつないだコアを止めて、そのコアに読ませ（address は仮想番地）、すぐ再開する。
        access = "memap": linux.memap.connect の口で、止めずに読む（address は物理番地）。"""
        t = self._lt(target)
        cpu = self._linux_ring_reader(t, access)
        words = 0

        def read(address: int, count: int) -> bytes:
            nonlocal words
            words += count
            return b"".join(w.to_bytes(4, "little") for w in cpu.read_words(address, count))

        out = {"records": [], "lost": [], "torn": [], "corrupt": [], "head": None, "left": 0}
        header_error = None
        with self._lock:
            was = access == "halt" and cpu.state() == "halted"
            t0 = time.perf_counter()
            failed = False
            try:
                if access == "halt" and not was:
                    cpu.halt()                               # 止める要求の後の失敗でも、下の finally で必ず再開する
                try:
                    head = parse_header(read(address, HEADER // 4))
                    if t.ring is not None and head["capacity"] != t.ring["capacity"]:
                        raise RingError(f"頭の capacity {head['capacity']} が ring_open の {t.ring['capacity']} と違う")
                except RingError as e:
                    header_error = str(e)                    # 事実で返す（この読みの頭は使わない）
                else:
                    if t.ring_reader is None or (t.ring_reader.capacity, t.ring_reader.first) != (head["capacity"], head["first"]):
                        t.ring_reader = RingReader(head["capacity"], head["first"])
                    out = t.ring_reader.take(head["head"], lambda f, n: read(address + HEADER + f * RECORD, n * RECORD // 4),
                                             limit=max_records)
            except BaseException:
                failed = True
                raise
            finally:
                if access == "halt" and not was:
                    try:
                        cpu.resume()
                    except Exception:                        # noqa: BLE001  元の誤りがあればそちらを返す
                        if not failed:
                            raise
                read_ms = (time.perf_counter() - t0) * 1e3
                halted_ms = read_ms if access == "halt" else 0.0       # memap は止めない
        records = [self._linux_ring_record(target, t, r) for r in out["records"]]
        with t.lock:
            _count_ring_read(t.ring_stats, out, records, halted_ms, header_error is not None)
        return {"records": records, "lost": out["lost"], "torn": out["torn"], "corrupt": out["corrupt"],
                "head": out["head"], "left": out["left"], "header_error": header_error, "halted_ms": halted_ms,
                "read_ms": read_ms, "words": words, "core": cpu.index, "access": access,
                "kept_halted": was if access == "halt" else None}

    def _linux_ring_record(self, target: str, t: _Target, r: dict) -> dict:
        regs = dict(zip(("r0", "r1", "r2", "r3", "lr"), r["regs"]))
        body = {"method": "ptrace", "probe": t.by_bp.get(r["bp"], f"bp{r['bp']}"), "tid": r["tid"], "target_ns": r["t_ns"],
                "regs": regs, "clock": "CLOCK_MONOTONIC_RAW", "seq": r["seq"], "paths": ["b"]}
        kept = self._linux_keep(target, t, body)
        return {"agent_seq": r["seq"], "probe": body["probe"], "duplicate": not kept}

    def linux_ring_watch(self, target: str, interval_s: float, address: int, access: str, max_records: int) -> dict:
        """interval_s ごとに linux.ring.read を続ける（止めた回数と時間は linux.agent.status の ring_stats）。"""
        t = self._lt(target)
        self.linux_ring_unwatch(target=target)
        t.ring_stop = threading.Event()
        t.ring_stats["error"] = None
        stop = t.ring_stop

        def loop():
            while not stop.wait(interval_s):
                try:
                    self.linux_ring_read(target=target, address=address, access=access, max_records=max_records)
                except Exception as e:                       # noqa: BLE001  止めて事実を残す（調停を止めない）
                    t.ring_stats["error"] = f"{type(e).__name__}: {e}"
                    return
        threading.Thread(target=loop, daemon=True, name=f"linux-ring-{target}").start()   # 頭の誤りは事実として数え、続ける
        return {"watching": True}

    def linux_ring_unwatch(self, target: str) -> dict:
        t = self._lt(target)
        if t.ring_stop is not None:
            t.ring_stop.set()
            t.ring_stop = None
        return {"watching": False, "ring_stats": dict(t.ring_stats)}

    def _linux_stop_sync(self, t: _Target) -> None:
        if t.sync_stop is not None:
            t.sync_stop.set()
            t.sync_stop = None

    def linux_trace_stop(self, target: str) -> dict:
        t = self._lt(target)
        self._linux_stop_sync(t)
        if self._running(t):
            if t.trace_clock is not None:
                t.agent.request("trace_close")
            self.linux_sync(target=target, count=1)    # 最後の出来事の後ろに組を置いてから記録に入れる
        self._linux_flush(target, t, final=True)
        return {"stopped": True, "pairs": len(t.clock.pairs)}

    def linux_events_read(self, target: str, since: int = 0, limit: int = 200) -> dict:
        t = self._lt(target)
        with t.lock:
            records = [self._linux_converted(t, e)
                       for e in itertools.islice((e for e in t.events if e["seq"] > since), limit)]
            return {"records": records, "next": records[-1]["seq"] if records else max(since, 0),
                    "dropped": t.dropped, "not_recorded": t.not_recorded}

    # --- 出来事 ----------------------------------------------------------------------

    def _linux_event(self, target: str, ev: dict) -> None:
        t = self._linux.get(target)
        if t is None:
            return
        kind = ev.get("ev")
        if kind == "bp":
            name = t.by_bp.get(ev["bp"])
            if name is None:
                return
            seq = ev.get("seq")
            body = {"method": "ptrace", "probe": name, "tid": ev["tid"], "target_ns": ev["t_ns"], "regs": ev["regs"],
                    "clock": "CLOCK_MONOTONIC_RAW", "seq": seq, "paths": ["a"]}
        elif kind == "trace":
            body = self._linux_trace_body(target, t, ev["line"])
            if body is None:
                return
        else:
            if kind == "exit":                       # ptrace していたプロセスが終わった: その bp はエージェントから消えた
                for p in t.probes.values():
                    if p["method"] == "ptrace" and p.get("pid") == ev.get("pid"):
                        p["ended"] = {"code": ev.get("code"), "signal": ev.get("signal")}
            body = {"method": "agent", "probe": kind, "target_ns": None, "detail": ev}
        self._linux_keep(target, t, body)

    def _linux_trace_body(self, target: str, t: _Target, raw: str) -> dict | None:
        """trace_pipe の 1 行を出来事の本体に。捨てる行（形が分からない・有効にしていないイベント）は数えて None。
        カーネルのバッファのあふれは、その場で出来事として残して None（呼び手は入れ直さない）。"""
        # エージェントは 0x80 以上のバイトを u00XX の形で送る: バイトに戻して UTF-8 で読む
        line = raw.encode("latin-1", errors="replace").decode("utf-8", errors="replace")
        lost = parse_lost(line)
        if lost is not None:                             # カーネルのバッファがあふれて捨てた数（事実として残す）
            self._linux_keep(target, t, {"method": "agent", "probe": "trace_lost", "target_ns": None, "detail": lost})
            return None
        r = parse_trace_line(line)
        if r is None:
            t.trace_unparsed += 1
            return None
        probe = t.probes.get(r["event"])
        if r["ip"] is not None and probe is not None and probe["method"] == "uprobe":
            method = "uprobe"                            # uprobe の行は番地の括弧つき
        elif self._ftrace_enabled(t, r["event"]):
            method = "ftrace"
        else:
            t.trace_unrouted += 1                        # 有効にしていないイベント
            return None
        return {"method": method, "probe": r["event"], "pid": r["pid"], "comm": r["comm"], "cpu": r["cpu"],
                "target_ns": r["t_ns"], "args": r["args"], "text": r["text"], "clock": t.trace_clock}

    @staticmethod
    def _ftrace_enabled(t: _Target, printed: str) -> bool:
        """行に出る名前が、有効にしたイベントのものか。syscalls の sys_enter_X・sys_exit_X は、行に「sys_X」と出る（カーネルの書式）。"""
        for path in t.ftrace:
            group, name = path.split("/")
            if name == printed:
                return True
            if group == "syscalls" and name.startswith(("sys_enter_", "sys_exit_")) and \
                    "sys_" + name.split("_", 2)[2] == printed:
                return True
        return False

    @staticmethod
    def _convertible(body: dict) -> bool:
        # tracefs の mono_raw はエージェントの CLOCK_MONOTONIC_RAW と同じ時計
        return body["target_ns"] is not None and body.get("clock") in ("CLOCK_MONOTONIC_RAW", "mono_raw")

    @staticmethod
    def _linux_converted(t: _Target, body: dict) -> dict:
        """その時点の組で換算した写し（host_s・sync）。換算できなければそのまま。"""
        conv = t.clock.to_host(body["target_ns"]) if LinuxTools._convertible(body) else None
        if conv is None:
            return body
        return {**body, "host_s": conv["host_s"],
                "sync": {"method": "network", "half_rtt_ns": round(conv["half_rtt_s"] * 1e9),
                         "nearest_pair_ns": conv["nearest_pair_ns"], "outside": conv["outside"]}}

    def _linux_keep(self, target: str, t: _Target, body: dict, flush: bool = True) -> bool:
        """出来事をメモリ（と記録）に入れる。エージェントの seq が既にあれば（道 A と B の両方で受けた）入れずに道を足し、
        False を返す（調べるのと入れるのを 1 つのロックの中で: 2 つの読み手の取り合いで 2 つにしない）。"""
        with t.lock:
            seq = body.get("seq")
            known = t.by_seq.get(seq) if seq is not None else None
            if known is not None:
                for path in body.get("paths", []):
                    if path not in known["paths"]:
                        known["paths"].append(path)
                return False
            t.seq += 1
            if seq is not None:
                t.by_seq[seq] = body
                while len(t.by_seq) > KEEP:
                    t.by_seq.popitem(last=False)
            body["agent_seq"] = body.get("seq")
            body["seq"] = t.seq
            if len(t.events) == t.events.maxlen:
                t.dropped += 1
            t.events.append(body)
            rec = self._recorder
            if rec is not None and self._convertible(body):     # 記録中に受けた出来事だけを、その記録に入れる
                t.pending.append((body, rec))
        if flush:
            self._linux_flush(target, t, final=False)
        return True

    def _linux_flush(self, target: str, t: _Target, final: bool) -> None:
        """後ろに組がある出来事（final なら全部）を、内挿で換算して記録に入れる。記録していなければ捨てる（メモリには残る）。"""
        with t.lock:
            last = t.clock.pairs[-1][0] if t.clock.pairs else None
            # 組の最後の時刻が前に調べたときと同じなら、待っていた分はまだ待つ（新しく入った分だけを調べる。毎回全部を
            # 見直すと、組が来るまでの間に出来事が増えるほど 1 つごとの入れ方が遅くなる）
            start = t.examined if not final and last == t.examined_last else 0
            fresh = t.pending[start:]
            del t.pending[start:]                              # 調べ済みの分はそのまま（コピーしない）
            ready, wait = [], t.pending
            for e in fresh:
                (ready if final or (last is not None and e[0]["target_ns"] <= last) else wait).append(e)
            t.examined, t.examined_last = len(wait), last
            converted = [(self._linux_converted(t, e), rec) for e, rec in ready]
        for body, rec in converted:
            if rec is not self._recorder or "sync" not in body:
                t.not_recorded += 1                  # 記録が替わった・組が 2 つ未満（事実として数える）
                continue
            try:
                ticks, map_unc = rec.clock.to_ticks(body["host_s"])
            except RuntimeError:
                t.not_recorded += 1                  # 記録の時計の点がまだ無い
                continue
            body["sync"]["clockmap_uncertainty_ticks"] = map_unc
            rec.add(f"{target}.{body['method']}", body["probe"], ticks, body=body,
                    uncertainty=body["sync"]["half_rtt_ns"] / 1e9 * rec.clock.hz + map_unc)

class _MemapReader:
    """止めずに読む口を、止めて読む CPU と同じ形で使う（止める・再開は何もしない）"""

    index = None

    def __init__(self, memap):
        self.memap = memap

    def halt(self) -> None:
        pass

    def resume(self) -> None:
        pass

    def read_words(self, address: int, count: int) -> list[int]:
        return self.memap.read_words(address, count)


class _ArmACpu:
    """道 B の止めて読む CPU（fprobe.arm_a_target.ArmATarget の上）。再開に失敗したら、誤りの印を消して再開を求め直す。"""

    def __init__(self, target):
        self.target = target
        self.index = getattr(target, "index", None)

    def state(self) -> str:
        return self.target.core.state()

    def halt(self) -> None:
        self.target.halt()

    def resume(self) -> None:
        """再開する（借りたレジスタを戻してから）。一時的な誤りなら、誤りの印を消して同じ再開をもう 1 度。それでも失敗したら、
        止めたまま残さないために生の再開の要求を出し、レジスタを戻せていないかもしれない事実を誤りで返す。"""
        try:
            self.target.resume()
            return
        except Exception as first:                           # noqa: BLE001
            port = self.target.core.port
            try:
                port.dap.clear_errors()
            except Exception:                                # noqa: BLE001
                pass
            try:
                self.target.resume()
                return
            except Exception:                                # noqa: BLE001
                pass
            try:
                port.restart_request()
            except Exception as e:                           # noqa: BLE001
                raise RuntimeError(f"コアを再開できない（{first}; 生の再開の要求も失敗: {e}）。コアが止まったまま") from first
            raise RuntimeError(f"再開の手順が失敗した（{first}）。生の再開の要求で再開したが、借りたレジスタ（r0・r1）を"
                               "戻せていないかもしれない") from first

    def read_words(self, address: int, count: int) -> list[int]:
        return self.target.read_memory_block32(address, count)


def _quote(s: str) -> str:
    return "'" + s.replace("'", "'\\''") + "'"
