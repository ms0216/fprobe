-- 自動生成ファイル。直接編集しないこと（python -m fprobe_gw gen で再生成）。
-- 生成元: gateware/fprobe_gw/caps.py とデザイン定義（fprobe_gw/designs.py）
-- board='Tang Primer 20K Dock' design='memtest' sys_clock_hz=100285714
library ieee;
use ieee.std_logic_1164.all;
use work.csr_bus_pkg.all;
use work.build_info_pkg.all;

package design_caps_pkg is
  constant CAPS_ROM : csr_word_array_t(0 to 35) := (
    x"50414346",
    x"00000090",
    x"00040001",
    x"00000002",
    x"00140002",
    x"676E6154",
    x"69725020",
    x"2072656D",
    x"204B3032",
    x"6B636F44",
    x"00070003",
    x"746D656D",
    x"00747365",
    x"00040004",
    x"05FA3D12",
    x"00040005",
    x"05FA3D12",
    x"00040006",
    x"00000001",
    x"00040007",
    x"0000000B",
    x"0004000B",
    x"08000000",
    x"0004000C",
    x"00000001",
    x"0004000D",
    x"0000000B",
    x"0004000E",
    x"00000007",
    x"00140010",
    BUILD_INFO(0),
    BUILD_INFO(1),
    BUILD_INFO(2),
    BUILD_INFO(3),
    BUILD_INFO(4),
    x"00000000"
  );
  constant MEM_TEST : boolean := true;
  constant XTRIG : boolean := true;
  constant TRIG_SEQ : boolean := true;
  constant AUX : boolean := false;
  constant SAMPLER : boolean := true;
  constant HAS_JTAG : boolean := true;
  constant INJECTOR : boolean := true;
  constant LA_FILTER : boolean := false;
  constant LA_PULSE : boolean := false;
  constant LA_STREAM_EVENTS : boolean := false;
  constant XOUT_COUNT : natural := 0;
  constant LA_INPUTS : natural := 0;
  constant SWO_MANCHESTER : boolean := true;
  constant UART_FLOW : boolean := false;
  constant MEM_ADDR_BITS : natural := 23;
  constant EVENT_RING_BITS : natural := 0;
end package;
