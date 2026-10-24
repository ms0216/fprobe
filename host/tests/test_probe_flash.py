"""Probe の Flash の道具（fprobe.probe_flash）: 書き込み（検証・進捗・速度）、消去、検証・ファイルとの比較、読み出して
保存、領域とアルゴリズム（.FLM）、速度の計測、CMSIS-Pack。偽のターゲット（flash_fakes）。"""

from types import SimpleNamespace

import pytest
from flash_fakes import FLASH, FLASH_SIZE, LARGE, RAM, SMALL, FakeChip
from flm_builder import write_flm
from intelhex import IntelHex
from pyocd.core.target import Target
from test_packs import INDEX, FakeCache

from fprobe.mcp_server import Probe
from fprobe.packs import Packs
from probe_fakes import FakeDevice

QSPI = 0x90000000


@pytest.fixture
def probe(tmp_path, monkeypatch):
    monkeypatch.setenv("FPROBE_HOME", str(tmp_path))
    p = Probe("unused")
    p._session = SimpleNamespace(device=FakeDevice())
    yield p
    p._stop_observing()


def connect(probe, chip: FakeChip | None = None) -> FakeChip:
    chip = chip or FakeChip()
    probe._debug = SimpleNamespace(target=chip, options={"frequency": 4_000_000}, close=lambda: None)
    return chip


def hex_file(path, address, data):
    ih = IntelHex()
    ih.puts(address, data)
    ih.write_hex_file(str(path))
    return str(path)


# --- 書き込み ------------------------------------------------------------------------------

def test_flash_programs_verifies_reports_speed_and_runs(probe, tmp_path):
    chip = connect(probe)
    sent = []
    probe.attach_publisher(lambda topic, data: sent.append((topic, data)))
    data = bytes(range(256)) * 8
    out = probe.flash(hex_file(tmp_path / "fw.hex", FLASH, data))
    assert chip.flash[:len(data)] == data
    assert out["verify"]["match"] is True and out["program"]["bytes"] == len(data)
    assert out["program"]["program"]["bytes_per_s"] is not None and out["seconds"] > 0
    assert out["image"]["format"] == "hex" and out["state"] == "RUNNING" and ("reset",) in chip.ops
    topics = {t for t, _ in sent}
    assert topics == {"flash.progress"}
    phases = [d["phase"] for _, d in sent]
    assert phases[0] == "erase" and "program" in phases and phases[-1] == "verify" and sent[-1][1]["fraction"] == 1.0


def test_an_algorithm_guard_is_given_by_the_caller(probe):
    # 書き込みアルゴリズムの間のレジスタの手順（機器固有の守り）は使う側が渡す（プラグイン stm32_flash_clock 等）
    from fprobe.plugins import stm32_flash_clock
    assert probe.flash_algorithm_guard_get() == {"guard": None}
    guard = stm32_flash_clock.guard("f4")
    assert probe.flash_algorithm_guard_set(guard) == {"guard": guard}
    connect(probe)
    assert len(probe._flash_memory().guards) == 1
    assert probe.flash_algorithm_guard_set(None) == {"guard": None} and probe._flash_memory().guards == []
    import pytest
    with pytest.raises(ValueError):
        probe.flash_algorithm_guard_set({"enter": [{"op": "poke"}], "exit": []})


def test_flash_can_leave_the_target_halted_and_skip_verify(probe, tmp_path):
    chip = connect(probe)
    (tmp_path / "fw.bin").write_bytes(b"\x01\x02")
    out = probe.flash(str(tmp_path / "fw.bin"), address=FLASH + SMALL, verify=False, reset=False)
    assert out["verify"] is None and out["state"] == "HALTED" and ("reset",) not in chip.ops
    assert chip.flash[SMALL:SMALL + 2] == b"\x01\x02"


def test_flash_fails_loudly_when_verify_finds_differences(probe, tmp_path):
    chip = connect(probe)
    chip.stuck_mask = 0xFE                                       # 最下位ビットが 1 にならない Flash
    with pytest.raises(RuntimeError, match="検証で違い.*0x08000001"):
        probe.flash(hex_file(tmp_path / "fw.hex", FLASH, b"\x00\x01\x02\x03"))


def test_flash_rejects_unknown_erase_mode(probe, tmp_path):
    connect(probe)
    with pytest.raises(ValueError, match="erase"):
        probe.flash(hex_file(tmp_path / "fw.hex", FLASH, b"\x00"), erase="everything")


# --- 消去・検証・読み出し ---------------------------------------------------------------------

def test_erase_a_range_the_chip_or_mass(probe):
    chip = connect(probe)
    chip.flash[:] = b"\x00" * FLASH_SIZE
    out = probe.flash_erase(address=FLASH + 4 * SMALL, length=1)
    assert out["sectors"] == 1 and out["start"] == FLASH + 4 * SMALL and out["end"] == FLASH + 4 * SMALL + LARGE
    assert chip.flash[4 * SMALL:4 * SMALL + LARGE] == b"\xFF" * LARGE and chip.flash[0] == 0
    assert probe.flash_erase(mode="chip")["method"] == "chip" and chip.flash == b"\xFF" * FLASH_SIZE
    assert probe.flash_erase(mode="mass")["method"] == "mass" and ("mass_erase",) in chip.ops
    with pytest.raises(ValueError, match="address と length"):
        probe.flash_erase()
    with pytest.raises(ValueError, match="mode"):
        probe.flash_erase(mode="all")


def test_verify_compares_a_file_without_writing(probe, tmp_path):
    chip = connect(probe)
    chip.flash[:4] = b"\x00\x01\x02\x03"
    path = hex_file(tmp_path / "fw.hex", FLASH, b"\x00\x01\x09\x03")
    out = probe.flash_verify(path)
    assert out["match"] is False and out["differing_bytes"] == 1
    assert out["differences"] == [{"address": FLASH + 2, "length": 1, "expected": "09", "actual": "02"}]
    assert out["image"]["source"] == path and not [op for op in chip.ops if op[0] in ("erase", "program")]


def test_read_saves_to_a_file_with_checksum_and_speed(probe, tmp_path):
    chip = connect(probe)
    chip.flash[:] = bytes(i & 0xFF for i in range(FLASH_SIZE))
    out = probe.flash_read(str(tmp_path / "dump.bin"))                     # 省略時は起動する Flash の全体
    assert out["address"] == FLASH and out["bytes"] == FLASH_SIZE and out["format"] == "bin"
    assert (tmp_path / "dump.bin").read_bytes() == chip.flash and len(out["crc32"]) == 8
    assert out["read"]["bytes_per_s"] is not None
    part = probe.flash_read(str(tmp_path / "part.hex"), address=FLASH + 0x10, length=0x20)
    assert part["format"] == "hex" and probe.flash_verify(str(tmp_path / "part.hex"))["match"]
    assert probe.flash_verify(str(tmp_path / "dump.bin"), address=FLASH)["match"]


def test_read_defaults_to_the_whole_contiguous_flash_across_regions(probe, tmp_path):
    chip = connect(probe, FakeChip(split=True))                               # セクタの大きさごとの領域
    out = probe.flash_read(str(tmp_path / "all.bin"))
    assert out["address"] == FLASH and out["bytes"] == FLASH_SIZE and (tmp_path / "all.bin").read_bytes() == chip.flash


# --- 領域・外付けのアルゴリズム -----------------------------------------------------------------

def test_regions_and_an_external_algorithm_that_survives_reconnecting(probe, tmp_path):
    connect(probe)
    flm = write_flm(tmp_path / "qspi.FLM", QSPI, 0x10000, 0x100, [(0x1000, 0)])
    added = probe.flash_algo_add(str(flm), name="qspi")
    assert added["start"] == QSPI and added["algorithm"] == f"flm:{flm}"
    names = [r["name"] for r in probe.flash_regions()["regions"]]
    assert names == ["flash", "qspi"]
    assert probe.flash_regions()["algorithms"] == [{"name": "qspi", "flm": str(flm), "address": None, "size": None,
                                                    "ram_address": None, "ram_size": None}]
    connect(probe)                                                           # 接続し直す（pyOCD の新しいターゲット）
    assert [r["name"] for r in probe.flash_regions()["regions"]] == ["flash", "qspi"]
    probe.flash_algo_remove("qspi")
    assert [r["name"] for r in probe.flash_regions()["regions"]] == ["flash"]
    assert probe.flash_regions()["algorithms"] == []
    with pytest.raises(KeyError):
        probe.flash_algo_remove("qspi")


def test_algorithms_can_be_registered_before_connecting(probe, tmp_path):
    flm = write_flm(tmp_path / "qspi.FLM", QSPI, 0x10000, 0x100, [(0x1000, 0)])
    assert probe.flash_algo_add(str(flm), name="qspi") == {"name": "qspi", "pending": True}
    connect(probe)
    assert "qspi" in [r["name"] for r in probe.flash_regions()["regions"]]


# --- 速度の計測 ---------------------------------------------------------------------------

def test_benchmark_measures_ram_and_flash_reads_and_restores_ram(probe):
    chip = connect(probe, FakeChip(running=True))
    chip.ram[:] = bytes(i * 3 & 0xFF for i in range(len(chip.ram)))
    before = bytes(chip.ram)
    out = probe.flash_benchmark(ram_length=0x1000, read_length=0x2000)
    assert out["frequency_hz"] == 4_000_000
    assert out["ram"]["address"] == RAM and out["ram"]["write"]["bytes"] == 0x1000 and out["ram"]["verified"] is True
    assert out["flash_read"]["bytes"] == 0x2000 and out["flash_program"] is None
    assert chip.ram == before and chip.state == Target.State.RUNNING           # 中身と走る状態を戻す


def test_benchmark_programming_restores_the_sector_and_the_program_state(probe):
    chip = connect(probe, FakeChip(running=True))
    original = bytes((i * 5) & 0xFF for i in range(FLASH_SIZE))
    chip.flash[:] = original
    chip.ram[:] = b"\x11" * len(chip.ram)
    regs = dict(chip.regs)
    sector = FLASH + 4 * SMALL + LARGE                                         # 最後のセクタ
    out = probe.flash_benchmark(ram_length=0, read_length=0, program_address=sector, program_length=LARGE)
    prog = out["flash_program"]
    assert prog["address"] == sector and prog["verified"] is True and prog["restored"] is True
    assert prog["erase"]["bytes"] == LARGE and prog["program"]["bytes"] == LARGE
    assert chip.flash == original and chip.ram == b"\x11" * len(chip.ram) and chip.regs == regs
    assert chip.state == Target.State.RUNNING
    with pytest.raises(ValueError, match="セクタ"):
        probe.flash_benchmark(program_address=sector + 4, program_length=LARGE - 4)


# --- CMSIS-Pack -------------------------------------------------------------------------

def test_packs_are_found_installed_and_listed(probe, tmp_path):
    cache = FakeCache(tmp_path, INDEX)
    probe._packs = Packs(cache_factory=lambda: cache)
    assert [f["part"] for f in probe.pack_find("f401")["parts"]] == ["STM32F401RETx"]
    out = probe.pack_install("STM32F401RETx")
    assert out["packs"] == [{"pack": "Keil.STM32F4xx_DFP", "version": "3.0.0"}]
    assert out["targets"] == ["stm32f401retx"]                                  # target_connect に渡す名前
    assert [p["pack"] for p in probe.pack_list()["packs"]] == ["Keil.STM32F4xx_DFP"]
