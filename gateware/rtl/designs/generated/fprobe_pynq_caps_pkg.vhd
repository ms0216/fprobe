-- 自動生成ファイル。直接編集しないこと（python -m fprobe_gw gen で再生成）。
-- 生成元: gateware/fprobe_gw/caps.py とデザイン定義（fprobe_gw/designs.py）
-- board='PYNQ-Z2' design='fprobe_pynq' sys_clock_hz=100000000
library ieee;
use ieee.std_logic_1164.all;
use work.csr_bus_pkg.all;
use work.build_info_pkg.all;

package design_caps_pkg is
  constant CAPS_ROM : csr_word_array_t(0 to 47) := (
    x"50414346",
    x"000000C0",
    x"00040001",
    x"00000002",
    x"00070002",
    x"514E5950",
    x"00325A2D",
    x"000B0003",
    x"6F727066",
    x"705F6562",
    x"00716E79",
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
    x"00002000",
    x"0004000A",
    x"05F5E100",
    x"00100012",
    x"13121110",
    x"17161514",
    x"31302120",
    x"35343332",
    x"0004000D",
    x"000005BB",
    x"0004000E",
    x"00000007",
    x"0005000F",
    x"86050402",
    x"00000087",
    x"00020011",
    x"00000104",
    x"00040013",
    x"000F000D",
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
  constant LA_FILTER : boolean := true;
  constant LA_PULSE : boolean := true;
  constant LA_STREAM_EVENTS : boolean := false;
  constant OBS_PORT : boolean := true;
  constant SYNC : boolean := false;
  constant XOUT_COUNT : natural := 2;
  constant LA_INPUTS : natural := 6;
  constant SWO_MANCHESTER : boolean := true;
  constant UART_FLOW : boolean := true;
  constant MEM_ADDR_BITS : natural := 0;
  constant EVENT_RING_BITS : natural := 10;
end package;
