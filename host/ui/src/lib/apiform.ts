// API パネルの計算（画面に全 API を入れる、段階 A。docs/superpowers/specs/2026-10-22-ui-all-apis-design.md）:
// 仕様（host/api/fprobe_api.json）からメソッドの一覧と引数の欄を作り、欄の値から引数を組み立てる。Svelte に依らない。
// 範囲は MCP の道具と同じ（session.* はつなぎ方の手続き、binary は画面の専用の経路なので出さない）。

export interface SpecMethod {
  kind: "read" | "control";
  summary?: string;
  params?: Schema;
  binary?: boolean;
  irreversible?: boolean;
}

export interface ApiSpec {
  methods: Record<string, SpecMethod>;
}

export interface ApiMethod {
  name: string;
  kind: "read" | "control";
  summary: string;
  irreversible: boolean;
  params: Schema;
}

type Schema = Record<string, any>;

// scalar: 整数・数・文字列・列挙のどれか（anyOf [integer, string] 等）。整数として読めれば整数（0x も）、だめなら数、
// 列挙の値、文字列の順に読む
export type FieldKind = "integer" | "number" | "boolean" | "string" | "enum" | "scalar" | "json";

export interface Field {
  name: string;
  kind: FieldKind;
  nullable: boolean;
  required: boolean;
  options?: unknown[];
  accepts?: ("integer" | "number" | "string")[];   // scalar が受ける型
  min?: number;
  max?: number;
  exclusiveMin?: number;
  description?: string;
  default?: unknown;
}

/** 欄の誤り（画面が i18n の文言にする） */
export interface FieldError {
  code: "required" | "integer" | "number" | "unsafe" | "min" | "exclusiveMin" | "max" | "boolean" | "enum" | "json"
    | "null";
  limit?: number;
}

/** 欄の値: 文字列（空 = 送らない）、null = null を送ると明示（null を許す欄だけ） */
export type FieldValues = Record<string, string | null | undefined>;

export function apiMethods(spec: ApiSpec): ApiMethod[] {
  return Object.entries(spec.methods)
    .filter(([name, m]) => !name.startsWith("session.") && !m.binary)
    .sort(([a], [b]) => (a < b ? -1 : a > b ? 1 : 0))
    .map(([name, m]) => ({ name, kind: m.kind, summary: m.summary ?? "", irreversible: !!m.irreversible,
                           params: m.params ?? {} }));
}

/** 型の候補（anyOf・oneOf・type の配列）を、null 以外の候補と「null を許すか」に分ける */
function branches(p: Schema): [Schema[], boolean] {
  const list: Schema[] = p.anyOf ?? p.oneOf
    ?? (Array.isArray(p.type) ? p.type.map((type: string) => ({ ...p, type })) : [p]);
  const rest = list.filter((b) => b.type !== "null");
  return [rest, rest.length < list.length];
}

const SCALAR_TYPES = new Set(["integer", "number", "string"]);

function kindOf(b: Schema): FieldKind {
  if (Array.isArray(b.enum)) return "enum";
  if (b.type === "integer" || b.type === "number" || b.type === "boolean" || b.type === "string") return b.type;
  return "json";
}

export function fieldsOf(params: Schema): Field[] {
  const required = new Set<string>(params.required ?? []);
  return Object.entries<Schema>(params.properties ?? {}).map(([name, raw]) => {
    const [rest, nullable] = branches(raw);
    let kind: FieldKind = "json";
    let p: Schema = raw;
    if (rest.length === 1) {
      p = { ...raw, ...rest[0] };
      kind = kindOf(p);
    } else if (rest.length > 1 && rest.every((b) => Array.isArray(b.enum) || SCALAR_TYPES.has(b.type))) {
      kind = "scalar";
      p = Object.assign({}, raw, ...rest.filter((b) => !Array.isArray(b.enum)));
    }
    const field: Field = { name, kind, nullable, required: required.has(name) };
    if (kind === "enum") field.options = p.enum;
    if (kind === "scalar") {
      field.accepts = rest.filter((b) => SCALAR_TYPES.has(b.type)).map((b) => b.type);
      const options = rest.flatMap((b) => (Array.isArray(b.enum) ? b.enum : []));
      if (options.length) field.options = options;
    }
    if (typeof p.minimum === "number") field.min = p.minimum;
    if (typeof p.maximum === "number") field.max = p.maximum;
    if (typeof p.exclusiveMinimum === "number") field.exclusiveMin = p.exclusiveMinimum;
    const description = raw.description ?? p.description;
    if (description) field.description = description;
    if ("default" in raw) field.default = raw.default;
    return field;
  });
}

const INTEGER = /^-?(0x[0-9a-f]+|\d+)$/i;

/** INTEGER に合う文字列（10 進・0x の 16 進・負の数）を数に。Number は "0x…" も読む */
function integer(text: string): number {
  return text.startsWith("-") ? -Number(text.slice(1)) : Number(text);
}

function parse(f: Field, text: string): { value: unknown } | { error: FieldError } {
  switch (f.kind) {
    case "integer": {
      if (!INTEGER.test(text)) return { error: { code: "integer" } };
      const value = integer(text);
      return Number.isSafeInteger(value) ? range(f, value) : { error: { code: "unsafe" } };
    }
    case "number": {
      const value = Number(text);
      if (text === "" || !Number.isFinite(value)) return { error: { code: "number" } };
      return range(f, value);
    }
    case "boolean":
      return text === "true" ? { value: true } : text === "false" ? { value: false } : { error: { code: "boolean" } };
    case "enum": {
      const option = f.options?.find((o) => String(o) === text);
      return option === undefined ? { error: { code: "enum" } } : { value: option };
    }
    case "scalar": {
      const accepts = f.accepts ?? [];
      if (accepts.includes("integer") && INTEGER.test(text)) {
        const value = integer(text);
        return Number.isSafeInteger(value) ? range(f, value) : { error: { code: "unsafe" } };
      }
      if (accepts.includes("number") && Number.isFinite(Number(text))) return range(f, Number(text));
      const option = f.options?.find((o) => String(o) === text);
      if (option !== undefined) return { value: option };
      if (accepts.includes("string")) return { value: text };
      return { error: { code: f.options ? "enum" : "integer" } };
    }
    case "string":
      return { value: text };
    case "json": {
      let unsafe = false;                              // JSON.parse は 2^53 を超える整数を黙って丸める
      let value: unknown;
      try {
        value = JSON.parse(text, (_key, v) => {
          if (typeof v === "number" && Number.isInteger(v) && !Number.isSafeInteger(v)) unsafe = true;
          return v;
        });
      } catch {
        return { error: { code: "json" } };
      }
      return unsafe ? { error: { code: "unsafe" } } : { value };
    }
  }
}

function range(f: Field, value: number): { value: number } | { error: FieldError } {
  if (f.min !== undefined && value < f.min) return { error: { code: "min", limit: f.min } };
  if (f.exclusiveMin !== undefined && value <= f.exclusiveMin) return { error: { code: "exclusiveMin", limit: f.exclusiveMin } };
  if (f.max !== undefined && value > f.max) return { error: { code: "max", limit: f.max } };
  return { value };
}

/** 欄の値から引数。空の欄は送らない（既定の値はデーモンが決める）。null は null を送る。文字列の欄は空白・改行を
 *  削らない。誤りがあれば送らずに欄ごとの誤り */
export function buildParams(fields: Field[], values: FieldValues):
  { params: Record<string, unknown> } | { errors: Record<string, FieldError> } {
  const params: Record<string, unknown> = {};
  const errors: Record<string, FieldError> = {};
  for (const f of fields) {
    const raw = values[f.name];
    if (raw === null) {
      if (f.nullable) params[f.name] = null;
      else errors[f.name] = { code: "null" };
      continue;
    }
    const text = f.kind === "string" ? (raw ?? "") : (raw ?? "").trim();
    if (text === "") {
      if (f.required) errors[f.name] = { code: "required" };
      continue;
    }
    const r = parse(f, text);
    if ("error" in r) errors[f.name] = r.error;
    else params[f.name] = r.value;
  }
  return Object.keys(errors).length ? { errors } : { params };
}

/** 欄がその値をそのまま表せるか（Activity の引数は長い値が {length, head} に要約されている: 型が合わなければ入れない） */
function fits(f: Field, v: unknown): boolean {
  switch (f.kind) {
    case "integer":
    case "number":
      return typeof v === "number";
    case "boolean":
      return typeof v === "boolean";
    case "string":
      return typeof v === "string";
    case "enum":
      return (f.options ?? []).includes(v);
    case "scalar":
      return typeof v === "number" || typeof v === "string";
    case "json":
      return true;
  }
}

/** 引数（Activity の行の params）から欄の値。null は「null を送る」。欄の型に合わない値（要約された値）は入れず、
 *  その欄の名前を unrestored に返す */
export function valuesOf(fields: Field[], params: Record<string, unknown>):
  { values: Record<string, string | null>; unrestored: string[] } {
  const values: Record<string, string | null> = {};
  const unrestored: string[] = [];
  for (const f of fields) {
    const v = params[f.name];
    if (v === undefined) continue;
    if (v === null) {
      if (f.nullable) values[f.name] = null;
      else unrestored.push(f.name);
      continue;
    }
    if (!fits(f, v)) {
      unrestored.push(f.name);
      continue;
    }
    values[f.name] = f.kind === "json" ? JSON.stringify(v) : String(v);
  }
  return { values, unrestored };
}

export type ConfirmLevel = "none" | "confirm" | "danger";

/** 呼ぶ前の確かめ: read は無し、control は確かめ、取り返しのつかない操作（仕様の印）は赤 */
export function confirmLevel(m: Pick<ApiMethod, "kind" | "irreversible">): ConfirmLevel {
  return m.irreversible ? "danger" : m.kind === "control" ? "confirm" : "none";
}
