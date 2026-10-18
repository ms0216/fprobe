// 画面の部品が共有するもの（接続・状態）。App が作り、Svelte の context で渡す。dockview のパネルは別の mount の
// 根になるので、同じ context の Map を mount の引数で渡す（lib/dock.ts）。
import { getContext } from "svelte";
import type { DaemonConnection } from "./api";
import type { CommandContext } from "./commands";
import type { ScreenSync } from "./screen";
import type { Reveal } from "./timeline/state";

export interface Shared extends CommandContext {
  conn: Pick<DaemonConnection, "call" | "on" | "onLink">;
  dock: { show(panel: string): void; reset(): void };
  screen: ScreenSync;
  /** ビューの行から時間軸のその時刻へ飛ぶ（Timeline のパネルを前に出し、選択カーソルを置く） */
  reveal(r: Reveal): void;
  /** 番地の場所を開く（Source のその行・Disassembly のその命令。時間軸のダブルクリック・ビューの関数名から） */
  showAddress(address: number): void;
  /** Memory のパネルをその番地で開く（時間軸のダブルクリック・ビューの番地から） */
  showMemory(address: number): void;
}

export const SHARED = Symbol("fprobe");

export function shared(): Shared {
  return getContext<Shared>(SHARED);
}
