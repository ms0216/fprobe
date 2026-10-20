-- PYNQ-Z2 のボード定数。
-- ツール側の定義（gateware/fprobe_gw/boards/pynq_z2.py）と一致していることを
-- tests/test_boards_pynq_z2.py が検査する。値を変える場合は両方を変えること。

library ieee;
use ieee.std_logic_1164.all;

package pynq_z2_pkg is

  constant CLK125_HZ : natural := 125_000_000;

  -- MMCME2: 125 MHz * 8 / 1 = 1000 MHz（VCO）、/ 10 = 100 MHz
  constant MMCM_CLKIN_PERIOD_NS : real := 8.0;
  constant MMCM_MULT : real := 8.0;
  constant MMCM_DIVCLK : positive := 1;
  constant MMCM_OUT_DIV : real := 10.0;

  constant SYS_CLK_HZ : natural := 100_000_000;

  -- デバッグの線（PMOD JA、pmod1）の割り当て。Primer の J12 と同じ並び
  constant DBG_SWCLK   : natural := 0;
  constant DBG_TDI     : natural := 1;
  constant DBG_NRESET  : natural := 2;
  constant DBG_SWDIO   : natural := 3;
  constant DBG_SPARE   : natural := 4;
  constant DBG_UART_RX : natural := 5;
  constant DBG_SWO     : natural := 6;
  constant DBG_UART_TX : natural := 7;

  -- ロジアナの専用の入力の数（PMOD JB の 6 本。fprobe_gw/boards/pynq_z2.py の資源 la_in と一致）
  constant LA_INPUT_PINS : natural := 6;

  -- クロストリガの出力の端子の候補 0〜3（pmod1 の番号。fprobe_gw/boards/pynq_z2.py の output_pins と一致）
  type xout_pin_array_t is array (0 to 3) of natural range 0 to 7;
  constant XOUT_PIN_CHOICES : xout_pin_array_t := (4, 5, 6, 7);   -- 予備、UART RX、SWO、UART TX

  -- UART の RTS/CTS の端子の候補 0・1（uart_flow_pins と一致）
  type uart_flow_pin_array_t is array (0 to 1) of natural range 0 to 7;
  constant UART_FLOW_PIN_CHOICES : uart_flow_pin_array_t := (4, 1);   -- 予備、TDI

end package;
