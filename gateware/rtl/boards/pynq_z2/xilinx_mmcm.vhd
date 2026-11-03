-- Xilinx 7 系の MMCME2_BASE の固定分周の包み（gowin_rpll と同じ役目: 入力クロック → 1 本の出力クロック）。
-- 分周の値はボード定数パッケージ（pynq_z2_pkg）から渡す。出力は BUFG を通す。
-- CAP_DIV > 0 なら 2 本目の出力 cap_clkout（VCO / CAP_DIV。sys の整数倍で、同じ VCO から割るので立ち上がりがそろう）も出す。
-- インスタンス名 mmcm_i は、ツール側のクロック制約（fprobe_gw/boards/pynq_z2.py の sys_clock_pin・cap_clock_pin）が参照する。
library ieee;
use ieee.std_logic_1164.all;

entity xilinx_mmcm is
  generic (
    CLKIN_PERIOD_NS : real := 8.0;
    MULT            : real := 8.0;
    DIVCLK          : positive := 1;
    OUT_DIV         : real := 10.0;
    CAP_DIV         : natural := 0          -- 0 = 2 本目の出力を使わない
  );
  port (
    clkin      : in  std_logic;
    clkout     : out std_logic;
    cap_clkout : out std_logic;             -- CAP_DIV = 0 のときは '0'
    lock       : out std_logic
  );
end entity;

architecture xilinx of xilinx_mmcm is
  -- 宣言は Vivado の unisim と同じ名前・口（使う口だけ。GHDL では tests/rtl/sim_xilinx_prims.vhd の模型に結び付く）
  component MMCME2_BASE
    generic (
      CLKFBOUT_MULT_F  : real := 5.0;
      CLKIN1_PERIOD    : real := 0.0;
      CLKOUT0_DIVIDE_F : real := 1.0;
      CLKOUT1_DIVIDE   : integer := 1;
      DIVCLK_DIVIDE    : integer := 1
    );
    port (
      CLKIN1   : in  std_logic;
      CLKFBIN  : in  std_logic;
      RST      : in  std_logic;
      PWRDWN   : in  std_logic;
      CLKFBOUT : out std_logic;
      CLKOUT0  : out std_logic;
      CLKOUT1  : out std_logic;
      LOCKED   : out std_logic
    );
  end component;

  component BUFG
    port (I : in std_logic; O : out std_logic);
  end component;

  signal feedback : std_logic;
  signal unbuffered, cap_unbuffered : std_logic;

  function cap_divide return integer is
  begin
    if CAP_DIV = 0 then
      return 1;
    end if;
    return CAP_DIV;
  end function;
begin
  mmcm_i : MMCME2_BASE
    generic map (CLKFBOUT_MULT_F => MULT, CLKIN1_PERIOD => CLKIN_PERIOD_NS, CLKOUT0_DIVIDE_F => OUT_DIV,
                 CLKOUT1_DIVIDE => cap_divide, DIVCLK_DIVIDE => DIVCLK)
    port map (CLKIN1 => clkin, CLKFBIN => feedback, RST => '0', PWRDWN => '0', CLKFBOUT => feedback,
              CLKOUT0 => unbuffered, CLKOUT1 => cap_unbuffered, LOCKED => lock);

  bufg_i : BUFG port map (I => unbuffered, O => clkout);

  cap_g : if CAP_DIV > 0 generate
    cap_bufg_i : BUFG port map (I => cap_unbuffered, O => cap_clkout);
  end generate;
  no_cap_g : if CAP_DIV = 0 generate
    cap_clkout <= '0';
  end generate;
end architecture;
