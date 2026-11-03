import { readFileSync } from "node:fs";
import { resolve } from "node:path";
import { describe, expect, it } from "vitest";
import { EMPTY_ATTACH_FORM, EMPTY_GDB_FORM, EMPTY_HALT_FORM, haltDebugAttachParams, haltGdbServeParams, haltOpenParams, haltThreadsParams, memapJtagParams,
  type GdbServeForm, type HaltForm } from "../src/lib/linuxhalt";

const zynq: HaltForm = { ...EMPTY_HALT_FORM, backend: "fprobe", jtagKhz: "2000", offsets: "C:/x/offsets.json",
  pageOffset: "0xC0000000", physOffset: "0", linearMapEnd: "0xEF800000", maxTasks: "100000", maxThreads: "4096",
  mmuFormat: "short", journal: "C:/x/j.json", fpgaRequestTimes: "false", part: "zynq7000" };

describe("linux.halt.open の引数（既定の値を持たない）", () => {
  it("書いた欄だけを送り、数は 0x も読む", () => {
    expect(haltOpenParams(zynq)).toEqual({ params: { backend: "fprobe", jtag_khz: 2000, offsets: "C:/x/offsets.json",
      page_offset: 0xC0000000, phys_offset: 0, linear_map_end: 0xEF800000, max_tasks: 100000, max_threads: 4096,
      mmu_format: "short", journal: "C:/x/j.json", fpga_request_times: false, part: "zynq7000" } });
  });
  it("必須の欄が空ならその欄の名前（fpga_request_times も選ぶまで送らない）", () => {
    expect(haltOpenParams(EMPTY_HALT_FORM)).toEqual({ error: "backend" });
    expect(haltOpenParams({ ...zynq, jtagKhz: "" })).toEqual({ error: "jtag_khz" });
    expect(haltOpenParams({ ...zynq, fpgaRequestTimes: "" })).toEqual({ error: "fpga_request_times" });
    expect(haltOpenParams({ ...zynq, journal: " " })).toEqual({ error: "journal" });
  });
  it("CTI の上限は ns か unmeasured のまま送る（組の欠けの判断はデーモン）", () => {
    const r = haltOpenParams({ ...zynq, ctiBoard: "zynq7000", ctiRequestBoundNs: "74", ctiEventBoundNs: "unmeasured",
      ctiHwWaitS: "0.05", ctiAckPolls: "100" });
    expect(r).toMatchObject({ params: { cti_board: "zynq7000", cti_request_bound_ns: 74, cti_event_bound_ns: "unmeasured",
      cti_hw_wait_s: 0.05, cti_ack_polls: 100 } });
    expect(haltOpenParams({ ...zynq, ctiBoard: "zynq7000" })).toMatchObject({ params: { cti_board: "zynq7000" } });
    expect(haltOpenParams({ ...zynq, ctiEventBoundNs: "soon" })).toEqual({ error: "cti_event_bound_ns" });
    expect(haltOpenParams({ ...zynq, watchBoundNs: "unmeasured", watchPollS: "0.001" }))
      .toMatchObject({ params: { fpga_halt_watch_bound_ns: "unmeasured", fpga_halt_watch_poll_s: 0.001 } });
  });
  it("OpenOCD の欄", () => {
    const r = haltOpenParams({ ...zynq, backend: "openocd", part: "", ocdBoard: "rpi2b", ocdPort: "6666", ocdTimeoutS: "60",
      hwSlots: "5", watchSlots: "4" });
    expect(r).toMatchObject({ params: { backend: "openocd", ocd_board: "rpi2b", ocd_port: 6666, ocd_timeout_s: 60, hw_slots: 5,
      watch_slots: 4 } });
    expect(haltOpenParams({ ...zynq, ocdTimeoutS: "0" })).toEqual({ error: "ocd_timeout_s" });
  });
});

describe("linux.halt.gdb.serve・threads の引数", () => {
  const gdb: GdbServeForm = { ...EMPTY_GDB_FORM, pid: "812", port: "3340", skewLimitNs: "74", threadId: "stack",
    kernelIsa: "arm", maxStepRetries: "8", bpUnmapped: "error" };
  it("全部の欄を使う人が決める（log は書いたときだけ）", () => {
    expect(haltGdbServeParams(gdb)).toEqual({ params: { pid: 812, port: 3340, skew_limit_ns: 74, thread_id: "stack",
      kernel_isa: "arm", max_step_retries: 8, bp_unmapped: "error" } });
    expect(haltGdbServeParams({ ...gdb, log: "C:/x/rsp.log" })).toMatchObject({ params: { log: "C:/x/rsp.log" } });
    expect(haltGdbServeParams({ ...gdb, threadId: "" })).toEqual({ error: "thread_id" });
    expect(haltGdbServeParams({ ...gdb, bpUnmapped: "" })).toEqual({ error: "bp_unmapped" });
    expect(haltGdbServeParams({ ...gdb, port: "70000" })).toEqual({ error: "port" });
  });
  it("threads の pid", () => {
    expect(haltThreadsParams("812")).toEqual({ params: { pid: 812 } });
    expect(haltThreadsParams("")).toEqual({ error: "pid" });
  });
});

describe("linux.memap.connect_jtag の引数", () => {
  it("chain_idcode・ap・frequency_hz はすべて明示", () => {
    expect(memapJtagParams("pynq", { chainIdcode: "0x4BA00477", ap: "0", frequencyHz: "2000000" }))
      .toEqual({ params: { target: "pynq", chain_idcode: 0x4BA00477, ap: 0, frequency_hz: 2000000 } });
    expect(memapJtagParams("pynq", { chainIdcode: "", ap: "0", frequencyHz: "2000000" })).toEqual({ error: "chain_idcode" });
    expect(memapJtagParams("pynq", { chainIdcode: "0x4BA00477", ap: "256", frequencyHz: "2000000" })).toEqual({ error: "ap" });
  });
});

describe("画面は考えない層（API の応答を描くだけ）", () => {
  const src = readFileSync(resolve(import.meta.dirname, "../src/components/panels/LinuxHaltPanel.svelte"), "utf8");
  it("停止の上限を基準と比べない・並べ替えない（skew_verdict は API の値を出す）", () => {
    expect(src).not.toMatch(/skew_upper_bound_ns\s*[<>]=?\s*[\w$(]/);
    expect(src).not.toMatch(/\.sort\(/);
    expect(src).toMatch(/skew_verdict/);
  });
  it("欄は空から（ボードの値・番地・速さを入れておかない）", () => {
    expect(src).toMatch(/EMPTY_HALT_FORM/);
    expect(src).not.toMatch(/0x4BA00477|zynq7000"\s*}|2000000/);
  });
  it("Linux のパネルに止めずに読む口の JTAG がある", () => {
    const linux = readFileSync(resolve(import.meta.dirname, "../src/components/panels/LinuxPanel.svelte"), "utf8");
    expect(linux).toMatch(/linux\.memap\.connect_jtag/);
  });
});


describe("linux.halt.debug.attach の欄（人も画面から attach する）", () => {
  const filled = { ...EMPTY_ATTACH_FORM, pid: "1234", elf: "C:/pi/race2", skewLimitNs: "68", threadId: "stack" as const,
    kernelIsa: "arm" as const, maxStepRetries: "8", bpUnmapped: "error" as const, maxHaltS: "60" };
  it("必須の欄が空なら送らずにその名前（既定を持たない）", () => {
    expect(haltDebugAttachParams(EMPTY_ATTACH_FORM)).toEqual({ error: "pid" });
    expect(haltDebugAttachParams({ ...filled, maxHaltS: "0" })).toEqual({ error: "max_halt_s" });
  });
  it("書いた欄だけを送る（画面の道の種類・上限・取り寄せ元）", () => {
    expect(haltDebugAttachParams(filled)).toEqual({ params: { pid: 1234, elf: "C:/pi/race2", skew_limit_ns: 68,
      thread_id: "stack", kernel_isa: "arm", max_step_retries: 8, bp_unmapped: "error", max_halt_s: 60 } });
    const all = haltDebugAttachParams({ ...filled, fetchFromTarget: "pi", breakpointKind: "sw", stepMaxInstructions: "4096",
      stepTimeoutS: "2" });
    expect("params" in all && all.params).toMatchObject({ fetch_from_target: "pi", breakpoint_kind: "sw",
      step_max_instructions: 4096, step_timeout_s: 2 });
    expect(haltDebugAttachParams({ ...filled, stepTimeoutS: "x" })).toEqual({ error: "step_timeout_s" });
  });
});
