-- Tang Primer 20K Dock 用の fprobe（EthernetLink 版）。製品デザイン（primer20k_fprobe_top）と同じ fprobe_core・DDR3 を、
-- USB の代わりに Ethernet（RTL8201F、RMII、UDP）で PC につなぐ。仕様: docs/superpowers/specs/2026-10-17-m11-ethernet-link-design.md
--
--   sys ドメイン: clk27 → rPLL → sys_clk（100 MHz）。eth_link（frame_mux・udp_stack）と fprobe_core。
--   eth ドメイン: eth_ref_clk（PHY が出す 50 MHz）。eth_link の中の RMII の送受信だけ。
--   mem ドメイン: DDR3（primer20k_ddr3）。製品デザインと同じ。
--   eth_rst（F10）は USB3317 と共用。起動時に primer20k_ulpi_reset と同じパルスを 1 回出し、以後 high。
--   LED（アクティブロー）: led(0)=PHY のリセット解除, led(1)=送信先（PC）が決まった, led(2)=消灯, led(3)=消灯,
--                          led(4)=sys 心拍, led(5)=DDR3 較正済み
library ieee;
use ieee.std_logic_1164.all;
use ieee.numeric_std.all;
use work.util_pkg.all;
use work.primer20k_pkg.all;

entity primer20k_fprobe_eth_top is
  port (
    clk27       : in    std_logic;
    eth_ref_clk : in    std_logic;
    eth_crs_dv  : in    std_logic;
    eth_rxd     : in    std_logic_vector(1 downto 0);
    eth_tx_en   : out   std_logic;
    eth_txd     : out   std_logic_vector(1 downto 0);
    eth_rst     : out   std_logic;
    pmod1       : inout std_logic_vector(7 downto 0);   -- 拡張ヘッダ J12
    uart_tx     : out   std_logic;                      -- BL702 の UART: 診断（115200、0.1 秒ごとに状態）
    aux_rx      : in    std_logic;                      -- 補助マイコン（Pico）の UART（J14 の 5 番）
    led         : out   std_logic_vector(5 downto 0);
    -- DDR3
    ddr_addr    : out   std_logic_vector(13 downto 0);
    ddr_ba      : out   std_logic_vector(2 downto 0);
    ddr_cs_n    : out   std_logic;
    ddr_ras_n   : out   std_logic;
    ddr_cas_n   : out   std_logic;
    ddr_we_n    : out   std_logic;
    ddr_ck      : out   std_logic;
    ddr_ck_n    : out   std_logic;
    ddr_cke     : out   std_logic;
    ddr_odt     : out   std_logic;
    ddr_reset_n : out   std_logic;
    ddr_dm      : out   std_logic_vector(1 downto 0);
    ddr_dq      : inout std_logic_vector(15 downto 0);
    ddr_dqs     : inout std_logic_vector(1 downto 0);
    ddr_dqs_n   : inout std_logic_vector(1 downto 0)
  );
end entity;

architecture rtl of primer20k_fprobe_eth_top is
  constant LINK_PACKET_SIZE : positive := 512;   -- fprobe_core の packet_size（USB の High-Speed と同じ 512 バイト）
  signal sys_clk, sys_rst, pll_lock, link_rst, phy_released, peer_known : std_logic;
  -- DDR3（mem ドメイン）
  signal ddr3_memory_clk, mem_pll_lock, mem_clk, mem_rst, mem_ready : std_logic;
  signal mem_cmd_valid, mem_cmd_ready, mem_cmd_write, mem_rd_valid : std_logic;
  signal mem_cmd_addr : unsigned(22 downto 0);
  signal mem_cmd_data, mem_rd_data : std_logic_vector(127 downto 0);
  -- eth_link ⇄ fprobe_core（どちらも sys）
  signal req_valid, req_ready, req_last, rsp_valid, rsp_ready, rsp_last : std_logic;
  signal req_data, rsp_data : std_logic_vector(7 downto 0);
  signal dreq_valid, dreq_ready, dreq_last, drsp_valid, drsp_ready, drsp_last : std_logic;
  signal dreq_data, drsp_data : std_logic_vector(7 downto 0);
  signal swo_valid, swo_ready, st_valid, st_ready : std_logic;
  signal swo_data, st_data : std_logic_vector(7 downto 0);
  signal uo_valid, uo_ready, ui_valid, ui_ready : std_logic;
  signal uo_data, ui_data : std_logic_vector(7 downto 0);
  signal line_rate : unsigned(31 downto 0);
  signal line_changed : std_logic;
  -- デバッグポート
  signal gpio_drive, gpio_enable : std_logic_vector(7 downto 0);
  signal swclk_o, swclk_oe, swdio_o, swdio_oe, nreset_low, tdi_o, tdi_oe, target_tx, target_tx_oe : std_logic;
  signal func_o, func_oe : std_logic_vector(7 downto 0);
  signal heartbeat : unsigned(HEARTBEAT_BITS - 1 downto 0) := (others => '0');
  signal diag : std_logic_vector(15 downto 0);                 -- eth_link の状態（診断へ）
begin
  phy_reset_i : entity work.primer20k_ulpi_reset
    port map (clk27 => clk27, resetb => eth_rst, released => phy_released);

  pll_i : entity work.gowin_rpll
    generic map (FCLKIN_MHZ => "27", DEVICE => "GW2A-18C",
                 IDIV_SEL => PLL_IDIV_SEL, FBDIV_SEL => PLL_FBDIV_SEL, ODIV_SEL => PLL_ODIV_SEL)
    port map (clkin => clk27, clkout => sys_clk, lock => pll_lock);
  sys_reset_i : entity work.reset_sync
    port map (clk => sys_clk, arst_in => not pll_lock, rst => sys_rst);
  -- リンクは PHY のリセット解除（REF_CLK が出始める）まで止めておく
  link_reset_i : entity work.reset_sync
    port map (clk => sys_clk, arst_in => not (pll_lock and phy_released), rst => link_rst);

  link_i : entity work.eth_link
    generic map (MAC => ETH_MAC, IP => ETH_IP, PORT_NUM => ETH_UDP_PORT)
    port map (clk => sys_clk, rst => link_rst, ref_clk => eth_ref_clk, crs_dv => eth_crs_dv, rxd => eth_rxd,
              txd => eth_txd, tx_en => eth_tx_en, peer_known => peer_known, diag => diag,
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

  mem_pll_i : entity work.gowin_rpll
    generic map (FCLKIN_MHZ => "27", DEVICE => "GW2A-18C", IDIV_SEL => MEM_PLL_IDIV_SEL,
                 FBDIV_SEL => MEM_PLL_FBDIV_SEL, ODIV_SEL => MEM_PLL_ODIV_SEL)
    port map (clkin => clk27, clkout => ddr3_memory_clk, lock => mem_pll_lock);

  ddr3_i : entity work.primer20k_ddr3
    port map (clk27 => clk27, memory_clk => ddr3_memory_clk, pll_lock => mem_pll_lock, rst => not pll_lock,
              mem_clk => mem_clk, mem_rst => mem_rst, mem_ready => mem_ready,
              cmd_valid => mem_cmd_valid, cmd_ready => mem_cmd_ready, cmd_write => mem_cmd_write,
              cmd_addr => mem_cmd_addr, cmd_data => mem_cmd_data, rd_valid => mem_rd_valid, rd_data => mem_rd_data,
              ddr_addr => ddr_addr, ddr_ba => ddr_ba, ddr_cs_n => ddr_cs_n, ddr_ras_n => ddr_ras_n,
              ddr_cas_n => ddr_cas_n, ddr_we_n => ddr_we_n, ddr_ck => ddr_ck, ddr_ck_n => ddr_ck_n,
              ddr_cke => ddr_cke, ddr_odt => ddr_odt, ddr_reset_n => ddr_reset_n, ddr_dm => ddr_dm,
              ddr_dq => ddr_dq, ddr_dqs => ddr_dqs, ddr_dqs_n => ddr_dqs_n);

  core_i : entity work.fprobe_core
    -- 構成の機能はデザインの生成パッケージから全部渡す（ケイパビリティと回路を一致させる。tests/test_core_generics.py）
    -- LA_INTERNAL: ロジアナは DDR3 の深いバッファ
    generic map (CAPS => work.design_caps_pkg.CAPS_ROM, MEM_ADDR_BITS => work.design_caps_pkg.MEM_ADDR_BITS,
                 MEM_TEST => work.design_caps_pkg.MEM_TEST, XTRIG => work.design_caps_pkg.XTRIG,
                 TRIG_SEQ => work.design_caps_pkg.TRIG_SEQ, SAMPLER => work.design_caps_pkg.SAMPLER,
                 SAMPLER_MATCH => work.design_caps_pkg.SAMPLER_MATCH, SAMPLER_CHECK_MAX => work.design_caps_pkg.SAMPLER_CHECK_MAX,
                 HAS_JTAG => work.design_caps_pkg.HAS_JTAG, INJECTOR => work.design_caps_pkg.INJECTOR,
                 HAS_SWO_MANCHESTER => work.design_caps_pkg.SWO_MANCHESTER, UART_FLOW => work.design_caps_pkg.UART_FLOW,
                 AUX => work.design_caps_pkg.AUX, SYNC => work.design_caps_pkg.SYNC,
                 OBS_PORT => work.design_caps_pkg.OBS_PORT, XOUT_COUNT => work.design_caps_pkg.XOUT_COUNT,
                 LA_INPUT_COUNT => work.design_caps_pkg.LA_INPUTS, LA_FILTER => work.design_caps_pkg.LA_FILTER,
                 LA_PULSE => work.design_caps_pkg.LA_PULSE, LA_STREAM_EVENTS => work.design_caps_pkg.LA_STREAM_EVENTS,
                 LA_STREAM_GAPS => work.design_caps_pkg.LA_STREAM_GAPS, EVENT_RING_BITS => work.design_caps_pkg.EVENT_RING_BITS,
                 EVENT_BYTE_PACK => work.design_caps_pkg.EVENT_BYTE_PACK, CAP_RATIO => work.design_caps_pkg.CAP_RATIO,
                 LA_TESTGEN => work.design_caps_pkg.LA_TESTGEN, LA_LANES => work.design_caps_pkg.LA_LANES,
                 LA_LANE_ADDR_BITS => work.design_caps_pkg.LA_LANE_ADDR_BITS, LA_LANE_IN_AGE => work.design_caps_pkg.LA_LANE_IN_AGE,
                 -- ボードで決まるもの
                 CLK_HZ => SYS_CLK_HZ, LA_INTERNAL => false)
    port map (clk => sys_clk, rst => sys_rst,
              ctrl_in_valid => req_valid, ctrl_in_ready => req_ready, ctrl_in_data => req_data,
              ctrl_in_last => req_last,
              ctrl_out_valid => rsp_valid, ctrl_out_ready => rsp_ready, ctrl_out_data => rsp_data,
              ctrl_out_last => rsp_last,
              packet_size => LINK_PACKET_SIZE,
              dap_in_valid => dreq_valid, dap_in_ready => dreq_ready, dap_in_data => dreq_data, dap_in_last => dreq_last,
              dap_out_valid => drsp_valid, dap_out_ready => drsp_ready, dap_out_data => drsp_data,
              dap_out_last => drsp_last,
              swclk_o => swclk_o, swclk_oe => swclk_oe, swdio_o => swdio_o, swdio_oe => swdio_oe,
              swdio_i => pmod1(DBG_SWDIO), nreset_low => nreset_low, nreset_i => pmod1(DBG_NRESET),
              tdi_o => tdi_o, tdi_oe => tdi_oe, tdo_i => pmod1(DBG_SWO),
              swo_i => pmod1(DBG_SWO), swo_out_valid => swo_valid, swo_out_ready => swo_ready,
              swo_out_data => swo_data,
              uart_baud => line_rate, uart_baud_load => line_changed,
              uart_in_valid => uo_valid, uart_in_ready => uo_ready, uart_in_data => uo_data,
              uart_out_valid => ui_valid, uart_out_ready => ui_ready, uart_out_data => ui_data,
              uart_rx => pmod1(DBG_UART_RX), uart_tx => target_tx, uart_tx_oe => target_tx_oe, la_pins => pmod1,
              gpio_drive => gpio_drive, gpio_enable => gpio_enable, gpio_level => pmod1,
              mem_clk => mem_clk, mem_rst => mem_rst, mem_ready => mem_ready,
              mem_cmd_valid => mem_cmd_valid, mem_cmd_ready => mem_cmd_ready, mem_cmd_write => mem_cmd_write,
              mem_cmd_addr => mem_cmd_addr, mem_cmd_data => mem_cmd_data, mem_rd_valid => mem_rd_valid,
              mem_rd_data => mem_rd_data,
              stream_out_valid => st_valid, stream_out_ready => st_ready, stream_out_data => st_data,
              aux_rx => aux_rx);

  -- デバッグポートの機能ピン（それ以外のピンは入力のまま）
  pin_func_i : entity work.debug_pin_func
    generic map (DBG_SWCLK => DBG_SWCLK, DBG_TDI => DBG_TDI, DBG_NRESET => DBG_NRESET, DBG_SWDIO => DBG_SWDIO,
                 DBG_UART_TX => DBG_UART_TX)
    port map (swclk_o => swclk_o, swclk_oe => swclk_oe, swdio_o => swdio_o, swdio_oe => swdio_oe, nreset_low => nreset_low,
              tdi_o => tdi_o, tdi_oe => tdi_oe, uart_tx => target_tx, uart_tx_oe => target_tx_oe,
              func_o => func_o, func_oe => func_oe, uart_cts_n => open);

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

  diag_i : entity work.primer20k_eth_diag
    generic map (CLK_HZ => SYS_CLK_HZ)
    port map (clk => sys_clk, rst => sys_rst, diag => diag,
              req_valid => req_valid, req_ready => req_ready, req_last => req_last, req_data => req_data,
              rsp_valid => rsp_valid, rsp_ready => rsp_ready, rsp_last => rsp_last,
              dreq_valid => dreq_valid, dreq_ready => dreq_ready, dreq_last => dreq_last, dreq_data => dreq_data,
              drsp_valid => drsp_valid, drsp_ready => drsp_ready, drsp_last => drsp_last,
              st_valid => st_valid, st_ready => st_ready, swo_valid => swo_valid, swo_ready => swo_ready,
              uo_valid => uo_valid, uo_ready => uo_ready, ui_valid => ui_valid, ui_ready => ui_ready,
              uart_tx => uart_tx);

  led(0) <= not phy_released;
  led(1) <= not peer_known;
  led(2) <= '1';
  led(3) <= '1';
  led(4) <= not heartbeat(heartbeat'high);
  led(5) <= not mem_ready;
end architecture;
