"""PYNQ-Z2 の起動イメージ（FIT。image.ub）の中のデバイスツリーに、reserved-memory のノードを足した別の FIT を作る。

深いバッファの DDR のリング（既定 0x1E000000 + 32 MB）を Linux の予約領域にすると、カーネルは使わず（memblock の予約）、
/dev/mem の割り当てが書き込み結合になり、PS の読みが 約 125 → 約 235 MB/s になる（2026-10-31 実測、PYNQ-Z2）。
Linux の管理外（起動パラメータ mem=479M）のままだと、/dev/mem は最も遅い割り当てになる。

使い方: python fit_reserve.py image.ub image-wc.ub [--base 0x1E000000] [--size 0x2000000]
元のファイルは変えない。変えるのは、FIT の fdt イメージの dtb（reserved-memory の追加）とその sha1 だけ。カーネルはそのまま。
SPDX-License-Identifier: AGPL-3.0-only
"""
import argparse
import hashlib
import struct
from pathlib import Path

FDT_MAGIC = 0xD00DFEED


class Node:
    def __init__(self, name):
        self.name, self.props, self.children = name, [], []

    def prop(self, name):
        return next((v for k, v in self.props if k == name), None)

    def child(self, name):
        return next((c for c in self.children if c.name == name), None)


def _pad4(n):
    return (n + 3) & ~3


def parse(blob):
    """DTB を木にする。戻り値: {root, rsv（メモリ予約表）, ver, lastcomp, bootcpu}。"""
    magic, _total, off_struct, off_strings, off_rsv, ver, lastcomp, bootcpu, sz_strings, sz_struct = struct.unpack(">10I", blob[:40])
    if magic != FDT_MAGIC:
        raise ValueError(f"DTB でない（magic 0x{magic:08x}）")
    strings = blob[off_strings:off_strings + sz_strings]
    pos, end, stack, root = off_struct, off_struct + sz_struct, [], None
    while pos < end:
        token = struct.unpack(">I", blob[pos:pos + 4])[0]
        pos += 4
        if token == 1:                                           # BEGIN_NODE
            nul = blob.index(b"\0", pos)
            node = Node(blob[pos:nul].decode())
            pos = _pad4(nul + 1)
            if stack:
                stack[-1].children.append(node)
            else:
                root = node
            stack.append(node)
        elif token == 2:                                         # END_NODE
            stack.pop()
        elif token == 3:                                         # PROP
            length, name_off = struct.unpack(">II", blob[pos:pos + 8])
            pos += 8
            name = strings[name_off:strings.index(b"\0", name_off)].decode()
            stack[-1].props.append((name, bytes(blob[pos:pos + length])))
            pos = _pad4(pos + length)
        elif token == 9:                                         # END
            break
        elif token != 4:                                         # NOP 以外
            raise ValueError(f"DTB のトークン {token} が読めない（位置 {pos}）")
    return {"root": root, "rsv": blob[off_rsv:off_struct], "ver": ver, "lastcomp": lastcomp, "bootcpu": bootcpu}


def serialize(tree):
    strings, offsets, body = bytearray(), {}, bytearray()

    def name_offset(name):
        if name not in offsets:
            offsets[name] = len(strings)
            strings.extend(name.encode() + b"\0")
        return offsets[name]

    def emit(node):
        raw = node.name.encode() + b"\0"
        body.extend(struct.pack(">I", 1) + raw + b"\0" * (_pad4(len(raw)) - len(raw)))
        for name, value in node.props:
            body.extend(struct.pack(">III", 3, len(value), name_offset(name)) + value + b"\0" * (_pad4(len(value)) - len(value)))
        for child in node.children:
            emit(child)
        body.extend(struct.pack(">I", 2))

    emit(tree["root"])
    body.extend(struct.pack(">I", 9))
    off_rsv = 40
    off_struct = off_rsv + len(tree["rsv"])
    off_strings = off_struct + len(body)
    header = struct.pack(">10I", FDT_MAGIC, off_strings + len(strings), off_struct, off_strings, off_rsv, tree["ver"],
                         tree["lastcomp"], tree["bootcpu"], len(strings), len(body))
    return header + tree["rsv"] + bytes(body) + bytes(strings)


def add_reserved_memory(fit_bytes, base, size, name="fprobe_ring"):
    """FIT の fdt-0 の dtb に reserved-memory/<name>@<base> を足した FIT を返す。dtb の末尾の余白（実行時の書き足し用）は元と同じにする。"""
    fit = parse(fit_bytes)
    fdt = fit["root"].child("images").child("fdt-0")
    original = fdt.prop("data")
    totalsize, off_strings, size_strings = struct.unpack(">10I", original[:40])[1], struct.unpack(">10I", original[:40])[3], struct.unpack(">10I", original[:40])[8]
    padding = totalsize - (off_strings + size_strings)
    inner = parse(original)
    root = inner["root"]
    if root.child("reserved-memory") is not None:
        raise ValueError("デバイスツリーに reserved-memory が既にある（足さない。手で確かめる）")
    cells = [root.prop("#address-cells"), root.prop("#size-cells")]
    if cells != [struct.pack(">I", 1)] * 2:
        raise ValueError("ルートの #address-cells・#size-cells が 1 でない（1 セル幅だけ扱う）")
    reserved = Node("reserved-memory")
    reserved.props = [("#address-cells", struct.pack(">I", 1)), ("#size-cells", struct.pack(">I", 1)), ("ranges", b"")]
    ring = Node(f"{name}@{base:x}")
    ring.props = [("reg", struct.pack(">II", base, size))]       # no-map・reusable・compatible なし: Linux は使わず、写像は残る
    reserved.children.append(ring)
    memory = [c.name for c in root.children].index("memory@0")
    root.children.insert(memory + 1, reserved)
    data = bytearray(serialize(inner) + b"\0" * padding)
    data[4:8] = struct.pack(">I", len(data))
    data = bytes(data)
    fdt.props = [(k, data if k == "data" else v) for k, v in fdt.props]
    digest = fdt.child("hash-1")
    if digest is None or digest.prop("algo").rstrip(b"\0") != b"sha1":
        raise ValueError("fdt-0 に sha1 の hash-1 が無い（形が想定と違う）")
    digest.props = [(k, hashlib.sha1(data).digest() if k == "value" else v) for k, v in digest.props]
    return serialize(fit)


def main():
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("src", type=Path)
    parser.add_argument("dst", type=Path)
    parser.add_argument("--base", type=lambda s: int(s, 0), default=0x1E000000)
    parser.add_argument("--size", type=lambda s: int(s, 0), default=0x2000000)
    args = parser.parse_args()
    if args.dst.resolve() == args.src.resolve():
        raise SystemExit("元のファイルを上書きしない。別の名前にする")
    args.dst.write_bytes(add_reserved_memory(args.src.read_bytes(), args.base, args.size))
    print(f"{args.dst}: reserved-memory 0x{args.base:08x} + 0x{args.size:x} を足した")


if __name__ == "__main__":
    main()
