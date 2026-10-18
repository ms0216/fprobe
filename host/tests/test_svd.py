"""fprobe.svd: CMSIS-SVD の読み込み・値の解読・番地から引く・SVD を探す。"""

import zipfile
from pathlib import Path

import pytest

from fprobe.svd import BUNDLED_DIR, Device, find_svd, load_device

MINI = Path(__file__).resolve().parent / "data" / "mini.svd"


@pytest.fixture(scope="module")
def mini():
    return Device.load(MINI)


@pytest.fixture(scope="module")
def f401():
    return load_device("STM32F401RETx")


# --- 読み込み（手書きの SVD） ---------------------------------------------------------------

def test_device_and_peripherals(mini):
    assert mini.name == "MINI1" and mini.cpu["name"] == "CM4"
    assert [p.name for p in mini.peripherals] == ["TIMER0", "TIMER1", "UART"]
    timer = mini.peripheral("timer0")                                           # 大文字小文字を区別しない
    assert (timer.base_address, timer.group, timer.address_blocks) == (0x40001000, "TIMER", ((0x40001000, 0x400),))


def test_register_defaults_are_inherited(mini):
    ctrl = mini.register("TIMER0.CTRL")
    assert (ctrl.address, ctrl.size, ctrl.access, ctrl.reset_value, ctrl.reset_mask) == (
        0x40001000, 32, "read-write", 0x4, 0xFFFFFFFF)
    status = mini.register("TIMER0.STATUS")
    assert status.access == "read-only" and status.reset_value == 0
    assert [f.access for f in status.fields] == ["read-only", "read-only"]     # フィールドはレジスタの access を継ぐ
    assert mini.register("UART.DR").size == 16                                  # 周辺の size


def test_field_positions_in_all_three_forms(mini):
    fields = {f.name: f for f in mini.register("TIMER0.CTRL").fields}
    assert (fields["EN"].bit_offset, fields["EN"].bit_width) == (0, 1)
    assert (fields["MODE"].bit_offset, fields["MODE"].bit_width, fields["MODE"].mask) == (1, 3, 0b1110)
    assert (fields["PRESCALE"].bit_offset, fields["PRESCALE"].bit_width) == (8, 8)
    assert fields["START"].access == "write-only" and fields["START"].modified_write_values == "oneToSet"


def test_dim_arrays_lists_and_clusters(mini):
    names = [r.name for r in mini.peripheral("TIMER0").registers]
    assert ["CH[0]", "CH[1]", "CH[2]", "CH[3]"] == [n for n in names if n.startswith("CH[")]
    assert mini.register("TIMER0.CH[3]").address == 0x40001000 + 0x10 + 3 * 4
    assert mini.register("TIMER0.CH[2]").reset_value == 0xFFFF0000
    pins = [mini.register(f"TIMER0.PIN{x}_CFG") for x in "ABC"]
    assert [p.address for p in pins] == [0x40001020, 0x40001022, 0x40001024] and pins[0].size == 16
    assert [(f.name, f.bit_offset) for f in pins[1].fields] == [("SEL0", 0), ("SEL1", 4)]
    length = mini.register("TIMER0.BUF[1].LEN")
    assert (length.address, length.size, length.reset_value) == (0x40001000 + 0x40 + 0x10 + 4, 16, 0x10)
    assert mini.register("TIMER0.BUF[0].ADDR").path == "TIMER0.BUF[0].ADDR"


def test_derived_from(mini):
    timer1 = mini.peripheral("TIMER1")
    assert timer1.derived_from == "TIMER0" and timer1.base_address == 0x40002000
    assert timer1.group == "TIMER"
    assert mini.register("TIMER1.CH[1]").address == 0x40002014
    ctrl2 = mini.register("TIMER0.CTRL2")
    assert ctrl2.address == 0x40001004 and [f.name for f in ctrl2.fields] == ["EN", "MODE", "PRESCALE", "START"]
    sr = mini.register("UART.SR")                                                # 別の周辺のレジスタから
    assert sr.address == 0x40003004 and [f.name for f in sr.fields] == ["DONE", "COUNT"] and sr.size == 16
    done = mini.register("TIMER0.STATUS").fields[0]
    assert [e.name for e in done.enums] == ["Off", "On"]                         # enumeratedValues の derivedFrom


# --- 値の解読・組み立て -------------------------------------------------------------------

def test_decode_with_enumerations(mini):
    ctrl = mini.register("TIMER0.CTRL")
    decoded = {fv.field.name: fv for fv in ctrl.decode(0x0000_3405)}             # EN=1, MODE=2, PRESCALE=0x34
    assert (decoded["EN"].value, decoded["EN"].name) == (1, "On")
    assert (decoded["MODE"].value, decoded["MODE"].name) == (2, "Periodic")
    assert (decoded["PRESCALE"].value, decoded["PRESCALE"].name) == (0x34, None)
    mode = next(f for f in ctrl.fields if f.name == "MODE")
    assert mode.name_of(4) == "Capture" and mode.name_of(6) == "Capture"         # #1x0 の x は無関係
    assert mode.name_of(5) == "Reserved"                                         # isDefault
    assert mode.value_of("Periodic") == 2
    assert decoded["MODE"].bits == "[3:1]"


def test_encode_read_modify_write(mini):
    ctrl = mini.register("TIMER0.CTRL")
    assert ctrl.encode(0x0000_3405, {"MODE": "OneShot", "PRESCALE": 0xFF}) == 0x0000_FF01
    assert ctrl.encode(0, {"en": 1}) == 1                                        # フィールド名も大文字小文字を区別しない
    # START は oneToSet: 変えないときは 0 を書き戻す（読んだ 1 をそのまま書くと、また始めてしまう）
    assert ctrl.encode(0x8000_0001, {"EN": 0}) == 0
    assert ctrl.encode(0, {"START": 1}) == 0x8000_0000
    with pytest.raises(ValueError):
        ctrl.encode(0, {"PRESCALE": 0x100})                                      # 幅を超える
    with pytest.raises(KeyError):
        ctrl.encode(0, {"NOPE": 1})
    with pytest.raises(ValueError):
        ctrl.encode(0, {"MODE": "Capture"})                                      # 無関係のビットがあり値が決まらない
    with pytest.raises(ValueError):
        mini.register("TIMER0.STATUS").encode(0, {"COUNT": 1})                   # 読むだけのフィールド


class FakeBus:
    def __init__(self, values):
        self.values, self.log = dict(values), []

    def read(self, address, width):
        self.log.append(("read", address, width))
        return self.values.get(address, 0)

    def write(self, address, width, value):
        self.log.append(("write", address, width, value))
        self.values[address] = value


def test_read_and_write_through_register_access(mini):
    bus = FakeBus({0x40001000: 0x0000_3405, 0x40003000: 0x41})
    value = mini.read_register(bus, "TIMER0.CTRL")
    assert value.value == 0x3405 and value.fields[0].name == "On"
    assert mini.write_fields(bus, "TIMER0.CTRL", {"EN": "Off"}) == 0x3404
    assert bus.log[-2:] == [("read", 0x40001000, 32), ("write", 0x40001000, 32, 0x3404)]
    with pytest.raises(PermissionError):
        mini.read_register(bus, "TIMER0.DATA")                                   # readAction: 読むと消える
    with pytest.raises(PermissionError):
        mini.read_register(bus, "UART.DR")                                       # フィールドの readAction
    assert mini.read_register(bus, "UART.DR", allow_side_effects=True).value == 0x41
    assert bus.log[-1] == ("read", 0x40003000, 16)                               # レジスタの幅で読む
    assert mini.register("TIMER0.DATA").has_read_side_effects


# --- 番地から引く -------------------------------------------------------------------------

def test_find_by_address(mini):
    hit = mini.find(0x40001016)
    assert (hit.register.path, hit.offset) == ("TIMER0.CH[1]", 2)
    assert mini.find(0x40001045).register.path == "TIMER0.BUF[0].LEN"                # 16 ビットの 2 バイト目
    assert mini.find(0x40001046) is None
    assert [r.name for r in mini.registers_at(0x40001060)] == ["ALT_IN", "ALT_OUT"]
    assert mini.find(0x40001300) is None and mini.peripheral_at(0x40001300).name == "TIMER0"
    assert mini.peripheral_at(0x50000000) is None
    assert str(mini.find(0x40002010)) == "TIMER1.CH[0]"


# --- STM32F401（ST の SVD の実物） ---------------------------------------------------------

def test_stm32f401_known_registers(f401):
    assert f401.name == "STM32F401"
    cr = f401.register("RCC.CR")
    assert cr.address == 0x40023800 and cr.reset_value == 0x83
    hseon = next(f for f in cr.fields if f.name == "HSEON")
    assert (hseon.bit_offset, hseon.bit_width) == (16, 1)
    moder = f401.register("GPIOA.MODER")
    assert (moder.address, moder.reset_value) == (0x40020000, 0xA8000000)
    assert {f.name: f.extract(0xA8000000) for f in moder.fields}["MODER15"] == 0b10
    assert f401.register("GPIOC.MODER").address == 0x40020800                   # GPIOH から派生
    assert f401.register("GPIOD.MODER").address == 0x40020C00                   # "0X" の 16 進
    assert f401.find(0x40020014).register.path == "GPIOA.ODR"
    assert f401.find(0x40023830).register.path == "RCC.AHB1ENR"


# --- SVD を探す ---------------------------------------------------------------------------

def test_find_svd_prefers_the_longest_match(tmp_path, monkeypatch):
    monkeypatch.delenv("FPROBE_SVD_PATH", raising=False)
    monkeypatch.setattr(Path, "home", lambda: tmp_path / "home")
    found = find_svd("STM32F401RETx")
    assert found.name == "STM32F401" and found.origin == "bundled" and found.path == BUNDLED_DIR / "STM32F401.svd"
    user = tmp_path / "svd"
    user.mkdir()
    (user / "STM32F40x.svd").write_text(MINI.read_text(encoding="utf-8"), encoding="utf-8")
    (user / "STM32F401RE.svd").write_text(MINI.read_text(encoding="utf-8"), encoding="utf-8")
    monkeypatch.setenv("FPROBE_SVD_PATH", str(user))
    assert find_svd("stm32f401retx").path == user / "STM32F401RE.svd"           # 利用者の場所が先・最も長く合う
    assert find_svd("STM32F407VG").path == user / "STM32F40x.svd"               # x は任意の 1 文字
    assert find_svd("anything", paths=[MINI]).path == MINI                      # 明示のファイルは名前に依らず使う
    assert find_svd("NO_SUCH_PART_12345") is None
    with pytest.raises(FileNotFoundError):
        load_device("NO_SUCH_PART_12345")


def test_find_svd_in_pyocd_builtin_zip(tmp_path, monkeypatch):
    archive = tmp_path / "svd_data.zip"
    with zipfile.ZipFile(archive, "w") as z:
        z.writestr("MINI1.svd", MINI.read_bytes())
    monkeypatch.setattr("fprobe.svd._pyocd_zip", lambda: archive)
    monkeypatch.delenv("FPROBE_SVD_PATH", raising=False)
    monkeypatch.setattr(Path, "home", lambda: tmp_path / "home")
    found = find_svd("MINI1")
    assert (found.origin, found.member) == ("pyocd", "MINI1.svd")
    assert load_device("MINI1").name == "MINI1"
