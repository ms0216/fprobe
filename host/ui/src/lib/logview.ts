// ログ（段階 B §3 の 8。defmt / trice / 文字 等の解読したログ）: log.start の引数と、log.read の読み足し（next から）。
// 記録は API の事実のまま並べる（重要度・時刻の文字・場所・誤り）。Svelte に依らない
import type { DaemonConnection } from "./api";

export interface StartForm {
  format: "text" | "defmt" | "trice" | "compact" | "memfault";
  source: "rtt" | "swo" | "uart";
  channel: string;
  elf: string;
  til: string;
}

export interface LogRecord {
  seq: number;
  text: string;
  format?: string;
  kind?: string;
  level?: string;
  timestamp_text?: string;
  location?: { file?: string; line?: number; module?: string; function?: string };
  error?: string;
  t?: number;
}

/** log.start の引数（書いた欄だけ） */
export function startParams(form: StartForm): Record<string, unknown> {
  const out: Record<string, unknown> = { format: form.format, source: form.source };
  const channel = form.channel.trim();
  if (channel !== "") out.channel = /^\d+$/.test(channel) ? Number(channel) : channel;   // 名前（defmt 等）は文字のまま
  if (form.elf.trim()) out.elf = form.elf.trim();
  if (form.til.trim()) out.til = form.til.trim();
  return out;
}

const KEEP = 5000;                                     // 画面に持つ行の上限（古いものから捨てる。デーモンには残る）

const LIMIT = 200;                                     // 1 回の log.read の件数（デーモンの既定と同じ）

/** 記録の場所（file・line・module・function のうち分かるもの）を 1 行に */
export function locationText(loc: { file?: string; line?: number; module?: string; function?: string } | undefined): string {
  if (!loc) return "";
  const where = loc.file ? (loc.line != null ? `${loc.file}:${loc.line}` : loc.file) : (loc.module ?? "");
  return [where, loc.function].filter(Boolean).join(" ");
}

export class LogView {
  records: LogRecord[] = [];
  dropped = 0;
  running = false;
  private since = 0;
  private generation = 0;                              // reset で上げる（前の絞り込みの応答を捨てる）
  private queue: Promise<void> = Promise.resolve();    // 読み取りを 1 つずつ（重ねて同じ記録を足さない）

  constructor(private conn: Pick<DaemonConnection, "call">) {}

  reset(): void {
    this.generation += 1;
    this.records = [];
    this.since = 0;
    this.dropped = 0;
  }

  /** since から読み足す（level 以上・contains を含むものだけ）。返った件数が上限に達している間は続けて読む */
  more(filter: { level: string; contains: string }): Promise<void> {
    const generation = this.generation;
    this.queue = this.queue.then(() => this.read(filter, generation));
    return this.queue;
  }

  private async read(filter: { level: string; contains: string }, generation: number): Promise<void> {
    for (;;) {
      if (generation !== this.generation) return;
      const params: Record<string, unknown> = { since: this.since };
      if (filter.level) params.level = filter.level;
      if (filter.contains) params.contains = filter.contains;
      const r = (await this.conn.call("log.read" as never, params as never, { quiet: true })) as any;
      if (generation !== this.generation) return;
      const last = this.records.at(-1)?.seq ?? 0;
      const fresh = (r.records ?? []).filter((x: LogRecord) => x.seq > last);
      this.records = [...this.records, ...fresh].slice(-KEEP);
      this.since = Math.max(this.since, r.next ?? this.since);
      this.dropped = r.dropped ?? 0;
      this.running = !!r.running;
      if ((r.records ?? []).length < LIMIT) return;
    }
  }
}
