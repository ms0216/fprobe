"""CMSIS-DAP の DAP_Info で返す値（単一の定義元）と、VHDL パッケージ dap_info_pkg の生成。

文字列は USB の定義（usb/fprobe_device.py）と同じものを使う。機能を足したら CAPABILITIES を更新する。
文字列は論理ではなく ROM（BSRAM）に置く（docs/superpowers/specs/2026-10-18-b22-fpga-area-design.md §3.2）:
ROM_SLOTS 枠 × ROM_SLOT_BYTES バイト。枠 k = DAP_Info の ID k。枠の先頭バイトが NUL 込みの長さ（無い ID は 0）、
続いて文字と NUL。
"""

from .usb import fprobe_device as usb

__all__ = ["CAPABILITIES", "CAPABILITY_JTAG", "CAPABILITY_SWO_MANCHESTER", "PACKET_COUNT", "ROM_SLOT_BYTES", "ROM_SLOTS", "STRINGS",
           "capabilities", "render_package", "rom"]

PROTOCOL_VERSION = "2.1.1"
FIRMWARE_VERSION = "0.1.0"
CAPABILITIES = 0x45            # 構成に依らない: bit0 SWD, bit2 SWO（UART）, bit6 SWO のストリーム（エンドポイント）
CAPABILITY_JTAG = 0x02         # bit1 JTAG（構成で変わる: fprobe_core のジェネリック JTAG）
CAPABILITY_SWO_MANCHESTER = 0x08   # bit3 SWO（Manchester）（構成で変わる: fprobe_core のジェネリック HAS_SWO_MANCHESTER）
PACKET_COUNT = 4               # 応答を溜められる数（usb_device の DAP_DEPTH と合わせる）

# DAP_Info の ID → 文字列（NUL 終端を付けて返す）
STRINGS = {
    0x01: usb.STRINGS[usb.STR_MANUFACTURER - 1],
    0x02: usb.STRINGS[usb.STR_PRODUCT - 1],
    0x03: usb.STRINGS[usb.STR_SERIAL - 1],
    0x04: PROTOCOL_VERSION,
    0x09: FIRMWARE_VERSION,
}

ROM_SLOTS = 16                 # 文字列の ID 0x00〜0x0F（0x10 以上に文字列の ID は無い = 長さ 0 で答える）
ROM_SLOT_BYTES = 32            # 長さ 1 バイト + 文字列 + NUL
ADDR_BITS = (ROM_SLOTS * ROM_SLOT_BYTES - 1).bit_length()
assert 1 << ADDR_BITS == ROM_SLOTS * ROM_SLOT_BYTES          # 番地は折り返す（dap_cmd_config）


def capabilities(jtag: bool, swo_manchester: bool = True) -> int:
    """DAP_Info の Capabilities（ID 0xF0）。jtag: 構成に JTAG があるか。swo_manchester: SWO の Manchester 方式があるか。"""
    return CAPABILITIES | (CAPABILITY_JTAG if jtag else 0) | (CAPABILITY_SWO_MANCHESTER if swo_manchester else 0)


def rom() -> bytes:
    """文字列の ROM の内容。"""
    data = bytearray(ROM_SLOTS * ROM_SLOT_BYTES)
    for info_id, text in STRINGS.items():
        if not text.isascii():
            raise ValueError(f"DAP_Info 0x{info_id:02X} {text!r} が ASCII でない")
        if not 0 < info_id < ROM_SLOTS or len(text) + 2 > ROM_SLOT_BYTES:
            raise ValueError(f"DAP_Info 0x{info_id:02X} {text!r} が ROM の枠に収まらない")
        slot = info_id * ROM_SLOT_BYTES
        data[slot] = len(text) + 1
        data[slot + 1:slot + 1 + len(text)] = text.encode()
    return bytes(data)


def render_package() -> str:
    data = rom()
    rows = ",\n".join("    " + ", ".join(f'x"{b:02X}"' for b in data[i:i + 16]) for i in range(0, len(data), 16))
    return f"""-- 自動生成ファイル。直接編集しないこと（python -m fprobe_gw gen で再生成）。
-- 生成元: gateware/fprobe_gw/dap_info.py
library ieee;
use ieee.std_logic_1164.all;

package dap_info_pkg is

  -- DAP_Info の Capabilities（ID 0xF0）。JTAG・SWO の Manchester の bit は構成（ジェネリック）で足す
  constant DAP_CAPABILITIES    : std_logic_vector(7 downto 0) := x"{CAPABILITIES:02X}";
  constant DAP_CAPABILITY_JTAG : std_logic_vector(7 downto 0) := x"{CAPABILITY_JTAG:02X}";
  constant DAP_CAPABILITY_SWO_MANCHESTER : std_logic_vector(7 downto 0) := x"{CAPABILITY_SWO_MANCHESTER:02X}";
  constant DAP_PACKET_COUNT    : natural := {PACKET_COUNT};

  -- 文字列の ROM（同期読み出しで BSRAM になる）: 枠 k（ID k）= 長さ（NUL 込み。無い ID は 0）、文字、NUL
  constant DAP_INFO_SLOTS      : natural := {ROM_SLOTS};
  constant DAP_INFO_SLOT_BYTES : natural := {ROM_SLOT_BYTES};
  constant DAP_INFO_ADDR_BITS  : natural := {ADDR_BITS};
  type dap_info_rom_t is array (0 to {len(data) - 1}) of std_logic_vector(7 downto 0);
  constant DAP_INFO_ROM : dap_info_rom_t := (
{rows}
  );

end package;
"""
