// デーモンの API（JSON-RPC 2.0 over WebSocket）の利用者。Svelte に依らない。
// 名乗る（session.hello）→ 購読（session.subscribe）→ open。切れたら間をあけてつなぎ直す。
// 仕様で binary のメソッド（timeline.tiles）の応答はバイナリのフレームで届く: 結果の data に区画の型付きの配列。
import { API_VERSION, type Methods } from "../../../api/generated/fprobe_api";
import { t } from "./i18n";
import { decodeFrame } from "./timeline/frame";

export type { Methods };
export type LinkState = "connecting" | "open" | "closed";
const E_VERSION = -32010;
const E_INVALID_PARAMS = -32602;
/** 読む物がまだ無い（取っていない・開いていない）。機器の誤りと分けて、パネルは「無い」と出す */
export const E_NO_DATA = -32023;

export type Role = "human" | "ai" | "script";

export class RpcError extends Error {
  constructor(public code: number, message: string) {
    super(message);
  }
}

/** API の名前空間（MCP の道具の名前の頭: target_halt・profile_start 等）。誤りの文の AI 向けの案内を見分ける */
export const TOOL_NAMESPACES = ["session", "ui", "probe", "target", "jtag", "debug", "la", "timeline", "events", "watch",
  "rtos", "profile", "cycles", "timing", "testpoint", "xtrig", "swo", "rtt", "terminal", "user", "uart", "recording",
  "snapshot", "rules", "inject", "hooks", "heap", "stream", "log", "trace", "scvd", "gpio", "spi", "fpga_obs", "system", "linux", "sampler"];
const TOOL = new RegExp(`\\b(?:${TOOL_NAMESPACES.join("|")})_[a-z0-9_]+\\b`);
const PARAM = /\b[a-z][a-z0-9_]*=[^\s、。）]+/;

/** 誤りを人に見せる文: デーモンが付ける Python の例外の名前（"RuntimeError: " 等）と JSON-RPC の番号は外す
 *  （利用者には意味が無く、本文が隠れる）。AI 向けの道具の名前・引数の案内（「（target_halt）」「live=true で…」）も
 *  外す（画面ではその操作のボタンの名前が違い、利用者の言葉でない） */
export function errorText(e: unknown): string {
  const message = (e instanceof Error ? e.message : String(e))
    .replace(/^(?:[A-Z][A-Za-z0-9_.]*(?:Error|Exception|Expired|Connected|Disconnected|Exit)|NoData): /, "");
  const plain = message.replace(/（[^（）]*）/g, (group) => (TOOL.test(group) || PARAM.test(group) ? "" : group));
  const sentences = plain.split("。").filter((s) => !TOOL.test(s) && !PARAM.test(s));
  return (sentences.join("。").trim() || plain.trim()).replace(/。$/, "");
}

/** まだ無い（E_NO_DATA）: 読めなかった誤りではない */
export function isNoData(e: unknown): boolean {
  return e instanceof RpcError && e.code === E_NO_DATA;
}

export interface ConnectionOptions {
  name: string;
  role: Role;
  token?: string;
  topics: string[];
  optionalTopics?: string[];     // 古いデーモンが知らなければ外す通知（購読を断られたら topics だけで購読し直す）
  WebSocketImpl?: typeof WebSocket;
  retryMaxMs?: number;
}

type Pending = { resolve: (v: any) => void; reject: (e: Error) => void };

export class DaemonConnection {
  private ws: WebSocket | null = null;
  private ready = false;
  private stopped = false;
  private nextId = 1;
  private retryMs = 1000;
  private timer: ReturnType<typeof setTimeout> | null = null;
  private refusal: RpcError | null = null;        // 名乗りを断られた理由（closed と一緒に渡す）
  private pending = new Map<number, Pending>();
  private topicListeners = new Map<string, Set<(data: any) => void>>();
  private linkListeners = new Set<(state: LinkState, info?: any) => void>();

  constructor(private url: string, private opts: ConnectionOptions) {}

  start(): void {
    this.stopped = false;
    this.open();
  }

  stop(): void {
    this.stopped = true;
    if (this.timer) clearTimeout(this.timer);
    this.ws?.close();
  }

  /** quiet: 画面の自動の読み（状態を変えない読みだけ）を操作の記録に残さない */
  call<M extends keyof Methods>(method: M, params?: Methods[M]["params"], opts?: { quiet?: boolean }): Promise<any> {
    if (!this.ready) return Promise.reject(new Error(t("tip.offline")));
    return this.request(method, params ?? {}, opts?.quiet ? { quiet: true } : {});
  }

  on(topic: string, fn: (data: any) => void): () => void {
    const set = this.topicListeners.get(topic) ?? new Set();
    set.add(fn);
    this.topicListeners.set(topic, set);
    return () => set.delete(fn);
  }

  /** open のときは hello の結果、名乗りを断られた後の closed のときはその RpcError を info に渡す */
  onLink(fn: (state: LinkState, info?: any) => void): () => void {
    this.linkListeners.add(fn);
    return () => this.linkListeners.delete(fn);
  }

  private open(): void {
    const Impl = this.opts.WebSocketImpl ?? WebSocket;
    const ws = new Impl(this.url);
    this.ws = ws;
    this.emitLink("connecting");
    ws.onopen = () => void this.greet();
    ws.binaryType = "arraybuffer";
    ws.onmessage = (ev) => this.dispatch(ev.data instanceof ArrayBuffer ? decodeFrame(ev.data) : JSON.parse(String(ev.data)));
    ws.onclose = () => this.closed(ws);
  }

  private async greet(): Promise<void> {
    const { name, role, token, topics, optionalTopics = [] } = this.opts;
    try {
      const hello = await this.request("session.hello", { name, role, api_version: API_VERSION, ...(token ? { token } : {}) });
      try {
        await this.request("session.subscribe", { topics: [...topics, ...optionalTopics] });
      } catch (e) {
        if (!(e instanceof RpcError && e.code === E_INVALID_PARAMS && optionalTopics.length)) throw e;
        await this.request("session.subscribe", { topics });
      }
      this.ready = true;
      this.retryMs = 1000;
      this.emitLink("open", hello);
    } catch (e) {
      // 名乗りを断られた（トークン・版）。理由は closed と一緒に渡す。版の違いはつなぎ直しても直らないので止める
      if (e instanceof RpcError) {
        this.refusal = e;
        if (e.code === E_VERSION) this.stopped = true;
      }
      this.ws?.close();
    }
  }

  private request(method: string, params: object, extra: object = {}): Promise<any> {
    const id = this.nextId++;
    return new Promise((resolve, reject) => {
      this.pending.set(id, { resolve, reject });
      this.ws!.send(JSON.stringify({ jsonrpc: "2.0", id, method, params, ...extra }));
    });
  }

  private dispatch(message: any): void {
    if ("id" in message && message.id !== null && this.pending.has(message.id)) {
      const p = this.pending.get(message.id)!;
      this.pending.delete(message.id);
      if (message.error) p.reject(new RpcError(message.error.code, message.error.message));
      else p.resolve(message.result);
    } else if (typeof message.method === "string") {
      this.topicListeners.get(message.method)?.forEach((fn) => fn(message.params));
    }
  }

  private closed(ws: WebSocket): void {
    if (ws !== this.ws) return;
    this.ready = false;
    for (const p of this.pending.values()) p.reject(new Error(t("error.connectionLost")));
    this.pending.clear();
    this.emitLink("closed", this.refusal ?? undefined);
    this.refusal = null;
    if (this.stopped) return;
    this.timer = setTimeout(() => this.open(), this.retryMs);
    this.retryMs = Math.min(this.retryMs * 2, this.opts.retryMaxMs ?? 10000);
  }

  private emitLink(state: LinkState, info?: any): void {
    this.linkListeners.forEach((fn) => fn(state, info));
  }
}
