// 試す窓の欄の読み取り（verify.ts の台本・規則・フックの欄から API の引数を組むときの共通の検査）。
// 誤りは欄の道筋 → 文言のキーの末尾（画面が verify.err.<code> にする）。Svelte に依らない。
/** 欄の誤り（欄の道筋 → 文言のキーの末尾。画面が verify.err.<code> にする） */
export type Errors = Record<string, string>;
export type Built<T> = { params: T } | { errors: Errors };

export const INTEGER = /^(0x[0-9a-f]+|\d+)$/i;

/** 誤りを置く（同じ欄の最初の誤りだけ）。値の代わりに返せるよう undefined を返す */
export function put(errors: Errors, path: string, code: string): undefined {
  if (!(path in errors)) errors[path] = code;
  return undefined;
}

/** 0 以上の整数（10 進・0x）。空なら undefined */
export function uint(text: string, path: string, errors: Errors, required = false): number | undefined {
  const s = text.trim();
  if (!s) {
    if (required) put(errors, path, "required");
    return undefined;
  }
  if (!INTEGER.test(s) || !Number.isSafeInteger(Number(s))) {
    put(errors, path, "integer");
    return undefined;
  }
  return Number(s);
}

/** 0 以上の数（秒）。空なら undefined */
export function seconds(text: string, path: string, errors: Errors, required = false): number | undefined {
  const s = text.trim();
  if (!s) {
    if (required) put(errors, path, "required");
    return undefined;
  }
  const v = Number(s);
  if (!Number.isFinite(v)) {
    put(errors, path, "number");
    return undefined;
  }
  if (v < 0) {
    put(errors, path, "min");
    return undefined;
  }
  return v;
}

/** 比べる値: 整数（0x も）・数・true / false */
export function scalar(text: string, path: string, errors: Errors): unknown {
  const s = text.trim();
  if (!s) return put(errors, path, "required");
  if (/^-?(0x[0-9a-f]+)$/i.test(s)) return s.startsWith("-") ? -Number(s.slice(1)) : Number(s);
  if (s === "true" || s === "false") return s === "true";
  const v = Number(s);
  if (Number.isFinite(v)) return v;
  return put(errors, path, "number");
}

export function json(text: string, path: string, errors: Errors, shape: "object" | "array" | "any" = "any"): unknown {
  const s = text.trim();
  if (!s) return put(errors, path, "required");
  let v: unknown;
  try {
    v = JSON.parse(s);
  } catch {
    return put(errors, path, "json");
  }
  const ok = shape === "any" || (shape === "array" ? Array.isArray(v)
    : typeof v === "object" && v !== null && !Array.isArray(v));
  return ok ? v : put(errors, path, shape);
}

/** 番地: 数（0x・10 進）なら数、ほかは ELF の変数名 */
export function address(text: string, path: string, errors: Errors): number | string | undefined {
  const s = text.trim();
  if (!s) return put(errors, path, "required");
  return INTEGER.test(s) ? Number(s) : s;
}

export const done = <T>(errors: Errors, params: T): Built<T> => (Object.keys(errors).length ? { errors } : { params });
