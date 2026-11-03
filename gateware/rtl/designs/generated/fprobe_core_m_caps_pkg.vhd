-- 自動生成ファイル。直接編集しないこと（python -m fprobe_gw gen で再生成）。
-- 生成元: gateware/fprobe_gw/caps.py とデザイン定義（fprobe_gw/designs.py）
-- board='Tang Primer 20K Dock' design='fprobe_core_m' sys_clock_hz=100285714
library ieee;
use ieee.std_logic_1164.all;
use work.csr_bus_pkg.all;
use work.build_info_pkg.all;

package design_caps_pkg is
  constant CAPS_ROM : csr_word_array_t(0 to 92) := (
    x"50414346",
    x"00000174",
    x"00040001",
    x"00000002",
    x"00140002",
    x"676E6154",
    x"69725020",
    x"2072656D",
    x"204B3032",
    x"6B636F44",
    x"000D0003",
    x"6F727066",
    x"635F6562",
    x"5F65726F",
    x"0000006D",
    x"00040004",
    x"05FA3D12",
    x"00040005",
    x"05FA3D12",
    x"00040006",
    x"00000001",
    x"00040007",
    x"0000000B",
    x"00040008",
    x"00000010",
    x"00040009",
    x"00000000",
    x"0004000A",
    x"05FA3D12",
    x"00100012",
    x"13121110",
    x"17161514",
    x"00002120",
    x"00000000",
    x"0004000B",
    x"08000000",
    x"0004000D",
    x"0001800D",
    x"0004000E",
    x"00000019",
    x"0005000F",
    x"86850402",
    x"00000007",
    x"00020011",
    x"00000104",
    x"00060014",
    x"00040003",
    x"00000001",
    x"00300015",
    x"00010000",
    x"00000100",
    x"01000001",
    x"00010000",
    x"00000100",
    x"01000001",
    x"00000000",
    x"80800000",
    x"FF8080FF",
    x"80FF8080",
    x"8080FF80",
    x"FF8080FF",
    x"00600019",
    x"01000102",
    x"00010004",
    x"01000103",
    x"00010004",
    x"0100010F",
    x"00010004",
    x"0000050D",
    x"00000001",
    x"01000205",
    x"00010002",
    x"02000206",
    x"00000000",
    x"05000207",
    x"00010002",
    x"05000219",
    x"00010002",
    x"02000A1A",
    x"0000FFFE",
    x"0300011B",
    x"00010004",
    x"02000717",
    x"00000000",
    x"02000818",
    x"00000000",
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
  constant TRIG_SEQ : boolean := false;
  constant AUX : boolean := true;
  constant SAMPLER : boolean := true;
  constant SAMPLER_MATCH : boolean := false;
  constant HAS_JTAG : boolean := true;
  constant INJECTOR : boolean := false;
  constant LA_FILTER : boolean := false;
  constant LA_PULSE : boolean := false;
  constant LA_STREAM_EVENTS : boolean := false;
  constant LA_STREAM_GAPS : boolean := false;
  constant OBS_PORT : boolean := false;
  constant SYNC : boolean := false;
  constant XOUT_COUNT : natural := 2;
  constant LA_INPUTS : natural := 0;
  constant SWO_MANCHESTER : boolean := false;
  constant UART_FLOW : boolean := true;
  constant MEM_ADDR_BITS : natural := 23;
  constant EVENT_RING_BITS : natural := 0;
  constant CAP_RATIO : natural := 1;
  constant LA_LANES : positive := 1;
  constant LA_LANE_ADDR_BITS : positive := 1;
  constant LA_FINE_PINS : boolean := false;
  constant LA_LANE_IN_AGE : natural := 0;
  constant LA_TESTGEN : boolean := false;
  constant EVENT_BYTE_PACK : boolean := false;
  constant SAMPLER_CHECK_MAX : positive := 1;
end package;
