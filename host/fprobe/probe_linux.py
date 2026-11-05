"""組み込み Linux のアプリのデバッグ（R26 段階 1、道 A）: linux.* の API。

SSH でつないだターゲットごとに、能力の事実・エージェント・プローブ・時刻合わせ・出来事を持つ。出来事はメモリに置き
（linux.events.read）、記録中なら `<ターゲット>.<方式>` の stream に入れる。仕様 docs/superpowers/specs/2026-10-22-linux-app-debug-design.md。

領域ごとの分け方: 状態の入れ物は linux/target.py、ftrace・ログ・core・perf・gdb の道具は probe_linux_tools.py、リングと
止めずに読む口は probe_linux_ring.py、出来事の入れ方は probe_linux_events.py（いずれも LinuxService に混ぜ込む部品）。ここには接続・エージェント・プローブ・
時刻合わせ・記録の始め終わりを置く。"""
from __future__ import annotations

import hashlib
import itertools
import os
import threading
import time

from .linux.agent import BINARY, Agent, deploy
from .linux.caps import CAPS_SCRIPT, parse_caps
from .linux.ptrace_plan import plan_breakpoint
from .linux.shell import sh_quote, text_of
from .linux.sysroot import sysroot_file
from .linux.ssh import GuardedSsh, SshTarget, TargetHalted
from .linux.target import Target as _Target
from .linux.uprobe import GROUP, NAME, site_of, uprobe_line
from .linux.ring_cpu import ArmACpu, MemapReader, sync_uncertainty  # noqa: F401
from .probe_linux_events import LinuxEventsMixin
from .probe_linux_ring import LinuxRingMixin
from .probe_linux_tools import BATCH_BACKSTOP_S, FTRACE_EVENT, LinuxToolsMixin  # noqa: F401  （旧名を保つ）
from .probe_service import HostView

_ArmACpu = ArmACpu                                   # 試験が旧名で取る
_MemapReader = MemapReader

METHODS = ("uprobe", "ptrace")


class LinuxService(LinuxToolsMixin, LinuxRingMixin, LinuxEventsMixin):
    API = ("linux_agent_deploy", "linux_agent_start", "linux_agent_status", "linux_agent_stop", "linux_capabilities",
           "linux_connect", "linux_core_fetch", "linux_core_limit_set", "linux_core_pattern_set", "linux_core_status",
           "linux_disconnect", "linux_events_read", "linux_ftrace_disable", "linux_ftrace_enable", "linux_gdb_batch",
           "linux_gdb_serve", "linux_gdb_status", "linux_gdb_stop", "linux_log_read", "linux_memap_connect",
           "linux_memap_connect_jtag", "linux_memap_disconnect", "linux_perf_record", "linux_perf_stat",
           "linux_probe_add", "linux_probe_list", "linux_probe_remove", "linux_ring_open", "linux_ring_read",
           "linux_ring_unwatch", "linux_ring_watch", "linux_sync", "linux_targets", "linux_trace_start",
           "linux_trace_stop")
    # ほかの領域（Probe の委譲）・試験に出す名前
    SHARED = ("_close_linux", "_linux", "_linux_before_recording_stop", "_linux_event", "_linux_flush", "_linux_keep",
              "_linux_ring_cpu", "_linux_ring_record", "_running")
    # ホスト（Probe の核と、ほかの領域）から使う名前
    # _linux_halted_reason: 対象が linux.halt で全コアを止められているか（止まっていれば ssh・エージェントを叩かない）
    NEEDS = ("_debug", "_ensure_design", "_jtag_wire", "_linux_halted_reason", "_lock", "_raw", "_recorder",
             "_require_disconnected")

    def __init__(self, host):
        self._host = HostView(host, self.NEEDS, type(self).__name__)
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
        r = t.ssh.run(f"{pre}sh -c {sh_quote(cmd)}" if t.sudo else cmd, input=stdin, timeout=timeout)
        if r.returncode:
            raise RuntimeError(f"ターゲットで失敗した（{r.returncode}）: {cmd}: {text_of(r.stderr).strip()}")
        return text_of(r.stdout)

    # --- つなぐ・能力 ----------------------------------------------------------------

    def linux_connect(self, target: str, host: str, user: str, port: int = 22, key: str | None = None,
                      sudo: bool = False, sudo_password_env: str | None = None) -> dict:
        reason = self._host._linux_halted_reason(target)
        if reason is not None:                               # 止まった対象: 切り直さず、叩かない
            raise TargetHalted(reason)
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
                raise RuntimeError(f"sudo のパスワードの確かめに失敗した（{v.returncode}）: {text_of(v.stderr).strip()}")
        r = ssh.run("sh -s", input=CAPS_SCRIPT.encode(), timeout=60)
        if r.returncode:
            raise RuntimeError(f"{user}@{host} に入れない（{r.returncode}）: {text_of(r.stderr).strip()}")
        caps = parse_caps(text_of(r.stdout))
        guarded = GuardedSsh(ssh, lambda: self._host._linux_halted_reason(target))
        self._linux[target] = _Target(guarded, sudo, caps, sudo_password_env)
        return caps

    def linux_disconnect(self, target: str) -> dict:
        """切る。対象が linux.halt で止まっていれば、対象の上の後始末（ftrace・プローブ・エージェントの終わり）は叩かずに
        手元だけ閉じ、叩かなかったことを remote_cleanup_skipped（理由）で返す。"""
        t = self._lt(target)
        halted = self._host._linux_halted_reason(target)
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
        out = {"target": target, "disconnected": True}
        if halted is not None:
            out["remote_cleanup_skipped"] = halted
        return out

    def linux_capabilities(self, target: str) -> dict:
        return self._lt(target).caps

    def linux_targets(self) -> dict:
        """いま linux.connect でつないでいるターゲットの一覧（名前と、つないだときに採った能力。複数を同時に持てる）。
        人の画面が、AI がつないだものも映すための読み（接続の状態の置き場所はデーモンの 1 か所）。"""
        return {"targets": [{"target": name, "capabilities": t.caps} for name, t in self._linux.items()]}

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
        # 起動していなければ、Agent.status と同じ欄を「何も無い」で（応答の形を道筋で変えない）
        st = t.agent.status() if t.agent else {"running": False, "exit_code": None, "stderr_tail": "", "watchdog_s": None,
                                               "event_errors": 0, "last_event_error": None, "bad_lines": 0}
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
        local = sysroot_file(target, binary)
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
            self._sh(t, f"echo {sh_quote(line)} >> {events}/uprobe_events && echo 1 > {events}/events/{GROUP}/{name}/enable")
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
            except TargetHalted:
                continue                             # 止まっている間は休み、走り出したら続ける
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
