import { readFileSync } from "node:fs";
import { resolve } from "node:path";
import { describe, expect, it } from "vitest";
import { EMPTY_ATTACH_FORM, EMPTY_GDB_FORM, EMPTY_HALT_FORM, attachFormOf, exampleAttachForm, exampleHaltForm, haltFormOf, shouldRefreshOn, haltDebugAttachParams, haltGdbServeParams, haltOpenParams, haltThreadsParams, memapJtagParams,
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
  it("欄の最初の値は仕様の「例・推奨の値」から（画面のコードにボードの値・番地・速さを直接書かない）", () => {
    // CLAUDE.md（2026-11-03）: 値の出どころは API の仕様。画面のコードに直接書くと、人に見えて AI（MCP）に見えない
    expect(src).toMatch(/exampleHaltForm/);
    expect(src).not.toMatch(/0x4BA00477|zynq7000"\s*}|2000000|0xBB400000|0x80000000/);
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

describe("欄の最初の値は API の仕様の「例・推奨の値」（見えて・書き換えられ・送るときは欄の値を明示して送る）", () => {
  // 最初の値の基準（CLAUDE.md 2026-11-04）: A・B（間違えれば失敗が見える・どの対象にも通じる）は仕様の examples から入り、
  // C・D・E（判定の閾値・対象のカーネルに固有・書き換える・壊す）は空から
  it("open の欄: A・B の欄は仕様の examples が入り、対象のカーネルに固有の欄（番地・MMU の形式・部品）とパスは空のまま", () => {
    const f = exampleHaltForm();
    expect([f.backend, f.jtagKhz, f.maxTasks, f.maxThreads, f.fpgaRequestTimes]).toEqual(["fprobe", "25000", "100000", "4096", "true"]);
    expect([f.pageOffset, f.physOffset, f.linearMapEnd, f.mmuFormat, f.part, f.offsets, f.journal]).toEqual(["", "", "", "", "", "", ""]);
    const built = haltOpenParams({ ...f, offsets: "C:/x/o.json", journal: "C:/x/j.json", pageOffset: "0x80000000", physOffset: "0",
                                   linearMapEnd: "0xBB400000", mmuFormat: "short" });
    expect("params" in built && built.params).toMatchObject({ backend: "fprobe", jtag_khz: 25000, page_offset: 2147483648,
      linear_map_end: 3141533696, max_tasks: 100000, max_threads: 4096, mmu_format: "short", fpga_request_times: true });
  });

  it("欄を書き換えれば、書き換えた値が送られる（画面が既定を黙って補わない）", () => {
    const built = haltOpenParams({ ...exampleHaltForm(), offsets: "o", journal: "j", jtagKhz: "1000", pageOffset: "0x80000000",
                                   physOffset: "0", linearMapEnd: "0xBB400000", mmuFormat: "short" });
    expect("params" in built && (built.params as { jtag_khz: number }).jtag_khz).toBe(1000);
  });

  it("attach の欄: A・B の欄は仕様の examples が入り、判定の閾値・対象に固有・書き換える・壊しうる欄と pid・elf は空のまま", () => {
    const f = exampleAttachForm();
    expect([f.maxStepRetries, f.bpUnmapped, f.stepMaxInstructions, f.stepTimeoutS]).toEqual(["8", "error", "200000", "5"]);
    expect([f.skewLimitNs, f.threadId, f.kernelIsa, f.maxHaltS, f.breakpointKind, f.pid, f.elf]).toEqual(["", "", "", "", "", "", ""]);
  });
});

describe("使っている設定を欄に映す（AI が open / attach したときも、人の画面の欄に同じ値が見える）", () => {
  it("open の設定（API の options）が欄の値になる。無い・null の設定は今の欄のまま", () => {
    const f = haltFormOf(EMPTY_HALT_FORM, { backend: "fprobe", jtag_khz: 25000, offsets: "C:/o.json", page_offset: 2147483648,
      phys_offset: 0, linear_map_end: 3141533696, max_tasks: 100000, max_threads: 4096, mmu_format: "short",
      journal: "C:/j.json", fpga_request_times: true, part: "bcm2836" });
    expect([f.backend, f.jtagKhz, f.offsets, f.pageOffset, f.physOffset, f.maxThreads, f.mmuFormat, f.journal, f.fpgaRequestTimes, f.part])
      .toEqual(["fprobe", "25000", "C:/o.json", "0x80000000", "0x0", "4096", "short", "C:/j.json", "true", "bcm2836"]);
    expect(haltFormOf({ ...EMPTY_HALT_FORM, part: "mine" }, { part: null }).part).toBe("mine");
  });

  it("attach の設定が欄の値になる。渡さなかった任意の設定（null）は欄を空のまま", () => {
    const f = attachFormOf(EMPTY_ATTACH_FORM, { pid: 1234, elf: "C:/x/race2", skew_limit_ns: 50000000, thread_id: "stack",
      kernel_isa: "arm", max_step_retries: 8, bp_unmapped: "error", max_halt_s: 120, fetch_from_target: null,
      breakpoint_kind: "sw", step_max_instructions: 200000, step_timeout_s: 5 });
    expect([f.pid, f.elf, f.skewLimitNs, f.threadId, f.kernelIsa, f.maxStepRetries, f.bpUnmapped, f.maxHaltS, f.fetchFromTarget,
      f.breakpointKind, f.stepMaxInstructions, f.stepTimeoutS])
      .toEqual(["1234", "C:/x/race2", "50000000", "stack", "arm", "8", "error", "120", "", "sw", "200000", "5"]);
  });
});

describe("状態の読み直しの循環を作らない（2026-11-04 実機: attach 中に毎秒 500 回の問い合わせになった）", () => {
  it("状態を変える操作では読み直し、パネル自身の読み（status・debug.status 等）の記録では読み直さない", () => {
    for (const m of ["linux.halt.open", "linux.halt.close", "linux.halt.debug.attach", "linux.halt.debug.close", "linux.halt.halt_all",
                     "linux.halt.resume_all", "linux.halt.debug.run", "linux.halt.debug.halt", "linux.halt.debug.break.insert"]) {
      expect(shouldRefreshOn(m), m).toBe(true);
    }
    for (const m of ["linux.halt.status", "linux.halt.debug.status", "linux.halt.debug.threads", "linux.halt.processes",
                     "linux.halt.gdb.status", "debug.backtrace", "target.status"]) {
      expect(shouldRefreshOn(m), m).toBe(false);
    }
  });
});
