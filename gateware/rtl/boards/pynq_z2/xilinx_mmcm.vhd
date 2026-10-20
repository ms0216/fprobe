-- Xilinx 7 系の MMCME2_BASE の固定分周の包み（gowin_rpll と同じ役目: 入力クロック → 1 本の出力クロック）。
-- 分周の値はボード定数パッケージ（pynq_z2_pkg）から渡す。出力は BUFG を通す。
-- インスタンス名 mmcm_i は、ツール側のクロック制約（fprobe_gw/boards/pynq_z2.py の sys_clock_pin）が参照する。
library ieee;
use ieee.std_logic_1164.all;

entity xilinx_mmcm is
  generic (
    CLKIN_PERIOD_NS : real := 8.0;
    MULT            : real := 8.0;
    DIVCLK          : positive := 1;
    OUT_DIV         : real := 10.0
  );
  port (
    clkin  : in  std_logic;
    clkout : out std_logic;
    lock   : out std_logic
  );
end entity;

architecture xilinx of xilinx_mmcm is
  -- 宣言は Vivado の unisim と同じ名前・口（使う口だけ。GHDL では tests/rtl/sim_xilinx_prims.vhd の模型に結び付く）
  component MMCME2_BASE
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
  end component;

  component BUFG
    port (I : in std_logic; O : out std_logic);
  end component;

  signal feedback : std_logic;
  signal unbuffered : std_logic;
begin
  mmcm_i : MMCME2_BASE
    generic map (CLKFBOUT_MULT_F => MULT, CLKIN1_PERIOD => CLKIN_PERIOD_NS, CLKOUT0_DIVIDE_F => OUT_DIV,
                 DIVCLK_DIVIDE => DIVCLK)
    port map (CLKIN1 => clkin, CLKFBIN => feedback, RST => '0', PWRDWN => '0', CLKFBOUT => feedback,
              CLKOUT0 => unbuffered, LOCKED => lock);

  bufg_i : BUFG port map (I => unbuffered, O => clkout);
end architecture;
