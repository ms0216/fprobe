import json
import time
from pathlib import Path

import pytest

from fprobe.mcp_server import Probe
from fprobe.recording import Recorder, Recording

HERE = Path(__file__).parent


@pytest.fixture
def probe(monkeypatch, tmp_path):
    monkeypatch.setenv("FPROBE_HOME", str(tmp_path / "home"))
    monkeypatch.setenv("FPROBE_SSH", str(HERE / "linux_fake_ssh.py"))
    monkeypatch.setenv("FPROBE_SCP", str(HERE / "linux_fake_scp.py"))
    monkeypatch.setenv("PYTHONPATH", str(HERE))
    log = tmp_path / "agent.log"
    monkeypatch.setenv("FPROBE_FAKE_AGENT_LOG", str(log))
    p = Probe("unused")
    p.agent_log = log
    yield p
    p._close_linux()
    if p._recorder is not None:
        p._recorder.close()


def _connect(p, target="pynq"):
    caps = p.linux_connect(target=target, host="h", user="u")
    p.linux_agent_start(target=target)
    return caps


def _wait(cond, timeout=5.0):
    end = time.monotonic() + timeout
    while not cond() and time.monotonic() < end:
        time.sleep(0.01)
    return cond()


def _sent(p, op):
    return [json.loads(line) for line in p.agent_log.read_text(encoding="utf-8").splitlines() if f'"{op}"' in line]


def test_connect_returns_caps(probe):
    caps = probe.linux_connect(target="pynq", host="h", user="u")
    assert caps["machine"] == "armv7l" and caps["config"]["CONFIG_UPROBE_EVENTS"] == "n"
    assert probe.linux_capabilities(target="pynq") == caps


def test_probe_add_ptrace_sends_bp_add(probe):
    _connect(probe)
    r = probe.linux_probe_add(target="pynq", name="tick", binary="/tmp/tick_thumb", symbol="tick", method="ptrace",
                              regs=["r0"], pid=1234)
    assert r["plan"]["thumb"] is True and r["plan"]["offset"] == 0x5cc and len(r["sha256"]) == 64
    sent = _sent(probe, "bp_add")[0]
    assert (sent["pid"], sent["path"], sent["offset"], sent["thumb"], sent["regs"]) == (1234, "/tmp/tick_thumb", 0x5cc, True, ["r0"])
    assert [x["name"] for x in probe.linux_probe_list(target="pynq")["probes"]] == ["tick"]


def test_probe_add_uprobe_refuses_thumb(probe):
    _connect(probe)
    with pytest.raises(ValueError, match="ARM 命令だけ"):
        probe.linux_probe_add(target="pynq", name="tick", binary="/tmp/tick_thumb", symbol="tick", method="uprobe")


def test_probe_add_uprobe_without_kernel_support(probe):
    _connect(probe)
    with pytest.raises(RuntimeError, match="uprobe_events"):
        probe.linux_probe_add(target="pynq", name="tick", binary="/tmp/tick_arm", symbol="tick", method="uprobe")


def test_trace_event_goes_to_recording(probe, tmp_path):
    _connect(probe)
    rec = probe._recorder = Recorder(tmp_path / "rec", 100e6)
    now = time.monotonic()
    rec.clock.add(now - 1, 0, 1e-4)
    rec.clock.add(now + 1, 200_000_000, 1e-4)
    probe.linux_probe_add(target="pynq", name="tick", binary="/tmp/tick_arm", symbol="tick", method="ptrace",
                          regs=["r0"], pid=1)
    s = probe.linux_sync(target="pynq", count=2)
    assert len(s["pairs"]) == 2
    probe._linux["pynq"].agent.request("emit_bp", bp=1, t_ns=1_001_500_000)
    assert _wait(lambda: probe.linux_events_read(target="pynq")["records"])
    rec.flush()
    events = Recording.open(tmp_path / "rec").events(streams=["pynq.ptrace"])
    assert len(events) == 1 and events[0].kind == "tick"
    sync = events[0].body["sync"]
    assert sync["method"] == "network" and "half_rtt_ns" in sync and "clockmap_uncertainty_ticks" in sync
    assert events[0].body["regs"] == {"r0": 7} and events[0].body["target_ns"] == 1_001_500_000


def test_event_after_last_pair_waits_for_next_pair(probe, tmp_path):
    """最後の組より後ろの出来事は延ばして換算しない: 次の組ができてから内挿で記録に入れる。読むときもその時点の組で換算する。"""
    _connect(probe)
    rec = probe._recorder = Recorder(tmp_path / "rec", 100e6)
    now = time.monotonic()
    rec.clock.add(now - 1, 0, 1e-4)
    rec.clock.add(now + 1, 200_000_000, 1e-4)
    probe.linux_probe_add(target="pynq", name="tick", binary="/tmp/tick_arm", symbol="tick", method="ptrace", pid=1)
    probe.linux_sync(target="pynq", count=2)                       # 組: 1_001_000_000・1_002_000_000
    probe._linux["pynq"].agent.request("emit_bp", bp=1, t_ns=1_002_500_000)
    assert _wait(lambda: probe.linux_events_read(target="pynq")["records"])
    assert probe.linux_events_read(target="pynq")["records"][0]["sync"]["outside"] is True
    rec.flush()
    assert Recording.open(tmp_path / "rec").events(streams=["pynq.ptrace"]) == []
    probe.linux_sync(target="pynq", count=1)                       # 組: 1_003_000_000
    assert probe.linux_events_read(target="pynq")["records"][0]["sync"]["outside"] is False
    rec.flush()
    events = Recording.open(tmp_path / "rec").events(streams=["pynq.ptrace"])
    assert len(events) == 1 and events[0].body["sync"]["outside"] is False


def test_waiting_events_are_released_by_a_later_pair_even_when_scanned_in_between(probe, tmp_path):
    """組を待つ出来事は、待つ間に新しい出来事が入っても（調べ済みの分を飛ばしても）、後ろの組ができたときに全部入る。"""
    _connect(probe)
    rec = probe._recorder = Recorder(tmp_path / "rec", 100e6)
    now = time.monotonic()
    rec.clock.add(now - 1, 0, 1e-4)
    rec.clock.add(now + 1, 200_000_000, 1e-4)
    t = probe._linux["pynq"]
    t.clock.add(1_000_000_000, now, 1e-4)
    t.clock.add(2_000_000_000, now + 1, 1e-4)

    def keep(ns):
        probe._linux_keep("pynq", t, {"method": "ptrace", "probe": "p", "target_ns": ns, "clock": "CLOCK_MONOTONIC_RAW"})

    keep(1_500_000_000)                                           # 最後の組の前: すぐ入る
    keep(2_100_000_000)                                           # 後: 待つ
    keep(2_200_000_000)                                           # 待つ間にもう 1 つ（前の調べを飛ばす）
    keep(1_600_000_000)                                           # 古い時刻で遅れて着いた: すぐ入る
    assert [e[0]["target_ns"] for e in t.pending] == [2_100_000_000, 2_200_000_000]
    t.clock.add(3_000_000_000, now + 2, 1e-4)
    probe._linux_flush("pynq", t, final=False)
    assert t.pending == [] and t.not_recorded == 0
    rec.flush()
    assert len(Recording.open(tmp_path / "rec").events(streams=["pynq.ptrace"])) == 4


def test_events_without_recording_stay_in_memory(probe):
    _connect(probe)
    probe.linux_probe_add(target="pynq", name="tick", binary="/tmp/tick_arm", symbol="tick", method="ptrace", pid=1)
    probe._linux["pynq"].agent.request("emit_bp", bp=1, t_ns=5)
    assert _wait(lambda: probe.linux_events_read(target="pynq")["records"])
    r = probe.linux_events_read(target="pynq")
    assert r["records"][0]["probe"] == "tick" and r["records"][0]["target_ns"] == 5 and r["records"][0].get("sync") is None
    assert probe.linux_events_read(target="pynq", since=r["next"])["records"] == []


def test_two_targets_are_independent(probe):
    _connect(probe, "a")
    _connect(probe, "b")
    probe.linux_probe_add(target="a", name="tick", binary="/tmp/tick_arm", symbol="tick", method="ptrace", pid=1)
    assert probe.linux_probe_list(target="b")["probes"] == []
    probe.linux_disconnect(target="a")
    with pytest.raises(ValueError, match="a"):
        probe.linux_capabilities(target="a")
    assert probe.linux_capabilities(target="b")["machine"] == "armv7l"


def test_early_hit_is_not_dropped(probe, monkeypatch):
    """bp_add の返事より先に届いた当たりも、そのプローブの出来事として残る（I3）"""
    monkeypatch.setenv("FPROBE_FAKE_AGENT_EARLY_HIT", "1")
    _connect(probe)
    probe.linux_probe_add(target="pynq", name="tick", binary="/tmp/tick_arm", symbol="tick", method="ptrace", pid=1)
    assert _wait(lambda: probe.linux_events_read(target="pynq")["records"])
    assert probe.linux_events_read(target="pynq")["records"][0]["probe"] == "tick"


def test_recording_gets_only_events_that_arrived_while_recording(probe, tmp_path):
    """記録を始める前に受けた出来事は、後から始めた記録に入らない。記録していない間は記録を待つ列に積まない（I4・I5）"""
    _connect(probe)
    probe.linux_probe_add(target="pynq", name="tick", binary="/tmp/tick_arm", symbol="tick", method="ptrace", pid=1)
    probe.linux_sync(target="pynq", count=2)
    probe._linux["pynq"].agent.request("emit_bp", bp=1, t_ns=1_002_500_000)
    assert _wait(lambda: probe.linux_events_read(target="pynq")["records"])
    assert probe._linux["pynq"].pending == []
    rec = probe._recorder = Recorder(tmp_path / "rec", 100e6)
    now = time.monotonic()
    rec.clock.add(now - 1, 0, 1e-4)
    rec.clock.add(now + 1, 200_000_000, 1e-4)
    probe.linux_sync(target="pynq", count=1)
    rec.flush()
    assert Recording.open(tmp_path / "rec").events(streams=["pynq.ptrace"]) == []


def test_recording_stop_flushes_pending(probe, tmp_path):
    """記録を止める前に、組を待っていた出来事を入れる（I4）"""
    _connect(probe)
    rec = probe._recorder = Recorder(tmp_path / "rec", 100e6)
    now = time.monotonic()
    rec.clock.add(now - 1, 0, 1e-4)
    rec.clock.add(now + 1, 200_000_000, 1e-4)
    probe.linux_probe_add(target="pynq", name="tick", binary="/tmp/tick_arm", symbol="tick", method="ptrace", pid=1)
    probe.linux_sync(target="pynq", count=2)
    probe._linux["pynq"].agent.request("emit_bp", bp=1, t_ns=1_002_500_000)
    assert _wait(lambda: probe._linux["pynq"].pending)
    probe._linux_before_recording_stop()
    rec.flush()
    events = Recording.open(tmp_path / "rec").events(streams=["pynq.ptrace"])
    assert len(events) == 1 and events[0].body["sync"]["outside"] is False   # 止める前に組を 1 つ足してから入れる


def test_process_exit_ends_its_ptrace_probes(probe):
    """ptrace していたプロセスが終わったら、そのプローブは「終わった」と事実で残り、外せる（I6）"""
    _connect(probe)
    probe.linux_probe_add(target="pynq", name="tick", binary="/tmp/tick_arm", symbol="tick", method="ptrace", pid=77)
    probe._linux["pynq"].agent.request("emit_exit", pid=77)
    assert _wait(lambda: probe.linux_probe_list(target="pynq")["probes"][0].get("ended"))
    assert probe.linux_probe_list(target="pynq")["probes"][0]["ended"] == {"code": 0, "signal": 0}
    assert probe.linux_probe_remove(target="pynq", name="tick")["removed"] == "tick"
    assert probe.linux_probe_list(target="pynq")["probes"] == []


def test_agent_stop_clears_ptrace_probes(probe):
    _connect(probe)
    probe.linux_probe_add(target="pynq", name="tick", binary="/tmp/tick_arm", symbol="tick", method="ptrace", pid=1)
    probe.linux_agent_stop(target="pynq")
    assert probe.linux_probe_list(target="pynq")["probes"] == []


def test_trace_line_bytes_are_restored(probe):
    """エージェントは 0x80 以上のバイトを u00XX の形で送る: 元の UTF-8 に戻して解く（取りこぼさない）"""
    _connect(probe)
    t = probe._linux["pynq"]
    t.probes["enter"] = {"name": "enter", "method": "uprobe"}
    t.trace_clock = "mono_raw"
    line = "  app-7 [000] d..1. 1.000001: enter: (0x10) s=\"あ\""
    probe._linux_event("pynq", {"ev": "trace", "line": line.encode("utf-8").decode("latin-1"), "rx_ns": 0})
    assert probe.linux_events_read(target="pynq")["records"][0]["args"] == {"s": "\"あ\""}


class FakeRingCpu:
    """止めて読む CPU の偽物（道 B）: 仮想番地 base からリングのバイト列を語で読ませる"""

    def __init__(self, base: int, data: bytes, fail: bool = False):
        self.base, self.data, self.fail = base, data, fail
        self.halted, self.halts, self.resumes, self.index = False, 0, 0, 3

    def state(self):
        return "halted" if self.halted else "running"

    def halt(self):
        self.halted, self.halts = True, self.halts + 1

    def resume(self):
        self.halted, self.resumes = False, self.resumes + 1

    def read_words(self, address: int, count: int) -> list[int]:
        assert self.halted
        if self.fail:
            raise RuntimeError("読めない")
        off = address - self.base
        return [int.from_bytes(self.data[off + 4 * i: off + 4 * i + 4], "little") for i in range(count)]


def _ring_bytes(seqs, head, first=0, cap=64):
    from fprobe.linux.ring import RECORD, encode_header, encode_record
    slots = [b"\0" * RECORD] * cap
    for s in seqs:
        slots[(s - first) % cap] = encode_record(s, t_ns=1_001_500_000 + s, tid=9, bp=1, regs=[s, 0, 0, 0, 0])
    return encode_header(cap, head, first=first) + b"".join(slots)


def _ring_probe(probe, cpu):
    _connect(probe)
    probe.linux_probe_add(target="pynq", name="tick", binary="/tmp/tick_arm", symbol="tick", method="ptrace", pid=1)
    probe._linux_ring_cpu = lambda: cpu


def test_ring_read_returns_records_and_halts_once(probe):
    cpu = FakeRingCpu(0xA0000000, _ring_bytes([0, 1, 2], head=3))
    _ring_probe(probe, cpu)
    r = probe.linux_ring_read(target="pynq", address=0xA0000000, access="halt", max_records=100)
    assert [e["agent_seq"] for e in r["records"]] == [0, 1, 2] and r["lost"] == [] and r["core"] == 3
    assert cpu.halts == 1 and cpu.resumes == 1 and not cpu.halted and r["halted_ms"] >= 0
    ev = probe.linux_events_read(target="pynq")["records"]
    assert [(e["probe"], e["paths"], e["regs"]["r0"]) for e in ev] == [("tick", ["b"], 0), ("tick", ["b"], 1), ("tick", ["b"], 2)]


def test_same_event_from_a_and_b_is_kept_once(probe):
    cpu = FakeRingCpu(0xA0000000, _ring_bytes([0], head=1))
    _ring_probe(probe, cpu)
    probe._linux["pynq"].agent.request("emit_bp", bp=1, t_ns=1_001_500_000, seq=0)
    assert _wait(lambda: probe.linux_events_read(target="pynq")["records"])
    probe.linux_ring_read(target="pynq", address=0xA0000000, access="halt", max_records=10)
    ev = probe.linux_events_read(target="pynq")["records"]
    assert len(ev) == 1 and ev[0]["paths"] == ["a", "b"]


def test_ring_read_requires_a_known_access(probe):
    _ring_probe(probe, FakeRingCpu(0xA0000000, _ring_bytes([], head=0)))
    with pytest.raises(ValueError, match="access"):
        probe.linux_ring_read(target="pynq", address=0xA0000000, access="other", max_records=10)


def test_ring_read_resumes_on_error(probe):
    cpu = FakeRingCpu(0xA0000000, _ring_bytes([0], head=1), fail=True)
    _ring_probe(probe, cpu)
    with pytest.raises(RuntimeError, match="読めない"):
        probe.linux_ring_read(target="pynq", address=0xA0000000, access="halt", max_records=10)
    assert cpu.resumes == 1 and not cpu.halted


class HaltFailsCpu(FakeRingCpu):
    def halt(self):
        self.halted, self.halts = True, self.halts + 1
        raise RuntimeError("止めた後の手順で失敗")


def test_ring_read_resumes_when_halt_fails(probe):
    """止める要求の後に失敗しても、コアを止めたまま残さない（Linux が止まる）"""
    cpu = HaltFailsCpu(0xA0000000, _ring_bytes([0], head=1))
    _ring_probe(probe, cpu)
    with pytest.raises(RuntimeError, match="止めた後"):
        probe.linux_ring_read(target="pynq", address=0xA0000000, access="halt", max_records=10)
    assert not cpu.halted and cpu.resumes == 1


def test_agent_restart_does_not_hide_new_events(probe):
    """新しいエージェントの seq は 0 から: 前のエージェントの番号と取り違えて捨てない"""
    _connect(probe)
    probe.linux_probe_add(target="pynq", name="tick", binary="/tmp/tick_arm", symbol="tick", method="ptrace", pid=1)
    probe._linux["pynq"].agent.request("emit_bp", bp=1, t_ns=5, seq=0)
    assert _wait(lambda: len(probe.linux_events_read(target="pynq")["records"]) == 1)
    probe.linux_agent_stop(target="pynq")
    probe.linux_agent_start(target="pynq")
    probe.linux_probe_add(target="pynq", name="tick", binary="/tmp/tick_arm", symbol="tick", method="ptrace", pid=1)
    probe._linux["pynq"].agent.request("emit_bp", bp=2, t_ns=6, seq=0)
    assert _wait(lambda: len(probe.linux_events_read(target="pynq")["records"]) == 2)


def test_a_and_b_racing_keep_one_event(probe):
    """道 A の読み手と道 B の読み手が同じ seq を同時に受けても 1 つ"""
    import threading
    _connect(probe)
    probe.linux_probe_add(target="pynq", name="tick", binary="/tmp/tick_arm", symbol="tick", method="ptrace", pid=1)
    t = probe._linux["pynq"]
    n = 300
    a = threading.Thread(target=lambda: [probe._linux_event("pynq", {"ev": "bp", "seq": s, "bp": 1, "tid": 1, "t_ns": s,
                                                                      "regs": {"r0": s}}) for s in range(n)])
    b = threading.Thread(target=lambda: [probe._linux_ring_record("pynq", t, {"seq": s, "t_ns": s, "tid": 1, "bp": 1,
                                                                              "kind": 1, "regs": [s, 0, 0, 0, 0]})
                                         for s in range(n)])
    a.start(); b.start(); a.join(); b.join()
    ev = probe.linux_events_read(target="pynq", limit=10000)["records"]
    assert sorted(e["agent_seq"] for e in ev) == list(range(n))


def test_bad_ring_header_is_a_fact_and_watch_keeps_going(probe):
    data = bytearray(_ring_bytes([0, 1], head=2))
    data[16] ^= 0xFF                                                  # head が壊れて読めた
    cpu = FakeRingCpu(0xA0000000, bytes(data))
    _ring_probe(probe, cpu)
    r = probe.linux_ring_read(target="pynq", address=0xA0000000, access="halt", max_records=10)
    assert r["records"] == [] and "CRC" in r["header_error"] and not cpu.halted
    cpu.data = _ring_bytes([0, 1], head=2)
    assert [x["agent_seq"] for x in probe.linux_ring_read(target="pynq", address=0xA0000000, access="halt",
                                                          max_records=10)["records"]] == [0, 1]


def test_uncached_ring_is_refused_for_halt(probe):
    _ring_probe(probe, FakeRingCpu(0xA0000000, _ring_bytes([], head=0)))
    probe.linux_ring_open(target="pynq", phys=0x20000000, size=4096, cached=False, linger=False)
    with pytest.raises(ValueError, match="cached"):
        probe.linux_ring_read(target="pynq", address=0xA0000000, access="halt", max_records=10)


def test_resume_retries_the_full_resume_before_a_raw_restart():
    from fprobe.probe_linux import _ArmACpu

    class Port:
        def __init__(self):
            self.cleared, self.raw = 0, 0
            self.dap = self
        def clear_errors(self):
            self.cleared += 1
        def restart_request(self):
            self.raw += 1

    class Target:
        def __init__(self):
            self.calls, self.core = 0, type("C", (), {})()
            self.core.port = Port()
        def resume(self):
            self.calls += 1
            if self.calls == 1:
                raise RuntimeError("一時的な誤り")

    tgt = Target()
    _ArmACpu(tgt).resume()
    assert tgt.calls == 2 and tgt.core.port.cleared == 1 and tgt.core.port.raw == 0


def test_batch_quotes_commands(probe, monkeypatch, tmp_path):
    log = tmp_path / "ssh.log"
    monkeypatch.setenv("FPROBE_FAKE_SSH_LOG", str(log))
    monkeypatch.setenv("FPROBE_FAKE_SSH_REPLIES", json.dumps({"gdb -batch": "#0  main () at t.c:3\n"}))
    probe.linux_connect(target="pynq", host="h", user="u")
    r = probe.linux_gdb_batch(target="pynq", commands=["bt", "print 'x'; echo $HOME"], pid=812, timeout_s=30)
    assert r["exit_code"] == 0 and "main ()" in r["stdout"]
    sent = [line for line in log.read_text(encoding="utf-8").splitlines() if "gdb -batch" in line][0]
    assert sent == r["command"]
    assert sent == r"timeout -s TERM 30 gdb -batch -nx -p 812 -ex 'bt' -ex 'print '\''x'\''; echo $HOME'"


def test_batch_needs_pid_or_core(probe):
    probe.linux_connect(target="pynq", host="h", user="u")
    with pytest.raises(ValueError, match="pid"):
        probe.linux_gdb_batch(target="pynq", commands=["bt"], timeout_s=5)


def test_batch_refuses_without_gdb(probe):
    probe.linux_connect(target="pynq", host="h", user="u")
    probe._linux["pynq"].caps["tools"]["gdb"] = None
    with pytest.raises(RuntimeError, match="gdb"):
        probe.linux_gdb_batch(target="pynq", commands=["bt"], pid=1, timeout_s=5)


def test_gdb_serve_and_stop(probe):
    import socket
    probe.linux_connect(target="pynq", host="h", user="u")
    with pytest.raises(RuntimeError, match="gdbserver"):
        probe.linux_gdb_serve(target="pynq", pid=5, port=0)
    probe._linux["pynq"].caps["tools"]["gdbserver"] = "/usr/bin/gdbserver"
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    r = probe.linux_gdb_serve(target="pynq", pid=5, port=port)
    assert r["port"] == port and r["command"] == "gdbserver --once --attach - 5"
    c = socket.create_connection(("127.0.0.1", port), timeout=5)
    c.sendall(b"$g#67")
    got = b""
    while len(got) < 5:
        part = c.recv(16)
        assert part
        got += part
    assert got == b"$G#67"
    c.close()
    st = probe.linux_gdb_stop(target="pynq")
    assert st["bytes_to_target"] == 5 and st["closed_by"] in ("gdb", "stop")


def test_batch_timeout_returns_partial_output(probe, monkeypatch):
    """時間切れでも例外にせず、途中までの出力と timed_out を返す（ターゲットの側も timeout で gdb を終わらせる）"""
    monkeypatch.setenv("FPROBE_FAKE_SSH_REPLIES", json.dumps({"gdb -batch": "#0  main ()\n"}))
    monkeypatch.setenv("FPROBE_FAKE_SSH_DELAY", "30")
    monkeypatch.setattr("fprobe.probe_linux.BATCH_BACKSTOP_S", 0.5)
    probe.linux_connect(target="pynq", host="h", user="u")
    r = probe.linux_gdb_batch(target="pynq", commands=["continue"], pid=5, timeout_s=0.5)
    assert r["timed_out"] is True and "main ()" in r["stdout"] and r["command"].startswith("timeout -s TERM 0.5 gdb")


def test_gdb_status_without_relay_has_the_same_keys(probe):
    probe.linux_connect(target="pynq", host="h", user="u")
    st = probe.linux_gdb_status(target="pynq")
    assert set(st) >= {"port", "connected", "target_running", "exit_code", "bytes_to_target", "bytes_from_target",
                       "closed_by", "error", "stderr_tail"} and st["connected"] is False


def test_ftrace_event_name_is_checked(probe):
    probe.linux_connect(target="pynq", host="h", user="u")
    probe._linux["pynq"].caps["tracefs"] = "/sys/kernel/tracing"
    for bad in ("../x/enable", "sched", "sched/../../x", "Sched/switch"):
        with pytest.raises(ValueError, match="名前"):
            probe.linux_ftrace_enable(target="pynq", events=[bad])


def test_trace_lines_are_split_by_kind(probe, monkeypatch):
    monkeypatch.setenv("FPROBE_FAKE_SSH_REPLIES", json.dumps({"enable": ""}))
    _connect(probe)
    t = probe._linux["pynq"]
    t.caps["tracefs"] = "/sys/kernel/tracing"
    probe.linux_ftrace_enable(target="pynq", events=["sched/sched_switch"])
    t.probes["enter"] = {"name": "enter", "method": "uprobe"}
    t.trace_clock = "mono_raw"
    lines = ["  app-7 [000] d..1. 1.000001: enter: (0x10) a=5",
             "  sh-9 [002] d.... 1.000002: sched_switch: prev_comm=sh prev_pid=9 ==> next_comm=x next_pid=3",
             "  sh-9 [002] d.... 1.000003: irq_handler_entry: irq=5 name=x"]           # 有効にしていない
    for line in lines:
        probe._linux_event("pynq", {"ev": "trace", "line": line, "rx_ns": 0})
    ev = probe.linux_events_read(target="pynq")["records"]
    assert [(e["method"], e["probe"]) for e in ev] == [("uprobe", "enter"), ("ftrace", "sched_switch")]
    assert ev[1]["args"]["next_pid"] == "3" and ev[1]["clock"] == "mono_raw"


def test_log_read_returns_json_lines(probe, monkeypatch):
    lines = '{"__CURSOR":"c1","__MONOTONIC_TIMESTAMP":"100","MESSAGE":"a"}\n{"__CURSOR":"c2","MESSAGE":"b"}\n'
    monkeypatch.setenv("FPROBE_FAKE_SSH_REPLIES", json.dumps({"journalctl": lines}))
    probe.linux_connect(target="pynq", host="h", user="u")
    r = probe.linux_log_read(target="pynq", lines=2, unit="ssh", since_cursor="c0")
    assert [e["MESSAGE"] for e in r["entries"]] == ["a", "b"] and r["next_cursor"] == "c2"
    assert r["clock"] == "CLOCK_MONOTONIC" and "--after-cursor 'c0'" in r["command"] and "-u 'ssh'" in r["command"]


def test_log_read_reports_failure(probe, monkeypatch):
    monkeypatch.setenv("FPROBE_FAKE_SSH_FAIL", "journalctl")
    probe.linux_connect(target="pynq", host="h", user="u")
    r = probe.linux_log_read(target="pynq", lines=5)
    assert r["entries"] == [] and r["exit_code"] == 1 and "Failed to open journal" in r["stderr"] and r["next_cursor"] is None


def _ftrace_probe(probe, monkeypatch):
    monkeypatch.setenv("FPROBE_FAKE_SSH_REPLIES", json.dumps({"enable": ""}))
    _connect(probe)
    t = probe._linux["pynq"]
    t.caps["tracefs"] = "/sys/kernel/tracing"
    t.trace_clock = "mono_raw"
    return t


def test_trace_pipe_facts_are_not_dropped(probe, monkeypatch):
    """LOST の印・形の違う行（syscalls）・どれにも当たらない行を黙って捨てない"""
    t = _ftrace_probe(probe, monkeypatch)
    probe.linux_ftrace_enable(target="pynq", events=["syscalls/sys_enter_openat"])
    for line in ["CPU:1 [LOST 37 EVENTS]",
                 "  cat-77 [000] ..... 5.000001: sys_openat(dfd: ffffff9c, filename: 7ef4, flags: 0, mode: 0)",
                 "garbage"]:
        probe._linux_event("pynq", {"ev": "trace", "line": line, "rx_ns": 0})
    ev = probe.linux_events_read(target="pynq")["records"]
    assert ev[0]["probe"] == "trace_lost" and ev[0]["detail"] == {"cpu": 1, "lost": 37}
    assert ev[1]["method"] == "ftrace" and ev[1]["probe"] == "sys_openat" and "filename: 7ef4" in ev[1]["text"]
    st = probe.linux_agent_status(target="pynq")
    assert st["trace_unparsed"] == 1


def test_probe_name_does_not_capture_tracepoints(probe, monkeypatch):
    """ptrace のプローブの名前が tracepoint と同じでも、tracepoint の行を uprobe に取り違えない"""
    t = _ftrace_probe(probe, monkeypatch)
    probe.linux_probe_add(target="pynq", name="sched_switch", binary="/tmp/tick_arm", symbol="tick", method="ptrace", pid=1)
    probe.linux_ftrace_enable(target="pynq", events=["sched/sched_switch"])
    probe._linux_event("pynq", {"ev": "trace", "line": "  sh-9 [002] d.... 1.000002: sched_switch: prev_pid=9 ==> next_pid=3",
                                "rx_ns": 0})
    assert probe.linux_events_read(target="pynq")["records"][0]["method"] == "ftrace"


def test_disable_keeps_other_groups_with_the_same_name(probe, monkeypatch):
    _ftrace_probe(probe, monkeypatch)
    probe.linux_ftrace_enable(target="pynq", events=["a/x", "b/x"])
    assert probe.linux_ftrace_disable(target="pynq", events=["a/x"])["enabled"] == ["b/x"]


def test_disconnect_disables_ftrace_events(probe, monkeypatch, tmp_path):
    log = tmp_path / "ssh.log"
    monkeypatch.setenv("FPROBE_FAKE_SSH_LOG", str(log))
    _ftrace_probe(probe, monkeypatch)
    probe.linux_ftrace_enable(target="pynq", events=["sched/sched_switch"])
    probe.linux_disconnect(target="pynq")
    assert any("echo 0 >" in line and "sched/sched_switch/enable" in line for line in log.read_text(encoding="utf-8").splitlines())


def test_event_name_with_newline_is_refused(probe, monkeypatch):
    _ftrace_probe(probe, monkeypatch)
    with pytest.raises(ValueError, match="名前"):
        probe.linux_ftrace_enable(target="pynq", events=["sched/sched_switch\n"])
    assert probe.linux_ftrace_enable(target="pynq", events=["xhci-hcd/xhci_urb_enqueue"])["enabled"] == ["xhci-hcd/xhci_urb_enqueue"]


def test_sudo_password_comes_from_the_named_env(probe, monkeypatch, tmp_path):
    """パスワードの要る sudo（PYNQ）: 使う側が環境変数の名前を明示し、パスワードは sudo -S の stdin の最初の行で渡す"""
    log, pw = tmp_path / "ssh.log", tmp_path / "sudo.log"
    monkeypatch.setenv("FPROBE_FAKE_SSH_LOG", str(log))
    monkeypatch.setenv("FPROBE_FAKE_SUDO_LOG", str(pw))
    monkeypatch.setenv("MY_SUDO_PW", "pw1")
    probe.linux_connect(target="pynq", host="h", user="u", sudo=True, sudo_password_env="MY_SUDO_PW")
    probe.linux_log_read(target="pynq", lines=1)
    probe.linux_agent_start(target="pynq")
    assert probe.linux_agent_status(target="pynq")["running"]
    sent = log.read_text(encoding="utf-8").splitlines()
    assert any(line.startswith("sudo -S -k -p '' journalctl") for line in sent)   # -k: 覚えた資格を使わず必ず stdin を読む
    assert any(line == "sudo -S -k -p '' .fprobe/fprobe_agent" for line in sent)
    assert pw.read_text(encoding="utf-8").splitlines() == ["pw1", "pw1"]
    assert probe._linux["pynq"].agent.sudo_input is None                       # 書いた後は持たない


def test_sudo_password_env_missing_is_a_fact(probe, monkeypatch):
    monkeypatch.delenv("NO_SUCH_PW", raising=False)
    with pytest.raises(RuntimeError, match="NO_SUCH_PW"):                    # つなぐときに分かる
        probe.linux_connect(target="pynq", host="h", user="u", sudo=True, sudo_password_env="NO_SUCH_PW")


class FakeMemap:
    """止めずに読む口の偽物（物理番地 base からリングのバイト列）"""

    def __init__(self, base: int, data: bytes):
        self.base, self.data, self.reads = base, data, 0
        self.info = {"tool": "fake"}

    def read_words(self, address: int, count: int) -> list[int]:
        self.reads += 1
        off = address - self.base
        return [int.from_bytes(self.data[off + 4 * i: off + 4 * i + 4], "little") for i in range(count)]

    def close(self):
        self.closed = True


def _memap_probe(probe, data, cached):
    _connect(probe)
    probe.linux_probe_add(target="pynq", name="tick", binary="/tmp/tick_arm", symbol="tick", method="ptrace", pid=1)
    probe.linux_ring_open(target="pynq", phys=0x1DF00000, size=64 * 48 + 64, cached=cached, linger=False)
    mem = FakeMemap(0x1DF00000, data)
    probe._linux["pynq"].memap = mem
    return mem


def test_memap_ring_read_does_not_halt(probe):
    mem = _memap_probe(probe, _ring_bytes([0, 1], head=2), cached=False)
    r = probe.linux_ring_read(target="pynq", address=0x1DF00000, access="memap", max_records=10)
    assert [x["agent_seq"] for x in r["records"]] == [0, 1] and r["halted_ms"] == 0 and r["core"] is None
    assert r["access"] == "memap" and mem.reads >= 2


def test_memap_refuses_cached_ring(probe):
    _memap_probe(probe, _ring_bytes([], head=0), cached=True)
    with pytest.raises(ValueError, match="cached"):
        probe.linux_ring_read(target="pynq", address=0x1DF00000, access="memap", max_records=10)


def test_memap_requires_connect(probe):
    _connect(probe)
    with pytest.raises(RuntimeError, match="linux.memap.connect"):
        probe.linux_ring_read(target="pynq", address=0x1DF00000, access="memap", max_records=10)


def test_ring_open_passes_linger(probe):
    """linger（SSH が切れてもリングに書き続ける）は使う側が明示し、エージェントにそのまま渡る"""
    _connect(probe)
    probe.linux_ring_open(target="pynq", phys=0x20000000, size=4096, cached=True, linger=True)
    sent = _sent(probe, "ring_open")[0]
    assert sent["linger"] is True and probe._linux["pynq"].ring["linger"] is True


def test_wrong_sudo_password_fails_at_connect(probe, monkeypatch):
    monkeypatch.setenv("MY_SUDO_PW", "bad")
    monkeypatch.setenv("FPROBE_FAKE_SUDO_BAD", "1")
    with pytest.raises(RuntimeError, match="sudo"):
        probe.linux_connect(target="pynq", host="h", user="u", sudo=True, sudo_password_env="MY_SUDO_PW")


def test_sudo_password_env_needs_sudo(probe):
    with pytest.raises(ValueError, match="sudo"):
        probe.linux_connect(target="pynq", host="h", user="u", sudo=False, sudo_password_env="MY_SUDO_PW")


def test_memap_read_does_not_claim_a_halt(probe):
    _memap_probe(probe, _ring_bytes([0], head=1), cached=False)
    r = probe.linux_ring_read(target="pynq", address=0x1DF00000, access="memap", max_records=10)
    assert r["kept_halted"] is None and r["halted_ms"] == 0


def test_agent_start_passes_watchdog(probe):
    probe.linux_connect(target="pynq", host="h", user="u")
    probe.linux_agent_start(target="pynq", watchdog_s=5.0)
    assert _sent(probe, "watchdog")[0]["seconds"] == 5.0
