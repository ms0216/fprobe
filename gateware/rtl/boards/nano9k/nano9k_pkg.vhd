-- Tang Nano 9K のボード定数。
-- ツール側の定義（gateware/fprobe_gw/boards/nano9k.py）と一致していることを tests/test_boards_nano9k.py が検査する。

package nano9k_pkg is

  constant CLK27_HZ : natural := 27_000_000;

  -- rPLL: 27 MHz * 2 / 1 = 54 MHz（VCO 432 MHz, PFD 27 MHz）。GW1NR-9 の論理部の速度に合わせて控えめにする
  constant PLL_IDIV_SEL  : natural := 0;   -- 入力分周比 - 1
  constant PLL_FBDIV_SEL : natural := 1;   -- 帰還分周比 - 1
  constant PLL_ODIV_SEL  : natural := 8;   -- 出力分周比（VCO / CLKOUT）

  constant SYS_CLK_HZ : natural := 54_000_000;

  -- SerialLink（BL702 の USB-UART）のボーレート
  constant SERIAL_BAUD : natural := 2_000_000;

  -- デバッグコネクタ（J5 の 5〜12 番 = pmod1 の 0〜7）の割り当て（Primer 20K と同じ並び）
  constant DBG_SWCLK   : natural := 0;
  constant DBG_TDI     : natural := 1;
  constant DBG_NRESET  : natural := 2;
  constant DBG_SWDIO   : natural := 3;
  constant DBG_SPARE   : natural := 4;
  constant DBG_UART_RX : natural := 5;
  constant DBG_SWO     : natural := 6;
  constant DBG_UART_TX : natural := 7;

end package;
