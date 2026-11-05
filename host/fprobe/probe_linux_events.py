"""Linux のターゲットの出来事の入れ方: エージェント・trace_pipe・perf・リングから届く出来事を、メモリ（と記録）に入れる。

LinuxService（fprobe.probe_linux）に混ぜ込む部品（LinuxEventsMixin）。エージェントの seq で重複を 1 つにし（道 A と B）、
組が後ろに来た出来事から PC の時刻へ内挿で換算して記録に入れる。換算した出来事の不確かさは sync_uncertainty。
"""
from __future__ import annotations

from .linux.ring_cpu import sync_uncertainty
from .linux.target import KEEP
from .linux.target import Target as _Target
from .linux.uprobe import parse_lost, parse_trace_line


class LinuxEventsMixin:
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
        conv = t.clock.to_host(body["target_ns"]) if LinuxEventsMixin._convertible(body) else None
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
            rec = self._host._recorder
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
            if rec is not self._host._recorder or "sync" not in body:
                t.not_recorded += 1                  # 記録が替わった・組が 2 つ未満（事実として数える）
                continue
            try:
                ticks, map_unc = rec.clock.to_ticks(body["host_s"])
            except RuntimeError:
                t.not_recorded += 1                  # 記録の時計の点がまだ無い
                continue
            body["sync"]["clockmap_uncertainty_ticks"] = map_unc
            rec.add(f"{target}.{body['method']}", body["probe"], ticks, body=body,
                    uncertainty=sync_uncertainty(body["sync"], rec.clock.hz, map_unc))
