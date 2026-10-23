// 止めると壊れるターゲットの安全: 止める前の警告（debug.halt_risk）と、走らせたままの致命的エラー（target.state の fatal）
import { describe, expect, it } from "vitest";
import { t } from "../src/lib/i18n";
import { Safety, clueText, confirmText, fatalTitle, isFatal, linkText, needsWarning, reasonText, type HaltRisk } from "../src/lib/safety";
import { AppState } from "../src/lib/state";
import { FakeConn } from "./fake-conn";

const RISK: HaltRisk = {
  elf: "/x/zmk.elf", matched: true, monitor_in_elf: false,
  matches: [{ kind: "radio", count: 12, symbols: ["RADIO_IRQHandler", "ll_adv_enable"] },
            { kind: "rtos", count: 1, symbols: ["_kernel"], rtos: "Zephyr" }],
};

const tick = () => new Promise((r) => setTimeout(r, 0));

function setup(risk: HaltRisk | null, answer = true) {
  const conn = new FakeConn({ "debug.halt_risk": () => { if (!risk) throw new Error("no elf"); return risk; } });
  const app = new AppState(conn as any);
  const asked: string[] = [];
  const safety = new Safety(conn as any, app, (text) => { asked.push(text); return answer; });
  return { conn, app, safety, asked };
}

describe("止める前の警告", () => {
  it("止めると壊れうる ELF で、モニタ方式でなければ警告する", () => {
    expect(needsWarning(RISK, { state: "RUNNING" })).toBe(true);
    expect(needsWarning(RISK, { state: "HALTED", debug_mode: "monitor" })).toBe(false);
    expect(needsWarning({ ...RISK, matched: false, matches: [] }, { state: "RUNNING" })).toBe(false);
    expect(needsWarning(null, { state: "RUNNING" })).toBe(false);
  });

  it("理由は手がかりのシンボルと数で、止めずに調べる方法へ案内する", () => {
    expect(reasonText(RISK.matches[0], t)).toContain("RADIO_IRQHandler");
    expect(reasonText(RISK.matches[0], t)).toContain("11");
    expect(reasonText(RISK.matches[1], t)).toContain("Zephyr");
    const text = confirmText(RISK, "halt", t);
    expect(text).toContain(t("safety.action.halt"));
    expect(text).toContain(t("safety.addMonitor"));
    expect(confirmText({ ...RISK, monitor_in_elf: true }, "step", t)).toContain(t("safety.useMonitor"));
  });

  it("ELF を読んだら見分け、止める操作の前に 1 度だけ確かめる（ELF ごと）", async () => {
    const { conn, app, safety, asked } = setup(RISK);
    conn.link!("open");
    await tick();
    expect(safety.snapshot.risk?.matched).toBe(true);
    expect(conn.quiet).toContain("debug.halt_risk");
    conn.emit("target.state", { state: "RUNNING" });
    expect(await safety.confirmHalt("halt", t)).toBe(true);
    expect(await safety.confirmHalt("breakpoint", t)).toBe(true);
    expect(asked.length).toBe(1);
    void app;
  });

  it("「いいえ」なら止めない。モニタ方式なら聞かない", async () => {
    const { conn, safety, asked } = setup(RISK, false);
    conn.link!("open");
    await tick();
    conn.emit("target.state", { state: "RUNNING" });
    expect(await safety.confirmHalt("halt", t)).toBe(false);
    conn.emit("target.state", { state: "RUNNING", debug_mode: "monitor" });
    expect(await safety.confirmHalt("halt", t)).toBe(true);
    expect(asked.length).toBe(1);
  });

  it("ELF が無ければ聞かない。ELF を読み直したら見分け直す", async () => {
    const { conn, safety, asked } = setup(null);
    conn.link!("open");
    await tick();
    expect(safety.snapshot.risk).toBeNull();
    expect(await safety.confirmHalt("step", t)).toBe(true);
    expect(asked).toEqual([]);
    conn.responders["debug.halt_risk"] = RISK;
    conn.emit("activity", { method: "debug.elf.load" });
    await tick();
    expect(safety.snapshot.risk?.elf).toBe("/x/zmk.elf");
  });
});

describe("走らせたままの致命的エラー", () => {
  it("ティックが変わっていないだけ（tick_stopped）は致命的エラーと言わない。ほかの種類は PC・ロックアップの事実", () => {
    expect(isFatal({ kind: "tick_stopped", since: 1, evidence: [] } as any)).toBe(false);
    for (const kind of ["lockup", "fatal_halt", "fault_loop"]) expect(isFatal({ kind, since: 1, evidence: [] } as any)).toBe(true);
  });

  it("target.state の fatal を受け取り、見出しと手がかりを言葉にする", () => {
    const { conn, safety } = setup(null);
    const fatal = { kind: "fatal_halt", function: "arch_system_halt", since: 1700000000,
                    evidence: [{ clue: "pc_in", function: "arch_system_halt" }, { clue: "tick_stopped", tick: 1234 }] };
    conn.emit("target.state", { state: "RUNNING", fatal });
    expect(safety.snapshot.fatal).toEqual(fatal);
    expect(fatalTitle(safety.snapshot.fatal!, t)).toContain("arch_system_halt");
    expect(clueText(fatal.evidence[1] as any, t)).toContain("1234");
    conn.emit("target.state", { state: "HALTED", pc: 0 });
    expect(safety.snapshot.fatal).toBeNull();
  });
});

describe("プローブの USB のつなぎ直し（B28: target.state の link_lost）", () => {
  it("機能が変わっていれば、変わったものを前 → 後で並べる", () => {
    expect(linkText({ at: 1, changed: {} }, t)).toBe(t("safety.linkLost"));
    const text = linkText({ at: 1, changed: { jtag: [false, true], sampler: [true, false] } }, t);
    expect(text).toContain(t("safety.linkLost"));
    expect(text).toContain("jtag: false → true, sampler: true → false");
  });

  it("版が分かれば、版が変わったかを確実に言う（U49）", () => {
    const version = (changed: boolean | null, latest_build: boolean | null) =>
      ({ before: "aaaa", after: "bbbb", changed, latest_build });
    const changedFeatures = { jtag: [false, true] as [unknown, unknown] };
    expect(linkText({ at: 1, changed: changedFeatures, version: version(false, null) }, t)).toBe(t("safety.linkLost"));
    const flashed = linkText({ at: 1, changed: {}, version: version(true, false) }, t);
    expect(flashed).toContain(t("safety.linkVersion", { before: "aaaa", after: "bbbb" }));
    const latest = linkText({ at: 1, changed: {}, version: version(true, true) }, t);
    expect(latest).toContain(t("safety.linkVersionLatest", { before: "aaaa", after: "bbbb" }));
    const unknown = linkText({ at: 1, changed: changedFeatures, version: version(null, null) }, t);
    expect(unknown).toContain("jtag: false → true");
  });
});
