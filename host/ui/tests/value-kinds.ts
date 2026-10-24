// 試験で値の形（fprobe.dwarf.values。どの値も kind を持つ object）を短く書く
export const vint = (value: number) => ({ kind: "int", value });
export const vfloat = (value: number | string) => ({ kind: "float", value });
export const vbool = (value: boolean) => ({ kind: "bool", value });
export const vbytes = (hex: string) => ({ kind: "bytes", hex });
export const venum = (name: string | null, value: number) => ({ kind: "enum", name, value });
export const vptr = (address: number, to: string, more: Record<string, unknown> = {}) =>
  ({ kind: "pointer", address, to, ...more });
export const vstruct = (members: Record<string, unknown>) => ({ kind: "struct", members });
export const varray = (items: unknown[], length = items.length, more: Record<string, unknown> = {}) =>
  ({ kind: "array", length, items, ...more });
export const ints = (...values: number[]) => values.map(vint);
export const vna = (reason: string) => ({ kind: "unavailable", reason });
