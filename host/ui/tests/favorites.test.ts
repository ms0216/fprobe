// お気に入り（lib/favorites.ts）と、API パネル・お気に入りが共有する呼び出し（lib/apicall.ts）
import { describe, expect, it } from "vitest";
import spec from "../../api/fprobe_api.json";
import { callApi } from "../src/lib/apicall";
import { apiMethods, type ApiSpec } from "../src/lib/apiform";
import { Confirmer } from "../src/lib/confirm";
import { Favorites, checkFavorite } from "../src/lib/favorites";
import { FakeConn } from "./fake-conn";

const METHODS = apiMethods(spec as unknown as ApiSpec);
const flush = async () => { for (let i = 0; i < 10; i++) await Promise.resolve(); };

function memoryStore(initial: string | null = null) {
  let value = initial;
  return { load: () => value, save: (v: string) => (value = v), get value() { return value; } };
}

describe("Favorites", () => {
  it("足す・名前を変える・並べ替える・消す。ブラウザに覚え、開き直しても同じ", () => {
    const store = memoryStore();
    const f = new Favorites(store);
    const a = f.add("target.dp.read", { register: 0x24 });
    const b = f.add("recording.list", {}, "記録の一覧");
    expect(f.list.map((x) => [x.label, x.method])).toEqual([["target.dp.read", "target.dp.read"], ["記録の一覧", "recording.list"]]);
    f.rename(a, "TARGETID");
    f.move(b, -1);
    expect(f.list.map((x) => x.label)).toEqual(["記録の一覧", "TARGETID"]);
    expect(new Favorites(store).list).toEqual(f.list);
    f.remove(b);
    expect(f.list.map((x) => x.id)).toEqual([a]);
  });

  it("覚えたものが壊れていても空から動く", () => {
    expect(new Favorites(memoryStore("{x")).list).toEqual([]);
    expect(new Favorites(memoryStore('[{"method": 1}]')).list).toEqual([]);
  });

  it("変わりを知らせる", () => {
    const f = new Favorites(memoryStore());
    let seen = -1;
    f.subscribe((list) => (seen = list.length));
    expect(seen).toBe(0);
    f.add("recording.list", {});
    expect(seen).toBe(1);
  });
});

describe("checkFavorite", () => {
  it("仕様に無いメソッド・引数が合わないものは事実で知らせる", () => {
    expect(checkFavorite({ id: "1", label: "x", method: "no.such", params: {} }, METHODS)).toEqual({ problem: "missing" });
    const bad = checkFavorite({ id: "1", label: "x", method: "target.dp.read", params: { register: [1] } }, METHODS);
    expect("problem" in bad && bad.problem).toBe("params");
    const ok = checkFavorite({ id: "1", label: "x", method: "target.dp.read", params: { register: 36 } }, METHODS);
    expect("method" in ok && ok.params).toEqual({ register: 36 });
  });
});

describe("callApi", () => {
  const read = METHODS.find((m) => m.name === "recording.list")!;
  const control = METHODS.find((m) => m.name === "recording.trigger_clear")!;
  const danger = METHODS.find((m) => m.name === "target.flash.erase")!;

  it("read は確かめずに呼び、結果を返す", async () => {
    const conn = new FakeConn({ "recording.list": { recordings: [] } });
    const ask = new Confirmer();
    expect(await callApi(read, {}, { conn, app: { aiWorking: false }, ask })).toEqual({ ok: true, text: '{\n  "recordings": []\n}' });
    expect(conn.calls).toEqual([["recording.list", {}]]);
  });

  it("control は確かめ、いいえなら呼ばない。元に戻せない操作は赤", async () => {
    const conn = new FakeConn({ "recording.trigger_clear": {}, "target.flash.erase": {} });
    const ask = new Confirmer();
    let danger_ = false;
    ask.subscribe((r) => { if (r) danger_ = !!r.danger; });
    const p = callApi(control, {}, { conn, app: { aiWorking: false }, ask });
    await flush();
    expect(danger_).toBe(false);
    ask.answer(false);
    expect(await p).toBeNull();
    expect(conn.calls).toEqual([]);
    const q = callApi(danger, { mode: "chip" }, { conn, app: { aiWorking: false }, ask });
    await flush();
    expect(danger_).toBe(true);
    ask.answer(true);
    expect((await q)?.ok).toBe(true);
    expect(conn.calls).toEqual([["target.flash.erase", { mode: "chip" }]]);
  });

  it("誤りは結果の誤りとして返す", async () => {
    const conn = new FakeConn({ "recording.list": () => { throw new Error("RuntimeError: 記録が無い"); } });
    expect(await callApi(read, {}, { conn, app: { aiWorking: false }, ask: new Confirmer() })).toEqual({ ok: false, text: "記録が無い" });
  });
});

describe("Favorites.update（API で開いて直したものを置き換える）", () => {
  it("同じお気に入りのメソッドと引数を置き換える（名前と位置は保つ）", () => {
    const f = new Favorites(memoryStore());
    const a = f.add("target.dp.read", { register: 0x24 }, "TARGETID");
    f.add("recording.list", {});
    f.update(a, "target.dp.read", { register: 4 });
    expect(f.list[0]).toMatchObject({ id: a, label: "TARGETID", params: { register: 4 } });
    expect(f.list.length).toBe(2);
  });
});
