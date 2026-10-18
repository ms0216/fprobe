// ブラウザの試作: ?r=canvas（Canvas 2D、Path2D）/ ?r=canvasrect（Canvas 2D、fillRect）/ ?r=webgl（WebGL2 のシェーダで波形、文字は 2D の重ね）
// ?mode=bench で views.json を順に描いて時間を測り、/result へ送る。既定は対話（ホイールで拡大縮小・ドラッグで移動）。
import {
  LABEL_W, W, AXIS_H, ROW_H, LANE_H, CH, LANES, HEIGHT, LABEL_MIN_PX, ST_X,
  loadData, decimateWave, decimateLane, axisTicks, fmtTime, stats,
} from "./core.js";

const q = new URLSearchParams(location.search);
const renderer = q.get("r") ?? "canvas", mode = q.get("mode") ?? "interactive", tag = q.get("tag") ?? "browser";
const FULL_W = LABEL_W + W;
const status = document.getElementById("status");

const data = await loadData();
status.textContent = `loaded ${data.loadMs.toFixed(0)} ms`;
const waveBuf = new Uint8Array(CH * W);
const laneCounts = [...Array(LANES)].map(() => new Uint32Array(W));

function makeCanvas(z) {
  const c = document.createElement("canvas");
  c.width = FULL_W; c.height = HEIGHT; c.style.width = FULL_W + "px"; c.style.height = HEIGHT + "px";
  c.style.position = "absolute"; c.style.left = "0"; c.style.top = "0"; c.style.zIndex = z;
  document.getElementById("stage").appendChild(c);
  return c;
}

// --- 文字（軸・名前・ラベル）: 両方の描き方で共通の 2D ---------------------------
function drawText(g, v0, v1, lanesOut) {
  g.fillStyle = "#ddd"; g.font = "12px sans-serif"; g.textBaseline = "middle";
  for (let c = 0; c < CH; c++) g.fillText(`ch${c}`, 8, AXIS_H + c * ROW_H + ROW_H / 2);
  for (let l = 0; l < LANES; l++) g.fillText(data.lanes[l].name, 8, AXIS_H + CH * ROW_H + l * LANE_H + LANE_H / 2);
  g.strokeStyle = "#444"; g.beginPath();
  for (const t of axisTicks(v0, v1, data.meta.tick_s)) {
    const x = LABEL_W + ((t - v0) * W) / (v1 - v0);
    g.moveTo(x, AXIS_H - 6); g.lineTo(x, HEIGHT);
    g.fillText(fmtTime(t * data.meta.tick_s), x + 3, AXIS_H / 2);
  }
  g.stroke();
  g.fillStyle = "#000";
  for (let l = 0; l < LANES; l++) {
    const r = lanesOut[l]; if (!r.sparse) continue;
    const y = AXIS_H + CH * ROW_H + l * LANE_H + LANE_H / 2;
    for (let j = 0; j < r.items.length; j += 3) {
      if (r.items[j + 1] - r.items[j] >= LABEL_MIN_PX) g.fillText(`#${r.items[j + 2]}`, LABEL_W + r.items[j] + 3, y);
    }
  }
}

function decimateAll(v0, v1) {
  decimateWave(data.channels, v0, v1, waveBuf);
  return data.lanes.map((lane, l) => decimateLane(lane, v0, v1, laneCounts[l]));
}

// --- Canvas 2D --------------------------------------------------------------------
function canvasRenderer() {
  const g = makeCanvas(1).getContext("2d", { alpha: false });
  return (v0, v1) => {
    const lanesOut = decimateAll(v0, v1);
    g.fillStyle = "#111"; g.fillRect(0, 0, FULL_W, HEIGHT);
    const lines = new Path2D(), xs = new Path2D();
    for (let c = 0; c < CH; c++) {
      const row = c * W, yHi = AXIS_H + c * ROW_H + 4, yLo = AXIS_H + (c + 1) * ROW_H - 4;
      let p = 0;
      while (p < W) {
        const s = waveBuf[row + p]; let e = p + 1;
        while (e < W && waveBuf[row + e] === s) e++;
        const x0 = LABEL_W + p, x1 = LABEL_W + e;
        if (s === ST_X) xs.rect(x0, yHi, x1 - x0, yLo - yHi);
        else {
          const y = s ? yHi : yLo + 0.5;
          lines.moveTo(x0, y); lines.lineTo(x1, y);
          if (e < W && waveBuf[row + e] !== ST_X) { lines.moveTo(x1, yHi); lines.lineTo(x1, yLo); }
        }
        p = e;
      }
    }
    g.strokeStyle = "#4c4"; g.lineWidth = 1; g.stroke(lines);
    g.fillStyle = "#2a6a2a"; g.fill(xs);
    const rects = new Path2D();
    for (let l = 0; l < LANES; l++) {
      const top = AXIS_H + CH * ROW_H + l * LANE_H, r = lanesOut[l];
      if (r.sparse) {
        for (let j = 0; j < r.items.length; j += 3) rects.rect(LABEL_W + r.items[j], top + 6, r.items[j + 1] - r.items[j], LANE_H - 12);
      } else {
        const counts = laneCounts[l], lm = Math.log2(1 + r.max);
        for (let p = 0; p < W; p++) {
          if (!counts[p]) continue;
          const h = ((LANE_H - 6) * Math.log2(1 + counts[p])) / lm;
          rects.rect(LABEL_W + p, top + LANE_H - 3 - h, 1, h);
        }
      }
    }
    g.fillStyle = "#58a"; g.fill(rects);
    drawText(g, v0, v1, lanesOut);
  };
}

// --- Canvas 2D（fillRect だけ。線も 1 画素の矩形。Path2D の大きなパスを避ける） ----------
function canvasRectRenderer() {
  const g = makeCanvas(1).getContext("2d", { alpha: false });
  return (v0, v1) => {
    const lanesOut = decimateAll(v0, v1);
    g.fillStyle = "#111"; g.fillRect(0, 0, FULL_W, HEIGHT);
    for (const pass of [0, 1]) {                       // 0: 線、1: 変化の塊（色ごとにまとめる）
      g.fillStyle = pass ? "#2a6a2a" : "#4c4";
      for (let c = 0; c < CH; c++) {
        const row = c * W, yHi = AXIS_H + c * ROW_H + 4, yLo = AXIS_H + (c + 1) * ROW_H - 5;
        let p = 0;
        while (p < W) {
          const s = waveBuf[row + p]; let e = p + 1;
          while (e < W && waveBuf[row + e] === s) e++;
          const x0 = LABEL_W + p, x1 = LABEL_W + e;
          if (s === ST_X) { if (pass) g.fillRect(x0, yHi, x1 - x0, yLo - yHi); }
          else if (!pass) {
            g.fillRect(x0, s ? yHi : yLo, x1 - x0, 1);
            if (e < W && waveBuf[row + e] !== ST_X) g.fillRect(x1, yHi, 1, yLo - yHi);
          }
          p = e;
        }
      }
    }
    g.fillStyle = "#58a";
    for (let l = 0; l < LANES; l++) {
      const top = AXIS_H + CH * ROW_H + l * LANE_H, r = lanesOut[l];
      if (r.sparse) {
        for (let j = 0; j < r.items.length; j += 3) g.fillRect(LABEL_W + r.items[j], top + 6, r.items[j + 1] - r.items[j], LANE_H - 12);
      } else {
        const counts = laneCounts[l], lm = Math.log2(1 + r.max);
        for (let p = 0; p < W; p++) {
          if (!counts[p]) continue;
          const h = ((LANE_H - 6) * Math.log2(1 + counts[p])) / lm;
          g.fillRect(LABEL_W + p, top + LANE_H - 3 - h, 1, h);
        }
      }
    }
    drawText(g, v0, v1, lanesOut);
  };
}

// --- WebGL2: 間引いた画素の状態を毎フレーム小さなテクスチャで送り、シェーダで描く ----
function webglRenderer() {
  const gl = makeCanvas(0).getContext("webgl2", { antialias: false });
  const g = makeCanvas(1).getContext("2d");
  const vs = `#version 300 es
  in vec2 p; void main() { gl_Position = vec4(p, 0, 1); }`;
  const fs = `#version 300 es
  precision highp float; precision highp usampler2D;
  uniform usampler2D wave; uniform usampler2D lane;
  out vec4 o;
  const float LW = ${LABEL_W}.0, AX = ${AXIS_H}.0, RH = ${ROW_H}.0, LH = ${LANE_H}.0, H = ${HEIGHT}.0;
  void main() {
    float x = gl_FragCoord.x - LW, y = H - gl_FragCoord.y;     // 上から
    o = vec4(0.067, 0.067, 0.067, 1);
    if (x < 0.0 || y < AX) return;
    int px = int(x);
    if (y < AX + ${CH}.0 * RH) {
      int row = int((y - AX) / RH); float yy = y - AX - float(row) * RH;
      uint s = texelFetch(wave, ivec2(px, row), 0).r;
      uint sp = px > 0 ? texelFetch(wave, ivec2(px - 1, row), 0).r : s;
      if (yy < 4.0 || yy > RH - 4.0) return;
      if (s == 2u) { o = vec4(0.16, 0.42, 0.16, 1); return; }
      bool edge = sp != s && sp != 2u;
      bool lvl = s == 1u ? yy < 5.0 : yy > RH - 5.0;
      if (lvl || edge) o = vec4(0.3, 0.8, 0.3, 1);
      return;
    }
    int l = int((y - AX - ${CH}.0 * RH) / LH); float yy = y - AX - ${CH}.0 * RH - float(l) * LH;
    uint h = texelFetch(lane, ivec2(px, l), 0).r;            // 0..255 = 高さの割合
    if (h > 0u && yy > LH - 3.0 - (LH - 6.0) * float(h) / 255.0 && yy < LH - 3.0) o = vec4(0.34, 0.53, 0.67, 1);
  }`;
  const prog = gl.createProgram();
  for (const [type, src] of [[gl.VERTEX_SHADER, vs], [gl.FRAGMENT_SHADER, fs]]) {
    const s = gl.createShader(type); gl.shaderSource(s, src); gl.compileShader(s);
    if (!gl.getShaderParameter(s, gl.COMPILE_STATUS)) throw new Error(gl.getShaderInfoLog(s));
    gl.attachShader(prog, s);
  }
  gl.linkProgram(prog); gl.useProgram(prog);
  const buf = gl.createBuffer(); gl.bindBuffer(gl.ARRAY_BUFFER, buf);
  gl.bufferData(gl.ARRAY_BUFFER, new Float32Array([-1, -1, 1, -1, -1, 1, 1, 1]), gl.STATIC_DRAW);
  gl.enableVertexAttribArray(0); gl.vertexAttribPointer(0, 2, gl.FLOAT, false, 0, 0);
  gl.pixelStorei(gl.UNPACK_ALIGNMENT, 1);
  const tex = (unit, h) => {
    const t = gl.createTexture(); gl.activeTexture(gl.TEXTURE0 + unit); gl.bindTexture(gl.TEXTURE_2D, t);
    gl.texStorage2D(gl.TEXTURE_2D, 1, gl.R8UI, W, h);
    gl.texParameteri(gl.TEXTURE_2D, gl.TEXTURE_MIN_FILTER, gl.NEAREST); gl.texParameteri(gl.TEXTURE_2D, gl.TEXTURE_MAG_FILTER, gl.NEAREST);
    return t;
  };
  const waveTex = tex(0, CH), laneTex = tex(1, LANES);
  gl.uniform1i(gl.getUniformLocation(prog, "wave"), 0); gl.uniform1i(gl.getUniformLocation(prog, "lane"), 1);
  const laneBuf = new Uint8Array(LANES * W);
  return (v0, v1) => {
    const lanesOut = decimateAll(v0, v1);
    laneBuf.fill(0);
    for (let l = 0; l < LANES; l++) {
      const r = lanesOut[l], row = l * W;
      if (r.sparse) {
        for (let j = 0; j < r.items.length; j += 3) {
          for (let p = Math.floor(r.items[j]); p < Math.min(W, Math.ceil(r.items[j + 1])); p++) laneBuf[row + p] = 255;
        }
      } else {
        const counts = laneCounts[l], lm = Math.log2(1 + r.max);
        for (let p = 0; p < W; p++) if (counts[p]) laneBuf[row + p] = Math.max(1, (255 * Math.log2(1 + counts[p])) / lm);
      }
    }
    gl.activeTexture(gl.TEXTURE0); gl.bindTexture(gl.TEXTURE_2D, waveTex);
    gl.texSubImage2D(gl.TEXTURE_2D, 0, 0, 0, W, CH, gl.RED_INTEGER, gl.UNSIGNED_BYTE, waveBuf);
    gl.activeTexture(gl.TEXTURE1); gl.bindTexture(gl.TEXTURE_2D, laneTex);
    gl.texSubImage2D(gl.TEXTURE_2D, 0, 0, 0, W, LANES, gl.RED_INTEGER, gl.UNSIGNED_BYTE, laneBuf);
    gl.drawArrays(gl.TRIANGLE_STRIP, 0, 4);
    g.clearRect(0, 0, FULL_W, HEIGHT);
    drawText(g, v0, v1, lanesOut);
  };
}

const draw = renderer === "webgl" ? webglRenderer() : renderer === "canvasrect" ? canvasRectRenderer() : canvasRenderer();

if (mode === "bench") {
  const work = [], intervals = [];
  let i = 0, last = 0;
  const frame = (now) => {
    if (i > 0) intervals.push(now - last);
    last = now;
    if (i < data.views.length) {
      const [v0, v1] = data.views[i];
      const t = performance.now(); draw(v0, v1); work.push(performance.now() - t);
      i++; requestAnimationFrame(frame);
    } else {
      const skip = data.meta.warmup;
      const result = {
        impl: `${tag}-${renderer}`, ua: navigator.userAgent, load_ms: data.loadMs,
        work_ms: stats(work.slice(skip)), interval_ms: stats(intervals.slice(skip)),
        raw_work: work.slice(skip).map((x) => +x.toFixed(2)), raw_interval: intervals.slice(skip).map((x) => +x.toFixed(2)),
        js_heap_mb: performance.memory ? performance.memory.usedJSHeapSize / 2 ** 20 : null,
      };
      fetch("/result", { method: "POST", body: JSON.stringify(result) }).then(() => { status.textContent = "done"; document.title = "done"; });
    }
  };
  requestAnimationFrame(frame);
} else {
  let [v0, v1] = q.has("v") ? data.views[+q.get("v")] : [0, data.meta.span], dragging = null, dirty = true;   // ?v=番号 で views の視点から
  const stage = document.getElementById("stage");
  stage.addEventListener("wheel", (e) => {
    e.preventDefault();
    const a = Math.min(1, Math.max(0, (e.offsetX - LABEL_W) / W)), at = v0 + a * (v1 - v0);
    const span = Math.min(data.meta.span, Math.max(50, (v1 - v0) * Math.exp(e.deltaY * 0.002)));
    v0 = Math.min(Math.max(0, at - a * span), data.meta.span - span); v1 = v0 + span; dirty = true;
  }, { passive: false });
  stage.addEventListener("mousedown", (e) => { dragging = { x: e.clientX, v0 }; });
  addEventListener("mouseup", () => { dragging = null; });
  addEventListener("mousemove", (e) => {
    if (!dragging) return;
    const span = v1 - v0;
    v0 = Math.min(Math.max(0, dragging.v0 - ((e.clientX - dragging.x) * span) / W), data.meta.span - span); v1 = v0 + span; dirty = true;
  });
  const loop = () => {
    if (dirty) { const t = performance.now(); draw(v0, v1); status.textContent = `${(performance.now() - t).toFixed(2)} ms/frame`; dirty = false; }
    requestAnimationFrame(loop);
  };
  loop();
}
