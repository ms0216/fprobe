// Recordings の計算: 長期の記録の設定の欄 → recording.start の引数、書き出し（recording.export）の引数と既定のパス。
// Svelte に依らない
export type ExportFormat = "perfetto" | "vcd" | "csv" | "json";

/** 書き出しの形式（ラベルは製品の名前・ファイルの形式で、訳さない） */
export const EXPORT_FORMATS: { id: ExportFormat; label: string }[] = [
  { id: "perfetto", label: "Perfetto (Chrome JSON)" },
  { id: "vcd", label: "VCD" },
  { id: "csv", label: "CSV" },
  { id: "json", label: "JSON" },
];

/** 書き出しの既定のパス（デーモンの PC の作業フォルダに、記録の名前と形式の拡張子で） */
export function defaultExportPath(recording: string | null | undefined, format: ExportFormat): string {
  const base = recording || "recording";
  return format === "perfetto" ? `${base}.perfetto.json` : `${base}.${format}`;
}

/** recording.export の引数。範囲 [t0, t1) は CSV・JSON・Perfetto だけ（VCD は波形の全体） */
export function exportParams(format: ExportFormat, path: string, range: [number, number] | null):
  { format: ExportFormat; path: string; t0?: number; t1?: number } {
  return { format, path, ...(range && format !== "vcd" ? { t0: range[0], t1: range[1] } : {}) };
}

export interface LongTermForm { segmentS: string; maxMb: string; decimateAfterS: string }

/** 長期の記録の欄 → recording.start の引数（空の欄は送らない = 普通の記録）。読めない値は投げる */
export function longTermParams(f: LongTermForm): { segment_s?: number; max_bytes?: number; decimate_after_s?: number } {
  const num = (text: string, what: string, positive: boolean) => {
    const v = Number(text.trim());
    if (!Number.isFinite(v) || (positive ? v <= 0 : v < 0)) throw new Error(`${what}: ${text}`);
    return v;
  };
  return {
    ...(f.segmentS.trim() ? { segment_s: num(f.segmentS, "segment_s", true) } : {}),
    ...(f.maxMb.trim() ? { max_bytes: Math.max(1, Math.round(num(f.maxMb, "max_bytes", true) * 1024 * 1024)) } : {}),
    ...(f.decimateAfterS.trim() ? { decimate_after_s: num(f.decimateAfterS, "decimate_after_s", false) } : {}),
  };
}
