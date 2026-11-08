-- 自動生成ファイル。直接編集しないこと（python -m fprobe_gw gen で再生成）。
-- 生成元: gateware/fprobe_gw/caps.py とデザイン定義（fprobe_gw/designs.py）
-- board='PYNQ-Z2' design='fprobe_pynq_fast' sys_clock_hz=100000000
library ieee;
use ieee.std_logic_1164.all;
use work.csr_bus_pkg.all;
use work.build_info_pkg.all;

package design_caps_pkg is
  constant CAPS_ROM : csr_word_array_t(0 to 97) := (
    x"50414346",
    x"00000188",
    x"00040001",
    x"00000002",
    x"00070002",
    x"514E5950",
    x"00325A2D",
    x"00100003",
    x"6F727066",
    x"705F6562",
    x"5F716E79",
    x"74736166",
    x"00040004",
    x"05F5E100",
    x"00040005",
    x"05F5E100",
    x"00040006",
    x"00000003",
    x"00040007",
    x"0000000B",
    x"00040008",
    x"00000010",
    x"00040009",
    x"00020000",
    x"0004000A",
    x"2FAF0800",
    x"00100012",
    x"13121110",
    x"17161514",
    x"31302120",
    x"35343332",
    x"0004000D",
    x"0005658B",
    x"0004000E",
    x"0000001F",
    x"0005000F",
    x"86050402",
    x"00000087",
    x"00020011",
    x"00000104",
    x"00060014",
    x"00040003",
    x"00000001",
    x"00300015",
    x"00018000",
    x"80000180",
    x"01800001",
    x"00018000",
    x"80000180",
    x"01800001",
    x"00008000",
    x"80000080",
    x"01800001",
    x"00018000",
    x"80000180",
    x"01800001",
    x"00040016",
    x"17D78400",
    x"00040017",
    x"0000FCFF",
    x"00780019",
    x"0102010B",
    x"00010025",
    x"01020112",
    x"0008002D",
    x"0000050D",
    x"00000001",
    x"01000113",
    x"00010004",
    x"00000614",
    x"00000004",
    x"00000516",
    x"00000002",
    x"01020115",
    x"0008002D",
    x"01000205",
    x"00010002",
    x"02000206",
    x"0000FFFF",
    x"05000207",
    x"00010002",
    x"05000908",
    x"00010002",
    x"02000A1A",
    x"0000FFFD",
    x"0300011B",
    x"00010005",
    x"02000717",
    x"0000FFFF",
    x"02000818",
    x"0000FFFF",
    x"00140010",
    BUILD_INFO(0),
    BUILD_INFO(1),
    BUILD_INFO(2),
    BUILD_INFO(3),
    BUILD_INFO(4),
    x"00000000"
  );
  constant MEM_TEST : boolean := false;
  constant XTRIG : boolean := true;
  constant TRIG_SEQ : boolean := true;
  constant AUX : boolean := false;
  constant SAMPLER : boolean := true;
  constant SAMPLER_MATCH : boolean := true;
  constant HAS_JTAG : boolean := true;
  constant INJECTOR : boolean := true;
  constant LA_FILTER : boolean := false;
  constant LA_PULSE : boolean := false;
  constant LA_STREAM_EVENTS : boolean := false;
  constant LA_STREAM_GAPS : boolean := false;
  constant LA_RLE_TOKENS : boolean := false;
  constant LA_RLE_REPEAT : boolean := false;
  constant OBS_PORT : boolean := true;
  constant SYNC : boolean := false;
  constant XOUT_COUNT : natural := 2;
  constant LA_INPUTS : natural := 6;
  constant SWO_MANCHESTER : boolean := true;
  constant UART_FLOW : boolean := true;
  constant MEM_ADDR_BITS : natural := 0;
  constant EVENT_RING_BITS : natural := 10;
  constant CAP_RATIO : natural := 4;
  constant LA_LANES : positive := 8;
  constant LA_LANE_ADDR_BITS : positive := 14;
  constant LA_FINE_PINS : boolean := true;
  constant LA_LANE_IN_AGE : natural := 5;
  constant LA_TESTGEN : boolean := true;
  constant EVENT_BYTE_PACK : boolean := true;
  constant SAMPLER_CHECK_MAX : positive := 16;
end package;
