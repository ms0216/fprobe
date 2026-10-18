-- USB3317 の RESETB 生成（Primer 20K では F10 を RTL8201F Ethernet PHY と共用）。
--
-- コンフィギュレーション直後に一度だけ PULSE_CYCLES の間 low（リセット）にし、以後は high に保つ。
-- データシート上の最小パルス幅は 1 us。共用先の Ethernet PHY も同時にリセットされるので、
-- 動作中に再びリセットすることはしない（docs/reference/usb2-hs-device-on-ulpi.md §1）。
-- released は解除済みを示す（usb ドメインのリセット解除条件に使う）。
library ieee;
use ieee.std_logic_1164.all;

entity primer20k_ulpi_reset is
  generic (
    PULSE_CYCLES : positive := 128     -- clk27 で約 4.7 us
  );
  port (
    clk27    : in  std_logic;
    resetb   : out std_logic;
    released : out std_logic
  );
end entity;

architecture rtl of primer20k_ulpi_reset is
  signal count : natural range 0 to PULSE_CYCLES := 0;
  signal done  : std_logic := '0';
begin
  process (clk27)
  begin
    if rising_edge(clk27) then
      if count = PULSE_CYCLES then
        done <= '1';
      else
        count <= count + 1;
      end if;
    end if;
  end process;
  resetb   <= done;
  released <= done;
end architecture;
