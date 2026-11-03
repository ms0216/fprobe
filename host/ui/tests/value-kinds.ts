// 試験で値の形（fprobe.dwarf.values。どの値も kind を持つ object。API の応答の型 Value）を短く書く
import type { Value } from "../../api/generated/fprobe_api";

type Of<K extends Value["kind"]> = Extract<Value, { kind: K }>;
type Extra<K extends Value["kind"], Required extends string> = Partial<Omit<Of<K>, "kind" | Required>>;

export const vint = (value: number): Of<"int"> => ({ kind: "int", value });
export const vfloat = (value: Of<"float">["value"]): Of<"float"> => ({ kind: "float", value });
export const vbool = (value: boolean): Of<"bool"> => ({ kind: "bool", value });
export const vbytes = (hex: string): Of<"bytes"> => ({ kind: "bytes", hex });
export const venum = (name: string | null, value: number): Of<"enum"> => ({ kind: "enum", name, value });
export const vptr = (address: number, to: string, more: Extra<"pointer", "address" | "to"> = {}): Of<"pointer"> =>
  ({ kind: "pointer", address, to, ...more });
export const vstruct = (members: Record<string, Value>): Of<"struct"> => ({ kind: "struct", members });
export const varray = (items: Value[], length = items.length, more: Extra<"array", "length" | "items"> = {}): Of<"array"> =>
  ({ kind: "array", length, items, ...more });
export const ints = (...values: number[]) => values.map(vint);
export const vna = (reason: string): Of<"unavailable"> => ({ kind: "unavailable", reason });
