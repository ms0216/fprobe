// Logic Analyzer の画面で、ch の番号に付ける名前（時間軸の行の名前があればそれ、無ければ申告の名前）。見せ方だけ。
import type { LaChannel } from "./waveform";
import { channelName } from "./waveform";
import type { Translate } from "./i18n";
import type { GroupDef } from "./timeline/rows";
import { rowTitle } from "./timeline/titles";

export function laChannelLabel(groups: GroupDef[], ch: number, tr: Translate, layout: LaChannel[] | null): string {
  const def = groups.find((g) => g.id === "logic")?.rows.find((r) => r.id === `wave:${ch}`);
  return def ? rowTitle(def, tr, layout) : channelName(ch, layout);
}
