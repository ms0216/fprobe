-- PYNQ-Z2 のボード定数。
-- ツール側の定義（gateware/fprobe_gw/boards/pynq_z2.py）と一致していることを
-- tests/test_boards_pynq_z2.py が検査する。値を変える場合は両方を変えること。

library ieee;
use ieee.std_logic_1164.all;

package pynq_z2_pkg is

  constant CLK125_HZ : natural := 125_000_000;

  -- MMCME2: 125 MHz * 48 / 5 = 1200 MHz（VCO。-1 の上限）、/ 12 = 100 MHz（sys）。
  -- 取り込みのクロック cap_clk（2 本目の出力）は構成の CAP_RATIO で sys × CAP_RATIO = VCO / mmcm_cap_div(CAP_RATIO)
  -- （比 3 = 300 MHz、比 4 = 400 MHz）
  constant MMCM_CLKIN_PERIOD_NS : real := 8.0;
  constant MMCM_MULT : real := 48.0;
  constant MMCM_DIVCLK : positive := 5;
  constant MMCM_OUT_DIV : real := 12.0;

  constant SYS_CLK_HZ : natural := 100_000_000;

  -- cap_clk = sys × ratio の 2 本目の出力の分周（ratio <= 1 なら 0 = 2 本目の出力を使わない）。割り切れない比は合成で止める
  function mmcm_cap_div(ratio : natural) return natural;

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

  -- xilinx_la_iserdes（ISERDESE2、NETWORKING・DDR）の語の最も新しいレーンが、その語の出た CLKDIV の立ち上がりと同時の CLK の辺
  -- より何辺前のサンプルか（Vivado 2025.2 の UNISIM を xsim で動かした記録 tests/data/iserdes_ref_w*.txt。lanes = 6・8）
  function iserdes_newest_age(lanes : positive) return natural;

end package;

package body pynq_z2_pkg is
  function mmcm_cap_div(ratio : natural) return natural is
    constant OUT_DIV : natural := integer(MMCM_OUT_DIV);
  begin
    if ratio <= 1 then
      return 0;
    end if;
    assert OUT_DIV mod ratio = 0 report "CAP_RATIO does not divide the MMCM's sys divider" severity failure;
    return OUT_DIV / ratio;
  end function;

  function iserdes_newest_age(lanes : positive) return natural is
  begin
    return lanes - 3;
  end function;
end package body;
