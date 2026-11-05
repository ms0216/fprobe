// 時間軸の状態の型（TimelineState・state.ts が持つ形）。
import type { Origins } from "../time";
import type { CaptureInfo, View } from "../waveform";
import type { TimelineFrame } from "./compose";
import type { Anomaly } from "./decimator";
import type { PickedItem } from "./query";
import type { GroupDef, RowConfig, VisibleRow } from "./rows";
import type { TileHeader } from "./tile";

export type Source = "capture" | "recording";

/** ビューが時間軸に置く印（検索の結果・解読の注釈・長い順の上位）。目盛りの帯と縮図に描く。時刻は源の単位 */
export interface Mark {
  t: number;
  tEnd?: number;
  label?: string;
  tone?: 0 | 1 | 2 | 3;                // 0 = 普通 / 1 = bad / 2 = good / 3 = warn（段の tone と同じ）
}

/** 印の組（ビューごと。owner で置き換える）。source が今の源と違う組は描かない */
export interface MarkSet {
  owner: string;
  source: Source;
  marks: Mark[];                       // t の昇順
}

/** ほかの窓・AI のマウスのカーソル（画面の状態 ui.state の pointers）。role = human / ai / script */
export interface RemotePointer {
  t: number;
  source: Source;
  label: string;
  role: string;
}

/** ほかのビューから時間軸のその時刻へ飛ぶ（reveal）。時刻は源の単位（捕捉はサンプル番号、記録はタイムベース）か、
 *  タイムベースの µs（us。watch.series 等。記録の 1 単位の秒で換算する）。tEnd（usEnd）があれば区間が見えるように
 *  視点を合わせ、無ければ幅を保って真ん中に。item があればその出来事を選ぶ */
export interface Reveal {
  source: Source;
  t?: number;
  tEnd?: number;
  us?: number;
  usEnd?: number;
  item?: PickedItem | null;
}

export interface TimelineSnapshot {
  source: Source;
  capture: CaptureInfo | null;          // source = capture の捕捉
  recording: string | null;             // source = recording の記録の名前
  extent: [number, number] | null;      // データの範囲 [始め, 終わり)（捕捉はサンプル番号、記録はタイムベース）
  tickS: number;                        // 1 単位の秒
  view: View;                           // 見ている範囲（整数、[start, end)）
  width: number;                        // 間引きの幅（デバイスの画素）
  frame: TimelineFrame | null;          // 最新の間引きの結果
  header: TileHeader | null;            // その元のタイルの見出し
  rows: VisibleRow[];                   // 描く段（上から）
  groups: GroupDef[];                   // すべての段（隠したものも。段の一覧のメニュー）
  config: RowConfig;
  autoHidden: string[];                 // 頼まれずに隠した段（システム解析の並びで変化の無いピン。画面が数を出して見せ直す）
  selection: number | null;             // 選択カーソル
  reference: number | null;             // 基準のカーソル
  selected: PickedItem | null;          // 選んだ出来事
  overview: TimelineFrame | null;       // 縮図（全体のタイルを縮図の幅で）
  anomalies: Anomaly[];                 // 縮図の異常の印
  capturedBy: string | null;            // 捕捉した利用者（記録の通知で分かったとき）
  marks: MarkSet[];                     // ビューが置いた印（setMarks）
  pointers: RemotePointer[];            // ほかの窓・AI のマウスのカーソル
  origins: Origins;                     // 源ごとの時刻 0（記録・捕捉の始め）と 1 単位の秒。今の源でなくても持つ
  live: boolean;                        // 記録中の記録を見ている（範囲が伸びる）
  follow: boolean;                      // 伸びた端を追いかける（視点の終わりが端にある）
  loading: boolean;                     // タイルを読んでいる
  error: string | null;                 // 捕捉が無い以外の読みの誤り
  decimator: "worker" | "inline";
}
