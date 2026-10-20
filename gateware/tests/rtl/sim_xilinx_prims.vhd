-- GHDL 用の Xilinx のプリミティブの振る舞いの模型（sim_gowin_prims.vhd と同じ考え。合成には使わない）。
-- MMCME2_BASE: 入力の周期 × DIVCLK × OUT_DIV / MULT の周期のクロックを出し、入力の 16 周期後に LOCKED を立てる。
library ieee;
use ieee.std_logic_1164.all;

entity MMCME2_BASE is
  generic (
    CLKFBOUT_MULT_F  : real := 5.0;
    CLKIN1_PERIOD    : real := 0.0;
    CLKOUT0_DIVIDE_F : real := 1.0;
    DIVCLK_DIVIDE    : integer := 1
  );
  port (
    CLKIN1   : in  std_logic;
    CLKFBIN  : in  std_logic;
    RST      : in  std_logic;
    PWRDWN   : in  std_logic;
    CLKFBOUT : out std_logic;
    CLKOUT0  : out std_logic;
    LOCKED   : out std_logic
  );
end entity;

architecture model of MMCME2_BASE is
  constant HALF : time := CLKIN1_PERIOD * real(DIVCLK_DIVIDE) * CLKOUT0_DIVIDE_F / CLKFBOUT_MULT_F / 2.0 * 1 ns;
  signal clk : std_logic := '0';
  signal edges : natural := 0;
begin
  clk <= not clk after HALF;
  CLKOUT0 <= clk;
  CLKFBOUT <= CLKFBIN;

  process (CLKIN1)
  begin
    if rising_edge(CLKIN1) and edges < 16 then
      edges <= edges + 1;
    end if;
  end process;
  LOCKED <= '1' when edges = 16 and RST = '0' else '0';
end architecture;

library ieee;
use ieee.std_logic_1164.all;

entity BUFG is
  port (I : in std_logic; O : out std_logic);
end entity;

architecture model of BUFG is
begin
  O <= I;
end architecture;
