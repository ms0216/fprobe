"""CMSIS の書き込みアルゴリズム（.FLM）で足す外付けの Flash の領域（QSPI NOR 等）と、領域のアルゴリズムの出どころ。

pyOCD のメモリマップを書き換えるだけで、機器には触れない。fprobe.flash が再公開する。
"""

from pathlib import Path

FLM_ATTRIBUTE = "fprobe_flm"   # add_flm_region で足した領域の印（FlashRegion.attributes）


def _algorithm_source(region) -> str:
    flm = region.attributes.get(FLM_ATTRIBUTE)
    if flm:
        return f"flm:{flm}"
    if getattr(region, "flm", None) is not None:
        return "pack"
    return "builtin"


# --- 外付けの Flash（CMSIS の .FLM）----------------------------------------------------------

def add_flm_region(target, flm: str | Path, address: int | None = None, size: int | None = None,
                   ram_address: int | None = None, ram_size: int | None = None, name: str | None = None):
    """CMSIS の書き込みアルゴリズム（.FLM。Keil・メーカーの Pack に入っている形式）で Flash の領域をターゲットに足す。
    address・size は省略時に FLM の FlashDevice の値。アルゴリズムを置く RAM は ram_address（省略時は既定の RAM）。
    足した領域は接続し直すと消える（呼び手が覚えて足し直す）。"""
    from pyocd.core.memory_map import FlashRegion
    from pyocd.flash.flash import Flash
    from pyocd.target.pack.flash_algo import PackFlashAlgo
    from pyocd.target.pack.flm_region_builder import FlmFlashRegionBuilder

    path = Path(flm)
    algo = PackFlashAlgo(str(path))
    start = algo.flash_start if address is None else address
    length = algo.flash_size if size is None else size
    if length <= 0:
        raise ValueError(f"{path.name}: Flash の大きさが分からない（size を指定）")
    # 置き場所を変えたとき: セクタの並び（FLM では先頭からの相対）もそこから数える。アルゴリズムには実際の番地が
    # 渡る（Keil の Flash Download の Start / Size を変えたときと同じ）
    algo.flash_start, algo.flash_size = start, length
    for existing in target.memory_map.regions:
        if existing.intersects_range(start=start, length=length):
            raise ValueError(f"0x{start:08X}+0x{length:X} は既存の領域 {existing.name} と重なる")
    region = FlashRegion(name=name or path.stem, start=start, length=length, flm=algo, flash_class=Flash,
                         page_size=algo.page_size, erased_byte_value=algo.flash_info.value_empty)
    region.attributes[FLM_ATTRIBUTE] = str(path)
    if ram_address is not None:
        region._RAMstart = ram_address
        if ram_size is not None:
            region._RAMsize = ram_size
    if not FlmFlashRegionBuilder(target, target.memory_map).finalise_region(region) or region.algo is None:
        raise RuntimeError(f"{path.name} を書き込みアルゴリズムにできない（pyOCD のログを確認）")
    flash = Flash(target, region.algo)
    flash.region = region
    region.flash = flash
    target.memory_map.add_region(region)
    _update_core_maps(target)
    return region


def remove_region(target, name: str) -> None:
    """add_flm_region で足した領域を外す。"""
    for region in target.memory_map.regions:
        if region.name == name and region.attributes.get(FLM_ATTRIBUTE):
            target.memory_map.remove_region(region)
            _update_core_maps(target)
            return
    raise KeyError(f"足した領域 {name!r} は無い")


def _update_core_maps(target) -> None:
    """コアごとのメモリマップを作り直す（pyOCD の CoreSightTarget.create_flash の終わりと同じ）。"""
    from pyocd.core.memory_map import MemoryMap
    for core in getattr(target, "cores", {}).values():
        core.memory_map = MemoryMap([m for m in target.memory_map
                                     if m.attributes.get("pname") in (None, core.node_name)])
