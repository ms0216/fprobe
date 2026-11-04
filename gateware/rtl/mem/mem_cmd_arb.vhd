-- メモリの口（mem_port の cmd）を 2 つの要求元で分け合う。両方が求めていれば交互に通す。
--
-- どちらに通すか（sel）は 1 周期前に決めてレジスタに持つ（128 ビットの切り替えの選択を短い経路にする）。次の周期に通す側:
-- 片方だけが求めていればそちら、両方なら今と反対（受理されたら交代）。通す側が求めていなければその周期は空く
-- （la_deep_buffer では読みの帯域は PC へのリンクで抑えられ、書きの口の 2 周期に 1 拍に比べて小さい）。
-- 選ばれていない側は待つ（a_ready / b_ready = '0'）。読み出しの結果（rd_valid / rd_data）は分けない（呼ぶ側が渡す）。
library ieee;
use ieee.std_logic_1164.all;
use ieee.numeric_std.all;

entity mem_cmd_arb is
  generic (
    ADDR_BITS : positive
  );
  port (
    clk       : in  std_logic;
    rst       : in  std_logic;
    a_valid   : in  std_logic;
    a_ready   : out std_logic;
    a_write   : in  std_logic;
    a_addr    : in  unsigned(ADDR_BITS - 1 downto 0);
    a_data    : in  std_logic_vector(127 downto 0);
    b_valid   : in  std_logic;
    b_ready   : out std_logic;
    b_write   : in  std_logic;
    b_addr    : in  unsigned(ADDR_BITS - 1 downto 0);
    b_data    : in  std_logic_vector(127 downto 0);
    cmd_valid : out std_logic;
    cmd_ready : in  std_logic;
    cmd_write : out std_logic;
    cmd_addr  : out unsigned(ADDR_BITS - 1 downto 0);
    cmd_data  : out std_logic_vector(127 downto 0)
  );
end entity;

architecture rtl of mem_cmd_arb is
  signal sel : std_logic := '0';                                -- '1' = a を通す
begin
  cmd_valid <= (a_valid and sel) or (b_valid and not sel);
  cmd_write <= a_write when sel = '1' else b_write;
  cmd_addr  <= a_addr when sel = '1' else b_addr;
  cmd_data  <= a_data when sel = '1' else b_data;
  a_ready   <= cmd_ready and sel;
  b_ready   <= cmd_ready and not sel;

  process (clk)
  begin
    if rising_edge(clk) then
      if rst = '1' then
        sel <= '0';
      elsif a_valid = '1' and b_valid = '0' then
        sel <= '1';
      elsif b_valid = '1' and a_valid = '0' then
        sel <= '0';
      elsif a_valid = '1' and b_valid = '1' and cmd_ready = '1' then
        sel <= not sel;
      end if;
    end if;
  end process;
end architecture;
