// つないでいるターゲット: ステータス バーの線・速さ・名前（target.state から）と、コア クロックの既定
// （Code Profile の ITM の間隔・SWO の分周に使う）。Svelte に依らない。
import type { DaemonConnection } from "./api";

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

/** ターゲット名（pyOCD の名前）→ ファームがふつう使うコア クロック（MHz）。リセット直後の値ではない */
const TYPICAL: [RegExp, number][] = [
  [/^nrf51/i, 16],
  [/^nrf52|^nrf53|^nrf91/i, 64],
  [/^rp2040/i, 125],
  [/^rp2350/i, 150],
  [/^stm32f0/i, 48],
  [/^stm32f1/i, 72],
  [/^stm32f3/i, 72],
  [/^stm32f401/i, 84],
  [/^stm32f41[01]/i, 100],
  [/^stm32f4[01][57]/i, 168],
  [/^stm32f4[2-7]/i, 180],
  [/^stm32f7/i, 216],
  [/^stm32g0/i, 64],
  [/^stm32g4/i, 170],
  [/^stm32h7/i, 480],
  [/^stm32l0/i, 32],
  [/^stm32l4/i, 80],
  [/^stm32wb/i, 64],
];

export function typicalCoreClockMhz(target: string | undefined | null): number | null {
  if (!target) return null;
  return TYPICAL.find(([re]) => re.test(target))?.[1] ?? null;
}

/** コア クロックの既定: ELF に CMSIS の SystemCoreClock があればターゲットのその値（走行中でも読める）、
 *  無い・まだ初期化前（0 等）ならターゲット名から。どちらも無ければ null */
export async function coreClock(conn: Conn, target: string | undefined | null)
  : Promise<{ mhz: number; source: "SystemCoreClock" | "target" } | null> {
  try {
    const r = await conn.call("debug.variable.read", { name: "SystemCoreClock" }, { quiet: true });
    const hz = Number(r?.value);
    if (hz >= 1e6 && hz <= 2e9) return { mhz: Math.round(hz / 1e4) / 100, source: "SystemCoreClock" };
  } catch {
    /* ELF に無い・読めない: ターゲット名から */
  }
  const mhz = typicalCoreClockMhz(target);
  return mhz === null ? null : { mhz, source: "target" };
}
