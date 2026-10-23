# 試験の偽のエージェント（fprobe/linux/agent/fprobe_agent.c と同じ JSON の行のやりとり）。偽の ssh から起動される。
# 受けた要求は FPROBE_FAKE_AGENT_LOG（あれば）に 1 行ずつ書く（試験が確かめる）。
import json
import os
import sys
import time

LOG = os.environ.get("FPROBE_FAKE_AGENT_LOG")


def out(obj):
    sys.stdout.write(json.dumps(obj) + "\n")
    sys.stdout.flush()


t_ns = 1_000_000_000
gone = set()                                      # 終わったプロセスの bp（本物はプロセスの終わりで消す）
bp_pid = {}
for line in sys.stdin:
    req = json.loads(line)
    if LOG:
        with open(LOG, "a", encoding="utf-8") as f:
            f.write(line)
    op, rid = req.get("op"), req.get("id")
    if op == "hello":
        out({"id": rid, "ok": True, "version": 1, "clock": "CLOCK_MONOTONIC_RAW", "pid": os.getpid()})
    elif op == "time":
        t_ns += 1_000_000
        out({"id": rid, "ok": True, "t_ns": t_ns})
    elif op == "trace_open":
        out({"id": rid, "ok": True})
        for i in range(2):
            out({"ev": "trace", "line": f"  app-7 [000] d..1. 1.{i:06d}: enter: (0x10) a={i}", "rx_ns": t_ns})
    elif op == "trace_close":
        out({"id": rid, "ok": True})
    elif op == "bp_remove":
        if req["bp"] in gone:
            out({"id": rid, "ok": False, "error": f"no breakpoint {req['bp']}"})
        else:
            out({"id": rid, "ok": True, "hits": 0})
    elif op == "bp_add":
        bp_pid[req["bp"]] = req["pid"]
        if os.environ.get("FPROBE_FAKE_AGENT_EARLY_HIT"):    # 本物と同じく、返事より先に当たりが届くことがある
            out({"ev": "bp", "bp": req["bp"], "tid": 42, "t_ns": 1, "regs": {}})
        out({"id": rid, "ok": True, "address": 0x400000 + req["offset"], "threads": 1})
    elif op == "emit_bp":                            # 試験から: bp の出来事を 1 つ出す
        out({"id": rid, "ok": True})
        ev = {"ev": "bp", "bp": req["bp"], "tid": 42, "t_ns": req["t_ns"], "regs": {"r0": 7}}
        if "seq" in req:
            ev["seq"] = req["seq"]
        out(ev)
    elif op == "watchdog" and os.environ.get("FPROBE_FAKE_AGENT_OLD"):
        out({"id": rid, "ok": False, "error": "unknown op watchdog"})
    elif op in ("watchdog", "ping"):
        out({"id": rid, "ok": True})
    elif op == "ring_open":
        out({"id": rid, "ok": True, "capacity": (req["size"] - 64) // 48, "first": 0})
    elif op == "emit_exit":                          # 試験から: ptrace していたプロセスが終わった
        out({"id": rid, "ok": True})
        gone.update(b for b, pid in bp_pid.items() if pid == req["pid"])
        out({"ev": "exit", "pid": req["pid"], "code": 0, "signal": 0})
    elif op == "emit_raw":                           # 試験から: そのままの行を出す
        out({"id": rid, "ok": True})
        sys.stdout.write(req["text"] + "\n")
        sys.stdout.flush()
    elif op == "crash":
        sys.stderr.write("boom\n")
        sys.stderr.flush()
        sys.exit(3)
    elif op == "quit":
        out({"id": rid, "ok": True})
        break
    else:
        out({"id": rid, "ok": False, "error": f"unknown op {op}"})
    time.sleep(0)
