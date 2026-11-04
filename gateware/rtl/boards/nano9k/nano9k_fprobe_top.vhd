-- Tang Nano 9K 用の fprobe（SerialLink、小さい FPGA 用に機能を減らした構成）。PC とは BL702 の USB-UART 1 本。
-- 仕様: docs/superpowers/specs/2026-10-17-m11-serial-link-design.md
--
--   clk27 → rPLL → sys_clk（54 MHz）→ serial_fprobe（serial_link + fprobe_core。ボード非依存。primer20k_fprobe_serial_top と同じ）。
--   デバッグポートのピンは J5（pmod1、割り当ては nano9k_pkg の DBG_*）。補助マイコンは無い。
--   LED（アクティブロー）: led(0)=PLL ロック, led(4)=sys 心拍, それ以外は消灯
library ieee;
use ieee.std_logic_1164.all;
use work.nano9k_pkg.all;

entity nano9k_fprobe_top is
  port (
    clk27   : in    std_logic;
    uart_tx : out   std_logic;                       -- BL702 へ（PC の COM ポート）
    uart_rx : in    std_logic;
    pmod1   : inout std_logic_vector(7 downto 0);   -- ヘッダ J5 の 5〜12 番
    led     : out   std_logic_vector(5 downto 0)
  );
end entity;

architecture rtl of nano9k_fprobe_top is
  signal sys_clk, pll_lock : std_logic;
begin
  pll_i : entity work.gowin_rpll
    generic map (FCLKIN_MHZ => "27", DEVICE => "GW1NR-9C",
                 IDIV_SEL => PLL_IDIV_SEL, FBDIV_SEL => PLL_FBDIV_SEL, ODIV_SEL => PLL_ODIV_SEL)
    port map (clkin => clk27, clkout => sys_clk, lock => pll_lock);

  fprobe_i : entity work.serial_fprobe
    generic map (SYS_CLK_HZ => SYS_CLK_HZ, SERIAL_BAUD => SERIAL_BAUD,
                 DBG_SWCLK => DBG_SWCLK, DBG_TDI => DBG_TDI, DBG_NRESET => DBG_NRESET, DBG_SWDIO => DBG_SWDIO,
                 DBG_UART_RX => DBG_UART_RX, DBG_SWO => DBG_SWO, DBG_UART_TX => DBG_UART_TX)
    port map (sys_clk => sys_clk, pll_lock => pll_lock, uart_tx => uart_tx, uart_rx => uart_rx, pmod => pmod1,
              led => led);
end architecture;
