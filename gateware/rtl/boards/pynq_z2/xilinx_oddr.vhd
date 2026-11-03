-- 端子の出力の ODDR（Xilinx 7 系 ODDR、SAME_EDGE）の包み。ベンダーの部品はここだけ。
-- 仕様: docs/superpowers/specs/2026-11-03-dap-line-rate-design.md §3（JTAG の half_period = 1 で TMS を TCK の立ち下がりの半周期後に
-- 変える）。
--
-- clk の立ち上がり w で d1・d2 を取り、端子は w から clk の前半（High）に d1、後半（Low）に d2 を出す（UG471 v1.10 p.130
-- 「SAME_EDGE: data is presented to the ODDR on the same clock edge」）。d1 = d2 なら端子の直前の 1 段の FF（IOB）と同じ
-- （回路の中で決めた周期の 1 周期後に端子に出る）。ODDR は OLOGIC の中にあるので、端子までの遅れは配置で変わらない。
library ieee;
use ieee.std_logic_1164.all;

entity xilinx_oddr is
  generic (
    WIDTH : positive := 8
  );
  port (
    clk : in  std_logic;
    d1  : in  std_logic_vector(WIDTH - 1 downto 0);   -- 次の周期の前半（clk = 1）の値
    d2  : in  std_logic_vector(WIDTH - 1 downto 0);   -- 次の周期の後半（clk = 0）の値
    q   : out std_logic_vector(WIDTH - 1 downto 0)    -- 端子へ（OBUF / OBUFT の I）
  );
end entity;

architecture xilinx of xilinx_oddr is
  -- 宣言は Vivado の unisim と同じ名前・口（使う口だけ。GHDL では tests/rtl/sim_xilinx_prims.vhd の模型に結び付く）
  component ODDR
    generic (
      DDR_CLK_EDGE : string := "OPPOSITE_EDGE";
      INIT         : bit := '0';
      SRTYPE       : string := "SYNC"
    );
    port (
      Q  : out std_logic;
      C  : in  std_logic;
      CE : in  std_logic;
      D1 : in  std_logic;
      D2 : in  std_logic;
      R  : in  std_logic;
      S  : in  std_logic
    );
  end component;
begin
  bits : for i in 0 to WIDTH - 1 generate
    oddr_i : ODDR
      generic map (DDR_CLK_EDGE => "SAME_EDGE", INIT => '0', SRTYPE => "SYNC")
      port map (Q => q(i), C => clk, CE => '1', D1 => d1(i), D2 => d2(i), R => '0', S => '0');
  end generate;
end architecture;
