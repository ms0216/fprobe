-- debug_pin_func の試験台: Primer 20K の割り当て（primer20k_pkg の DBG_* と UART_FLOW_PIN_CHOICES = (4, 1)）で包む。
library ieee;
use ieee.std_logic_1164.all;

entity debug_pin_func_harness is
  port (
    swclk_o, swclk_oe, swdio_o, swdio_oe, nreset_low, tdi_o, tdi_oe, uart_tx, uart_tx_oe : in std_logic;
    rts_claim  : in  std_logic;
    rts_pin    : in  std_logic_vector(1 downto 0);
    uart_rts_n : in  std_logic;
    cts_claim  : in  std_logic;
    cts_pin    : in  std_logic_vector(1 downto 0);
    pins_in    : in  std_logic_vector(7 downto 0);
    func_o     : out std_logic_vector(7 downto 0);
    func_oe    : out std_logic_vector(7 downto 0);
    uart_cts_n : out std_logic
  );
end entity;

architecture rtl of debug_pin_func_harness is
begin
  dut_i : entity work.debug_pin_func
    generic map (DBG_SWCLK => 0, DBG_TDI => 1, DBG_NRESET => 2, DBG_SWDIO => 3, DBG_UART_TX => 7,
                 FLOW_PIN_CHOICES => (4, 1))
    port map (swclk_o => swclk_o, swclk_oe => swclk_oe, swdio_o => swdio_o, swdio_oe => swdio_oe,
              nreset_low => nreset_low, tdi_o => tdi_o, tdi_oe => tdi_oe, uart_tx => uart_tx, uart_tx_oe => uart_tx_oe,
              rts_claim => rts_claim, rts_pin => rts_pin, uart_rts_n => uart_rts_n, cts_claim => cts_claim,
              cts_pin => cts_pin, pins_in => pins_in, func_o => func_o, func_oe => func_oe, uart_cts_n => uart_cts_n);
end architecture;
