// WebGL2 の描画: 間引いた「段 × 画素」の RGBA（compose.ts）を RGBA8UI のテクスチャで送り、画面全体の 1 枚の四角形を
// シェーダで塗る。段の位置と描き方は小さなテクスチャ 2 枚（y → 段の番号、段 → 上端・高さ・描き方・色）。GPU の作業は
// 画素の数だけで決まり、データの量・段の数に依らない。文字は重ねの Canvas 2D。
import { MODE, type TimelineFrame } from "./compose";
import { PALETTE_COLORS, alignFrame, type Colors, type Layout, type Rgba } from "./layout";
import type { TimelineRenderer } from "./renderer";

const VERTEX = `#version 300 es
in vec2 p;
void main() { gl_Position = vec4(p, 0.0, 1.0); }`;

const FRAGMENT = `#version 300 es
precision highp float;
precision highp int;
precision highp usampler2D;
uniform usampler2D u_data;      // 画素 × 段 の RGBA（compose.ts）
uniform usampler2D u_rowOf;     // y（デバイスの画素）→ 段の番号 + 1（0 = 段の外）
uniform highp sampler2D u_rows; // 段 → (上端, 高さ, 描き方, 色)
uniform float u_height, u_scale, u_dpr;
uniform int u_frameWidth;
uniform vec4 c_line, c_fill, c_bar, c_head, c_grid;
uniform vec4 u_palette[${PALETTE_COLORS}];
out vec4 o;

vec4 over(vec4 top, vec4 under) { return top + under * (1.0 - top.a); }     // 乗算済みの α
vec4 pal(uint i) { return u_palette[min(int(i) - 1, ${PALETTE_COLORS - 1})]; }
vec4 dim(vec4 c, float k) { return vec4(c.rgb * k, c.a); }

void main() {
  float x = gl_FragCoord.x - 0.5;
  float y = u_height - gl_FragCoord.y;
  o = vec4(0.0);
  uint ri = texelFetch(u_rowOf, ivec2(int(y), 0), 0).r;
  if (ri == 0u) return;
  int row = int(ri) - 1;
  vec4 info = texelFetch(u_rows, ivec2(row, 0), 0);
  float yy = y - info.x, h = info.y;
  int mode = int(info.z + 0.5);
  float lw = max(1.0, floor(1.25 * u_dpr + 0.5));
  vec4 base = h - yy <= 1.0 ? c_grid * 0.6 : vec4(0.0);          // 段の下の区切り
  if (mode == ${MODE.header}) base = over(base, c_head);
  int px = int(floor(x / u_scale));
  if (px >= u_frameWidth || mode == ${MODE.none}) { o = base; return; }
  uvec4 d = texelFetch(u_data, ivec2(px, row), 0);
  float pad = 3.0 * u_dpr, top = pad, bottom = h - pad;
  float local = x - float(px) * u_scale;                          // 画素の中の位置
  vec4 c = vec4(0.0);
  if (mode == ${MODE.logic}) {
    top = pad + 1.0;
    if (yy >= top && yy <= bottom && d.r != 3u) {
      if (d.r == 2u) c = (yy < top + lw || yy > bottom - lw) ? c_line : c_fill;
      else {
        uint sp = px > 0 ? texelFetch(u_data, ivec2(px - 1, row), 0).r : d.r;
        bool edge = sp != d.r && sp < 2u && local < lw;
        bool level = d.r == 1u ? yy < top + lw : yy > bottom - lw;
        if (level || edge) c = c_line;
      }
    }
  } else if (mode == ${MODE.density} || mode == ${MODE.header}) {
    float full = bottom - top;
    if (d.r > 0u && yy <= bottom && yy >= bottom - full * float(d.r) / 255.0)
      c = mode == ${MODE.header} ? c_bar : pal(uint(info.w) + 1u) * 0.75;
  } else if (mode == ${MODE.intervals} || mode == ${MODE.state} || mode == ${MODE.bus}) {
    if (d.r > 0u && yy >= top && yy <= bottom) {
      float inset = min(float(d.b), 4.0) * 2.0 * u_dpr;
      vec4 own = pal(d.r);
      if (mode == ${MODE.bus}) {
        bool rim = yy < top + lw || yy > bottom - lw || ((d.g & 1u) != 0u && local < lw)
                   || ((d.g & 2u) != 0u && local >= u_scale - lw);
        c = rim ? own : own * 0.28;
      } else if (yy < top + inset) {
        c = d.a > 0u ? pal(d.a) * 0.85 : vec4(0.0);               // 入れ子の外側
      } else {
        c = mode == ${MODE.state} ? own * 0.55 : own * 0.9;
        if ((d.g & 1u) != 0u && local < lw) c = dim(own, 0.55);   // 区間の始め
        if ((d.g & 4u) != 0u && (int(x) + int(y)) % 4 == 0) c = dim(own, 0.5);   // 1 画素に複数
      }
    }
  } else if (mode == ${MODE.points}) {
    float r = 4.0 * u_dpr;
    int reach = int(ceil(r / u_scale));
    for (int k = -reach; k <= reach; k++) {
      int q = px + k;
      if (q < 0 || q >= u_frameWidth) continue;
      uvec4 e = texelFetch(u_data, ivec2(q, row), 0);
      if ((e.g & 8u) == 0u) continue;
      float dx = abs(x - (float(q) + 0.5) * u_scale);
      float tri = top + r * 1.6;                                   // 下向きの三角（上）と軸
      bool head = yy >= top && yy <= tri && dx <= r * (1.0 - (yy - top) / (tri - top)) + 0.5;
      bool stem = yy > tri && yy <= bottom && dx <= lw * 0.5;
      if (head || stem) { c = pal(e.r); break; }
    }
  } else if (mode == ${MODE.values}) {
    if (d.r > 0u) {
      float full = bottom - top;
      float yLow = bottom - full * float(d.b) / 255.0, yHigh = bottom - full * float(d.a) / 255.0;
      vec4 own = pal(d.r);
      if (yy >= yHigh - lw * 0.5 && yy <= yLow + lw * 0.5) c = own;
      else if (yy > yLow && yy <= bottom) c = own * 0.12;           // 線の下を薄く
    }
  }
  o = over(c, base);
}`;

const premultiplied = ([r, g, b, a]: Rgba): Rgba => [r * a, g * a, b * a, a];

export class GlRenderer implements TimelineRenderer {
  readonly kind = "webgl2";
  private prog: WebGLProgram;
  private dataTex: WebGLTexture;
  private rowOfTex: WebGLTexture;
  private rowsTex: WebGLTexture;
  private uniforms: Record<string, WebGLUniformLocation | null> = {};
  private maxTexture: number;
  private rowOf = new Uint16Array(0);
  private rowsKey = "";

  /** WebGL2 が使えなければ null（Canvas 2D に落とす） */
  static create(canvas: HTMLCanvasElement | OffscreenCanvas): GlRenderer | null {
    const gl = canvas.getContext("webgl2", { antialias: false, premultipliedAlpha: true, alpha: true,
                                             preserveDrawingBuffer: false }) as WebGL2RenderingContext | null;
    if (!gl) return null;
    try {
      return new GlRenderer(gl);
    } catch {
      return null;
    }
  }

  private constructor(private gl: WebGL2RenderingContext) {
    const prog = gl.createProgram()!;
    for (const [type, src] of [[gl.VERTEX_SHADER, VERTEX], [gl.FRAGMENT_SHADER, FRAGMENT]] as const) {
      const s = gl.createShader(type)!;
      gl.shaderSource(s, src);
      gl.compileShader(s);
      if (!gl.getShaderParameter(s, gl.COMPILE_STATUS)) throw new Error(gl.getShaderInfoLog(s) ?? "シェーダの誤り");
      gl.attachShader(prog, s);
    }
    gl.linkProgram(prog);
    if (!gl.getProgramParameter(prog, gl.LINK_STATUS)) throw new Error(gl.getProgramInfoLog(prog) ?? "リンクの誤り");
    gl.useProgram(prog);
    this.prog = prog;
    const buf = gl.createBuffer();
    gl.bindBuffer(gl.ARRAY_BUFFER, buf);
    gl.bufferData(gl.ARRAY_BUFFER, new Float32Array([-1, -1, 1, -1, -1, 1, 1, 1]), gl.STATIC_DRAW);
    gl.enableVertexAttribArray(0);
    gl.vertexAttribPointer(0, 2, gl.FLOAT, false, 0, 0);
    gl.pixelStorei(gl.UNPACK_ALIGNMENT, 1);
    this.dataTex = this.texture(0);
    this.rowOfTex = this.texture(1);
    this.rowsTex = this.texture(2);
    for (const name of ["u_data", "u_rowOf", "u_rows", "u_height", "u_scale", "u_dpr", "u_frameWidth", "c_line",
                        "c_fill", "c_bar", "c_head", "c_grid", "u_palette"]) {
      this.uniforms[name] = gl.getUniformLocation(prog, name);
    }
    gl.uniform1i(this.uniforms.u_data, 0);
    gl.uniform1i(this.uniforms.u_rowOf, 1);
    gl.uniform1i(this.uniforms.u_rows, 2);
    this.maxTexture = gl.getParameter(gl.MAX_TEXTURE_SIZE);
  }

  private texture(unit: number): WebGLTexture {
    const gl = this.gl;
    const t = gl.createTexture()!;
    gl.activeTexture(gl.TEXTURE0 + unit);
    gl.bindTexture(gl.TEXTURE_2D, t);
    gl.texParameteri(gl.TEXTURE_2D, gl.TEXTURE_MIN_FILTER, gl.NEAREST);
    gl.texParameteri(gl.TEXTURE_2D, gl.TEXTURE_MAG_FILTER, gl.NEAREST);
    gl.texParameteri(gl.TEXTURE_2D, gl.TEXTURE_WRAP_S, gl.CLAMP_TO_EDGE);
    gl.texParameteri(gl.TEXTURE_2D, gl.TEXTURE_WRAP_T, gl.CLAMP_TO_EDGE);
    return t;
  }

  /** この幅のフレームを描けるか（テクスチャの大きさの上限） */
  fits(width: number, height: number): boolean {
    return width <= this.maxTexture && height <= this.maxTexture;
  }

  draw(given: TimelineFrame | null, layout: Layout, colors: Colors): void {
    const gl = this.gl;
    const canvas = gl.canvas;
    if (canvas.width !== layout.width || canvas.height !== layout.height) {
      canvas.width = layout.width;
      canvas.height = layout.height;
    }
    gl.viewport(0, 0, layout.width, layout.height);
    gl.clearColor(0, 0, 0, 0);
    gl.clear(gl.COLOR_BUFFER_BIT);
    const rows = layout.tops.length;
    if (!rows || !this.fits(Math.max(given?.width ?? 1, layout.height), rows)) return;
    // 段の位置（y → 段）と、段の上端・高さ・描き方・色（フレームの段の並びと layout の並びは同じ）
    const key = `${layout.height}:${layout.tops.join(",")}:${layout.heights.join(",")}`;
    if (key !== this.rowsKey) {
      this.rowsKey = key;
      if (this.rowOf.length !== layout.height) this.rowOf = new Uint16Array(layout.height);
      this.rowOf.fill(0);
      layout.tops.forEach((top, r) => this.rowOf.fill(r + 1, top, Math.min(layout.height, top + layout.heights[r])));
      gl.activeTexture(gl.TEXTURE1);
      gl.bindTexture(gl.TEXTURE_2D, this.rowOfTex);
      gl.texImage2D(gl.TEXTURE_2D, 0, gl.R16UI, layout.height, 1, 0, gl.RED_INTEGER, gl.UNSIGNED_SHORT, this.rowOf);
    }
    const info = new Float32Array(rows * 4);
    const usable = given ? alignFrame(given, layout.ids) : null;
    for (let r = 0; r < rows; r++) {
      info.set([layout.tops[r], layout.heights[r], usable ? usable.modes[r] : MODE.none, layout.colors[r]], r * 4);
    }
    gl.activeTexture(gl.TEXTURE2);
    gl.bindTexture(gl.TEXTURE_2D, this.rowsTex);
    gl.texImage2D(gl.TEXTURE_2D, 0, gl.RGBA32F, rows, 1, 0, gl.RGBA, gl.FLOAT, info);
    const width = usable?.width ?? 1;
    gl.activeTexture(gl.TEXTURE0);
    gl.bindTexture(gl.TEXTURE_2D, this.dataTex);
    gl.texImage2D(gl.TEXTURE_2D, 0, gl.RGBA8UI, width, rows, 0, gl.RGBA_INTEGER, gl.UNSIGNED_BYTE,
                  usable ? usable.data : new Uint8Array(width * rows * 4));
    const u = this.uniforms;
    gl.useProgram(this.prog);
    gl.uniform1f(u.u_height, layout.height);
    gl.uniform1f(u.u_scale, layout.width / width);
    gl.uniform1f(u.u_dpr, layout.dpr);
    gl.uniform1i(u.u_frameWidth, usable ? width : 0);
    gl.uniform4fv(u.c_line, premultiplied(colors.line));
    gl.uniform4fv(u.c_fill, premultiplied(colors.fill));
    gl.uniform4fv(u.c_bar, premultiplied(colors.bar));
    gl.uniform4fv(u.c_head, premultiplied(colors.head));
    gl.uniform4fv(u.c_grid, premultiplied(colors.grid));
    gl.uniform4fv(u.u_palette, colors.palette.flatMap(premultiplied));
    gl.drawArrays(gl.TRIANGLE_STRIP, 0, 4);
  }

  dispose(): void {
    this.gl.getExtension("WEBGL_lose_context")?.loseContext();
  }
}
