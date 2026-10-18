// 画面の間引き（TS）が Python の la.Waveform.view・Recording.waveform_view・Recording.density と同じ結果になる。
// 固定データ（tests/fixtures）は host/tests/ui_fixture.py が作る: タイルのバイナリのフレームと、同じ視点の Python の結果。
import { readFileSync } from "node:fs";
import { resolve } from "node:path";
import { describe, expect, it } from "vitest";
import { MODE, composeFrame, type RowSpec } from "../src/lib/timeline/compose";
import { Pixels, ceilDiv, decimateWave, floorDiv, laneCounts, visibleRange } from "../src/lib/timeline/decimate";
import { decodeFrame, isFrame } from "../src/lib/timeline/frame";
import { fromSections, tileServes, type Tile } from "../src/lib/timeline/tile";

const dir = resolve(import.meta.dirname, "fixtures");
const fixture = JSON.parse(readFileSync(resolve(dir, "decimate.json"), "utf8"));
const CHARS = "01x-";

function load(name: string): Tile {
  const bytes = readFileSync(resolve(dir, name));
  const buf = bytes.buffer.slice(bytes.byteOffset, bytes.byteOffset + bytes.byteLength) as ArrayBuffer;
  const message = decodeFrame(buf);
  return fromSections(message.result, message.result.data);
}

function rows(tile: Tile, v0: number, v1: number, width: number): string[] {
  const out = new Uint8Array(tile.wave.channels.length * width);
  decimateWave(tile, new Pixels(v0, v1, width), out);
  return tile.wave.channels.map((_, r) => Array.from(out.subarray(r * width, (r + 1) * width), (s) => CHARS[s]).join(""));
}

describe("Python と同じ間引き（固定データ）", () => {
  for (const c of fixture.cases) {
    it(c.name, () => {
      const tile = load(c.frame);
      for (const v of c.views) {
        expect(rows(tile, v.v0, v.v1, v.width), `${v.v0}..${v.v1} / ${v.width}`).toEqual(v.wave);
        tile.lanes.forEach((lane) => {
          const out = new Uint32Array(v.width);
          laneCounts(tile, lane, new Pixels(v.v0, v.v1, v.width), out);
          expect(Array.from(out), `${lane.stream} ${v.v0}..${v.v1} / ${v.width}`).toEqual(v.lanes[lane.stream]);
        });
      }
    });
  }
});

describe("フレームとタイル", () => {
  it("フレームの区画を複写せずに型付きの配列で見る", () => {
    const bytes = readFileSync(resolve(dir, "recording-raw.bin"));
    const buf = bytes.buffer.slice(bytes.byteOffset, bytes.byteOffset + bytes.byteLength) as ArrayBuffer;
    expect(isFrame(buf)).toBe(true);
    const m = decodeFrame(buf);
    expect(m.id).toBe(1);
    expect(m.result.data["lane.0.t"]).toBeInstanceOf(Float64Array);
    expect(m.result.data["lane.0.t"].buffer).toBe(buf);
    expect(m.result.data["lane.0.kind"]).toBeInstanceOf(Uint16Array);
    expect(isFrame(new ArrayBuffer(4))).toBe(false);
  });

  it("タイルが視点を描けるか（範囲と要約の細かさ）", () => {
    const raw = load("capture-raw.bin"), summary = load("capture-summary.bin");
    expect(tileServes(raw.header, 0, 20000, 1000)).toBe(true);
    expect(tileServes(raw.header, 0, 20000, 0.01)).toBe(true);                 // 生データはどこまで拡大しても足りる
    expect(tileServes(summary.header, 0, 20000, 16)).toBe(true);
    expect(tileServes(summary.header, 0, 1000, 1)).toBe(false);               // 要約の区間より細かい画素は描けない
    expect(tileServes(load("capture-part.bin").header, 4000, 6000, 10)).toBe(false);
  });
});

describe("間引きの部品", () => {
  it("整数の割り算は大きな数でも正確", () => {
    expect(floorDiv(7, 2)).toBe(3);
    expect(floorDiv(-7, 2)).toBe(-4);
    expect(ceilDiv(7, 2)).toBe(4);
    expect(ceilDiv(-7, 2)).toBe(-3);
    for (const a of [2 ** 53 - 1, 2 ** 53 - 2, 3 * 1e15 - 1, 1500 * 4e12 + 1499]) {
      for (const b of [3, 7, 1500, 4e12]) {
        expect(floorDiv(a, b), `${a} / ${b}`).toBe(Number(BigInt(a) / BigInt(b)));   // 浮動小数の割り算の丸めを直す
      }
    }
  });

  it("段の中で見えている出来事（始めの前から続く区間も）", () => {
    const tile = load("recording-raw.bin");
    const at = tile.rowAt.get("uart.tx")!;
    const lane = tile.lanes[at.lane];
    if (lane.mode !== "raw") throw new Error("生の段のはず");
    const ev = lane.byRow[at.row];
    const k = ev.t.findIndex((t, i) => ev.tEnd[i] > t + 2);
    const [i0, i1] = visibleRange(ev, ev.t[k] + 1, ev.t[k] + 101);
    expect(i0).toBeLessThanOrEqual(k);
    expect(i1).toBeGreaterThan(k);
  });

  it("1 フレームの材料: 波形の段・まとまりの見出し（件数）・箱の段", () => {
    const tile = load("recording-raw.bin");
    const rows: RowSpec[] = [{ id: "wave:0", type: "logic", color: 0 }, { id: "group:uart", type: "group", color: 0 },
                             { id: "uart.tx", type: "bus", color: 3 }];
    const f = composeFrame(tile, 0, 20000, 400, rows, 7);
    expect(f.seq).toBe(7);
    expect(f.rows).toBe(3);
    expect(f.data.length).toBe(3 * 400 * 4);
    expect(Array.from(f.modes)).toEqual([MODE.logic, MODE.header, MODE.bus]);
    const uart = tile.lanes.find((l) => l.stream === "uart")!;
    expect(f.visible[1]).toBe(uart.count);
    const zoomed = composeFrame(tile, 5000, 5200, 400, rows);
    expect(zoomed.labels.length).toBeGreaterThan(0);                  // 拡大すると箱にラベル
  });
});
