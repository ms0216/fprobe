"""fprobe.dwarf の型・値・デバッグ情報: 試験用ファーム（host/firmware/dwarf_demo）の ELF と、その初期値（.data）。"""

from pathlib import Path

import pytest

from fprobe.dwarf.access import ImageAccess
from fprobe.dwarf.info import DebugInfo, FrameView
from fprobe.dwarf.location import MemoryPlace, RegisterPlace, Unavailable, ValuePlace
from fprobe.dwarf.typesys import ArrayType, EnumType, StructType, strip, type_name
from fprobe.dwarf.values import DecodeContext, decode, encode
from fprobe.symbols import Symbols

FIRMWARE = Path(__file__).resolve().parents[1] / "firmware" / "dwarf_demo"
SOURCE = (FIRMWARE / "dwarf_demo.c").read_text(encoding="utf-8").splitlines()
VARIANTS = ["dwarf_demo_O0.elf", "dwarf_demo_O1.elf", "dwarf_demo_O1_dwarf4.elf"]


def line_of(text: str) -> int:
    return next(i for i, line in enumerate(SOURCE, start=1) if text in line)


@pytest.fixture(scope="module", params=VARIANTS)
def elf(request):
    path = FIRMWARE / request.param
    return DebugInfo(path), Symbols(path)


@pytest.fixture(scope="module")
def o0():
    return DebugInfo(FIRMWARE / "dwarf_demo_O0.elf"), Symbols(FIRMWARE / "dwarf_demo_O0.elf")


def read_global(info, syms, name, max_elements=64):
    mem = ImageAccess.from_symbols(syms)
    var = info.global_variable(name)
    place = var.place()
    assert isinstance(place, MemoryPlace) and place.address == syms.variable(name.split("::")[-1]).address \
        if "::" not in name else True
    data = mem.read_memory(place.address, var.type.size)
    names = {s.address: s.name for s in syms._symbols.values()}
    describe = lambda a: names.get(a) or names.get(a & ~1)            # noqa: E731（関数は Thumb のビット付き）
    return decode(var.type, data, DecodeContext(mem.read_memory, describe, max_elements))


# --- 型 -------------------------------------------------------------------------------------------

def test_struct_members_offsets_and_type_names(elf):
    info, _ = elf
    sensor = strip(info.type_named("Sensor"))
    assert isinstance(sensor, StructType) and sensor.size == 80 and sensor is info.type_named("struct Sensor")
    offsets = {m.name: m.offset for m in sensor.members if m.name}
    assert offsets == {"name": 0, "pos": 8, "mode": 12, "flags": 16, "word": 20, "gain": 24, "scale": 32,
                       "samples": 40, "next": 64, "reg": 68}
    types = {m.name: type_name(m.type) for m in sensor.members}
    assert types["name"] == "char[8]" and types["samples"] == "int32_t[2][3]" and types["mode"] == "Mode"
    assert types["next"] == "struct Sensor *" and types["reg"] == "const volatile uint32_t *"
    assert types[None] == "union <anonymous>"
    raw16, extra = sensor.member("raw16")                          # 無名の共用体の中
    assert extra == 72 and raw16.offset == 0 and type_name(raw16.type) == "uint16_t"
    assert strip(sensor.member("next")[0].type).target is sensor                      # 自分を指す
    assert type_name(info.global_variable("sensors").type) == "Sensor[2]"
    assert type_name(info.global_variable("callback").type) == "Callback"
    assert type_name(strip(info.global_variable("callback").type)) == "int (*)(int)"
    assert type_name(info.global_variable("message").type) == "const char *"


def test_multidimensional_arrays_nest(elf):
    info, _ = elf
    matrix = info.global_variable("matrix").type
    assert isinstance(matrix, ArrayType) and matrix.length == 3 and matrix.size == 24
    assert isinstance(matrix.element, ArrayType) and matrix.element.length == 4 and matrix.element.size == 8


def test_bitfields_and_enums(elf):
    info, _ = elf
    flags = strip(info.type_named("struct Flags"))
    bits = {m.name: (m.bit_offset, m.bit_size) for m in flags.members}
    assert bits == {"ready": (0, 1), "mode": (1, 3), "level": (4, 4), "wide": (8, 12), "raw": (None, None)}
    mode = strip(info.type_named("Mode"))
    assert isinstance(mode, EnumType) and mode.signed and mode.enumerators == (
        ("MODE_IDLE", 0), ("MODE_RUN", 1), ("MODE_FAULT", -2))
    assert info.type_named("unsigned int").size == 4
    with pytest.raises(KeyError):
        info.type_named("struct Nothing")


# --- 値（ELF の初期値） --------------------------------------------------------------------------------

def test_structures_decode_to_nested_values(elf):
    info, syms = elf
    sensors = read_global(info, syms, "sensors")
    alpha, beta = sensors
    assert alpha["name"] == {"string": "alpha", "values": [97, 108, 112, 104, 97, 0, 0, 0]}
    assert alpha["pos"] == {"x": 10, "y": -20}
    assert alpha["mode"] == {"enum": "MODE_RUN", "value": 1} and beta["mode"] == {"enum": "MODE_FAULT", "value": -2}
    assert alpha["flags"] == {"ready": 1, "mode": 5, "level": -3, "wide": 0x123, "raw": 0xAB}
    assert beta["flags"] == {"ready": 0, "mode": 7, "level": 7, "wide": 0xFFF, "raw": 1}
    assert alpha["word"] == {"u32": 0x3F800000, "bytes": [0, 0, 0x80, 0x3F], "f": 1.0}
    assert alpha["gain"] == 1.5 and alpha["scale"] == -2.25 and beta["scale"] == 1e10
    assert alpha["samples"] == [[1, 2, 3], [-4, -5, -6]]
    assert alpha["next"] == {"pointer": syms.variable("sensors").address + 80, "to": "struct Sensor"}
    assert beta["next"] == {"pointer": 0, "to": "struct Sensor"}
    assert alpha["reg"] == {"pointer": 0xE000E010, "to": "const volatile uint32_t"}
    assert alpha["raw16"] == 0xBEEF and alpha["pair"] == [-17, -66]          # 無名の共用体は外側に展開


def test_scalars_strings_pointers_and_large_arrays(elf):
    info, syms = elf
    assert read_global(info, syms, "negative") == -5
    assert read_global(info, syms, "big") == 0x123456789ABCDEF0
    assert read_global(info, syms, "enabled") is True
    assert read_global(info, syms, "matrix") == [[0, 1, 2, 3], [10, 11, 12, 13], [20, 21, 22, 23]]
    assert read_global(info, syms, "greeting")["string"] == "hello, fprobe"
    message = read_global(info, syms, "message")
    assert message["string"] == "hello, fprobe" and message["pointer"] == syms.variable("greeting").address
    assert read_global(info, syms, "callback") == {"pointer": syms.symbol("square").address | 1,
                                                    "to": "int (int)", "symbol": "square"}
    ramp = read_global(info, syms, "ramp", max_elements=4)
    assert ramp == {"length": 100, "head": [1, 2, 0, 0]}
    assert read_global(info, syms, "s_ticks") == 7
    head = read_global(info, syms, "head")
    assert head["pointer"] == syms.variable("sensors").address and head["to"] == "Sensor"


def test_encode_round_trips_and_checks_ranges(elf):
    info, _ = elf
    mode = info.type_named("Mode")
    assert encode(mode, "MODE_FAULT") == (-2).to_bytes(4, "little", signed=True)
    assert decode(mode, encode(mode, 1)) == {"enum": "MODE_RUN", "value": 1}
    f = info.type_named("float")
    assert decode(f, encode(f, 2.5)) == 2.5
    i16 = info.type_named("int16_t")
    assert encode(i16, -1) == b"\xFF\xFF"
    with pytest.raises(ValueError):
        encode(i16, 70000)
    with pytest.raises(TypeError):
        encode(info.type_named("struct Point"), 1)


# --- 関数・スコープ・場所 ---------------------------------------------------------------------------------

def test_functions_and_scopes(elf):
    info, syms = elf
    walk = syms.symbol("walk")
    f = info.function_at(walk.address + 4)
    assert f.name == "walk" and f.low == walk.address and f.decl_file == "dwarf_demo.c"
    assert info.function_at(0x0800_0000) is None
    leaf = syms.symbol("leaf_sum")
    scope = info.scope_at(leaf.address + leaf.size - 4)
    assert [v.name for v in scope.parameters] == ["s", "depth"]
    assert {"total", "corner", "ratio"} <= {v.name for v in scope.locals}
    assert type_name(scope.lookup("s").type) == "const Sensor *"
    assert info.lookup("visits", walk.address + 4).kind == "static"
    assert info.lookup("walk::visits").name == "visits"
    assert info.lookup("s_ticks", walk.address).kind == "static"
    assert info.lookup("sensors", walk.address).kind == "global"
    with pytest.raises(KeyError):
        info.lookup("nothing", walk.address)


def test_o0_locals_live_on_the_stack(o0):
    info, syms = o0
    leaf = syms.symbol("leaf_sum")
    pc = syms.resolve(f"dwarf_demo.c:{line_of('probe_point(total + corner.x);')}")
    scope = info.scope_at(pc)
    regs = {13: 0x2000_7000, 7: 0x2000_7018}
    frame = FrameView(pc, ImageAccess().read_memory, regs.__getitem__)
    places = {v.name: v.place(frame) for v in scope.parameters + scope.locals}
    assert all(isinstance(p, MemoryPlace) and 0x2000_7000 <= p.address < 0x2000_7018 for p in places.values())
    assert places["s"].address == 0x2000_7014 and places["depth"].address == 0x2000_7010   # str r0/r1, [sp, #0x14/#0x10]
    assert leaf.address < pc < leaf.address + leaf.size


def test_o1_location_lists_are_chosen_by_pc():
    for name in ("dwarf_demo_O1.elf", "dwarf_demo_O1_dwarf4.elf"):
        info, syms = DebugInfo(FIRMWARE / name), Symbols(FIRMWARE / name)
        leaf = syms.symbol("leaf_sum")
        depth = info.scope_at(leaf.address + 8).lookup("depth")
        regs = {4: 3}
        at = lambda pc: depth.place(FrameView(pc, ImageAccess().read_memory, regs.__getitem__))   # noqa: E731
        assert at(leaf.address + 4) == RegisterPlace(1)                  # 入口: 第 2 引数は r1
        assert at(leaf.address + 0x30) == RegisterPlace(4)               # mov r4, r1 の後
        assert info.scope_at(leaf.address + 0x30).lookup("i") is None   # for の字句ブロックの外
        i = info.scope_at(leaf.address + 0x18).lookup("i")
        constant = i.place(FrameView(leaf.address + 0x18, ImageAccess().read_memory, regs.__getitem__))
        assert constant == ValuePlace((2).to_bytes(4, "little"))       # ループを畳んだ定数（DW_OP_consts 2）
        s = info.scope_at(leaf.address + 0x50).lookup("s")
        entry = s.place(FrameView(leaf.address + 0x50, ImageAccess().read_memory, regs.__getitem__))
        assert isinstance(entry, Unavailable) and "entry value" in entry.reason
        assert depth.place(None) == Unavailable("この番地では最適化で消えている")


# --- GCC の「extern の宣言 → 定義」（定義の DIE は型を持たず、DW_AT_specification で宣言を指す）---------------

GCC_SPEC = Path(__file__).parent / "data" / "gcc_spec" / "gcc_spec.elf"


def test_a_definition_takes_its_type_from_the_declaration_it_specifies():
    """Zephyr の _kernel と同じ形。定義の DIE に DW_AT_type が無いとき、宣言の型を使う（2026-10-18 に XIAO の ZMK で、
    _kernel が void になり _kernel.cpus[0].current を読めなかった）。"""
    info = DebugInfo(GCC_SPEC)
    var = info.global_variable("g_kernel")
    t = strip(var.type)
    assert isinstance(t, StructType) and t.name == "kernel", type_name(var.type)
    from fprobe.dwarf.cexpr import evaluate
    assert type_name(evaluate("g_kernel.cpus[1].id", info).type) == "int"
