-- Tang Primer 20K Dock 用の fprobe（SerialLink 版。M11 の移植性の確認）。PC とは BL702 の USB-UART（COM ポート）1 本。
-- 仕様: docs/superpowers/specs/2026-10-17-m11-serial-link-design.md
--
--   clk27 → rPLL → sys_clk（100 MHz）→ serial_fprobe（serial_link + fprobe_core。ボード非依存）。
--   デバッグポートのピンは J12（pmod1、割り当ては primer20k_pkg の DBG_*）。補助マイコン（Pico）の UART は J14 の 5 番。
--   LED（アクティブロー）: led(0)=PLL ロック, led(4)=sys 心拍, それ以外は消灯
library ieee;
use ieee.std_logic_1164.all;
use work.primer20k_pkg.all;

entity primer20k_fprobe_serial_top is
  port (
    clk27   : in    std_logic;
    uart_tx : out   std_logic;                       -- BL702 へ（PC の COM ポート）
    uart_rx : in    std_logic;
    pmod1   : inout std_logic_vector(7 downto 0);   -- 拡張ヘッダ J12
    aux_rx  : in    std_logic;                       -- 補助マイコン（Pico）の UART（J14 の 5 番）
    led     : out   std_logic_vector(5 downto 0)
  );
end entity;

architecture rtl of primer20k_fprobe_serial_top is
  signal sys_clk, pll_lock : std_logic;
begin
  pll_i : entity work.gowin_rpll
    generic map (FCLKIN_MHZ => "27", DEVICE => "GW2A-18C",
                 IDIV_SEL => PLL_IDIV_SEL, FBDIV_SEL => PLL_FBDIV_SEL, ODIV_SEL => PLL_ODIV_SEL)
    port map (clkin => clk27, clkout => sys_clk, lock => pll_lock);

  fprobe_i : entity work.serial_fprobe
    generic map (SYS_CLK_HZ => SYS_CLK_HZ, SERIAL_BAUD => SERIAL_BAUD,
                 DBG_SWCLK => DBG_SWCLK, DBG_TDI => DBG_TDI, DBG_NRESET => DBG_NRESET, DBG_SWDIO => DBG_SWDIO,
                 DBG_UART_RX => DBG_UART_RX, DBG_SWO => DBG_SWO, DBG_UART_TX => DBG_UART_TX)
    port map (sys_clk => sys_clk, pll_lock => pll_lock, uart_tx => uart_tx, uart_rx => uart_rx, pmod => pmod1,
              aux_rx => aux_rx, led => led);
end architecture;
