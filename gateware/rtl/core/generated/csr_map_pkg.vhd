-- 自動生成ファイル。直接編集しないこと（python -m fprobe_gw gen で再生成）。
-- 生成元: gateware/fprobe_gw/csr/fprobe_map.py, gateware/fprobe_gw/protocol.py
library ieee;
use ieee.std_logic_1164.all;
use work.csr_bus_pkg.all;

package csr_map_pkg is

  -- CONTROL プロトコル
  constant CTRL_PROTOCOL_VERSION : natural := 2;
  constant CTRL_OP_READ          : std_logic_vector(7 downto 0) := x"01";
  constant CTRL_OP_WRITE         : std_logic_vector(7 downto 0) := x"02";
  constant CTRL_OP_EXCHANGE      : std_logic_vector(7 downto 0) := x"03";
  constant CTRL_STATUS_OK         : std_logic_vector(7 downto 0) := x"00";
  constant CTRL_STATUS_BAD_OP     : std_logic_vector(7 downto 0) := x"01";
  constant CTRL_STATUS_BAD_LENGTH : std_logic_vector(7 downto 0) := x"02";
  constant CTRL_STATUS_BUS_ERROR  : std_logic_vector(7 downto 0) := x"03";
  constant CTRL_REQUEST_HEADER_BYTES  : natural := 8;
  constant CTRL_RESPONSE_TRAILER_BYTES : natural := 4;

  -- CSR の窓（csr_decoder の WINDOWS。単位はワード）
  constant CSR_WIN_SYSTEM : natural := 0;
  constant CSR_WIN_TIMEBASE : natural := 1;
  constant CSR_WIN_GPIO : natural := 2;
  constant CSR_WIN_UART : natural := 3;
  constant CSR_WIN_LA : natural := 4;
  constant CSR_WIN_XTRIG : natural := 5;
  constant CSR_WIN_MEM : natural := 6;
  constant CSR_WIN_LA_DEEP : natural := 7;
  constant CSR_WIN_EVENTS : natural := 8;
  constant CSR_WIN_DEBUG : natural := 9;
  constant CSR_WIN_TRIGSEQ : natural := 10;
  constant CSR_WIN_AUX : natural := 11;
  constant CSR_WIN_SAMPLER : natural := 12;
  constant CSR_WIN_XTRIG_OUT : natural := 13;
  constant CSR_WIN_CAPS : natural := 14;
  constant CSR_WIN_LA_MEM : natural := 15;
  constant CSR_WINDOWS : csr_windows_t(0 to 15) := (
    CSR_WIN_SYSTEM => (base => 0, size => 16),
    CSR_WIN_TIMEBASE => (base => 64, size => 16),
    CSR_WIN_GPIO => (base => 128, size => 16),
    CSR_WIN_UART => (base => 192, size => 16),
    CSR_WIN_LA => (base => 256, size => 16),
    CSR_WIN_XTRIG => (base => 320, size => 16),
    CSR_WIN_MEM => (base => 384, size => 16),
    CSR_WIN_LA_DEEP => (base => 448, size => 16),
    CSR_WIN_EVENTS => (base => 512, size => 16),
    CSR_WIN_DEBUG => (base => 576, size => 16),
    CSR_WIN_TRIGSEQ => (base => 640, size => 16),
    CSR_WIN_AUX => (base => 704, size => 16),
    CSR_WIN_SAMPLER => (base => 768, size => 32),
    CSR_WIN_XTRIG_OUT => (base => 832, size => 16),
    CSR_WIN_CAPS => (base => 1024, size => 1024),
    CSR_WIN_LA_MEM => (base => 16384, size => 8192)
  );

end package;
