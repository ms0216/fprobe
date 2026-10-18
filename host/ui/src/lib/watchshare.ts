// 変数の観測（watch.*）を画面のビューで分け合う。Svelte に依らない。
// デーモンの watch は 1 組（watch.set が式の組と周期を丸ごと置き換える）なので、Data Sampling と Dashboard
// （と AI・ほかの窓）が同じ組を使う。始めるときは「いま取っている式 ∪ 自分の式」を、やめるときは「自分だけが使う式」を
// 除いた残りを watch.set し直す（残りが無ければ watch.stop）。ほかの利用者が組を置き換えて自分の式が外れたら
// missing で画面に出す（取り合い）。
type Call = (method: any, params?: any, opts?: { quiet?: boolean }) => Promise<any>;
type Act = (method: any, params?: any) => Promise<any>;

/** この画面（同じ窓）のビューごとの使っている式 */
const owners = new Map<string, Set<string>>();

export function claim(owner: string, exprs: string[]): void {
  owners.set(owner, new Set(exprs));
}

export function release(owner: string): void {
  owners.delete(owner);
}

/** ほかのビューが使っている式 */
function others(owner: string): Set<string> {
  const out = new Set<string>();
  for (const [o, set] of owners) if (o !== owner) set.forEach((e) => out.add(e));
  return out;
}

/** 始めるときの式の組（いま取っている順のまま、足りない自分の式を後ろに） */
export function planStart(running: string[], mine: string[]): string[] {
  return [...running, ...mine.filter((e) => !running.includes(e))];
}

/** やめるときの残り（自分の式のうち、ほかのビューも使う式は残す。自分の知らない式 = AI 等のものも残す） */
export function planStop(running: string[], mine: string[], keep: Set<string>): string[] {
  return running.filter((e) => !mine.includes(e) || keep.has(e));
}

/** 自分の式のうち、いま取っていないもの（ほかの利用者が組を置き換えた）。watch.set が誤りで外した式（rejected）は除く */
export function missing(running: string[], mine: string[], rejected: string[] = []): string[] {
  return mine.filter((e) => !running.includes(e) && !rejected.includes(e));
}

export interface WatchState {
  names: string[];                   // いま取っている式（watch.status の watches）
  rate: number;
  status: any;
}

export async function readWatch(call: Call): Promise<WatchState> {
  const status = await call("watch.status", {}, { quiet: true });
  const names = Array.isArray(status?.watches) ? status.watches.map((w: any) => String(w.name)) : [];
  return { names, rate: Number(status?.rate_hz) || 0, status };
}

/** 自分の式を足して取る（いまの組を消さない）。rate は組全体の周期（ほかの式の周期も変わる） */
export async function startWatch(call: Call, act: Act, owner: string, mine: string[], rate: number): Promise<boolean> {
  claim(owner, mine);
  const now = await readWatch(call);
  const next = planStart(now.names, mine);
  if (next.length === now.names.length && now.rate === rate) return true;      // 足すものも周期の変更も無い
  return (await act("watch.set", { expressions: next, rate_hz: rate })) !== undefined;
}

/** 自分の式をやめる（ほかのビュー・ほかの利用者の式は取り続ける） */
export async function stopWatch(call: Call, act: Act, owner: string, mine: string[]): Promise<boolean> {
  const keep = others(owner);
  release(owner);
  const now = await readWatch(call);
  const rest = planStop(now.names, mine, keep);
  if (rest.length === now.names.length) return true;
  if (!rest.length) return (await act("watch.stop", {})) !== undefined;
  return (await act("watch.set", { expressions: rest, rate_hz: now.rate || undefined })) !== undefined;
}
