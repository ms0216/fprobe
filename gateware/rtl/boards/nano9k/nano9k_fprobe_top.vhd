-- Tang Nano 9K 用の fprobe（SerialLink、小さい FPGA 用に機能を減らした構成）。PC とは BL702 の USB-UART 1 本。
-- primer20k_fprobe_serial_top と同じ作り（ボード定数と PLL のデバイス名だけが違う）。
-- 仕様: docs/superpowers/specs/2026-10-17-m11-serial-link-design.md
--
--   sys ドメインだけ: clk27 → rPLL → sys_clk（54 MHz）。serial_link と fprobe_core を直結する（USB 版のような
--   クロックの乗り換えは無い）。外部メモリは使わず、ロジアナは内蔵 BSRAM。fprobe_core は USB 版と同じもの。
--   デバッグポートのピンは J5（pmod1、割り当ては nano9k_pkg の DBG_*）。
--   LED（アクティブロー）: led(0)=PLL ロック, led(4)=sys 心拍, それ以外は消灯
library ieee;
use ieee.std_logic_1164.all;
use ieee.numeric_std.all;
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
  signal sys_clk, sys_rst, pll_lock : std_logic;
  -- serial_link（PC 側）⇄ fprobe_core
  signal req_valid, req_ready, req_last, rsp_valid, rsp_ready, rsp_last : std_logic;
  signal req_data, rsp_data : std_logic_vector(7 downto 0);
  signal dreq_valid, dreq_ready, dreq_last, drsp_valid, drsp_ready, drsp_last : std_logic;
  signal dreq_data, drsp_data : std_logic_vector(7 downto 0);
  signal swo_valid, swo_ready, uo_valid, uo_ready, ui_valid, ui_ready, st_valid, st_ready : std_logic;
  signal swo_data, uo_data, ui_data, st_data : std_logic_vector(7 downto 0);
  signal line_rate : unsigned(31 downto 0);
  signal line_changed : std_logic;
  -- デバッグポート
  signal swclk_o, swclk_oe, swdio_o, swdio_oe, nreset_low, tdi_o, tdi_oe, target_tx, target_tx_oe : std_logic;
  signal gpio_drive, gpio_enable, func_o, func_oe : std_logic_vector(7 downto 0);
  signal heartbeat : unsigned(25 downto 0) := (others => '0');
begin
  pll_i : entity work.gowin_rpll
    generic map (FCLKIN_MHZ => "27", DEVICE => "GW1NR-9C",
                 IDIV_SEL => PLL_IDIV_SEL, FBDIV_SEL => PLL_FBDIV_SEL, ODIV_SEL => PLL_ODIV_SEL)
    port map (clkin => clk27, clkout => sys_clk, lock => pll_lock);
  sys_reset_i : entity work.reset_sync
    port map (clk => sys_clk, arst_in => not pll_lock, rst => sys_rst);

  link_i : entity work.serial_link
    generic map (CLK_HZ => SYS_CLK_HZ, BAUD => SERIAL_BAUD)
    port map (clk => sys_clk, rst => sys_rst, rx => uart_rx, tx => uart_tx, bad_frames => open,
              ctrl_out_valid => req_valid, ctrl_out_ready => req_ready, ctrl_out_data => req_data,
              ctrl_out_last => req_last,
              ctrl_in_valid => rsp_valid, ctrl_in_ready => rsp_ready, ctrl_in_data => rsp_data, ctrl_in_last => rsp_last,
              dap_out_valid => dreq_valid, dap_out_ready => dreq_ready, dap_out_data => dreq_data,
              dap_out_last => dreq_last,
              dap_in_valid => drsp_valid, dap_in_ready => drsp_ready, dap_in_data => drsp_data, dap_in_last => drsp_last,
              swo_in_valid => swo_valid, swo_in_ready => swo_ready, swo_in_data => swo_data,
              stream_in_valid => st_valid, stream_in_ready => st_ready, stream_in_data => st_data,
              uart_out_valid => uo_valid, uart_out_ready => uo_ready, uart_out_data => uo_data,
              uart_in_valid => ui_valid, uart_in_ready => ui_ready, uart_in_data => ui_data,
              line_rate => line_rate, line_changed => line_changed);

  core_i : entity work.fprobe_core
    generic map (CAPS => work.design_caps_pkg.CAPS_ROM, CLK_HZ => SYS_CLK_HZ, MEM_ADDR_BITS => 0,
                 MEM_TEST => work.design_caps_pkg.MEM_TEST,
                 XTRIG => work.design_caps_pkg.XTRIG, TRIG_SEQ => work.design_caps_pkg.TRIG_SEQ,
                 SAMPLER => work.design_caps_pkg.SAMPLER, SAMPLER_MATCH => work.design_caps_pkg.SAMPLER_MATCH,
                 HAS_JTAG => work.design_caps_pkg.HAS_JTAG, INJECTOR => work.design_caps_pkg.INJECTOR,
                 HAS_SWO_MANCHESTER => work.design_caps_pkg.SWO_MANCHESTER,
                 UART_FLOW => work.design_caps_pkg.UART_FLOW,
                 LA_FILTER => work.design_caps_pkg.LA_FILTER, LA_PULSE => work.design_caps_pkg.LA_PULSE,
                 LA_STREAM_EVENTS => work.design_caps_pkg.LA_STREAM_EVENTS, LA_INTERNAL => true,
                 EVENT_RING_BITS => work.design_caps_pkg.EVENT_RING_BITS)
    port map (clk => sys_clk, rst => sys_rst,
              ctrl_in_valid => req_valid, ctrl_in_ready => req_ready, ctrl_in_data => req_data,
              ctrl_in_last => req_last,
              ctrl_out_valid => rsp_valid, ctrl_out_ready => rsp_ready, ctrl_out_data => rsp_data,
              ctrl_out_last => rsp_last,
              packet_size => 512,
              dap_in_valid => dreq_valid, dap_in_ready => dreq_ready, dap_in_data => dreq_data, dap_in_last => dreq_last,
              dap_out_valid => drsp_valid, dap_out_ready => drsp_ready, dap_out_data => drsp_data,
              dap_out_last => drsp_last,
              swclk_o => swclk_o, swclk_oe => swclk_oe, swdio_o => swdio_o, swdio_oe => swdio_oe,
              swdio_i => pmod1(DBG_SWDIO), nreset_low => nreset_low, nreset_i => pmod1(DBG_NRESET),
              tdi_o => tdi_o, tdi_oe => tdi_oe, tdo_i => pmod1(DBG_SWO),
              swo_i => pmod1(DBG_SWO), swo_out_valid => swo_valid, swo_out_ready => swo_ready, swo_out_data => swo_data,
              uart_baud => line_rate, uart_baud_load => line_changed,
              uart_in_valid => uo_valid, uart_in_ready => uo_ready, uart_in_data => uo_data,
              uart_out_valid => ui_valid, uart_out_ready => ui_ready, uart_out_data => ui_data,
              uart_rx => pmod1(DBG_UART_RX), uart_tx => target_tx, uart_tx_oe => target_tx_oe, la_pins => pmod1,
              gpio_drive => gpio_drive, gpio_enable => gpio_enable, gpio_level => pmod1,
              mem_cmd_valid => open, mem_cmd_write => open, mem_cmd_addr => open, mem_cmd_data => open,
              stream_out_valid => st_valid, stream_out_ready => st_ready, stream_out_data => st_data);

  -- デバッグポートの機能ピン（USB 版と同じ）
  process (all)
  begin
    func_o  <= (others => '0');
    func_oe <= (others => '0');
    func_o(DBG_SWCLK)    <= swclk_o;
    func_oe(DBG_SWCLK)   <= swclk_oe;
    func_o(DBG_SWDIO)    <= swdio_o;
    func_oe(DBG_SWDIO)   <= swdio_oe;
    func_o(DBG_NRESET)   <= '0';
    func_oe(DBG_NRESET)  <= nreset_low;
    func_o(DBG_TDI)      <= tdi_o;
    func_oe(DBG_TDI)     <= tdi_oe;
    func_o(DBG_UART_TX)  <= target_tx;
    func_oe(DBG_UART_TX) <= target_tx_oe;    -- ホストが UART に最初の 1 バイトを送るまで駆動しない（B29）
  end process;

  pins : for i in pmod1'range generate
    pmod1(i) <= gpio_drive(i) when gpio_enable(i) = '1' else
                func_o(i)     when func_oe(i) = '1' else 'Z';
  end generate;

  process (sys_clk)
  begin
    if rising_edge(sys_clk) then
      heartbeat <= heartbeat + 1;
    end if;
  end process;

  led <= (0 => not pll_lock, 4 => not heartbeat(heartbeat'high), others => '1');
end architecture;
