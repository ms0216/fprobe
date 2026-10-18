// 段とまとまりの表示の名前（文言の表 i18n.ts）。データの名前（タスク・チャンネル・変数）があればそれ、無ければ
// 規則の名前を SystemView / Tracealyzer / Ozone の語で。
import { EN, type Key, type Translate } from "../i18n";
import { channelName } from "../waveform";
import type { RowDef } from "./rows";

const known = (key: string): key is Key => key in EN;

export function groupTitle(group: string, tr: Translate): string {
  const key = `timeline.group.${group}`;
  return known(key) ? tr(key) : group;
}

export function rowTitle(def: RowDef, tr: Translate): string {
  if (def.type === "group") return groupTitle(def.group, tr);
  if (def.type === "logic") return def.title ?? channelName(def.channel ?? 0);
  if (def.title) return def.title;
  const colon = def.id.indexOf(":");
  const key = colon >= 0 ? def.id.slice(colon + 1) : null;
  if (def.group === "host" && key !== null) {                        // 操作の段は利用者の種類（AI・人）
    const role = `timeline.role.${key}`;
    return known(role) ? tr(role) : key;
  }
  const rule = `timeline.row.${def.group}.${def.rule}`;
  const base = known(rule) ? tr(rule) : def.rule ?? tr("timeline.row.other");
  return key === null ? base : `${base} ${key}`;
}
