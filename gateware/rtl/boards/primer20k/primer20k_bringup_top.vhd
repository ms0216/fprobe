-- Tang Primer 20K Dock 用 bringup トップ。
--
--   clk27 → rPLL → sys_clk（SDC はこの信号名を参照する）
--   PLL ロックまで reset_sync で sys_clk ドメインをリセットに保つ。
--   LED（アクティブロー）: led(0)=心拍, led(1)=常時点灯, led(2)=UART 受信, led(3)=受信取りこぼし,
--   led(4..5)=消灯
library ieee;
use ieee.std_logic_1164.all;
use work.primer20k_pkg.all;

entity primer20k_bringup_top is
  port (
    clk27   : in  std_logic;
    uart_rx : in  std_logic;
    uart_tx : out std_logic;
    led     : out std_logic_vector(5 downto 0)
  );
end entity;

architecture rtl of primer20k_bringup_top is
  signal sys_clk   : std_logic;
  signal pll_lock  : std_logic;
  signal sys_rst   : std_logic;
  signal heartbeat : std_logic;
  signal activity  : std_logic;
  signal rx_error  : std_logic;
begin
  pll_i : entity work.gowin_rpll
    generic map (FCLKIN_MHZ => "27", DEVICE => "GW2A-18C",
                 IDIV_SEL => PLL_IDIV_SEL, FBDIV_SEL => PLL_FBDIV_SEL, ODIV_SEL => PLL_ODIV_SEL)
    port map (clkin => clk27, clkout => sys_clk, lock => pll_lock);

  reset_i : entity work.reset_sync
    port map (clk => sys_clk, arst_in => not pll_lock, rst => sys_rst);

  core_i : entity work.bringup_core
    generic map (SYS_CLK_HZ => SYS_CLK_HZ, BAUD => 115_200, REVISION => BOARD_REVISION)
    port map (clk => sys_clk, rst => sys_rst, rx => uart_rx, tx => uart_tx,
              heartbeat => heartbeat, activity => activity, rx_error => rx_error);

  led(0) <= not heartbeat;
  led(1) <= '0';
  led(2) <= not activity;
  led(3) <= not rx_error;
  led(5 downto 4) <= (others => '1');
end architecture;
