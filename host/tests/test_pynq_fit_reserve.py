"""PYNQ-Z2 の起動イメージ（FIT）に reserved-memory を足す道具（host/pynq/fit-reserve/fit_reserve.py）。"""
import hashlib
import importlib.util
import struct
from pathlib import Path

import pytest

SRC = Path(__file__).resolve().parents[1] / "pynq" / "fit-reserve" / "fit_reserve.py"
spec = importlib.util.spec_from_file_location("fit_reserve", SRC)
fr = importlib.util.module_from_spec(spec)
spec.loader.exec_module(fr)

PADDING = 4096


def node(name, props=(), children=()):
    n = fr.Node(name)
    n.props, n.children = list(props), list(children)
    return n


def cells(*values):
    return b"".join(struct.pack(">I", v) for v in values)


def inner_dtb(extra_children=()):
    root = node("", [("#address-cells", cells(1)), ("#size-cells", cells(1))],
                [node("cpus"), node("memory@0", [("device_type", b"memory\0"), ("reg", cells(0, 0x20000000))]), *extra_children])
    blob = bytearray(fr.serialize({"root": root, "rsv": bytes(16), "ver": 17, "lastcomp": 16, "bootcpu": 0}) + bytes(PADDING))
    blob[4:8] = struct.pack(">I", len(blob))                      # dtc -p と同じ: totalsize に余白を含める
    return bytes(blob)


def fit(inner, kernel=b"K" * 5000):
    images = node("images", children=[
        node("kernel-0", [("data", kernel), ("type", b"kernel\0")], [node("hash-1", [("value", hashlib.sha1(kernel).digest()), ("algo", b"sha1\0")])]),
        node("fdt-0", [("data", inner), ("type", b"flat_dt\0")], [node("hash-1", [("value", hashlib.sha1(inner).digest()), ("algo", b"sha1\0")])])])
    root = node("", [("description", b"fit\0")], [images, node("configurations", [("default", b"conf-1\0")])])
    return fr.serialize({"root": root, "rsv": bytes(16), "ver": 17, "lastcomp": 16, "bootcpu": 0})


def fdt_of(blob):
    image = fr.parse(blob)["root"].child("images").child("fdt-0")
    return image, image.prop("data")


def test_the_ring_becomes_a_reserved_memory_node_with_a_valid_hash_and_the_same_padding():
    original = fit(inner_dtb())
    out = fr.add_reserved_memory(original, 0x1E000000, 0x2000000)
    image, data = fdt_of(out)
    tree = fr.parse(data)["root"]
    reserved = tree.child("reserved-memory")
    assert reserved.prop("#address-cells") == cells(1) and reserved.prop("#size-cells") == cells(1) and reserved.prop("ranges") == b""
    ring = reserved.child("fprobe_ring@1e000000")
    assert ring.prop("reg") == cells(0x1E000000, 0x2000000)
    assert [p for p, _ in ring.props] == ["reg"]                   # no-map・reusable・compatible を付けない（付けると Linux が使う・写像が無い）
    assert image.child("hash-1").prop("value") == hashlib.sha1(data).digest()
    totalsize, off_strings, size_strings = (struct.unpack(">10I", data[:40])[i] for i in (1, 3, 8))
    assert totalsize == len(data) == off_strings + size_strings + PADDING   # 実行時に U-Boot が書き足す余白は元のまま


def test_nothing_else_changes_the_kernel_the_other_nodes_and_the_input():
    original = fit(inner_dtb(), kernel=b"vmlinux" * 1000)
    out = fr.add_reserved_memory(original, 0x1E000000, 0x2000000)
    before, after = fr.parse(original)["root"], fr.parse(out)["root"]
    assert after.child("images").child("kernel-0").prop("data") == before.child("images").child("kernel-0").prop("data")
    assert after.child("configurations").prop("default") == b"conf-1\0"
    cpus_and_memory = [c.name for c in fr.parse(fdt_of(out)[1])["root"].children]
    assert cpus_and_memory == ["cpus", "memory@0", "reserved-memory"]
    assert fr.parse(fdt_of(original)[1])["root"].child("reserved-memory") is None


def test_an_existing_reserved_memory_or_an_unexpected_shape_is_refused_not_guessed():
    with pytest.raises(ValueError, match="reserved-memory が既にある"):
        fr.add_reserved_memory(fit(inner_dtb([node("reserved-memory")])), 0x1E000000, 0x2000000)
    two_cells = fr.serialize({"root": node("", [("#address-cells", cells(2)), ("#size-cells", cells(2))], [node("memory@0")]),
                              "rsv": bytes(16), "ver": 17, "lastcomp": 16, "bootcpu": 0})
    with pytest.raises(ValueError, match="#address-cells"):
        fr.add_reserved_memory(fit(two_cells), 0x1E000000, 0x2000000)
    with pytest.raises(ValueError, match="DTB でない"):
        fr.parse(b"\0" * 64)


def test_the_tool_never_overwrites_its_input(tmp_path):
    src = tmp_path / "image.ub"
    src.write_bytes(fit(inner_dtb()))
    with pytest.raises(SystemExit):
        import sys
        old = sys.argv
        sys.argv = ["fit_reserve", str(src), str(src)]
        try:
            fr.main()
        finally:
            sys.argv = old
