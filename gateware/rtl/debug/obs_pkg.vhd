-- 対象の FPGA の観測（obs_jtag_engine・obs_record_rx）で共有する定義: スキャンの命令の語と、DR の見出しの語数。
-- 命令の語（16 ビット。ホストが CSR obs.cmd に書く形と同じ）: [3:0] 命令（OBS_OP_*）、[7:4] 番地、[15:8] 引数
library ieee;
use ieee.std_logic_1164.all;

package obs_pkg is

  subtype obs_cmd_t is std_logic_vector(15 downto 0);
  constant OBS_OP_NONE : std_logic_vector(3 downto 0) := x"0";
  constant OBS_OP_READ : std_logic_vector(3 downto 0) := x"2";    -- 番地の語を読む（ID の下位 0・上位 1 など）
  constant OBS_OP_SYNC : std_logic_vector(3 downto 0) := x"3";    -- 時刻合わせ（引数 = 通し番号）
  constant OBS_HEADER_WORDS : natural := 1;                        -- DR の記録の前の見出しの語数

  function obs_cmd(op, addr : std_logic_vector(3 downto 0); arg : std_logic_vector(7 downto 0)) return obs_cmd_t;
  function obs_cmd_op(c : obs_cmd_t) return std_logic_vector;
  function obs_cmd_arg(c : obs_cmd_t) return std_logic_vector;

end package;

package body obs_pkg is

  function obs_cmd(op, addr : std_logic_vector(3 downto 0); arg : std_logic_vector(7 downto 0)) return obs_cmd_t is
  begin
    return arg & addr & op;
  end function;

  function obs_cmd_op(c : obs_cmd_t) return std_logic_vector is
  begin
    return c(3 downto 0);
  end function;

  function obs_cmd_arg(c : obs_cmd_t) return std_logic_vector is
  begin
    return c(15 downto 8);
  end function;

end package body;
