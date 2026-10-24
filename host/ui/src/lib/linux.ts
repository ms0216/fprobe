// Linux のアプリのデバッグ（R26 段階 1）: linux.connect・linux.probe.add の引数と、linux.events.read の読み足し。
// 設定はすべて欄に出し、書いた欄だけを送る（既定はデーモンの API の既定）。Svelte に依らない
import type { DaemonConnection } from "./api";

export interface ConnectForm {
  target: string;
  host: string;
  user: string;
  port: string;
  key: string;
  sudo: boolean;
  sudoPasswordEnv: string;                             // sudo にパスワードが要るとき、それを入れた PC の環境変数の名前
}

export interface ProbeForm {
  name: string;
  binary: string;
  method: "" | "uprobe" | "ptrace";                    // "" = まだ選んでいない（既定を置かない）
  symbol: string;
  address: string;
  isa: "" | "arm" | "thumb";
  fetchargs: string;
  regs: string;
  ret: boolean;
  pid: string;
}

export interface LinuxEvent {
  seq: number;
  method?: string;
  probe: string;
  target_ns: number | null;
  clock?: string | null;
  pid?: number;
  tid?: number;
  args?: Record<string, string>;
  regs?: Record<string, number>;
  host_s?: number;
  sync?: { method: string; half_rtt_ns: number; nearest_pair_ns: number; outside: boolean; clockmap_uncertainty_ticks?: number };
}

type Built = { params: Record<string, unknown> } | { error: string };

const words = (s: string) => s.trim().split(/\s+/).filter(Boolean);

/** linux.connect の引数（port は 1..65535 の整数） */
export function connectParams(f: ConnectForm): Built {
  for (const k of ["target", "host", "user"] as const) if (!f[k].trim()) return { error: k };
  const port = f.port.trim() === "" ? NaN : Number(f.port);
  if (!Number.isInteger(port) || port < 1 || port > 65535) return { error: "port" };
  const out: Record<string, unknown> = { target: f.target.trim(), host: f.host.trim(), user: f.user.trim(), port, sudo: f.sudo };
  if (f.key.trim()) out.key = f.key.trim();
  if (f.sudoPasswordEnv.trim()) out.sudo_password_env = f.sudoPasswordEnv.trim();
  return { params: out };
}

/** linux.sync の引数（往復の回数は使う人が入れる 1 以上の整数。空を 0 として送らない） */
export function syncParams(target: string, count: string): Built {
  const n = count.trim() === "" ? NaN : Number(count);
  return Number.isInteger(n) && n >= 1 ? { params: { target, count: n } } : { error: "count" };
}

/** linux.trace.start の引数（時刻合わせの間隔は使う人が入れる正の秒） */
export function traceStartParams(target: string, interval: string): Built {
  const s = interval.trim() === "" ? NaN : Number(interval);
  return Number.isFinite(s) && s > 0 ? { params: { target, sync_interval_s: s } } : { error: "interval" };
}

/** linux.probe.add の引数（symbol と address はどちらか 1 つ。address は 10 進か 0x の 16 進） */
export function probeParams(target: string, f: ProbeForm): Built {
  if (!f.method) return { error: "method" };
  const out: Record<string, unknown> = { target, name: f.name.trim(), binary: f.binary.trim(), method: f.method };
  const symbol = f.symbol.trim();
  const address = f.address.trim();
  if (symbol && address) return { error: "address" };
  if (symbol) out.symbol = symbol;
  if (address) {
    if (!/^(0x[0-9a-fA-F]+|\d+)$/.test(address)) return { error: "address" };
    out.address = Number(address);
  }
  if (f.isa) out.isa = f.isa;
  if (words(f.fetchargs).length) out.fetchargs = words(f.fetchargs);
  if (words(f.regs).length) out.regs = words(f.regs);
  if (f.ret) out.ret = true;
  if (f.pid.trim()) {
    const pid = Number(f.pid);
    if (!Number.isInteger(pid) || pid < 1) return { error: "pid" };
    out.pid = pid;
  }
  return { params: out };
}

export interface RingForm {
  phys: string;
  size: string;
  cached: boolean;
  linger: boolean;
  address: string;                                     // 読み手から見たリングの頭（halt は仮想番地、memap は物理番地）
  access: "" | "halt" | "memap";                       // "" = まだ選んでいない（既定を置かない）
  maxRecords: string;
  interval: string;
}

export interface MemapForm {
  xsdb: string;
  cableSerial: string;
  chainIdcode: string;
  timeout: string;
}

export interface GdbForm {
  commands: string;                                    // 1 行 1 命令
  timeout: string;
  pid: string;
  core: string;
  binary: string;
  port: string;                                        // gdb.serve の PC の口
}

export interface LogForm {
  lines: string;
  unit: string;
  cursor: string;
}

/** 10 進か 0x の 16 進の 0 以上の整数（それ以外は null） */
function uint(s: string): number | null {
  const t = s.trim();
  return /^(0x[0-9a-fA-F]+|\d+)$/.test(t) ? Number(t) : null;
}

/** lo 以上 hi 以下の整数（10 進か 0x の 16 進。それ以外は null） */
function within(s: string, lo: number, hi: number): number | null {
  const v = uint(s);
  return v !== null && v >= lo && v <= hi ? v : null;
}

/** 正の有限の数（それ以外は null） */
function positive(s: string): number | null {
  const t = s.trim();
  const v = Number(t);
  return t !== "" && Number.isFinite(v) && v > 0 ? v : null;
}

/** 欄の値を順に組む。どれかが null ならその欄の名前を返す */
function build(base: Record<string, unknown>, fields: [string, unknown][]): Built {
  for (const [k, v] of fields) {
    if (v === null) return { error: k };
    base[k] = v;
  }
  return { params: base };
}

/** linux.agent.start（watchdog_s は書いたときだけ） */
export function agentStartParams(target: string, watchdog: string): Built {
  return watchdog.trim() ? build({ target }, [["watchdog_s", positive(watchdog)]]) : { params: { target } };
}

/** linux.ring.open（phys・size・cached・linger はすべて明示） */
export function ringOpenParams(target: string, f: RingForm): Built {
  return build({ target }, [["phys", uint(f.phys)], ["size", within(f.size, 112, Number.MAX_SAFE_INTEGER)], ["cached", f.cached], ["linger", f.linger]]);
}

/** linux.ring.read */
export function ringReadParams(target: string, f: RingForm): Built {
  return build({ target }, [["address", uint(f.address)], ["access", f.access || null],
    ["max_records", within(f.maxRecords, 1, 100000)]]);
}

/** linux.ring.watch（read の引数 + interval_s） */
export function ringWatchParams(target: string, f: RingForm): Built {
  const r = ringReadParams(target, f);
  return "error" in r ? r : build(r.params, [["interval_s", positive(f.interval)]]);
}

/** linux.memap.connect（xsdb の場所・ケーブルのシリアル・IDCODE・待つ秒数はすべて明示） */
export function memapParams(target: string, f: MemapForm): Built {
  return build({ target }, [["xsdb", f.xsdb.trim() || null], ["cable_serial", f.cableSerial.trim() || null],
    ["chain_idcode", within(f.chainIdcode, 0, 0xFFFFFFFF)], ["timeout_s", positive(f.timeout)]]);
}

/** linux.gdb.batch（pid か core + binary のどちらか） */
export function gdbBatchParams(target: string, f: GdbForm): Built {
  const commands = f.commands.split(/\r?\n/).map((c) => c.trim()).filter(Boolean);
  const r = build({ target }, [["commands", commands.length ? commands : null], ["timeout_s", positive(f.timeout)]]);
  if ("error" in r) return r;
  if (Boolean(f.pid.trim()) === Boolean(f.core.trim())) return { error: "pid" };
  if (f.pid.trim()) return build(r.params, [["pid", positive(f.pid) !== null ? uint(f.pid) : null]]);
  return build(r.params, [["core", f.core.trim()], ["binary", f.binary.trim() || null]]);
}

/** linux.gdb.serve */
export function gdbServeParams(target: string, f: GdbForm): Built {
  const port = uint(f.port);
  return build({ target }, [["pid", positive(f.pid) !== null ? uint(f.pid) : null], ["port", port && port <= 65535 ? port : null]]);
}

/** linux.ftrace.enable / disable（"subsystem/event" を空白で区切る） */
export function ftraceParams(target: string, events: string): Built {
  const list = words(events);
  const ok = list.length > 0 && list.every((e) => /^[a-z0-9_-]+\/[a-z0-9_]+$/.test(e));   // API の仕様と同じ形
  return build({ target }, [["events", ok ? list : null]]);
}

/** linux.log.read（unit・since_cursor は書いたときだけ） */
export function logParams(target: string, f: LogForm): Built {
  const r = build({ target }, [["lines", within(f.lines, 1, 100000)]]);
  if ("error" in r) return r;
  if (f.unit.trim()) r.params.unit = f.unit.trim();
  if (f.cursor.trim()) r.params.since_cursor = f.cursor.trim();
  return r;
}

export interface CoreForm {
  pid: string;
  pattern: string;                                     // core_pattern（例 /tmp/core.%e.%p）
  soft: string;                                        // バイトか unlimited
  hard: string;
  path: string;                                        // 写すターゲットのファイル
  localPath: string;                                   // PC に置く場所
}

export interface PerfForm {
  events: string;                                      // 空白で区切る
  duration: string;
  frequency: string;
  maxSamples: string;
  pid: string;                                         // 空なら全体
}

const PERF_EVENT = /^[A-Za-z0-9_:\-./]+$/;             // API の仕様と同じ形

function withPid(base: Record<string, unknown>, pid: string): Built {
  if (!pid.trim()) return { params: base };
  return build(base, [["pid", within(pid, 1, 2 ** 31 - 1)]]);
}

/** linux.core.status（pid は書いたときだけ） */
export function coreStatusParams(target: string, f: CoreForm): Built {
  return withPid({ target }, f.pid);
}

/** linux.core.pattern_set */
export function corePatternParams(target: string, f: CoreForm): Built {
  return build({ target }, [["pattern", f.pattern.trim() || null]]);
}

const limit = (s: string): number | "unlimited" | null => (s.trim() === "unlimited" ? "unlimited" : uint(s));

/** linux.core.limit_set（pid・soft・hard はすべて明示） */
export function coreLimitParams(target: string, f: CoreForm): Built {
  return build({ target }, [["pid", within(f.pid, 1, 2 ** 31 - 1)], ["soft", limit(f.soft)], ["hard", limit(f.hard)]]);
}

/** linux.core.fetch */
export function coreFetchParams(target: string, f: CoreForm): Built {
  return build({ target }, [["path", f.path.trim() || null], ["local_path", f.localPath.trim() || null]]);
}

/** linux.perf.stat（pid は書いたときだけ。無ければ全体） */
export function perfStatParams(target: string, f: PerfForm): Built {
  const events = words(f.events);
  const duration = positive(f.duration);
  const r = build({ target }, [["events", events.length && events.every((e) => PERF_EVENT.test(e)) ? events : null],
    ["duration_s", duration !== null && duration <= 3600 ? duration : null]]);
  return "error" in r ? r : withPid(r.params, f.pid);
}

/** linux.perf.record（duration_s・frequency_hz・max_samples は明示。pid は書いたときだけ） */
export function perfRecordParams(target: string, f: PerfForm): Built {
  const duration = positive(f.duration);
  const r = build({ target }, [["duration_s", duration !== null && duration <= 600 ? duration : null],
    ["frequency_hz", within(f.frequency, 1, 100000)], ["max_samples", within(f.maxSamples, 1, 100000)]]);
  return "error" in r ? r : withPid(r.params, f.pid);
}

/** 画面に持つ件数の上限（古いものから捨てる。捨てた数は discarded。デーモンには残る） */
export const LINUX_KEEP = 5000;

export class LinuxEvents {
  records: LinuxEvent[] = [];
  dropped = 0;
  discarded = 0;                                         // 画面に持つ上限を超えて捨てた記録の数
  private since = 0;

  constructor(private conn: Pick<DaemonConnection, "call">) {}

  reset(): void {
    this.records = [];
    this.since = 0;
    this.dropped = 0;
    this.discarded = 0;
  }

  async more(target: string): Promise<void> {
    const r = (await this.conn.call("linux.events.read" as never, { target, since: this.since } as never, { quiet: true })) as any;
    const all = [...this.records, ...(r.records ?? [])];
    this.discarded += Math.max(0, all.length - LINUX_KEEP);
    this.records = all.slice(-LINUX_KEEP);
    this.since = Math.max(this.since, r.next ?? this.since);
    this.dropped = r.dropped ?? 0;
  }
}
