// つないでいるターゲット: ステータス バーの線・速さ・名前（target.state から）と、コア クロックの既定
// （Code Profile の ITM の間隔・SWO の分周に使う）。Svelte に依らない。
import type { DaemonConnection } from "./api";
import type { Key } from "./i18n";

type Conn = Pick<DaemonConnection, "call">;

export interface TargetLink {
  wire: string;              // "SWD" / "JTAG"（古いデーモンは空）
  speed: number | null;      // 線の速さ（Hz）
  cpu: string;               // コアとターゲット名
}

/** ターゲットの状態（target.state の通知・target.status）→ 線・速さ・名前。つながっていなければ null */
export function linkOf(target: Record<string, any> | null | undefined): TargetLink | null {
  if (!target?.state || target.state === "DISCONNECTED") return null;
  return {
    wire: typeof target.wire === "string" ? target.wire.toUpperCase() : "",
    speed: typeof target.frequency_hz === "number" ? target.frequency_hz : null,
    cpu: [target.core, target.target].filter(Boolean).join(" · "),
  };
}

/** コア クロックの既定: ELF に CMSIS の SystemCoreClock があればターゲットのその値（走行中でも読める）を、読んだ値の
 *  まま（丸めない・妥当な範囲という境界で捨てない。初期化前の 0 もそのまま）。使うかは利用者が欄で確かめる。
 *  無い・読めない・数でなければ null（品種名から典型の値を推測しない。CLAUDE.md。使う側が入れる） */
export async function coreClock(conn: Conn, _target?: string | undefined | null)
  : Promise<{ mhz: number; source: "SystemCoreClock" } | null> {
  try {
    const r = await conn.call("debug.variable.read", { name: "SystemCoreClock" }, { quiet: true });
    const hz = typeof r?.value === "number" ? r.value : Number.NaN;
    if (Number.isFinite(hz)) return { mhz: hz / 1e6, source: "SystemCoreClock" };
  } catch {
    /* ELF に無い・読めない */
  }
  return null;
}

const HALT_REASONS = ["request", "breakpoint", "bkpt_instruction", "watchpoint", "vector_catch", "external"] as const;

/** 止まった理由（target.status の halt_reason。fprobe.fault.halt_reason）の文言のキー。停止中でない・知らない理由は null */
export function haltReasonKey(target: Record<string, any> | null | undefined): Key | null {
  const r = target?.state === "HALTED" ? target.halt_reason : undefined;
  return (HALT_REASONS as readonly string[]).includes(r) ? (`halt.${r}` as Key) : null;
}
