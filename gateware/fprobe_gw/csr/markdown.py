"""CSR 定義から Markdown のレジスタ表（docs/registers.md）を生成する。"""

from .model import CsrMap

__all__ = ["render_markdown"]


def render_markdown(csr_map: CsrMap) -> str:
    lines = ["# fprobe CSR レジスタ表", "",
             "<!-- 自動生成ファイル。直接編集しないこと（python -m fprobe_gw gen で再生成）。"
             "生成元: gateware/fprobe_gw/csr/fprobe_map.py -->", "",
             "CONTROL チャンネルの READ / WRITE で読み書きする（アドレスはバイト単位、32 ビットワード）。", "",
             "| ブロック | ベース | サイズ | 内容 |", "|---|---|---|---|"]
    lines += [f"| `{b.name}` | `0x{b.base:08X}` | `0x{b.size:X}` | {b.doc} |" for b in csr_map.blocks]
    for b in csr_map.blocks:
        if not b.registers:
            continue
        lines += ["", f"## {b.name}", "", "| アドレス | レジスタ | ビット | フィールド | 種類 | 初期値 / 値 | 説明 |",
                  "|---|---|---|---|---|---|---|"]
        for r in b.registers:
            for f in r.fields:
                bits = f"{f.lsb}" if f.width == 1 else f"{f.lsb + f.width - 1}:{f.lsb}"
                value = "—" if f.access == "RO" else f"`0x{f.value:X}`"
                access = f"{f.access}（書くと `{r.write_to}`）" if r.write_to else f.access
                lines.append(f"| `0x{b.base + r.offset:08X}` | `{r.name}` | {bits} | `{f.name}` | {access} "
                             f"| {value} | {r.doc if r.single else f.doc} |")
    return "\n".join(lines) + "\n"
