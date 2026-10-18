-- Tang Primer 20K Dock のボード定数。
-- ツール側の定義（gateware/fprobe_gw/boards/primer20k.py）と一致していることを
-- tests/test_boards_primer20k.py が検査する。値を変える場合は両方を変えること。

library ieee;
use ieee.std_logic_1164.all;

package primer20k_pkg is

  constant BOARD_REVISION : natural := 3714;

  constant CLK27_HZ : natural := 27_000_000;

  -- rPLL: 27 MHz * 26 / 7 = 100.2857 MHz（VCO 802.3 MHz, PFD 3.857 MHz）
  constant PLL_IDIV_SEL  : natural := 6;   -- 入力分周比 - 1
  constant PLL_FBDIV_SEL : natural := 25;  -- 帰還分周比 - 1
  constant PLL_ODIV_SEL  : natural := 8;   -- 出力分周比（VCO / CLKOUT）

  constant SYS_CLK_HZ : natural := 100_285_714;

  -- SerialLink（BL702 の USB-UART）のボーレート
  constant SERIAL_BAUD : natural := 2_000_000;

  -- EthernetLink の既定のアドレス（MAC はローカル管理）
  constant ETH_MAC      : std_logic_vector(47 downto 0) := x"026670720001";
  constant ETH_IP       : std_logic_vector(31 downto 0) := x"C0A84D02";   -- 192.168.77.2
  constant ETH_UDP_PORT : natural := 63920;

  -- DDR3 のメモリクロック用 rPLL: 27 MHz * 59 / 4 = 398.25 MHz（VCO 796.5 MHz）。ユーザー側はその 1/4
  constant MEM_PLL_IDIV_SEL  : natural := 3;
  constant MEM_PLL_FBDIV_SEL : natural := 58;
  constant MEM_PLL_ODIV_SEL  : natural := 2;

  -- デバッグコネクタ J12（pmod1）の割り当て（docs/boards/tang-primer-20k-dock.md）
  constant DBG_SWCLK   : natural := 0;
  constant DBG_TDI     : natural := 1;
  constant DBG_NRESET  : natural := 2;
  constant DBG_SWDIO   : natural := 3;
  constant DBG_SPARE   : natural := 4;
  constant DBG_UART_RX : natural := 5;
  constant DBG_SWO     : natural := 6;
  constant DBG_UART_TX : natural := 7;

  -- ロジアナの専用の入力の数（J14 の 7〜12 番。fprobe_gw/boards/primer20k.py の資源 la_in と一致。U22）
  constant LA_INPUT_PINS : natural := 6;

  -- クロストリガの出力の端子の候補 0〜3（J12 の番号。ctrl.pin の番号の順。fprobe_gw/boards/primer20k.py の output_pins と一致）
  type xout_pin_array_t is array (0 to 3) of natural range 0 to 7;
  constant XOUT_PIN_CHOICES : xout_pin_array_t := (4, 5, 6, 7);   -- 予備、UART RX、SWO、UART TX

  -- UART の RTS/CTS の端子の候補 0・1（J12 の番号。uart.flow の rts_pin・cts_pin の番号の順。
  -- fprobe_gw/boards/primer20k.py の uart_flow_pins と一致）
  type uart_flow_pin_array_t is array (0 to 1) of natural range 0 to 7;
  constant UART_FLOW_PIN_CHOICES : uart_flow_pin_array_t := (4, 1);   -- 予備、TDI

end package;
