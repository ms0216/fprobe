-- テスト専用: fprobe_core に試験用ケイパビリティ（test_caps_pkg、テストが一時ディレクトリに生成）を与える。
-- 機能の有無のジェネリックもケイパビリティから渡す（構成ごとの試験。申告と実装が一致すること）。
library ieee;
use ieee.std_logic_1164.all;

entity fprobe_core_harness is
  port (
    clk            : in  std_logic;
    rst            : in  std_logic;
    ctrl_in_valid  : in  std_logic;
    ctrl_in_ready  : out std_logic;
    ctrl_in_data   : in  std_logic_vector(7 downto 0);
    ctrl_in_last   : in  std_logic;
    ctrl_out_valid : out std_logic;
    ctrl_out_ready : in  std_logic;
    ctrl_out_data  : out std_logic_vector(7 downto 0);
    ctrl_out_last  : out std_logic;
    gpio_drive     : out std_logic_vector(7 downto 0);
    gpio_enable    : out std_logic_vector(7 downto 0);
    gpio_level     : in  std_logic_vector(7 downto 0);
    la_pins        : in  std_logic_vector(7 downto 0);
    la_inputs      : in  std_logic_vector(5 downto 0);    -- 専用の入力（構成の LA_INPUTS 本だけつなぐ）
    xout_o         : out std_logic_vector(1 downto 0);
    xout_oe        : out std_logic_vector(1 downto 0);
    xout_claim     : out std_logic_vector(1 downto 0);
    xout_pin       : out std_logic_vector(3 downto 0);
    uart_in_valid  : in  std_logic;
    uart_in_ready  : out std_logic;
    uart_in_data   : in  std_logic_vector(7 downto 0);
    uart_tx        : out std_logic;
    uart_tx_oe     : out std_logic;
    aux_rx         : in  std_logic
  );
end entity;

architecture rtl of fprobe_core_harness is
begin
  core_i : entity work.fprobe_core
    generic map (CAPS => work.test_caps_pkg.CAPS_ROM, CLK_HZ => 100_000_000, AUX => work.test_caps_pkg.AUX,
                 XTRIG => work.test_caps_pkg.XTRIG, TRIG_SEQ => work.test_caps_pkg.TRIG_SEQ,
                 SAMPLER => work.test_caps_pkg.SAMPLER, HAS_JTAG => work.test_caps_pkg.HAS_JTAG,
                 HAS_SWO_MANCHESTER => work.test_caps_pkg.SWO_MANCHESTER, UART_FLOW => work.test_caps_pkg.UART_FLOW,
                 INJECTOR => work.test_caps_pkg.INJECTOR, XOUT_COUNT => work.test_caps_pkg.XOUT_COUNT,
                 LA_FILTER => work.test_caps_pkg.LA_FILTER, LA_PULSE => work.test_caps_pkg.LA_PULSE,
                 LA_INPUT_COUNT => work.test_caps_pkg.LA_INPUTS)
    port map (clk => clk, rst => rst,
              ctrl_in_valid => ctrl_in_valid, ctrl_in_ready => ctrl_in_ready,
              ctrl_in_data => ctrl_in_data, ctrl_in_last => ctrl_in_last,
              ctrl_out_valid => ctrl_out_valid, ctrl_out_ready => ctrl_out_ready,
              ctrl_out_data => ctrl_out_data, ctrl_out_last => ctrl_out_last,
              packet_size => 512, dap_in_valid => '0', dap_in_ready => open, dap_in_data => x"00",
              dap_in_last => '0', dap_out_valid => open, dap_out_ready => '1', dap_out_data => open,
              dap_out_last => open, swclk_o => open, swclk_oe => open, swdio_o => open, swdio_oe => open,
              swdio_i => '1', nreset_low => open, nreset_i => '1',
              tdi_o => open, tdi_oe => open, tdo_i => '1',
              swo_i => '1', swo_out_valid => open, swo_out_ready => '1', swo_out_data => open,
              uart_baud => (others => '0'), uart_baud_load => '0', uart_in_valid => uart_in_valid,
              uart_in_ready => uart_in_ready, uart_in_data => uart_in_data,
              uart_out_valid => open, uart_out_ready => '1', uart_out_data => open,
              uart_rx => '1', uart_tx => uart_tx, uart_tx_oe => uart_tx_oe, la_pins => la_pins,
              la_inputs => la_inputs(maximum(work.test_caps_pkg.LA_INPUTS, 1) - 1 downto 0),
              xout_o => xout_o, xout_oe => xout_oe, xout_claim => xout_claim, xout_pin => xout_pin,
              gpio_drive => gpio_drive, gpio_enable => gpio_enable, gpio_level => gpio_level, aux_rx => aux_rx);
end architecture;
