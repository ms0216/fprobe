-- テスト専用: 1 周期に複数のサンプルの構成（LA_LANES > 1）の fprobe_core。専用の入力 la_inputs（と LA_FINE_PINS ならピン la_pins）を
-- PYNQ のトップと同じく 1 つの xilinx_la_iserdes（ISERDESE2 の模型。CLK = cap_clk の両方の辺、CLKDIV = clk。ch 0〜7 = ピン、続いて
-- 専用の入力）に通して la_pin_lanes・la_input_lanes に渡す。ピンを読む回路（la_pins・gpio_level）は ISERDESE2 の O を読む。
-- 他は fprobe_core_harness と同じ（試験用ケイパビリティ test_caps_pkg）。
library ieee;
use ieee.std_logic_1164.all;

entity fprobe_core_lanes_harness is
  generic (
    RESET_SPLIT : boolean := false;
    EVENT_COUNT_START_BELOW_4G : natural := 0
  );
  port (
    clk            : in  std_logic;
    cap_clk        : in  std_logic := '0';                -- CAP_RATIO >= 2 の構成の取り込みのクロック
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
    stream_out_valid : out std_logic;
    stream_out_ready : in  std_logic;
    stream_out_data  : out std_logic_vector(7 downto 0);
    aux_rx         : in  std_logic;
    sync_in        : in  std_logic;
    sync_out       : out std_logic;
    testgen_out    : out std_logic;                        -- 試験用の方形波（LA_TESTGEN の構成だけ。ほかは 0・0）
    testgen_oe     : out std_logic;
    -- CMSIS-DAP のパケットと SWD の線（ターゲットのモデル tests/models/swd_target.py をつなぐ）
    dap_in_valid   : in  std_logic;
    dap_in_ready   : out std_logic;
    dap_in_data    : in  std_logic_vector(7 downto 0);
    dap_in_last    : in  std_logic;
    dap_out_valid  : out std_logic;
    dap_out_ready  : in  std_logic;
    dap_out_data   : out std_logic_vector(7 downto 0);
    dap_out_last   : out std_logic;
    swclk          : out std_logic;
    swdio          : out std_logic;
    target_swdio   : in  std_logic
  );
end entity;

architecture rtl of fprobe_core_lanes_harness is
  signal sclk, sclk_oe, sdo, sdo_oe, line, sdi : std_logic;
  constant LANES : positive := work.test_caps_pkg.LA_LANES;
  constant INPUTS : positive := work.test_caps_pkg.LA_INPUTS;
  constant FINE_PINS : boolean := work.test_caps_pkg.LA_FINE_PINS;
  constant JA : natural := 8 * boolean'pos(FINE_PINS);
  constant CH : positive := JA + INPUTS;
  signal input_lanes : std_logic_vector(LANES * INPUTS - 1 downto 0);
  signal pin_lanes : std_logic_vector(LANES * 8 - 1 downto 0);
  signal serdes_pins, serdes_o : std_logic_vector(CH - 1 downto 0);
  signal serdes_q : std_logic_vector(LANES * CH - 1 downto 0);
  signal pins_in : std_logic_vector(7 downto 0);
begin
  serdes_i : entity work.xilinx_la_iserdes
    generic map (CHANNELS => CH, LANES => LANES)
    port map (clk_fast => cap_clk, clk_div => clk, rst => rst, pins => serdes_pins, q => serdes_q, o => serdes_o);
  serdes_pins(CH - 1 downto JA) <= la_inputs(INPUTS - 1 downto 0);
  lane_g : for k in 0 to LANES - 1 generate
    input_lanes((k + 1) * INPUTS - 1 downto k * INPUTS) <= serdes_q(k * CH + CH - 1 downto k * CH + JA);
  end generate;
  ja_g : if FINE_PINS generate
    serdes_pins(7 downto 0) <= la_pins;
    pins_in <= serdes_o(7 downto 0);
    ja_lane_g : for k in 0 to LANES - 1 generate
      pin_lanes((k + 1) * 8 - 1 downto k * 8) <= serdes_q(k * CH + 7 downto k * CH);
    end generate;
  end generate;
  no_ja_g : if not FINE_PINS generate
    pins_in <= la_pins;
    pin_lanes <= (others => '0');
  end generate;

  line <= sdo when sdo_oe = '1' else 'Z';
  line <= target_swdio;
  line <= 'H';
  sdi   <= to_x01(line);
  swdio <= sdi;
  swclk <= sclk when sclk_oe = '1' else '1';

  core_i : entity work.fprobe_core
    generic map (CAPS => work.test_caps_pkg.CAPS_ROM, CLK_HZ => 100_000_000, AUX => work.test_caps_pkg.AUX,
                 XTRIG => work.test_caps_pkg.XTRIG, TRIG_SEQ => work.test_caps_pkg.TRIG_SEQ,
                 SAMPLER => work.test_caps_pkg.SAMPLER, SAMPLER_MATCH => work.test_caps_pkg.SAMPLER_MATCH, HAS_JTAG => work.test_caps_pkg.HAS_JTAG,
                 HAS_SWO_MANCHESTER => work.test_caps_pkg.SWO_MANCHESTER, UART_FLOW => work.test_caps_pkg.UART_FLOW,
                 INJECTOR => work.test_caps_pkg.INJECTOR, XOUT_COUNT => work.test_caps_pkg.XOUT_COUNT,
                 LA_FILTER => work.test_caps_pkg.LA_FILTER, LA_PULSE => work.test_caps_pkg.LA_PULSE,
                 LA_INPUT_COUNT => work.test_caps_pkg.LA_INPUTS,
                 EVENT_RING_BITS => work.test_caps_pkg.EVENT_RING_BITS, SYNC => work.test_caps_pkg.SYNC,
                 RESET_SPLIT => RESET_SPLIT, CAP_RATIO => work.test_caps_pkg.CAP_RATIO,
                 LA_TESTGEN => work.test_caps_pkg.LA_TESTGEN,
                 LA_LANES => LANES, LA_LANE_ADDR_BITS => work.test_caps_pkg.LA_LANE_ADDR_BITS,
                 LA_LANE_IN_AGE => work.pynq_z2_pkg.iserdes_newest_age(LANES), LA_FINE_PINS => FINE_PINS,
                 EVENT_COUNT_START_BELOW_4G => EVENT_COUNT_START_BELOW_4G)
    port map (clk => clk, cap_clk => cap_clk, rst => rst,
              ctrl_in_valid => ctrl_in_valid, ctrl_in_ready => ctrl_in_ready,
              ctrl_in_data => ctrl_in_data, ctrl_in_last => ctrl_in_last,
              ctrl_out_valid => ctrl_out_valid, ctrl_out_ready => ctrl_out_ready,
              ctrl_out_data => ctrl_out_data, ctrl_out_last => ctrl_out_last,
              packet_size => 512, dap_in_valid => dap_in_valid, dap_in_ready => dap_in_ready,
              dap_in_data => dap_in_data, dap_in_last => dap_in_last, dap_out_valid => dap_out_valid,
              dap_out_ready => dap_out_ready, dap_out_data => dap_out_data, dap_out_last => dap_out_last,
              swclk_o => sclk, swclk_oe => sclk_oe, swdio_o => sdo, swdio_oe => sdo_oe, swdio_i => sdi, nreset_low => open, nreset_i => '1',
              tdi_o => open, tdi_oe => open, tdo_i => '1',
              swo_i => '1', swo_out_valid => open, swo_out_ready => '1', swo_out_data => open,
              uart_baud => (others => '0'), uart_baud_load => '0', uart_in_valid => uart_in_valid,
              uart_in_ready => uart_in_ready, uart_in_data => uart_in_data,
              uart_out_valid => open, uart_out_ready => '1', uart_out_data => open,
              uart_rx => '1', uart_tx => uart_tx, uart_tx_oe => uart_tx_oe, la_pins => pins_in, la_pin_lanes => pin_lanes,
              la_inputs => la_inputs(maximum(work.test_caps_pkg.LA_INPUTS, 1) - 1 downto 0), la_input_lanes => input_lanes,
              xout_o => xout_o, xout_oe => xout_oe, xout_claim => xout_claim, xout_pin => xout_pin,
              gpio_drive => gpio_drive, gpio_enable => gpio_enable, gpio_level => gpio_level, aux_rx => aux_rx,
              sync_in => sync_in, sync_out => sync_out, testgen_out => testgen_out, testgen_oe => testgen_oe,
              stream_out_valid => stream_out_valid, stream_out_ready => stream_out_ready,
              stream_out_data => stream_out_data);
end architecture;
