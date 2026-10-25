-- PYNQ-Z2 用の fprobe（docs/superpowers/specs/2026-10-19-pynq-z2-port-design.md）。PC とは PS の Linux の中継
-- （fprobe-relay、UDP 63920。線上の形式は Primer の Ethernet 版と同じ）越し。
--
--   sys ドメインだけ: sys_clk_in（125 MHz、H16）→ MMCM → sys_clk（100 MHz）。PS の M_AXI_GP0（ps7_bd が AXI4-Lite に
--   変換）の ACLK も sys_clk なので、クロックまたぎは無い。
--   zynq_frame_link（AXI4-Lite のスレーブ）⇄ frame_mux ⇄ fprobe_core。fprobe_core は Primer と同じもの（内蔵ロジアナ）。
--   デバッグの線は PMOD JA（pmod1、割り当ては pynq_z2_pkg の DBG_*）。端子の優先は Primer の USB 版と同じ: gpio →
--   クロストリガの出力 → UART の RTS/CTS → 機能（pin_select）。ロジアナの専用の入力 la_in は PMOD JB の 6 本。
--   深いバッファ（P3。構成の MEM_ADDR_BITS > 0）: ブロック設計 ps7_hp_bd の S_AXI_HP0 に axi_mem_port をつなぎ、PS の DDR の
--   領域（物理番地は PS の中継が zynq_frame_link の MEM_BASE に書く。書くまで mem_ready = 0）を fprobe_core の外部メモリに
--   する（mem_clk = sys）。内蔵ロジアナは持たない（Primer の DDR3 の構成と同じ）。MEM_ADDR_BITS = 0 なら ps7_bd（HP0 無し）。
--   LED（アクティブ High）: led(0) = MMCM のロック、led(1) = sys の心拍、led(2) = PC へ送るフレームがある、led(3) = 消灯
library ieee;
use ieee.std_logic_1164.all;
use ieee.numeric_std.all;
use work.util_pkg.all;
use work.pynq_z2_pkg.all;

entity pynq_z2_fprobe_top is
  port (
    sys_clk_in : in    std_logic;
    pmod1      : inout std_logic_vector(7 downto 0);   -- PMOD JA
    la_in      : in    std_logic_vector(LA_INPUT_PINS - 1 downto 0);   -- PMOD JB
    -- 対象の FPGA の観測の JTAG（Raspberry Pi のヘッダ。手放している間は Z）
    obs_tck    : out   std_logic;
    obs_tms    : out   std_logic;
    obs_tdi    : out   std_logic;
    obs_tdo    : in    std_logic;
    -- ボードの間の時刻の同期（Raspberry Pi のヘッダの GPIO16 = 36 番が親から、GPIO20 = 38 番が子へ。同期の無い構成では Z）
    sync_in    : in    std_logic;
    sync_out   : out   std_logic;
    -- PS の DAP の PJTAG（EMIO）の素通し（Raspberry Pi のヘッダの GPIO8〜11。independent の起動のときだけ DAP がつながる）
    ps_pjtag_tck : in  std_logic;
    ps_pjtag_tms : in  std_logic;
    ps_pjtag_tdi : in  std_logic;
    ps_pjtag_tdo : out std_logic;
    led        : out   std_logic_vector(3 downto 0)
  );
end entity;

architecture rtl of pynq_z2_fprobe_top is
  -- Vivado のブロック設計 ps7_bd（ps7_bd.tcl）が生成する包み。GHDL では使わない
  component ps7_bd_wrapper
    port (
      aclk          : in  std_logic;
      aresetn       : in  std_logic;
      pjtag_tck     : in  std_logic;
      pjtag_tms     : in  std_logic;
      pjtag_tdi     : in  std_logic;
      pjtag_tdo     : out std_logic;
      m_axi_araddr  : out std_logic_vector(31 downto 0);
      m_axi_arprot  : out std_logic_vector(2 downto 0);
      m_axi_arready : in  std_logic;
      m_axi_arvalid : out std_logic;
      m_axi_awaddr  : out std_logic_vector(31 downto 0);
      m_axi_awprot  : out std_logic_vector(2 downto 0);
      m_axi_awready : in  std_logic;
      m_axi_awvalid : out std_logic;
      m_axi_bready  : out std_logic;
      m_axi_bresp   : in  std_logic_vector(1 downto 0);
      m_axi_bvalid  : in  std_logic;
      m_axi_rdata   : in  std_logic_vector(31 downto 0);
      m_axi_rready  : out std_logic;
      m_axi_rresp   : in  std_logic_vector(1 downto 0);
      m_axi_rvalid  : in  std_logic;
      m_axi_wdata   : out std_logic_vector(31 downto 0);
      m_axi_wready  : in  std_logic;
      m_axi_wstrb   : out std_logic_vector(3 downto 0);
      m_axi_wvalid  : out std_logic
    );
  end component;

  component ps7_hp_bd_wrapper
    port (
      aclk          : in  std_logic;
      aresetn       : in  std_logic;
      pjtag_tck     : in  std_logic;
      pjtag_tms     : in  std_logic;
      pjtag_tdi     : in  std_logic;
      pjtag_tdo     : out std_logic;
      m_axi_araddr  : out std_logic_vector(31 downto 0);
      m_axi_arprot  : out std_logic_vector(2 downto 0);
      m_axi_arready : in  std_logic;
      m_axi_arvalid : out std_logic;
      m_axi_awaddr  : out std_logic_vector(31 downto 0);
      m_axi_awprot  : out std_logic_vector(2 downto 0);
      m_axi_awready : in  std_logic;
      m_axi_awvalid : out std_logic;
      m_axi_bready  : out std_logic;
      m_axi_bresp   : in  std_logic_vector(1 downto 0);
      m_axi_bvalid  : in  std_logic;
      m_axi_rdata   : in  std_logic_vector(31 downto 0);
      m_axi_rready  : out std_logic;
      m_axi_rresp   : in  std_logic_vector(1 downto 0);
      m_axi_rvalid  : in  std_logic;
      m_axi_wdata   : out std_logic_vector(31 downto 0);
      m_axi_wready  : in  std_logic;
      m_axi_wstrb   : out std_logic_vector(3 downto 0);
      m_axi_wvalid  : out std_logic;
      s_axi_hp0_araddr   : in  std_logic_vector(31 downto 0);
      s_axi_hp0_arburst  : in  std_logic_vector(1 downto 0);
      s_axi_hp0_arcache  : in  std_logic_vector(3 downto 0);
      s_axi_hp0_arid     : in  std_logic_vector(5 downto 0);
      s_axi_hp0_arlen    : in  std_logic_vector(3 downto 0);
      s_axi_hp0_arlock   : in  std_logic_vector(1 downto 0);
      s_axi_hp0_arprot   : in  std_logic_vector(2 downto 0);
      s_axi_hp0_arqos    : in  std_logic_vector(3 downto 0);
      s_axi_hp0_arready  : out std_logic;
      s_axi_hp0_arsize   : in  std_logic_vector(2 downto 0);
      s_axi_hp0_arvalid  : in  std_logic;
      s_axi_hp0_awaddr   : in  std_logic_vector(31 downto 0);
      s_axi_hp0_awburst  : in  std_logic_vector(1 downto 0);
      s_axi_hp0_awcache  : in  std_logic_vector(3 downto 0);
      s_axi_hp0_awid     : in  std_logic_vector(5 downto 0);
      s_axi_hp0_awlen    : in  std_logic_vector(3 downto 0);
      s_axi_hp0_awlock   : in  std_logic_vector(1 downto 0);
      s_axi_hp0_awprot   : in  std_logic_vector(2 downto 0);
      s_axi_hp0_awqos    : in  std_logic_vector(3 downto 0);
      s_axi_hp0_awready  : out std_logic;
      s_axi_hp0_awsize   : in  std_logic_vector(2 downto 0);
      s_axi_hp0_awvalid  : in  std_logic;
      s_axi_hp0_bid      : out std_logic_vector(5 downto 0);
      s_axi_hp0_bready   : in  std_logic;
      s_axi_hp0_bresp    : out std_logic_vector(1 downto 0);
      s_axi_hp0_bvalid   : out std_logic;
      s_axi_hp0_rdata    : out std_logic_vector(63 downto 0);
      s_axi_hp0_rid      : out std_logic_vector(5 downto 0);
      s_axi_hp0_rlast    : out std_logic;
      s_axi_hp0_rready   : in  std_logic;
      s_axi_hp0_rresp    : out std_logic_vector(1 downto 0);
      s_axi_hp0_rvalid   : out std_logic;
      s_axi_hp0_wdata    : in  std_logic_vector(63 downto 0);
      s_axi_hp0_wid      : in  std_logic_vector(5 downto 0);
      s_axi_hp0_wlast    : in  std_logic;
      s_axi_hp0_wready   : out std_logic;
      s_axi_hp0_wstrb    : in  std_logic_vector(7 downto 0);
      s_axi_hp0_wvalid   : in  std_logic
    );
  end component;

  constant MAX_PAYLOAD : positive := 512;
  constant RAM_BITS : positive := 10;

  signal sys_clk, sys_rst, mmcm_lock, aresetn : std_logic;
  signal obs_tck_o, obs_tms_o, obs_tdi_o, obs_oe : std_logic;      -- 対象の FPGA の観測の JTAG
  signal sync_o : std_logic;                                         -- ボードの間の時刻の同期（コアの出力）
  -- AXI4-Lite（PS → zynq_frame_link）
  signal araddr, awaddr, rdata, wdata : std_logic_vector(31 downto 0);
  signal arprot, awprot : std_logic_vector(2 downto 0);
  signal arready, arvalid, awready, awvalid, bready, bvalid, rready, rvalid, wready, wvalid : std_logic;
  signal bresp, rresp : std_logic_vector(1 downto 0);
  signal wstrb : std_logic_vector(3 downto 0);
  -- zynq_frame_link ⇄ frame_mux
  signal m_we, m_commit, m_busy, m_tx_valid, m_tx_done : std_logic;
  signal m_waddr, m_raddr : unsigned(RAM_BITS - 1 downto 0);
  signal m_wdata, m_rdata : std_logic_vector(7 downto 0);
  signal m_rx_len : natural range 0 to MAX_PAYLOAD + 1;
  signal m_tx_len : natural range 1 to MAX_PAYLOAD + 1;
  -- frame_mux ⇄ fprobe_core
  signal req_valid, req_ready, req_last, rsp_valid, rsp_ready, rsp_last : std_logic;
  signal req_data, rsp_data : std_logic_vector(7 downto 0);
  signal dreq_valid, dreq_ready, dreq_last, drsp_valid, drsp_ready, drsp_last : std_logic;
  signal dreq_data, drsp_data : std_logic_vector(7 downto 0);
  signal swo_valid, swo_ready, uo_valid, uo_ready, ui_valid, ui_ready, st_valid, st_ready : std_logic;
  signal swo_data, uo_data, ui_data, st_data : std_logic_vector(7 downto 0);
  signal line_rate : unsigned(31 downto 0);
  signal line_changed : std_logic;
  -- デバッグの線
  signal swclk_o, swclk_oe, swdio_o, swdio_oe, nreset_low, tdi_o, tdi_oe, uart_tx, uart_tx_oe : std_logic;
  signal uart_cts_n, uart_rts_n, rts_claim, cts_claim : std_logic;
  signal rts_pin, cts_pin : std_logic_vector(1 downto 0);
  signal gpio_drive, gpio_enable, func_o, func_oe : std_logic_vector(7 downto 0);
  signal pin_o, pin_oe, claimed : std_logic_vector(7 downto 0);
  signal xout_o, xout_oe, xout_claim : std_logic_vector(1 downto 0);
  signal xout_pin : std_logic_vector(3 downto 0);
  signal heartbeat : unsigned(25 downto 0);
  -- 外部メモリ（MEM_ADDR_BITS = 0 なら使わない: mem_ready = 0）
  constant MEM_BITS : natural := work.design_caps_pkg.MEM_ADDR_BITS;
  constant DEEP : boolean := MEM_BITS > 0;
  constant MEM_SPAN : natural := to_natural(DEEP) * (2 ** MEM_BITS) * 16;   -- MEM_BASE から使うバイト数
  signal mem_errors : unsigned(15 downto 0);
  signal mem_base : unsigned(31 downto 0);
  signal mem_ready, mem_cmd_valid, mem_cmd_ready, mem_cmd_write, mem_rd_valid : std_logic;
  signal mem_cmd_addr : unsigned(maximum(MEM_BITS, 1) - 1 downto 0);
  signal mem_cmd_data, mem_rd_data : std_logic_vector(127 downto 0);
begin
  clock_i : entity work.xilinx_mmcm
    generic map (CLKIN_PERIOD_NS => MMCM_CLKIN_PERIOD_NS, MULT => MMCM_MULT, DIVCLK => MMCM_DIVCLK,
                 OUT_DIV => MMCM_OUT_DIV)
    port map (clkin => sys_clk_in, clkout => sys_clk, lock => mmcm_lock);
  sys_reset_i : entity work.reset_sync
    port map (clk => sys_clk, arst_in => not mmcm_lock, rst => sys_rst);
  aresetn <= not sys_rst;

  ps_plain : if not DEEP generate
    ps_i : ps7_bd_wrapper
      port map (aclk => sys_clk, aresetn => aresetn,
                pjtag_tck => ps_pjtag_tck, pjtag_tms => ps_pjtag_tms, pjtag_tdi => ps_pjtag_tdi,
                pjtag_tdo => ps_pjtag_tdo,
                m_axi_araddr => araddr, m_axi_arprot => arprot, m_axi_arready => arready, m_axi_arvalid => arvalid,
                m_axi_awaddr => awaddr, m_axi_awprot => awprot, m_axi_awready => awready, m_axi_awvalid => awvalid,
                m_axi_bready => bready, m_axi_bresp => bresp, m_axi_bvalid => bvalid,
                m_axi_rdata => rdata, m_axi_rready => rready, m_axi_rresp => rresp, m_axi_rvalid => rvalid,
                m_axi_wdata => wdata, m_axi_wready => wready, m_axi_wstrb => wstrb, m_axi_wvalid => wvalid);
    mem_ready <= '0';
    mem_cmd_ready <= '0';
    mem_rd_valid <= '0';
    mem_rd_data <= (others => '0');
    mem_errors <= (others => '0');
  end generate;

  -- 深いバッファ: S_AXI_HP0 ⇄ axi_mem_port ⇄ fprobe_core の外部メモリ
  ps_deep : if DEEP generate
    signal hp_awaddr, hp_araddr : std_logic_vector(31 downto 0);
    signal hp_awlen, hp_arlen : std_logic_vector(3 downto 0);
    signal hp_awsize, hp_arsize : std_logic_vector(2 downto 0);
    signal hp_awburst, hp_arburst, hp_bresp, hp_rresp : std_logic_vector(1 downto 0);
    signal hp_awvalid, hp_awready, hp_wlast, hp_wvalid, hp_wready, hp_bvalid, hp_bready : std_logic;
    signal hp_arvalid, hp_arready, hp_rlast, hp_rvalid, hp_rready : std_logic;
    signal hp_wdata, hp_rdata : std_logic_vector(63 downto 0);
    signal hp_wstrb : std_logic_vector(7 downto 0);
  begin
    ps_i : ps7_hp_bd_wrapper
      port map (aclk => sys_clk, aresetn => aresetn,
                pjtag_tck => ps_pjtag_tck, pjtag_tms => ps_pjtag_tms, pjtag_tdi => ps_pjtag_tdi,
                pjtag_tdo => ps_pjtag_tdo,
                m_axi_araddr => araddr, m_axi_arprot => arprot, m_axi_arready => arready, m_axi_arvalid => arvalid,
                m_axi_awaddr => awaddr, m_axi_awprot => awprot, m_axi_awready => awready, m_axi_awvalid => awvalid,
                m_axi_bready => bready, m_axi_bresp => bresp, m_axi_bvalid => bvalid,
                m_axi_rdata => rdata, m_axi_rready => rready, m_axi_rresp => rresp, m_axi_rvalid => rvalid,
                m_axi_wdata => wdata, m_axi_wready => wready, m_axi_wstrb => wstrb, m_axi_wvalid => wvalid,
                s_axi_hp0_awaddr => hp_awaddr, s_axi_hp0_awlen => hp_awlen, s_axi_hp0_awsize => hp_awsize,
                s_axi_hp0_awburst => hp_awburst, s_axi_hp0_awcache => "0011", s_axi_hp0_awid => "000000",
                s_axi_hp0_awlock => "00", s_axi_hp0_awprot => "000", s_axi_hp0_awqos => "0000",
                s_axi_hp0_awvalid => hp_awvalid, s_axi_hp0_awready => hp_awready,
                s_axi_hp0_wdata => hp_wdata, s_axi_hp0_wstrb => hp_wstrb, s_axi_hp0_wlast => hp_wlast,
                s_axi_hp0_wid => "000000", s_axi_hp0_wvalid => hp_wvalid, s_axi_hp0_wready => hp_wready,
                s_axi_hp0_bresp => hp_bresp, s_axi_hp0_bid => open, s_axi_hp0_bvalid => hp_bvalid,
                s_axi_hp0_bready => hp_bready,
                s_axi_hp0_araddr => hp_araddr, s_axi_hp0_arlen => hp_arlen, s_axi_hp0_arsize => hp_arsize,
                s_axi_hp0_arburst => hp_arburst, s_axi_hp0_arcache => "0011", s_axi_hp0_arid => "000000",
                s_axi_hp0_arlock => "00", s_axi_hp0_arprot => "000", s_axi_hp0_arqos => "0000",
                s_axi_hp0_arvalid => hp_arvalid, s_axi_hp0_arready => hp_arready,
                s_axi_hp0_rdata => hp_rdata, s_axi_hp0_rresp => hp_rresp, s_axi_hp0_rlast => hp_rlast,
                s_axi_hp0_rid => open, s_axi_hp0_rvalid => hp_rvalid, s_axi_hp0_rready => hp_rready);

    mem_i : entity work.axi_mem_port
      generic map (ADDR_BITS => maximum(MEM_BITS, 1))
      port map (clk => sys_clk, rst => sys_rst, base => mem_base, ready => mem_ready,
                cmd_valid => mem_cmd_valid, cmd_ready => mem_cmd_ready, cmd_write => mem_cmd_write,
                cmd_addr => mem_cmd_addr, cmd_data => mem_cmd_data, rd_valid => mem_rd_valid, rd_data => mem_rd_data,
                errors => mem_errors,
                m_axi_awaddr => hp_awaddr, m_axi_awlen => hp_awlen, m_axi_awsize => hp_awsize,
                m_axi_awburst => hp_awburst, m_axi_awvalid => hp_awvalid, m_axi_awready => hp_awready,
                m_axi_wdata => hp_wdata, m_axi_wstrb => hp_wstrb, m_axi_wlast => hp_wlast, m_axi_wvalid => hp_wvalid,
                m_axi_wready => hp_wready, m_axi_bresp => hp_bresp, m_axi_bvalid => hp_bvalid, m_axi_bready => hp_bready,
                m_axi_araddr => hp_araddr, m_axi_arlen => hp_arlen, m_axi_arsize => hp_arsize,
                m_axi_arburst => hp_arburst, m_axi_arvalid => hp_arvalid, m_axi_arready => hp_arready,
                m_axi_rdata => hp_rdata, m_axi_rresp => hp_rresp, m_axi_rlast => hp_rlast, m_axi_rvalid => hp_rvalid,
                m_axi_rready => hp_rready);
  end generate;

  link_i : entity work.zynq_frame_link
    generic map (MAX_PAYLOAD => MAX_PAYLOAD, RAM_BITS => RAM_BITS, MEM_BYTES => MEM_SPAN)
    port map (clk => sys_clk, rst => sys_rst,
              s_axi_awaddr => awaddr(11 downto 0), s_axi_awvalid => awvalid, s_axi_awready => awready,
              s_axi_wdata => wdata, s_axi_wstrb => wstrb, s_axi_wvalid => wvalid, s_axi_wready => wready,
              s_axi_bresp => bresp, s_axi_bvalid => bvalid, s_axi_bready => bready,
              s_axi_araddr => araddr(11 downto 0), s_axi_arvalid => arvalid, s_axi_arready => arready,
              s_axi_rdata => rdata, s_axi_rresp => rresp, s_axi_rvalid => rvalid, s_axi_rready => rready,
              rx_we => m_we, rx_waddr => m_waddr, rx_wdata => m_wdata, rx_commit => m_commit, rx_len => m_rx_len,
              rx_busy => m_busy, tx_valid => m_tx_valid, tx_len => m_tx_len, tx_raddr => m_raddr,
              tx_rdata => m_rdata, tx_done => m_tx_done, mem_base => mem_base,
              mem_errors => mem_errors);

  mux_i : entity work.frame_mux
    generic map (MAX_PAYLOAD => MAX_PAYLOAD, RAM_BITS => RAM_BITS)
    port map (clk => sys_clk, rst => sys_rst,
              rx_we => m_we, rx_waddr => m_waddr, rx_wdata => m_wdata, rx_commit => m_commit, rx_len => m_rx_len,
              rx_busy => m_busy,
              tx_valid => m_tx_valid, tx_len => m_tx_len, tx_raddr => m_raddr, tx_rdata => m_rdata,
              tx_done => m_tx_done,
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
              line_rate => line_rate, line_changed => line_changed, diag => open);

  core_i : entity work.fprobe_core
    generic map (CAPS => work.design_caps_pkg.CAPS_ROM, CLK_HZ => SYS_CLK_HZ, MEM_ADDR_BITS => MEM_BITS,
                 MEM_TEST => work.design_caps_pkg.MEM_TEST,
                 XTRIG => work.design_caps_pkg.XTRIG, TRIG_SEQ => work.design_caps_pkg.TRIG_SEQ,
                 SAMPLER => work.design_caps_pkg.SAMPLER, SAMPLER_MATCH => work.design_caps_pkg.SAMPLER_MATCH,
                 HAS_JTAG => work.design_caps_pkg.HAS_JTAG, INJECTOR => work.design_caps_pkg.INJECTOR,
                 HAS_SWO_MANCHESTER => work.design_caps_pkg.SWO_MANCHESTER,
                 UART_FLOW => work.design_caps_pkg.UART_FLOW,
                 RESET_SPLIT => true,        -- 最悪の経路が reset_sync の出力の扇出（約 3500）だった。docs/reports/fprobe-core-notes.md §2
                 LA_FILTER => work.design_caps_pkg.LA_FILTER, LA_PULSE => work.design_caps_pkg.LA_PULSE,
                 LA_STREAM_EVENTS => work.design_caps_pkg.LA_STREAM_EVENTS,
                 AUX => work.design_caps_pkg.AUX, XOUT_COUNT => work.design_caps_pkg.XOUT_COUNT,
                 LA_INPUT_COUNT => work.design_caps_pkg.LA_INPUTS,
                 LA_INTERNAL => not DEEP, EVENT_RING_BITS => work.design_caps_pkg.EVENT_RING_BITS,
                 OBS_PORT => work.design_caps_pkg.OBS_PORT, SYNC => work.design_caps_pkg.SYNC)
    port map (clk => sys_clk, rst => sys_rst,
              ctrl_in_valid => req_valid, ctrl_in_ready => req_ready, ctrl_in_data => req_data,
              ctrl_in_last => req_last,
              ctrl_out_valid => rsp_valid, ctrl_out_ready => rsp_ready, ctrl_out_data => rsp_data,
              ctrl_out_last => rsp_last,
              packet_size => MAX_PAYLOAD,
              dap_in_valid => dreq_valid, dap_in_ready => dreq_ready, dap_in_data => dreq_data, dap_in_last => dreq_last,
              dap_out_valid => drsp_valid, dap_out_ready => drsp_ready, dap_out_data => drsp_data,
              dap_out_last => drsp_last,
              swclk_o => swclk_o, swclk_oe => swclk_oe, swdio_o => swdio_o, swdio_oe => swdio_oe,
              swdio_i => pmod1(DBG_SWDIO), nreset_low => nreset_low, nreset_i => pmod1(DBG_NRESET),
              tdi_o => tdi_o, tdi_oe => tdi_oe, tdo_i => pmod1(DBG_SWO) or claimed(DBG_SWO),
              swo_i => pmod1(DBG_SWO) or claimed(DBG_SWO), swo_out_valid => swo_valid, swo_out_ready => swo_ready,
              swo_out_data => swo_data, swo_flush => open,
              uart_baud => line_rate, uart_baud_load => line_changed,
              uart_in_valid => uo_valid, uart_in_ready => uo_ready, uart_in_data => uo_data,
              uart_out_valid => ui_valid, uart_out_ready => ui_ready, uart_out_data => ui_data,
              uart_rx => pmod1(DBG_UART_RX) or claimed(DBG_UART_RX), uart_tx => uart_tx, uart_tx_oe => uart_tx_oe,
              uart_host_rts => '0', uart_host_break => '0', uart_cts_n => uart_cts_n,
              uart_rts_n => uart_rts_n, uart_rts_claim => rts_claim, uart_rts_pin => rts_pin,
              uart_cts_claim => cts_claim, uart_cts_pin => cts_pin, uart_cts => open,
              la_pins => pmod1,
              la_inputs => la_in(maximum(work.design_caps_pkg.LA_INPUTS, 1) - 1 downto 0),
              gpio_drive => gpio_drive, gpio_enable => gpio_enable, gpio_level => pmod1,
              xout_o => xout_o, xout_oe => xout_oe, xout_claim => xout_claim, xout_pin => xout_pin,
              mem_clk => sys_clk, mem_rst => sys_rst, mem_ready => mem_ready,
              mem_cmd_valid => mem_cmd_valid, mem_cmd_ready => mem_cmd_ready, mem_cmd_write => mem_cmd_write,
              mem_cmd_addr => mem_cmd_addr, mem_cmd_data => mem_cmd_data, mem_rd_valid => mem_rd_valid,
              mem_rd_data => mem_rd_data,
              stream_out_valid => st_valid, stream_out_ready => st_ready, stream_out_data => st_data,
              obs_tck => obs_tck_o, obs_tms => obs_tms_o, obs_tdi => obs_tdi_o, obs_tdo => obs_tdo,
              obs_oe => obs_oe, sync_in => sync_in, sync_out => sync_o);
  sync_out <= sync_o when work.design_caps_pkg.SYNC else 'Z';
  obs_tck <= obs_tck_o when obs_oe = '1' else 'Z';
  obs_tms <= obs_tms_o when obs_oe = '1' else 'Z';
  obs_tdi <= obs_tdi_o when obs_oe = '1' else 'Z';

  -- デバッグの線の機能（Primer の USB 版と同じ）
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
    func_o(DBG_UART_TX)  <= uart_tx;
    func_oe(DBG_UART_TX) <= uart_tx_oe;    -- ホストが UART に最初の 1 バイトを送るまで駆動しない（B29）
    for j in UART_FLOW_PIN_CHOICES'range loop
      if cts_claim = '1' and to_integer(unsigned(cts_pin)) = j then
        func_oe(UART_FLOW_PIN_CHOICES(j)) <= '0';
      end if;
      if rts_claim = '1' and to_integer(unsigned(rts_pin)) = j then
        func_o(UART_FLOW_PIN_CHOICES(j))  <= uart_rts_n;
        func_oe(UART_FLOW_PIN_CHOICES(j)) <= '1';
      end if;
    end loop;
  end process;

  process (all)
  begin
    uart_cts_n <= '0';
    for j in UART_FLOW_PIN_CHOICES'range loop
      if cts_claim = '1' and to_integer(unsigned(cts_pin)) = j then
        uart_cts_n <= pmod1(UART_FLOW_PIN_CHOICES(j));
      end if;
    end loop;
  end process;

  pin_select_i : entity work.pin_select
    generic map (PINS => 8, ACTIONS => 2, PIN_CHOICES => (XOUT_PIN_CHOICES(0), XOUT_PIN_CHOICES(1), XOUT_PIN_CHOICES(2), XOUT_PIN_CHOICES(3)))
    port map (gpio_drive => gpio_drive, gpio_enable => gpio_enable, func_o => func_o, func_oe => func_oe,
              xout_o => xout_o, xout_oe => xout_oe, xout_claim => xout_claim, xout_pin => xout_pin,
              pin_o => pin_o, pin_oe => pin_oe, claimed => claimed);

  pins : for i in pmod1'range generate
    pmod1(i) <= pin_o(i) when pin_oe(i) = '1' else 'Z';
  end generate;

  process (sys_clk)
  begin
    if rising_edge(sys_clk) then
      if sys_rst = '1' then
        heartbeat <= (others => '0');
      else
        heartbeat <= heartbeat + 1;
      end if;
    end if;
  end process;

  led <= (0 => mmcm_lock, 1 => heartbeat(heartbeat'high), 2 => m_tx_valid, 3 => '0');
end architecture;
