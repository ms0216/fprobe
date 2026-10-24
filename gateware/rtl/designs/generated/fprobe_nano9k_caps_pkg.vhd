-- 自動生成ファイル。直接編集しないこと（python -m fprobe_gw gen で再生成）。
-- 生成元: gateware/fprobe_gw/caps.py とデザイン定義（fprobe_gw/designs.py）
-- board='Tang Nano 9K' design='fprobe_nano9k' sys_clock_hz=54000000
library ieee;
use ieee.std_logic_1164.all;
use work.csr_bus_pkg.all;
use work.build_info_pkg.all;

package design_caps_pkg is
  constant CAPS_ROM : csr_word_array_t(0 to 45) := (
    x"50414346",
    x"000000B8",
    x"00040001",
    x"00000002",
    x"000C0002",
    x"676E6154",
    x"6E614E20",
    x"4B39206F",
    x"000D0003",
    x"6F727066",
    x"6E5F6562",
    x"396F6E61",
    x"0000006B",
    x"00040004",
    x"0337F980",
    x"00040005",
    x"0337F980",
    x"00040006",
    x"00000002",
    x"00040007",
    x"0000000B",
    x"00040008",
    x"00000010",
    x"00040009",
    x"00002000",
    x"0004000A",
    x"0337F980",
    x"00100012",
    x"13121110",
    x"17161514",
    x"00002120",
    x"00000000",
    x"0004000D",
    x"00000008",
    x"0004000E",
    x"00000005",
    x"00060014",
    x"00040003",
    x"00000001",
    x"00140010",
    BUILD_INFO(0),
    BUILD_INFO(1),
    BUILD_INFO(2),
    BUILD_INFO(3),
    BUILD_INFO(4),
    x"00000000"
  );
  constant MEM_TEST : boolean := false;
  constant XTRIG : boolean := false;
  constant TRIG_SEQ : boolean := false;
  constant AUX : boolean := false;
  constant SAMPLER : boolean := true;
  constant SAMPLER_MATCH : boolean := false;
  constant HAS_JTAG : boolean := true;
  constant INJECTOR : boolean := false;
  constant LA_FILTER : boolean := false;
  constant LA_PULSE : boolean := false;
  constant LA_STREAM_EVENTS : boolean := false;
  constant OBS_PORT : boolean := false;
  constant SYNC : boolean := false;
  constant XOUT_COUNT : natural := 0;
  constant LA_INPUTS : natural := 0;
  constant SWO_MANCHESTER : boolean := true;
  constant UART_FLOW : boolean := false;
  constant MEM_ADDR_BITS : natural := 0;
  constant EVENT_RING_BITS : natural := 0;
end package;
