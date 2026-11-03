-- テスト専用: util_pkg の剰余（2^n で折り返す）の差・比べを、組み合わせ回路として出す（cocotb が値を入れて照らす）。
library ieee;
use ieee.std_logic_1164.all;
use ieee.numeric_std.all;
use work.util_pkg.all;

entity util_mod_harness is
  port (
    a       : in  unsigned(31 downto 0);
    b       : in  unsigned(31 downto 0);
    diff    : out unsigned(31 downto 0);   -- mod_diff(a, b)
    le      : out std_logic;               -- mod_le(a, b)
    lt      : out std_logic;               -- mod_lt(a, b)
    a8      : in  unsigned(7 downto 0);
    b8      : in  unsigned(7 downto 0);
    le8     : out std_logic;               -- 幅の違う値でも同じ規則（8 ビット）
    sat_a   : in  unsigned(31 downto 0);
    sat_b   : in  unsigned(31 downto 0);
    sat_sum : out unsigned(31 downto 0)    -- sat_add(sat_a, sat_b)
  );
end entity;

architecture rtl of util_mod_harness is
begin
  diff    <= mod_diff(a, b);
  le      <= '1' when mod_le(a, b) else '0';
  lt      <= '1' when mod_lt(a, b) else '0';
  le8     <= '1' when mod_le(a8, b8) else '0';
  sat_sum <= sat_add(sat_a, sat_b);
end architecture;
