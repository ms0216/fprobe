-- PYNQ-Z2 の P0 の最小の回路（仕様 docs/superpowers/specs/2026-10-19-pynq-z2-port-design.md §9）。
-- LED0 を 125 MHz の発振器（H16）で約 1 Hz に点滅させるだけ。PMOD の端子は口に持たない（使わない端子は Xilinx の既定で
-- 弱い引き下げ = ターゲットの線を動かさない）。PYNQ の標準の回路（base.bit）が PMODA を駆動してターゲットのキーを
-- 押した（2026-10-19）ので、fprobe の回路を書くまでの「安全な状態」としても使う。
library ieee;
use ieee.std_logic_1164.all;
use ieee.numeric_std.all;

entity pynq_z2_blinky_top is
  generic (
    DIV : positive := 62_500_000          -- 反転までの周期の数（125 MHz で 0.5 秒）
  );
  port (
    sys_clk_in : in  std_logic;
    led        : out std_logic_vector(3 downto 0)
  );
end entity;

architecture rtl of pynq_z2_blinky_top is
  signal count : natural range 0 to DIV - 1 := 0;
  signal blink : std_logic := '0';
begin
  process (sys_clk_in)
  begin
    if rising_edge(sys_clk_in) then
      if count = DIV - 1 then
        count <= 0;
        blink <= not blink;
      else
        count <= count + 1;
      end if;
    end if;
  end process;

  led <= "000" & blink;
end architecture;
