// 保護（段階 B §3 の 9）: オプションバイト（target.option_bytes.read の fields）を名前ごとに並べ、変えた値だけを
// target.option_bytes.write の fields にする。Svelte に依らない
const INTEGER = /^(0x[0-9a-f]+|\d+)$/i;

/** 変えた欄だけ（空の欄は変えない）。誤りは欄の名前、何も変えていなければ "nothing" */
export function changedFields(read: Record<string, number>, edited: Record<string, string>):
  { fields: Record<string, number> } | { error: string } {
  const fields: Record<string, number> = {};
  for (const [name, text] of Object.entries(edited)) {
    const t = text.trim();
    if (t === "") continue;
    if (!INTEGER.test(t)) return { error: name };
    const value = Number(t);
    if (value !== read[name]) fields[name] = value;
  }
  return Object.keys(fields).length ? { fields } : { error: "nothing" };
}
