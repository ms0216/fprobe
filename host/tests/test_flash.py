"""Flash の操作の部品（fprobe.flash）: 書く中身（ELF / HEX / BIN）、比較、進捗、消去・書き込み・読み出し、
ページ単位の書き換え（U28 の Flash ブレークポイントが使う）。偽の書き込みアルゴリズムと pyOCD の本物の FlashBuilder。"""

from pathlib import Path

import pytest
from flash_fakes import ALGO_RAM, FLASH, FLASH_SIZE, LARGE, PAGE, RAM, SMALL, FakeChip
from intelhex import IntelHex
from pyocd.core.target import Target

from flm_builder import write_flm

from fprobe.flash import (Block, FlashMemory, Image, Progress, Rate, RegisterGuard, Segment, add_flm_region, compare,
                          remove_region, save_image)
from fprobe.plugins import stm32_flash_clock
from fprobe.symbols import Symbols

RTT_DEMO = Path(__file__).resolve().parents[1] / "firmware" / "rtt_demo" / "rtt_demo.elf"


# --- 書く中身 -----------------------------------------------------------------------------

def test_image_merges_adjacent_segments_and_rejects_overlaps():
    image = Image([Segment(0x100, b"cd"), Segment(0x0FE, b"ab"), Segment(0x200, b"x")])
    assert image.segments == (Segment(0x0FE, b"abcd"), Segment(0x200, b"x"))
    assert image.size == 5 and image.start == 0x0FE and image.end == 0x201
    with pytest.raises(ValueError, match="重な"):
        Image([Segment(0x100, b"abcd"), Segment(0x102, b"z")])


def test_image_from_elf_uses_the_load_addresses():
    image = Image.load(RTT_DEMO)
    assert image.segments and image.format == "elf"
    expected = Image(list(Symbols(RTT_DEMO).segments))
    assert image.segments == expected.segments


def test_image_from_hex_and_bin(tmp_path):
    ih = IntelHex()
    ih.puts(0x08000000, b"\x01\x02\x03")
    ih.puts(0x08000100, b"\x04")
    ih.write_hex_file(str(tmp_path / "a.hex"))
    image = Image.load(tmp_path / "a.hex")
    assert image.format == "hex" and image.segments == (Segment(0x08000000, b"\x01\x02\x03"), Segment(0x08000100, b"\x04"))
    (tmp_path / "a.bin").write_bytes(b"\xAA\xBB")
    assert Image.load(tmp_path / "a.bin", address=0x08001000).segments == (Segment(0x08001000, b"\xAA\xBB"),)
    with pytest.raises(ValueError, match="address"):
        Image.load(tmp_path / "a.bin")


def test_the_format_is_read_from_the_contents_not_the_file_name(tmp_path):
    # 拡張子（名前の慣習）で決めない: 中身の先頭（ELF の印・Intel HEX の ':'）で決め、どちらでもなければ生のバイト列（?19）
    import shutil
    shutil.copy(RTT_DEMO, tmp_path / "firmware.hex")                    # 名前は .hex でも中身は ELF
    assert Image.load(tmp_path / "firmware.hex").format == "elf"
    (tmp_path / "raw.elf").write_bytes(b"\xAA\xBB")
    assert Image.load(tmp_path / "raw.elf", address=0x100).format == "bin"


def test_saved_files_load_back(tmp_path):
    data = bytes(range(256)) * 3
    for name in ("dump.bin", "dump.hex"):
        fmt = save_image(tmp_path / name, 0x08000000, data)
        assert fmt == name.split(".")[1]
        address = 0x08000000 if fmt == "bin" else None
        assert Image.load(tmp_path / name, address=address).segments == (Segment(0x08000000, data),)


# --- 比較・速度・進捗 -----------------------------------------------------------------------

def test_compare_reports_differing_runs():
    expected = Image([Segment(0x1000, b"abcdefgh"), Segment(0x2000, b"xyz")])
    actual = {0x1000: b"abXXefgY", 0x2000: b"xyz"}
    result = compare(expected, lambda address, length: actual[address][:length])
    assert not result.match and result.bytes == 11 and result.differing == 3
    assert [(d.address, d.expected, d.actual) for d in result.differences] == [(0x1002, b"cd", b"XX"), (0x1007, b"h", b"Y")]
    summary = result.as_dict(limit=1)
    assert summary["differences"] == [{"address": 0x1002, "length": 2, "expected": "6364", "actual": "5858"}]
    assert summary["more_differences"] == 1


def test_compare_edges_runs_at_both_ends_and_short_read():
    expected = Image([Segment(0x1000, b"abcd")])
    result = compare(expected, lambda address, length: b"Xbc" + b"Y")
    assert [(d.address, d.expected, d.actual) for d in result.differences] == [(0x1000, b"a", b"X"), (0x1003, b"d", b"Y")]
    assert compare(expected, lambda address, length: b"abcd").match
    with pytest.raises(ValueError, match="短い"):
        compare(expected, lambda address, length: b"abc")


def test_rate_is_bytes_per_second():
    assert Rate(1000, 0.5).as_dict() == {"bytes": 1000, "seconds": 0.5, "bytes_per_s": 2000.0}
    assert Rate(10, 0.0).as_dict()["bytes_per_s"] is None


def test_progress_is_thinned_but_keeps_the_ends():
    sent, now = [], [0.0]
    progress = Progress(sent.append, "program", interval_s=0.1, clock=lambda: now[0])
    cb = progress.callback("erase")
    cb(0.0)
    cb(0.3)                                                   # 間隔より早い: 送らない
    now[0] = 0.2
    cb(0.5)
    cb(1.0)                                                   # 段の終わりは必ず送る
    assert [(m["phase"], m["fraction"]) for m in sent] == [("erase", 0.0), ("erase", 0.5), ("erase", 1.0)]
    assert sent[1]["operation"] == "program" and sent[1]["elapsed_s"] == 0.2
    Progress(None, "x").callback("y")(0.5)                    # 送り先が無くてもよい


# --- 構成 -------------------------------------------------------------------------------

def test_sector_and_page_layout_follows_the_subregions():
    fm = FlashMemory(FakeChip())
    assert fm.sector_at(FLASH + 0x10) == Block(FLASH, SMALL)
    assert fm.sector_at(FLASH + 4 * SMALL + LARGE + 5) == Block(FLASH + 4 * SMALL + LARGE, LARGE)
    assert fm.page_at(FLASH + 0x123) == Block(FLASH + 0x100, PAGE)
    assert fm.sectors(FLASH + 0x3FF, 0x402) == [Block(FLASH, SMALL), Block(FLASH + SMALL, SMALL), Block(FLASH + 2 * SMALL, SMALL)]
    with pytest.raises(ValueError, match="Flash"):
        fm.sector_at(RAM)
    with pytest.raises(ValueError, match="Flash"):
        fm.sectors(FLASH + FLASH_SIZE - 4, 8)                  # Flash の外にはみ出す
    region, = fm.describe()
    assert region["start"] == FLASH and region["size"] == FLASH_SIZE and region["page_size"] == PAGE
    assert region["sectors"] == [{"start": FLASH, "count": 4, "size": SMALL},
                                 {"start": FLASH + 4 * SMALL, "count": 2, "size": LARGE}]
    assert region["algorithm"] == "builtin" and region["boot"] is True and region["erased_value"] == 0xFF


# --- 読む・消す・書く ----------------------------------------------------------------------

def test_read_in_chunks_with_progress():
    chip = FakeChip()
    chip.flash[:] = bytes(i & 0xFF for i in range(FLASH_SIZE))
    sent = []
    data, rate = FlashMemory(chip, read_chunk=0x1000).read(FLASH + 0x10, 0x2800, Progress(sent.append, "read"))
    assert data == bytes(chip.flash[0x10:0x2810]) and rate.bytes == 0x2800 and chip.reads == 3
    assert sent[-1]["fraction"] == 1.0


def test_erase_range_erases_whole_covering_sectors():
    chip = FakeChip()
    chip.flash[:] = b"\x00" * FLASH_SIZE
    report = FlashMemory(chip).erase(FLASH + SMALL + 4, SMALL)                 # 2 つのセクタにかかる
    assert [op for op in chip.ops if op[0] == "erase"] == [("erase", FLASH + SMALL), ("erase", FLASH + 2 * SMALL)]
    assert chip.flash[SMALL:3 * SMALL] == b"\xFF" * 2 * SMALL and chip.flash[:SMALL] == b"\x00" * SMALL
    assert report["sectors"] == 2 and report["rate"]["bytes"] == 2 * SMALL


def test_chip_and_mass_erase():
    chip = FakeChip()
    chip.flash[:] = b"\x00" * FLASH_SIZE
    FlashMemory(chip).erase_chip()
    assert ("erase_all",) in chip.ops and chip.flash == b"\xFF" * FLASH_SIZE
    chip.flash[:] = b"\x00" * FLASH_SIZE
    FlashMemory(chip).mass_erase()
    assert ("mass_erase",) in chip.ops and chip.flash == b"\xFF" * FLASH_SIZE


def test_program_writes_and_reports_what_it_did():
    chip = FakeChip()
    image = Image([Segment(FLASH, bytes(range(256)) * 2), Segment(FLASH + 4 * SMALL, b"\x12\x34")])
    sent = []
    report = FlashMemory(chip).program(image, progress=Progress(sent.append, "program"))
    assert chip.flash[:512] == bytes(range(256)) * 2 and chip.flash[4 * SMALL:4 * SMALL + 2] == b"\x12\x34"
    assert report.bytes == 514 and report.programmed_bytes > 0 and report.erase_method in ("sector", "chip")
    phases = [m["phase"] for m in sent]
    assert "erase" in phases and "program" in phases and sent[-1]["fraction"] == 1.0
    again = FlashMemory(chip).program(image)                    # 同じ中身: smart で書かない
    assert again.programmed_bytes == 0 and again.skipped_bytes >= again.bytes      # 数えるのはページ単位
    d = report.as_dict()
    assert {"bytes", "erase", "program", "skipped_bytes", "erase_method", "seconds"} <= set(d)


def test_program_reports_a_chip_erase():
    chip = FakeChip()
    chip.flash[:] = b"\x00" * FLASH_SIZE
    report = FlashMemory(chip).program(Image([Segment(FLASH, b"\x12")]), erase="chip")
    assert report.erase_method == "chip" and ("erase_all",) in chip.ops
    assert chip.flash[0] == 0x12 and chip.flash[1:] == b"\xFF" * (FLASH_SIZE - 1)       # chip は残りを保たない
    assert FlashMemory(chip).program(Image([Segment(FLASH, b"\x13")]), erase="sector").erase_method == "sector"


def test_program_erase_and_patch_across_separate_regions():
    # pyOCD の内蔵の定義はセクタの大きさごとに別の領域（STM32F4 等）。境界をまたぐ中身はそれぞれの領域で書く
    chip = FakeChip(split=True)
    fm = FlashMemory(chip)
    assert [r["name"] for r in fm.describe()] == ["flash", "flash_1"]
    data = bytes(range(256)) * 2
    report = fm.program(Image([Segment(FLASH + 4 * SMALL - 256, data)]))
    assert chip.flash[4 * SMALL - 256:4 * SMALL + 256] == data and report.bytes == 512
    assert fm.erase(FLASH + 3 * SMALL, SMALL + 1)["sectors"] == 2
    chip.flash[:] = bytes(i & 0xFF for i in range(FLASH_SIZE))
    fm.patch([(FLASH + 4 * SMALL - 1, b"\x00\x00")])                         # 2 つの領域にまたがる 2 バイト
    assert chip.flash[4 * SMALL - 1:4 * SMALL + 1] == b"\x00\x00" and chip.flash[4 * SMALL + 1] == 1


def test_program_refuses_addresses_outside_memory():
    with pytest.raises(ValueError, match="0x30000000"):
        FlashMemory(FakeChip()).program(Image([Segment(0x30000000, b"x")]))


def test_program_writes_ram_segments_directly():
    chip = FakeChip()
    report = FlashMemory(chip).program(Image([Segment(FLASH, b"\x01"), Segment(RAM + 0x2000, b"\x02\x03")]))
    assert chip.ram[0x2000:0x2002] == b"\x02\x03" and report.ram_bytes == 2


def test_verify_compares_the_image_with_the_target():
    chip = FakeChip()
    image = Image([Segment(FLASH, b"\x11\x22\x33")])
    fm = FlashMemory(chip)
    fm.program(image)
    assert fm.verify(image).match
    chip.flash[1] = 0x00
    result = fm.verify(image)
    assert not result.match and result.differences[0].address == FLASH + 1


# --- ページ単位（U28 の Flash ブレークポイント）-----------------------------------------------

def test_patch_rewrites_bytes_and_keeps_the_rest_of_the_sector():
    chip = FakeChip()
    original = bytes((i * 7) & 0xFF for i in range(FLASH_SIZE))
    chip.flash[:] = original
    report = FlashMemory(chip).patch([(FLASH + 0x10, b"\x00\xBE"), (FLASH + SMALL + 0x20, b"\x01\xBE")])
    expected = bytearray(original)
    expected[0x10:0x12] = b"\x00\xBE"
    expected[SMALL + 0x20:SMALL + 0x22] = b"\x01\xBE"
    assert chip.flash == expected                                               # セクタの残りはそのまま
    assert report.bytes == 4


def test_patch_preserves_the_core_context_and_the_ram_the_algorithm_uses():
    # 書き込みアルゴリズムはコアで動き、RAM（命令・バッファ・スタック）とレジスタを使う。デバッグ中の書き換えでは
    # 元に戻す（戻さないと止まっているプログラムの変数とレジスタが壊れる）
    chip = FakeChip(running=True)
    chip.ram[:] = bytes(i & 0xFF for i in range(len(chip.ram)))
    ram_before, regs_before = bytes(chip.ram), dict(chip.regs)
    FlashMemory(chip).patch([(FLASH + 0x40, b"\x00\xBE")])
    assert chip.ram == ram_before and chip.regs == regs_before
    assert chip.state == Target.State.RUNNING                                   # 走っていたら走らせ直す
    assert chip.flash[0x40:0x42] == b"\x00\xBE"


def test_patch_without_preserving_leaves_the_target_halted():
    chip = FakeChip(running=True)
    FlashMemory(chip).patch([(FLASH, b"\x00")], preserve=False)
    assert chip.state == Target.State.HALTED and chip.ram[0] == 0xA5


def test_low_level_sector_erase_and_page_program():
    chip = FakeChip()
    chip.flash[:] = b"\x00" * FLASH_SIZE
    fm = FlashMemory(chip)
    fm.erase_sector(FLASH + SMALL)
    fm.program_page(FLASH + SMALL + PAGE, b"\x5A" * PAGE)
    assert chip.flash[SMALL:SMALL + PAGE] == b"\xFF" * PAGE
    assert chip.flash[SMALL + PAGE:SMALL + 2 * PAGE] == b"\x5A" * PAGE
    with pytest.raises(ValueError, match="ページ"):
        fm.program_page(FLASH + 3, b"\x00")
    with pytest.raises(ValueError, match="セクタ"):
        fm.erase_sector(FLASH + 3)
    assert ALGO_RAM[0] == RAM


# --- 外付けの Flash（.FLM）------------------------------------------------------------------

QSPI = 0x90000000


def test_flm_region_is_added_with_its_sector_layout(tmp_path):
    flm = write_flm(tmp_path / "qspi.FLM", QSPI, 0x20000, 0x100, [(0x1000, 0x0), (0x10000, 0x10000)])
    chip = FakeChip()
    region = add_flm_region(chip, flm, name="qspi")
    assert region.start == QSPI and region.length == 0x20000 and region.flash is not None
    algo = region.flash.flash_algo
    assert RAM <= algo["load_address"] < RAM + 0x4000 and algo["pc_erase_sector"] != 0xFFFFFFFF
    fm = FlashMemory(chip)
    assert fm.sector_at(QSPI + 0x1234) == Block(QSPI + 0x1000, 0x1000)
    assert fm.sector_at(QSPI + 0x12345) == Block(QSPI + 0x10000, 0x10000)
    qspi = next(r for r in fm.describe() if r["name"] == "qspi")
    assert qspi["algorithm"] == f"flm:{flm}" and qspi["sectors"] == [
        {"start": QSPI, "count": 16, "size": 0x1000}, {"start": QSPI + 0x10000, "count": 1, "size": 0x10000}]
    with pytest.raises(ValueError, match="重なる"):
        add_flm_region(chip, flm, name="again")
    remove_region(chip, "qspi")
    assert all(r["name"] != "qspi" for r in fm.describe())
    with pytest.raises(KeyError):
        remove_region(chip, "flash")                                            # 足していない領域は外さない


def test_flm_region_can_be_placed_and_given_its_ram(tmp_path):
    flm = write_flm(tmp_path / "ext.FLM", 0, 0x10000, 0x100, [(0x1000, 0x0)])
    chip = FakeChip()
    region = add_flm_region(chip, flm, address=QSPI, size=0x8000, ram_address=RAM + 0x2000, ram_size=0x2000)
    assert region.start == QSPI and region.length == 0x8000 and region.name == "ext"
    assert RAM + 0x2000 <= region.flash.flash_algo["load_address"] < RAM + 0x4000


def test_algorithm_ram_covers_the_flash_algorithm():
    assert FlashMemory(FakeChip()).algorithm_ram() == [ALGO_RAM]


class Stm32F4Chip(FakeChip):
    """RCC・FLASH のレジスタを持つ STM32F4 の偽物。SYSCLK の切り替え（SW → SWS）はすぐ反映する。走っていたプログラムは
    PLL（84 MHz）・待ち 2 で、書き込みアルゴリズムは FLASH_ACR を 0 にする（Keil の FLM の Init と同じ）。"""
    part_number = "STM32F401xE"
    RCC_CR, RCC_CFGR, FLASH_ACR = 0x40023800, 0x40023808, 0x40023C00

    def __init__(self):
        super().__init__(running=True)
        self.periph = {self.RCC_CR: 0x0303_0083, self.RCC_CFGR: 0x0000_100A, self.FLASH_ACR: 0x702}
        self.clock_during_algorithm = []
        self.acr_writes = []

    def read32(self, address):
        return self.periph[address]

    def write32(self, address, value):
        if address == self.RCC_CFGR:
            value = (value & ~0xC) | ((value & 3) << 2)                  # SWS = SW
        if address == self.FLASH_ACR:
            self.acr_writes.append(value)
        self.periph[address] = value

    def run_algorithm(self):
        self.clock_during_algorithm.append(self.periph[self.RCC_CFGR] >> 2 & 3)
        self.periph[self.FLASH_ACR] = 0                                  # Init が待ちを 0 にする


def f4_guarded(chip) -> FlashMemory:
    """STM32F4 の守り（プラグイン stm32_flash_clock が作るレジスタの手順）を渡した FlashMemory。"""
    return FlashMemory(chip, guards=[RegisterGuard.from_dict(stm32_flash_clock.guard("f4"))])


def test_the_body_touches_no_chip_registers_without_a_guard():
    # 機器固有の守り（STM32F2/F4/F7 のクロック）は本体に入れない（3 回目の点検 ?18）。使う側が渡したときだけ
    chip = Stm32F4Chip()
    before = dict(chip.periph)
    with FlashMemory(chip).preserving([FLASH]):
        chip.run_algorithm()
    assert chip.clock_during_algorithm == [2] and chip.acr_writes == []            # PLL のまま（何もしない）
    assert {k: v for k, v in chip.periph.items() if k != chip.FLASH_ACR} == \
        {k: v for k, v in before.items() if k != chip.FLASH_ACR}


def test_a_register_guard_is_checked():
    with pytest.raises(ValueError):
        RegisterGuard.from_dict({"enter": [{"op": "poke", "address": 0}], "exit": []})
    with pytest.raises(ValueError):
        RegisterGuard.from_dict({"enter": [{"op": "write", "address": 0, "mask": 1}], "exit": []})   # value が無い


def test_stm32f4_runs_the_algorithm_on_hsi_and_restores_the_clock_and_wait_states():
    """Flash のファームが PLL で走っているまま書くと、待ち 0 のまま 84 MHz で走り、書き込みが失敗して LOCKUP した
    （2026-10-18 に実機で）。アルゴリズムの間は HSI にし、抜けるときに待ち → クロックの順に戻す。"""
    chip = Stm32F4Chip()
    with f4_guarded(chip).preserving([FLASH]):
        chip.run_algorithm()
    assert chip.clock_during_algorithm == [0]                            # HSI で動いた
    assert chip.periph[chip.FLASH_ACR] == 0x702 and chip.periph[chip.RCC_CFGR] == 0x100A
    assert chip.state == Target.State.RUNNING


def test_preserving_makes_the_core_fetch_another_flash_line_before_going_back():
    """書いた後、止めた場所（PC）の Flash の行を CPU が古い中身のまま持っていることがある（STM32F4 で、BKPT を戻した後も
    PC に置いたまま再開すると BKPT を実行し続けた。2026-10-18 に実機で）。RAM で 1 命令、別の行へ飛ばしてから戻す。"""
    chip = FakeChip()
    pc = chip.regs["pc"]
    ram_before = bytes(chip.ram)
    with FlashMemory(chip).preserving([FLASH]):
        chip.regs["pc"] = 0x20000001                                    # アルゴリズムが壊す
    fetches = [op[1] for op in chip.ops if op[0] == "fetch"]
    assert len(fetches) == 1 and fetches[0] >> 4 != pc >> 4 and FLASH <= fetches[0] < FLASH + 0x3000
    assert abs(fetches[0] - pc) >= FLASH_SIZE // 2 - 4              # 行の大きさ（16 バイト等）を仮定しない（?20）
    assert chip.regs["pc"] == pc and bytes(chip.ram) == ram_before


def test_stm32f4_resets_the_flash_caches_after_the_algorithm():
    """書いた後に ART の命令・データのキャッシュを捨てる（止めて ICRST・DCRST、戻して元の ACR）。捨てないと、戻した
    Flash の BKPT の古い命令をキャッシュから実行し続けた（2026-10-18 に実機で、休眠の BKPT を戻した後も止まった）。"""
    chip = Stm32F4Chip()
    with f4_guarded(chip).preserving([FLASH]):
        chip.run_algorithm()
    resets = [v for v in chip.acr_writes if v & (1 << 11)]
    assert resets and all(v & (1 << 12) and not v & (3 << 9) for v in resets)   # 止めたまま両方を捨てる
    assert chip.acr_writes[-1] == 0x702


def test_every_algorithm_operation_is_clock_guarded_once():
    """書く・消すのどの口でも（保つ・保たないに関わらず）アルゴリズムの間は HSI。入れ子でも切り替えは 1 回。"""
    chip = Stm32F4Chip()
    fm = f4_guarded(chip)
    algorithm = fm.region_at(FLASH).flash
    original = algorithm.erase_sector
    seen = []

    def erase_sector(address):                                           # アルゴリズムの中で見たクロック
        seen.append(chip.periph[chip.RCC_CFGR] >> 2 & 3)
        chip.periph[chip.FLASH_ACR] = 0
        return original(address)
    algorithm.erase_sector = erase_sector
    fm.erase_sector(FLASH)
    assert seen == [0] and chip.periph[chip.FLASH_ACR] == 0x702 and chip.periph[chip.RCC_CFGR] == 0x100A
    fm.program(Image([Segment(FLASH, b"\x00" * PAGE)]), erase="sector")
    assert chip.periph[chip.FLASH_ACR] == 0x702 and chip.periph[chip.RCC_CFGR] == 0x100A
