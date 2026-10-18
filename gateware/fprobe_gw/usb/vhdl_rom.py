"""USB ディスクリプタを VHDL の ROM パッケージ（usb_descriptors_pkg）として出力する。

生成物はリポジトリにコミットし、tests/test_usb_descriptors.py が生成器との一致を検査する。
再生成: `python -m fprobe_gw gen`
"""

from dataclasses import dataclass
from pathlib import Path

from ..rtl import RTL_ROOT
from . import descriptors as d
from .fprobe_device import MSOS20_VENDOR_CODE, FprobeUsbDevice

__all__ = ["COMMITTED_PACKAGE", "endpoint_masks", "interface_endpoint_masks", "render_package",
           "write_package"]

COMMITTED_PACKAGE = RTL_ROOT / "usb" / "generated" / "usb_descriptors_pkg.vhd"

SPEED_ANY, SPEED_HS, SPEED_FS = 0, 1, 2
DTYPE_MSOS20 = 0xFF          # ROM 内の擬似種別: MS OS 2.0 記述子セット（ベンダー要求で返す）


@dataclass(frozen=True)
class _Entry:
    dtype: int
    index: int
    speed: int
    data: bytes


def _entries(device: FprobeUsbDevice) -> list[_Entry]:
    entries = [
        _Entry(d.DEVICE, 0, SPEED_ANY, device.device_descriptor()),
        _Entry(d.CONFIGURATION, 0, SPEED_HS, device.configuration(high_speed=True)),
        _Entry(d.CONFIGURATION, 0, SPEED_FS, device.configuration(high_speed=False)),
        _Entry(d.OTHER_SPEED_CONFIGURATION, 0, SPEED_HS, device.other_speed_configuration(high_speed=True)),
        _Entry(d.OTHER_SPEED_CONFIGURATION, 0, SPEED_FS, device.other_speed_configuration(high_speed=False)),
        _Entry(d.DEVICE_QUALIFIER, 0, SPEED_ANY, device.device_qualifier()),
        _Entry(d.BOS, 0, SPEED_ANY, device.bos()),
        _Entry(DTYPE_MSOS20, 0, SPEED_ANY, device.msos20_set()),
    ]
    entries += [_Entry(d.STRING, i, SPEED_ANY, s) for i, s in enumerate(device.strings())]
    return entries


def interface_endpoint_masks(configuration: bytes) -> list[int]:
    """構成ディスクリプタから、インターフェース番号ごとのエンドポイント番号マスクを作る。"""
    masks: dict[int, int] = {}
    current = None
    for desc in d.split(configuration[9:]):
        if desc[1] == d.INTERFACE:
            current = desc[2]
            masks.setdefault(current, 0)
        elif desc[1] == d.ENDPOINT and current is not None:
            masks[current] |= 1 << (desc[2] & 0x0F)
    return [masks[i] for i in range(len(masks))]


def endpoint_masks(configuration: bytes) -> tuple[int, int]:
    """構成ディスクリプタから、存在する OUT / IN エンドポイント番号のマスクを作る（EP0 を含む）。"""
    out_mask = in_mask = 1
    for desc in d.split(configuration[9:]):
        if desc[1] == d.ENDPOINT:
            if desc[2] & 0x80:
                in_mask |= 1 << (desc[2] & 0x0F)
            else:
                out_mask |= 1 << (desc[2] & 0x0F)
    return out_mask, in_mask


def render_package(device: FprobeUsbDevice) -> str:
    entries = _entries(device)
    configuration = device.configuration(high_speed=True)
    if_masks = interface_endpoint_masks(configuration)
    out_mask, in_mask = endpoint_masks(configuration)
    rom = b"".join(e.data for e in entries)
    offsets, position = [], 0
    for e in entries:
        offsets.append(position)
        position += len(e.data)

    rom_lines = []
    for i in range(0, len(rom), 12):
        chunk = ", ".join(f'x"{b:02X}"' for b in rom[i:i + 12])
        rom_lines.append(f"    {chunk}{',' if i + 12 < len(rom) else ''}")
    entry_lines = [
        f"    ({e.dtype:3d}, {e.index:2d}, {e.speed}, {offset:4d}, {len(e.data):4d})"
        f"{',' if n + 1 < len(entries) else ''}"
        for n, (e, offset) in enumerate(zip(entries, offsets))]

    return "\n".join([
        "-- 自動生成ファイル。直接編集しないこと（python -m fprobe_gw gen で再生成）。",
        "-- 生成元: gateware/fprobe_gw/usb/fprobe_device.py",
        "library ieee;",
        "use ieee.std_logic_1164.all;",
        "",
        "package usb_descriptors_pkg is",
        "",
        f"  constant DESC_ROM_SIZE : positive := {len(rom)};",
        "  type desc_rom_t is array (0 to DESC_ROM_SIZE - 1) of std_logic_vector(7 downto 0);",
        "",
        "  -- 速度の指定: 0 = どちらでも, 1 = HS 時, 2 = FS 時",
        "  type desc_entry_t is record",
        "    dtype  : natural range 0 to 255;   -- ディスクリプタ種別（255 = MS OS 2.0 記述子セット）",
        "    index  : natural range 0 to 255;",
        "    speed  : natural range 0 to 2;",
        "    offset : natural range 0 to DESC_ROM_SIZE - 1;",
        "    length : natural range 0 to 65535;",
        "  end record;",
        "  type desc_entries_t is array (natural range <>) of desc_entry_t;",
        "",
        f"  constant MSOS20_VENDOR_CODE : natural := {MSOS20_VENDOR_CODE};",
        f"  constant DTYPE_MSOS20       : natural := {DTYPE_MSOS20};",
        "",
        "  -- エンドポイント・インターフェース番号（生成元の定義と一致）",
        *[f"  constant {name} : natural := {value};"
          for name, value in device.gateware_constants().items()],
        "",
        "  -- 存在するエンドポイント（ビット n = エンドポイント n、EP0 を含む）",
        f'  constant OUT_ENDPOINTS : std_logic_vector(15 downto 0) := x"{out_mask:04X}";',
        f'  constant IN_ENDPOINTS : std_logic_vector(15 downto 0) := x"{in_mask:04X}";',
        "",
        "  -- インターフェース番号ごとのエンドポイント番号マスク（SET_INTERFACE でトグルを戻す対象）",
        f"  constant NUM_INTERFACES : positive := {len(if_masks)};",
        "  type if_endpoints_t is array (0 to NUM_INTERFACES - 1) of std_logic_vector(15 downto 0);",
        "  constant IF_ENDPOINTS : if_endpoints_t := (",
        *[f'    x"{m:04X}"{"," if i + 1 < len(if_masks) else ""}' for i, m in enumerate(if_masks)],
        "  );",
        "",
        "  constant DESC_ENTRIES : desc_entries_t := (",
        *entry_lines,
        "  );",
        "",
        "  constant DESC_ROM : desc_rom_t := (",
        *rom_lines,
        "  );",
        "",
        "end package;",
        "",
    ])


def write_package(device: FprobeUsbDevice, path: Path = COMMITTED_PACKAGE) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(render_package(device), encoding="utf-8", newline="\n")
    return path
