import { describe, expect, it } from "vitest";
import { connectParams, LinuxEvents, probeParams, syncParams, traceStartParams, type ConnectForm, type ProbeForm } from "../src/lib/linux";
import { FakeConn } from "./fake-conn";

const conn: ConnectForm = { target: "pynq", host: "192.168.77.2", user: "xilinx", port: "22", key: "", sudo: false, sudoPasswordEnv: "" };
const probe: ProbeForm = { name: "tick", binary: "/tmp/tick", method: "ptrace", symbol: "tick", address: "", isa: "",
  fetchargs: "", regs: "r0 r1", ret: false, pid: "812" };

describe("connectParams", () => {
  it("空の key は入れず、port は数", () => {
    expect(connectParams(conn)).toEqual({ params: { target: "pynq", host: "192.168.77.2", user: "xilinx", port: 22, sudo: false } });
  });
  it("port が数でなければその欄を返す", () => {
    expect(connectParams({ ...conn, port: "x" })).toEqual({ error: "port" });
    expect(connectParams({ ...conn, port: "" })).toEqual({ error: "port" });
    expect(connectParams({ ...conn, target: " " })).toEqual({ error: "target" });
    expect(connectParams({ ...conn, host: "" })).toEqual({ error: "host" });
    expect(connectParams({ ...conn, user: "" })).toEqual({ error: "user" });
  });
});

describe("時刻合わせの回数・間隔（使う人が入れる。空を 0 として送らない）", () => {
  it("回数は 1 以上の整数、間隔は正の秒", () => {
    expect(syncParams("pi", "8")).toEqual({ params: { target: "pi", count: 8 } });
    expect(syncParams("pi", "")).toEqual({ error: "count" });
    expect(syncParams("pi", "0")).toEqual({ error: "count" });
    expect(traceStartParams("pi", "1.5")).toEqual({ params: { target: "pi", sync_interval_s: 1.5 } });
    expect(traceStartParams("pi", "")).toEqual({ error: "interval" });
    expect(traceStartParams("pi", "-1")).toEqual({ error: "interval" });
  });
});

describe("probeParams", () => {
  it("書いた欄だけ。regs・fetchargs は空白で分ける", () => {
    expect(probeParams("pynq", probe)).toEqual({ params: { target: "pynq", name: "tick", binary: "/tmp/tick",
      method: "ptrace", symbol: "tick", regs: ["r0", "r1"], pid: 812 } });
  });
  it("方式は使う人が選ぶ（空なら送らない。ptrace を既定にしない）", () => {
    expect(probeParams("pynq", { ...probe, method: "" })).toEqual({ error: "method" });
  });
  it("symbol と address の両方はエラー", () => {
    expect(probeParams("pynq", { ...probe, address: "0x10" })).toEqual({ error: "address" });
  });
  it("address は 0x の数", () => {
    const r = probeParams("pynq", { ...probe, symbol: "", address: "0x10", isa: "thumb" });
    expect(r).toMatchObject({ params: { address: 16, isa: "thumb" } });
    expect(probeParams("pynq", { ...probe, symbol: "", address: "zz" })).toEqual({ error: "address" });
  });
  it("uprobe の fetchargs と ret", () => {
    const r = probeParams("pynq", { ...probe, method: "uprobe", regs: "", pid: "", fetchargs: "a=%r0:s32 b=%r1", ret: true });
    expect(r).toMatchObject({ params: { method: "uprobe", fetchargs: ["a=%r0:s32", "b=%r1"], ret: true } });
  });
});

describe("LinuxEvents", () => {
  it("next から読み足す", async () => {
    const pages: Record<number, any> = {
      0: { records: [{ seq: 1, probe: "tick", target_ns: 5 }], next: 1, dropped: 0 },
      1: { records: [{ seq: 2, probe: "tick", target_ns: 6 }], next: 2, dropped: 3 },
    };
    const fake = new FakeConn({ "linux.events.read": (p: any) => pages[p.since] ?? { records: [], next: p.since, dropped: 3 } });
    const ev = new LinuxEvents(fake as any);
    await ev.more("pynq");
    await ev.more("pynq");
    expect(ev.records.map((r) => r.seq)).toEqual([1, 2]);
    expect(ev.dropped).toBe(3);
    expect(fake.calls.map(([, p]) => p.since)).toEqual([0, 1]);
  });
});

import { agentStartParams, ftraceParams, gdbBatchParams, gdbServeParams, logParams, memapParams, ringOpenParams,
  ringReadParams, ringWatchParams, type GdbForm, type LogForm, type MemapForm, type RingForm } from "../src/lib/linux";

describe("connectParams の sudo のパスワード", () => {
  it("環境変数の名前を書けば sudo_password_env", () => {
    expect(connectParams({ ...conn, sudo: true, sudoPasswordEnv: "PYNQ_PW" })).toMatchObject({ params: { sudo: true, sudo_password_env: "PYNQ_PW" } });
  });
});

describe("agentStartParams", () => {
  it("空なら watchdog なし、書けば正の秒数", () => {
    expect(agentStartParams("pi", "")).toEqual({ params: { target: "pi" } });
    expect(agentStartParams("pi", "9")).toEqual({ params: { target: "pi", watchdog_s: 9 } });
    expect(agentStartParams("pi", "0")).toEqual({ error: "watchdog_s" });
    expect(agentStartParams("pi", "x")).toEqual({ error: "watchdog_s" });
  });
});

const ring: RingForm = { phys: "0x20000000", size: "0x100000", cached: true, linger: false, address: "0xA0000000",
  access: "halt", maxRecords: "1000", interval: "0.5" };

describe("ring", () => {
  it("open は phys・size・cached・linger をすべて送る", () => {
    expect(ringOpenParams("pi", ring)).toEqual({ params: { target: "pi", phys: 0x20000000, size: 0x100000, cached: true, linger: false } });
    expect(ringOpenParams("pi", { ...ring, size: "" })).toEqual({ error: "size" });
  });
  it("read と watch は address・access・max_records（watch は interval_s も）", () => {
    expect(ringReadParams("pi", ring)).toEqual({ params: { target: "pi", address: 0xA0000000, access: "halt", max_records: 1000 } });
    expect(ringWatchParams("pi", ring)).toEqual({ params: { target: "pi", address: 0xA0000000, access: "halt", max_records: 1000, interval_s: 0.5 } });
    expect(ringReadParams("pi", { ...ring, maxRecords: "0" })).toEqual({ error: "max_records" });
    expect(ringWatchParams("pi", { ...ring, interval: "-1" })).toEqual({ error: "interval_s" });
  });
});

describe("memapParams", () => {
  it("IDCODE は 16 進も可。すべて明示", () => {
    const f: MemapForm = { xsdb: "C:/x/xsdb.bat", cableSerial: "1234-tulA", chainIdcode: "0x23727093", timeout: "30" };
    expect(memapParams("z", f)).toEqual({ params: { target: "z", xsdb: "C:/x/xsdb.bat", cable_serial: "1234-tulA", chain_idcode: 0x23727093, timeout_s: 30 } });
    expect(memapParams("z", { ...f, chainIdcode: "" })).toEqual({ error: "chain_idcode" });
  });
});

const gdb: GdbForm = { commands: "bt\ninfo registers\n", timeout: "20", pid: "812", core: "", binary: "", port: "3333" };

describe("gdb", () => {
  it("batch: 命令は行ごと、pid か core のどちらか", () => {
    expect(gdbBatchParams("pi", gdb)).toEqual({ params: { target: "pi", commands: ["bt", "info registers"], timeout_s: 20, pid: 812 } });
    expect(gdbBatchParams("pi", { ...gdb, pid: "", core: "/tmp/core", binary: "/tmp/app" }))
      .toEqual({ params: { target: "pi", commands: ["bt", "info registers"], timeout_s: 20, core: "/tmp/core", binary: "/tmp/app" } });
    expect(gdbBatchParams("pi", { ...gdb, core: "/tmp/core" })).toEqual({ error: "pid" });
    expect(gdbBatchParams("pi", { ...gdb, commands: " \n" })).toEqual({ error: "commands" });
  });
  it("serve: pid と port", () => {
    expect(gdbServeParams("pi", gdb)).toEqual({ params: { target: "pi", pid: 812, port: 3333 } });
    expect(gdbServeParams("pi", { ...gdb, port: "70000" })).toEqual({ error: "port" });
  });
});

describe("ftraceParams と logParams", () => {
  it("ftrace のイベントは空白で分ける", () => {
    expect(ftraceParams("pi", "sched/sched_switch irq/irq_handler_entry")).toEqual({ params: { target: "pi", events: ["sched/sched_switch", "irq/irq_handler_entry"] } });
    expect(ftraceParams("pi", " ")).toEqual({ error: "events" });
  });
  it("log は lines と書いた欄だけ", () => {
    const f: LogForm = { lines: "200", unit: "", cursor: "" };
    expect(logParams("pi", f)).toEqual({ params: { target: "pi", lines: 200 } });
    expect(logParams("pi", { ...f, unit: "ssh", cursor: "s=abc" })).toEqual({ params: { target: "pi", lines: 200, unit: "ssh", since_cursor: "s=abc" } });
  });
});

describe("範囲と形（API の仕様と同じ）", () => {
  it("access は選ばないと誤り", () => {
    expect(ringReadParams("pi", { ...ring, access: "" })).toEqual({ error: "access" });
  });
  it("size は 112 以上、max_records・lines は 100000 まで、chain_idcode は 32 ビット", () => {
    expect(ringOpenParams("pi", { ...ring, size: "100" })).toEqual({ error: "size" });
    expect(ringReadParams("pi", { ...ring, maxRecords: "100001" })).toEqual({ error: "max_records" });
    expect(logParams("pi", { lines: "100001", unit: "", cursor: "" })).toEqual({ error: "lines" });
    expect(memapParams("z", { xsdb: "x", cableSerial: "a", chainIdcode: "0x100000000", timeout: "1" })).toEqual({ error: "chain_idcode" });
  });
  it("ftrace のイベントは subsystem/event の形", () => {
    expect(ftraceParams("pi", "Sched/sched_switch")).toEqual({ error: "events" });
    expect(ftraceParams("pi", "sched_switch")).toEqual({ error: "events" });
  });
});

import { coreFetchParams, coreLimitParams, corePatternParams, coreStatusParams, perfRecordParams, perfStatParams,
  type CoreForm, type PerfForm } from "../src/lib/linux";

const core: CoreForm = { pid: "812", pattern: "/tmp/core.%e.%p", soft: "unlimited", hard: "unlimited", path: "/tmp/core.app.812",
  localPath: "C:/tmp/core" };
const perf: PerfForm = { events: "task-clock cycles", duration: "0.5", frequency: "499", maxSamples: "10000", pid: "812" };

describe("core dump", () => {
  it("status は pid を書いたときだけ", () => {
    expect(coreStatusParams("pi", { ...core, pid: "" })).toEqual({ params: { target: "pi" } });
    expect(coreStatusParams("pi", core)).toEqual({ params: { target: "pi", pid: 812 } });
  });
  it("pattern・limit・fetch はすべて明示", () => {
    expect(corePatternParams("pi", core)).toEqual({ params: { target: "pi", pattern: "/tmp/core.%e.%p" } });
    expect(corePatternParams("pi", { ...core, pattern: " " })).toEqual({ error: "pattern" });
    expect(coreLimitParams("pi", core)).toEqual({ params: { target: "pi", pid: 812, soft: "unlimited", hard: "unlimited" } });
    expect(coreLimitParams("pi", { ...core, soft: "4096" })).toMatchObject({ params: { soft: 4096 } });
    expect(coreLimitParams("pi", { ...core, soft: "lots" })).toEqual({ error: "soft" });
    expect(coreFetchParams("pi", core)).toEqual({ params: { target: "pi", path: "/tmp/core.app.812", local_path: "C:/tmp/core" } });
  });
});

describe("perf", () => {
  it("stat は events・duration_s（pid は書いたときだけ）", () => {
    expect(perfStatParams("pi", perf)).toEqual({ params: { target: "pi", events: ["task-clock", "cycles"], duration_s: 0.5, pid: 812 } });
    expect(perfStatParams("pi", { ...perf, events: "cycles;rm" })).toEqual({ error: "events" });
  });
  it("record は duration_s・frequency_hz・max_samples を明示", () => {
    expect(perfRecordParams("pi", { ...perf, pid: "" })).toEqual({ params: { target: "pi", duration_s: 0.5, frequency_hz: 499, max_samples: 10000 } });
    expect(perfRecordParams("pi", { ...perf, maxSamples: "" })).toEqual({ error: "max_samples" });
  });
});

describe("LinuxEvents の画面の上限", () => {
  it("画面に持つ上限を超えたら古い記録を捨て、捨てた数を数える（黙って捨てない）", async () => {
    const { LINUX_KEEP } = await import("../src/lib/linux");
    const n = LINUX_KEEP + 2;
    const fake = new FakeConn({ "linux.events.read": { records: Array.from({ length: n }, (_, k) => ({ seq: k + 1 })), next: n, dropped: 0 } });
    const ev = new LinuxEvents(fake as any);
    await ev.more("pynq");
    expect(ev.records.length).toBe(LINUX_KEEP);
    expect(ev.discarded).toBe(2);
  });
});
