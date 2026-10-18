"""CMSIS-Pack の導入と選択（fprobe.packs）。索引・ダウンロードは cmsis-pack-manager の Cache の偽物で（ネットに出ない）。"""

import os
from types import SimpleNamespace

import pytest

from fprobe.packs import Packs, glob_matcher


class FakeRef(SimpleNamespace):
    def get_pack_name(self):
        return f"{self.vendor}/{self.pack}/{self.version}.pack"


class FakeCache:
    def __init__(self, data_path, index=None):
        self.data_path = str(data_path)
        self.index = dict(index or {})
        self.descriptors = 0
        self.downloaded: list = []

    def cache_descriptors(self):
        self.descriptors += 1
        self.index.setdefault("STM32H743ZITx", {"name": "STM32H743ZITx", "vendor": "STMicroelectronics:13",
                                                "pack": ("Keil", "STM32H7xx_DFP", "4.0.0")})

    def packs_for_devices(self, devices):
        refs = {d["pack"] for d in devices}
        return [FakeRef(vendor=v, pack=p, version=ver) for v, p, ver in sorted(refs)]

    def download_pack_list(self, packs):
        self.downloaded += packs
        for ref in packs:
            path = self.data_path + "/" + ref.get_pack_name()

            os.makedirs(os.path.dirname(path), exist_ok=True)
            open(path, "wb").close()


INDEX = {
    "STM32F407VGTx": {"name": "STM32F407VGTx", "vendor": "STMicroelectronics:13", "pack": ("Keil", "STM32F4xx_DFP", "3.0.0")},
    "STM32F401RETx": {"name": "STM32F401RETx", "vendor": "STMicroelectronics:13", "pack": ("Keil", "STM32F4xx_DFP", "3.0.0")},
    "nRF52840_xxAA": {"name": "nRF52840_xxAA", "vendor": "Nordic Semiconductor:54", "pack": ("NordicSemiconductor", "nRF_DeviceFamilyPack", "8.0.0")},
}


@pytest.fixture
def packs(tmp_path):
    cache = FakeCache(tmp_path, INDEX)
    return Packs(cache_factory=lambda: cache), cache


def test_glob_matcher_is_case_insensitive_and_substring_without_wildcards():
    assert glob_matcher("f401")("STM32F401RETx") and not glob_matcher("f401")("STM32F407VGTx")
    assert glob_matcher("stm32f4*")("STM32F407VGTx") and not glob_matcher("stm32f4*")("xSTM32F4")


def test_find_lists_parts_with_their_pack_and_whether_installed(packs):
    p, cache = packs
    found = p.find("stm32f4")
    assert [f["part"] for f in found] == ["STM32F401RETx", "STM32F407VGTx"]
    assert found[0] == {"part": "STM32F401RETx", "vendor": "STMicroelectronics", "pack": "Keil.STM32F4xx_DFP",
                        "version": "3.0.0", "installed": False}
    assert cache.descriptors == 0                                   # 索引があればネットに出ない


def test_find_fetches_the_index_when_empty_or_asked(tmp_path):
    cache = FakeCache(tmp_path)
    p = Packs(cache_factory=lambda: cache)
    assert [f["part"] for f in p.find("h743")] == ["STM32H743ZITx"] and cache.descriptors == 1
    p.find("h743", update=True)
    assert cache.descriptors == 2


def test_install_downloads_the_packs_of_the_matching_parts(packs):
    p, cache = packs
    result = p.install("STM32F4*")
    assert [r.pack for r in cache.downloaded] == ["STM32F4xx_DFP"]          # 1 つの Pack にまとまる
    assert result["packs"] == [{"pack": "Keil.STM32F4xx_DFP", "version": "3.0.0"}]
    assert result["parts"] == ["STM32F401RETx", "STM32F407VGTx"]
    assert p.find("f401")[0]["installed"] is True
    assert p.installed() == [{"pack": "Keil.STM32F4xx_DFP", "version": "3.0.0",
                              "path": os.path.join(cache.data_path, "Keil/STM32F4xx_DFP/3.0.0.pack")}]
    with pytest.raises(ValueError, match="無い"):
        p.install("no_such_part")


def test_install_refuses_too_many_packs(packs):
    p, _ = packs
    with pytest.raises(ValueError, match="多すぎる"):
        p.install("*", limit=1)


def test_load_registers_the_devices_of_a_pack_file(monkeypatch, tmp_path):
    import fprobe.packs as m
    registered = []
    monkeypatch.setattr(m, "_pack_devices", lambda path: [SimpleNamespace(part_number="ABC123"),
                                                          SimpleNamespace(part_number="ABC124")])
    monkeypatch.setattr(m, "_register_pack", lambda path: registered.append(path))
    pack = tmp_path / "Vendor.ABC_DFP.1.0.0.pack"
    pack.write_bytes(b"PK")
    out = Packs(cache_factory=lambda: None).load(pack)
    assert out == {"path": str(pack), "targets": ["abc123", "abc124"]} and registered == [str(pack)]
    with pytest.raises(FileNotFoundError):
        Packs(cache_factory=lambda: None).load(tmp_path / "missing.pack")
