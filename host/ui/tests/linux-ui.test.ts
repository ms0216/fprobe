// Linux のプロセスの画面（Threads の Linux のスレッドと選択）と、AI が開くパネル（ui.state.set の panel）
import { readFileSync } from "node:fs";
import { describe, expect, it } from "vitest";
import { PANELS } from "../src/components/panels/registry";
import { LinuxThreads, type LinuxThreadsSnapshot } from "../src/lib/linuxthreads";
import { followPanelRequests } from "../src/lib/panelrequest";
import { FakeConn } from "./fake-conn";

const settle = async () => {
  for (let i = 0; i < 5; i++) await new Promise((r) => setTimeout(r, 0));
};
const LINUX = { pid: 100, comm: "race", tid: 101, tid_from: "rule", thread_comm: "race",
  threads: [{ tid: 100, comm: "race" }, { tid: 101, comm: "race" }], stop_kind: "attach", registers_from: "core",
  pc_error: null, elf: "/pc/race_arm", load_bias: 0 };
const ROWS = { pid: 100, load_bias: 0, stop: null, threads: [
  { tid: 100, comm: "race", on_core: 3, mode: "user", registers_from: "core", pc: 0x10, function: "add_one", file: "race.c", line: 10 },
  { tid: 101, comm: "race", on_core: 1, mode: "user", registers_from: "core", pc: 0x14, function: "add_one", file: "race.c", line: 11 }] };

describe("Threads の Linux のスレッド", () => {
  it("止まったら全スレッドを読み、行を選ぶと linux.halt.debug.select が飛ぶ。走れば古い印、閉じたら空", async () => {
    const conn = new FakeConn({ "linux.halt.debug.threads": ROWS });
    const acts: [string, any][] = [];
    const store = new LinuxThreads(conn as any, { act: async (m: any, p: any) => { acts.push([m, p]); return {}; } } as any);
    let snap!: LinuxThreadsSnapshot;
    store.subscribe((s) => (snap = s));
    store.onTarget({ state: "HALTED", pc: 0x14, run: 1, linux: LINUX });
    await settle();
    expect(snap.rows?.threads.map((t) => t.tid)).toEqual([100, 101]);
    expect(conn.calls).toEqual([["linux.halt.debug.threads", { variables: false, expressions: [], max_elements: 1 }]]);
    store.onTarget({ state: "HALTED", pc: 0x14, run: 1, linux: LINUX });           // 同じ止まり: 読み直さない
    await settle();
    expect(conn.calls.length).toBe(1);
    await store.select(100);
    expect(acts).toEqual([["linux.halt.debug.select", { tid: 100 }]]);
    store.onTarget({ state: "HALTED", pc: 0x10, run: 2, linux: { ...LINUX, tid: 100, tid_from: "selected" } });
    await settle();
    expect(conn.calls.length).toBe(2);                                             // 選んだら読み直す
    store.onTarget({ state: "RUNNING", run: 3, linux: LINUX });
    expect(snap.stale).toBe(true);
    store.onTarget({ state: "DISCONNECTED", run: 4 });
    expect(snap.rows).toBeNull();
  });
});

describe("AI が開くパネル（ui.state.set の panel）", () => {
  it("通知 ui.state の panel で、その窓がパネルを開く（同じパネルの頼み直しも seq で届く）", () => {
    const conn = new FakeConn({});
    const shown: string[] = [];
    followPanelRequests(conn as any, (id) => shown.push(id));
    conn.emit("ui.state", { changed: ["panel"], state: { panel: { id: "threads", seq: 1 } } });
    conn.emit("ui.state", { changed: ["cursors"], state: { panel: { id: "threads", seq: 1 } } });
    conn.emit("ui.state", { changed: ["panel"], state: { panel: { id: "threads", seq: 2 } } });
    expect(shown).toEqual(["threads", "threads"]);
  });

  it("仕様の panel の名前は画面のパネルの id と同じ（片方だけ変えるとずれる）", () => {
    const spec = JSON.parse(readFileSync(new URL("../../api/fprobe_api.json", import.meta.url), "utf-8"));
    const names = spec.methods["ui.state.set"].params.properties.panel.anyOf[0].enum;
    expect(names).toEqual(PANELS.map((p) => p.id));
  });
});
