-- DAP サンプラ（dap_sampler）の部品の間で共有する型: 確認を待つ語の記録。
-- 記録は分散 RAM（dap_sampler_out の buf）に置くので、RAM にはビット列で入れる（to_bits / to_entry）。
library ieee;
use ieee.std_logic_1164.all;

package dap_sampler_pkg is

  -- 確認を待つ語の記録（値を受け取った語。確認が通ったら件にして順に出す）
  type entry_t is record
    index    : std_logic_vector(6 downto 0);     -- 標本の番号（か所 × 16 + 語）
    addr     : std_logic_vector(31 downto 0);
    value    : std_logic_vector(31 downto 0);
    stamp    : std_logic_vector(47 downto 0);    -- 標本の時刻（読みに行った転送の線上の点）
    waits    : std_logic_vector(4 downto 0);     -- WAIT の回数（31 で止める）
    bound    : std_logic;                        -- 値を返した転送が読みに行った転送と別（種類 17 の件も出す）
    rsp_time : std_logic_vector(47 downto 0);    -- 値を返した転送の線上の点
    req      : std_logic_vector(3 downto 0);     -- 値を返した転送の要求
  end record;
  constant ENTRY_BITS : natural := 7 + 32 + 32 + 48 + 5 + 1 + 48 + 4;

  function to_bits(e : entry_t) return std_logic_vector;
  function to_entry(v : std_logic_vector(ENTRY_BITS - 1 downto 0)) return entry_t;

end package;

package body dap_sampler_pkg is

  function to_bits(e : entry_t) return std_logic_vector is
  begin
    return e.index & e.addr & e.value & e.stamp & e.waits & e.bound & e.rsp_time & e.req;
  end function;

  function to_entry(v : std_logic_vector(ENTRY_BITS - 1 downto 0)) return entry_t is
    variable e : entry_t;
  begin
    e.req      := v(3 downto 0);
    e.rsp_time := v(51 downto 4);
    e.bound    := v(52);
    e.waits    := v(57 downto 53);
    e.stamp    := v(105 downto 58);
    e.value    := v(137 downto 106);
    e.addr     := v(169 downto 138);
    e.index    := v(176 downto 170);
    return e;
  end function;

end package body;
