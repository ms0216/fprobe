"""fprobe.dwarf の型・値・デバッグ情報: 試験用ファーム（host/firmware/dwarf_demo）の ELF と、その初期値（.data）。"""

from pathlib import Path

import pytest
from value_kinds import ints, varray, vbool, venum, vfloat, vint, vptr, vstruct

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


def test_statics_with_the_same_name_are_not_chosen_silently():
    # 選ばない（総点検 ?9）: 外部結合が無く同じ名前の static が複数あれば、最初の 1 つを黙って選ばずに候補を返す。
    # "<ファイル>::<名前>" で選べる。関数の static も、同じ名前の関数が複数あれば同じ
    from dataclasses import replace

    from fprobe.dwarf.info import AmbiguousNameError
    info = DebugInfo(FIRMWARE / "dwarf_demo_O0.elf")
    info._index()
    [mine] = info._globals["s_ticks"]
    other = replace(mine, decl_file="lib/other.c", decl_line=7)
    info._globals["s_ticks"].append(other)
    with pytest.raises(AmbiguousNameError) as e:
        info.global_variable("s_ticks")
    assert [(c["file"], c["line"]) for c in e.value.candidates] == [("dwarf_demo.c", mine.decl_line), ("lib/other.c", 7)]
    assert "dwarf_demo.c::s_ticks" in str(e.value)
    assert info.global_variable("other.c::s_ticks") is other
    assert info.global_variable("dwarf_demo.c::s_ticks") is mine
    [visits] = info._function_statics["walk"]
    twin = replace(visits, decl_file="lib/other.c")
    info._function_statics["walk"].append(twin)
    with pytest.raises(AmbiguousNameError):
        info.global_variable("walk::visits")
    assert info.global_variable("other.c::walk::visits") is twin
    assert info.global_variable("sensors").kind == "global"                 # 1 つだけなら今までどおり
    [sensors] = info._globals["sensors"]
    info._globals["sensors"].append(replace(sensors, decl_file="lib/other.c"))   # 同じ実体（同じ位置式）が別の CU にも
    assert info.global_variable("sensors") is sensors


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
    assert sensors["kind"] == "array" and sensors["length"] == 2
    alpha, beta = (s["members"] for s in sensors["items"])
    assert all(s["kind"] == "struct" for s in sensors["items"])
    assert alpha["name"] == varray(ints(97, 108, 112, 104, 97, 0, 0, 0), string="alpha")
    assert alpha["pos"] == vstruct(x=vint(10), y=vint(-20))
    assert alpha["mode"] == venum("MODE_RUN", 1) and beta["mode"] == venum("MODE_FAULT", -2)
    assert alpha["flags"] == vstruct(ready=vint(1), mode=vint(5), level=vint(-3), wide=vint(0x123), raw=vint(0xAB))
    assert beta["flags"] == vstruct(ready=vint(0), mode=vint(7), level=vint(7), wide=vint(0xFFF), raw=vint(1))
    assert alpha["word"] == vstruct(u32=vint(0x3F800000), bytes=varray(ints(0, 0, 0x80, 0x3F)), f=vfloat(1.0))
    assert alpha["gain"] == vfloat(1.5) and alpha["scale"] == vfloat(-2.25) and beta["scale"] == vfloat(1e10)
    assert alpha["samples"] == varray([varray(ints(1, 2, 3)), varray(ints(-4, -5, -6))])
    assert alpha["next"] == vptr(syms.variable("sensors").address + 80, "struct Sensor")
    assert beta["next"] == vptr(0, "struct Sensor")
    assert alpha["reg"] == vptr(0xE000E010, "const volatile uint32_t")
    assert alpha["raw16"] == vint(0xBEEF) and alpha["pair"] == varray(ints(-17, -66))   # 無名の共用体は外側に展開


def test_scalars_strings_pointers_and_large_arrays(elf):
    info, syms = elf
    assert read_global(info, syms, "negative") == vint(-5)
    assert read_global(info, syms, "big") == vint(0x123456789ABCDEF0)
    assert read_global(info, syms, "enabled") == vbool(True)
    assert read_global(info, syms, "matrix") == varray([varray(ints(0, 1, 2, 3)), varray(ints(10, 11, 12, 13)),
                                                        varray(ints(20, 21, 22, 23))])
    assert read_global(info, syms, "greeting")["string"] == "hello, fprobe"
    message = read_global(info, syms, "message")
    assert message["kind"] == "pointer" and message["string"] == "hello, fprobe"
    assert message["address"] == syms.variable("greeting").address
    assert read_global(info, syms, "callback") == vptr(syms.symbol("square").address | 1, "int (int)", symbol="square")
    ramp = read_global(info, syms, "ramp", max_elements=4)
    assert ramp == varray(ints(1, 2, 0, 0), length=100)                       # 切った: items が length より少ない
    assert read_global(info, syms, "s_ticks") == vint(7)
    head = read_global(info, syms, "head")
    assert head["address"] == syms.variable("sensors").address and head["to"] == "Sensor"


def test_char_pointer_string_says_where_it_stopped_reading(elf):
    """char * の先の文字列を上限で切ったら、切ったことと使った上限を返す。NUL の前で読めなくなったらそれも返す。"""
    info, syms = elf
    mem = ImageAccess.from_symbols(syms)
    var = info.global_variable("message")
    data = mem.read_memory(var.place().address, var.type.size)
    whole = decode(var.type, data, DecodeContext(mem.read_memory))
    assert whole["string"] == "hello, fprobe" and "string_end" not in whole and "string_limit" not in whole
    cut = decode(var.type, data, DecodeContext(mem.read_memory, string_limit=5))
    assert cut["string"] == "hello" and cut["string_end"] == "limit" and cut["string_limit"] == 5
    start = syms.variable("greeting").address

    def short(address, n):                                          # 3 バイトより先は読めない
        if address + n > start + 3:
            raise OSError("読めない")
        return mem.read_memory(address, n)
    broken = decode(var.type, data, DecodeContext(short))
    assert broken["string"] == "hel" and broken["string_end"] == "unreadable"


def test_encode_round_trips_and_checks_ranges(elf):
    info, _ = elf
    mode = info.type_named("Mode")
    assert encode(mode, "MODE_FAULT") == (-2).to_bytes(4, "little", signed=True)
    assert decode(mode, encode(mode, 1)) == venum("MODE_RUN", 1)
    f = info.type_named("float")
    assert decode(f, encode(f, 2.5)) == vfloat(2.5)
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
        assert depth.place(None) == Unavailable("この番地は位置リストの範囲の外（DWARF にこの番地での位置が無い）")


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
