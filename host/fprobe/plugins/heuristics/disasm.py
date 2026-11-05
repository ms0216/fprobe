"""プラグイン heuristics: 逆アセンブルの境界・データの推定（Thumb の境界・tbb の表・リテラルプール）。"""

from collections.abc import Mapping, Sequence

from .. import ApiCaller
from ._common import _tag

def disasm_before_pc(caller: ApiCaller, pc: int, before: int = 8, after: int = 8) -> dict:
    """PC の前の命令の境界を「Thumb は数命令で揃う」と見込んで推定し、PC の前 before 命令・PC・後ろ after 命令を
    並べる（f2cfa3c より前の disasm.py の _sync_start の heuristic）。

    メモリは target.memory.read で読み、本体の逆アセンブラ（fprobe.disasm.Disassembler）をシンボル無しで使う。
    遠い候補（PC − 2 × (2 × before + 8)）から近い方へ、そこから読んで解読できないバイト無しで PC にちょうど着地する
    最初の点を境界とみなす。どこも着地しなければ PC から読む（sync = "start"）。
    経験則: Thumb の命令の境界は、違う所から読み始めても数命令で正しい境界に揃う。外れる場合: リテラルプール・表
    （データ）の直後、32 ビット命令の後半から読むともっともらしい誤った命令の列になることがある。関数の先頭・$t が
    分かるなら本体の debug.disasm.around（sync = function / mapping）が事実。"""
    from ...disasm import Disassembler
    from ...memview import BytesMemory
    reach = 2 * (2 * max(before, 1) + 8)
    tail = 4 * (after + 1) + 2
    base = max(0, pc - reach)
    data = bytes.fromhex(caller.call("target.memory.read", address=base, length=pc - base + tail + 2))
    dis = Disassembler(BytesMemory(base, data))
    start = pc
    for k in range((pc - base) // 2, 0, -1):
        try:
            head = dis.range(pc - 2 * k, pc).instructions
        except Exception:
            continue
        if head and head[-1].address + head[-1].size == pc and not any(i.kind == "data" for i in head):
            start = pc - 2 * k
            break
    instructions = list(dis.range(start, pc + tail - 2).instructions)
    at = next((n for n, i in enumerate(instructions) if i.address == pc), None)
    sync = "heuristic" if start < pc else "start"
    if at is None:                                       # PC が推定した列の命令の途中: PC から読む
        instructions, at, sync = list(dis.range(pc, pc + tail - 2).instructions), 0, "start"
    prior = instructions[:at][-before:] if before > 0 else []
    listing = prior + instructions[at:at + after + 1]
    return _tag("thumb_self_sync", "Thumb の命令の境界は、違う所から読んでも数命令で揃う",
                sync=sync, pc=pc, pc_index=len(prior), instructions=[i.as_dict() for i in listing])


def tbb_table_sizes(instructions: Sequence, window: int = 4) -> dict:
    """逆アセンブルの命令の列（fprobe.disasm.Instruction）の tbb / tbh ごとに、表の項目の数を直前 window 命令の中の
    "cmp 添字, #n" から n + 1 と推す（以前は本体の Disassembler がしていた）。結果の tables は
    Disassembler(branch_tables=...) にそのまま渡せる（表の直後のバイトをデータとして区切る）。branch_tables は同じ内容を
    API（debug.disasm.* の branch_tables）に渡す形にしたもの。

    経験則: コンパイラは switch の表引きの前に添字の範囲を "cmp 添字, #最大" と "bhi 既定へ" で確かめる（慣用句）。
    外れる場合: 範囲の確かめが window より前にある・別の形（subs・and での丸め等）・添字の値を cmp の後で変える・
    手書きのアセンブリ。表を大きく見積もると後ろの命令をデータにし、小さく見積もると表をコードとして読む。"""
    tables = {}
    for n, insn in enumerate(instructions):
        if insn.kind != "code" or insn.mnemonic.split(".")[0] not in ("tbb", "tbh"):
            continue
        parts = [p.strip() for p in insn.operands.strip("[]").split(",")]
        if len(parts) < 2:
            continue
        index = parts[1]
        for previous in reversed(instructions[max(0, n - window):n]):
            if previous.kind == "code" and previous.mnemonic.startswith("cmp"):
                reg, _, imm = previous.operands.partition(", #")
                if reg.strip() == index and imm:
                    tables[insn.address] = int(imm, 0) + 1
                    break
    return _tag("tbb_cmp_bound", f"tbb / tbh の直前 {window} 命令の cmp 添字, #n が表の範囲の確かめ（項目 n + 1）",
                window=window, tables=tables,
                branch_tables=[{"address": a, "entries": n} for a, n in tables.items()])   # debug.disasm.* の引数の形


def literal_pool_ranges(instructions: Sequence) -> dict:
    """逆アセンブルの命令の列（fprobe.disasm.Instruction か API の instructions の辞書）の ldr / vldr [pc, #n] が読む先を
    データの区間とみなす（以前は本体の Disassembler が $t・$d の無い所でしていた）。結果の data_ranges は API
    （debug.disasm.* の data_ranges）・Disassembler(data_ranges=[(address, size)]) に渡せる。

    経験則: PC 相対で読まれる番地はリテラルプール（コンパイラが関数の後ろに置く定数）で、命令ではない。
    外れる場合: 手書きのアセンブリ・自己書き換え・コードのバイトを定数として読む所（チェックサム等）。読む先がコードの
    途中なら、後ろの命令の境界がずれる。"""
    ranges: dict[int, int] = {}
    for insn in instructions:
        get = insn.get if isinstance(insn, Mapping) else lambda k, i=insn: getattr(i, k)
        mnemonic = get("mnemonic") or ""
        if get("kind") != "code" or get("literal") is None or not mnemonic.startswith(("ldr", "vldr")) \
                or "pc" not in (get("operands") or ""):
            continue
        operands = get("operands") or ""
        size = 8 if mnemonic.startswith("vldr") and operands.lstrip().startswith("d") else 4
        ranges[int(get("literal"))] = max(size, ranges.get(int(get("literal")), 0))
    return _tag("pc_load_literal_pool", "ldr / vldr [pc, #n] の読む先はリテラルプール（データ）",
                data_ranges=[{"address": a, "size": n} for a, n in sorted(ranges.items())])
