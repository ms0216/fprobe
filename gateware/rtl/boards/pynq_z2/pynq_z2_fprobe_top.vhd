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
--   1 周期に複数のサンプルの構成（LA_LANES > 1。fprobe_pynq_fast）: 専用の入力 la_in（と LA_FINE_PINS なら PMOD JA の 8 本）を
--   xilinx_la_iserdes（ISERDESE2、CLK = cap_clk の両方の辺、CLKDIV = sys）で取り、sys の 1 周期に LA_LANES = 2 × CAP_RATIO
--   サンプルにしてコアの la_input_lanes（・la_pin_lanes）に渡す（docs/superpowers/specs/2026-11-01-la-resolution-design.md の段 1・段 2）。
--   JA に ISERDESE2 を置くと、端子の入力は IBUF から ISERDESE2 の D にだけつながるので、JA を読む回路（DAP・UART・gpio・ロジアナの
--   sys の経路）はすべて ISERDESE2 の組み合わせの出力 O（= 端子の値。ja_in）を読む。出力は OLOGIC の組み合わせの経路で今と同じ。
--   クロック: MMCM（VCO 1200 MHz）の CLKOUT0 = sys 100 MHz、CLKOUT1 = cap_clk = sys × CAP_RATIO（pynq_z2_pkg.mmcm_cap_div）。
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
    -- 外部ピン入力の限界を測る試験用の方形波（Raspberry Pi のヘッダの GPIO12 = 32 番。有効にしたときだけ駆動、それ以外は Z）
    testgen_pin : out  std_logic;
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
  signal testgen_q, testgen_oe : std_logic;                          -- 外部ピン入力の限界を測る試験用の方形波（コアの出力）
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
  signal swdio_o_lead : std_logic;                                   -- SWDIO / TMS のその周期の前半（ODDR の D1）
  signal uart_cts_n, uart_rts_n, rts_claim, cts_claim : std_logic;
  signal rts_pin, cts_pin : std_logic_vector(1 downto 0);
  signal gpio_drive, gpio_enable, func_o, func_oe, func_o_lead : std_logic_vector(7 downto 0);
  signal pin_o, pin_oe, claimed, pin_o_lead : std_logic_vector(7 downto 0);
  -- PMOD JA の出力の最後の段: 出力は ODDR（xilinx_oddr。端子の OLOGIC、前半 pin_o_lead・後半 pin_o）、Hi-Z の制御は 1 段の FF
  -- （IOB の TFF）。TCK / TMS / TDI の端子までの遅れが配置に依らず揃う（stop-precision §3.1）。その代わり、どの機能の出力も
  -- pin_select の後 1 周期（sys）遅れて端子に出る（入力は変わらない。SWD / JTAG の往復はこの 1 周期だけ長く見える）。
  -- 前半と後半が違うのは JTAG の half_period = 1 の TMS だけ（TCK の立ち下がりの半周期 = 5 ns 後に変わる。jtag_phy の
  -- tms_lead、docs/superpowers/specs/2026-11-03-dap-line-rate-design.md §3）。pad_t = 1 で Hi-Z（OBUFT の T）
  signal pad_q : std_logic_vector(7 downto 0);
  signal pad_t : std_logic_vector(7 downto 0) := (others => '1');
  attribute IOB : string;
  attribute IOB of pad_t : signal is "TRUE";
  -- 同期の出力の最後の 1 段の FF（JA と同じく IOB に詰める。時刻の正しさ R9、段階 S3）: 線の辺が配置に依らず、JA の出力と
  -- 同じ段数（fprobe_gw.timing の BOARD_OUTPUT_TICKS）になる
  signal sync_pad : std_logic := '0';
  attribute IOB of sync_pad : signal is "TRUE";
  signal xout_o, xout_oe, xout_claim : std_logic_vector(1 downto 0);
  signal xout_pin : std_logic_vector(3 downto 0);
  signal heartbeat : unsigned(25 downto 0);
  -- 外部メモリ（MEM_ADDR_BITS = 0 なら使わない: mem_ready = 0）
  -- 取り込みのクロックを使う構成（CAP_RATIO >= 2）だけ、MMCM の 2 本目の出力を出す
  constant CAP_DIV : natural := mmcm_cap_div(work.design_caps_pkg.CAP_RATIO);
  signal cap_clk : std_logic;
  -- 1 周期に複数のサンプル（端子の ISERDESE2）。ch 0〜7 = PMOD JA（FINE_PINS のときだけ）、続いて専用の入力
  constant LANES : positive := work.design_caps_pkg.LA_LANES;
  constant INPUTS : natural := work.design_caps_pkg.LA_INPUTS;
  constant FINE_PINS : boolean := work.design_caps_pkg.LA_FINE_PINS and LANES > 1;
  constant JA_SERDES : natural := 8 * to_natural(FINE_PINS);
  constant SERDES_CH : natural := JA_SERDES + INPUTS * to_natural(LANES > 1);
  signal la_in_lanes : std_logic_vector(maximum(LANES * INPUTS, 1) - 1 downto 0);
  signal ja_lanes : std_logic_vector(LANES * 8 - 1 downto 0);
  signal ja_in : std_logic_vector(7 downto 0);                     -- PMOD JA の端子の値（ファブリックで読む回路はこれを使う）
  constant MEM_BITS : natural := work.design_caps_pkg.MEM_ADDR_BITS;
  constant DEEP : boolean := MEM_BITS > 0;
  constant MEM_SPAN : natural := to_natural(DEEP) * (2 ** MEM_BITS) * 16;   -- MEM_BASE から使うバイト数
  signal mem_errors : unsigned(15 downto 0);
  signal mem_base : unsigned(31 downto 0);
  signal mem_ready, mem_cmd_valid, mem_cmd_ready, mem_cmd_write, mem_rd_valid : std_logic;
  signal mem_cmd_addr : unsigned(maximum(MEM_BITS, 1) - 1 downto 0);
  signal mem_cmd_data, mem_rd_data : std_logic_vector(127 downto 0);
  -- PS が DDR のリングを直接読む流れのレジスタ（窓 zynq_frame_link → コアの la_ps_port。深いバッファの構成だけ意味を持つ）
  signal ps_bus_rd, ps_bus_wr : std_logic;
  signal ps_bus_addr : unsigned(7 downto 0);
  signal ps_bus_wdata, ps_bus_rdata : std_logic_vector(31 downto 0);
  signal mem_wr_pending : unsigned(3 downto 0);
begin
  clock_i : entity work.xilinx_mmcm
    generic map (CLKIN_PERIOD_NS => MMCM_CLKIN_PERIOD_NS, MULT => MMCM_MULT, DIVCLK => MMCM_DIVCLK,
                 OUT_DIV => MMCM_OUT_DIV, CAP_DIV => CAP_DIV)
    port map (clkin => sys_clk_in, clkout => sys_clk, cap_clkout => cap_clk, lock => mmcm_lock);
  sys_reset_i : entity work.reset_sync
    port map (clk => sys_clk, arst_in => not mmcm_lock, rst => sys_rst);

  assert LANES = 1 or LANES = 2 * work.design_caps_pkg.CAP_RATIO
    report "LA_LANES must be 2 * CAP_RATIO (ISERDESE2 DDR: CLK = cap_clk, CLKDIV = sys)" severity failure;
  -- 最も新しいレーンの遅れ: ケイパビリティの生成パッケージの値（時刻の表 fprobe_gw.timing が段数に使う。正は
  -- fprobe_gw.designs.pynq_z2_iserdes_newest_age）と、この包み（xilinx_la_iserdes）の関数が一致すること
  assert work.design_caps_pkg.LA_LANE_IN_AGE = iserdes_newest_age(maximum(LANES, 6)) * to_natural(LANES > 1)
    report "design_caps_pkg.LA_LANE_IN_AGE differs from pynq_z2_pkg.iserdes_newest_age" severity failure;
  lanes_g : if SERDES_CH > 0 generate
    signal serdes_pins, serdes_o : std_logic_vector(SERDES_CH - 1 downto 0);
    signal serdes_q : std_logic_vector(LANES * SERDES_CH - 1 downto 0);
  begin
    serdes_i : entity work.xilinx_la_iserdes
      generic map (CHANNELS => SERDES_CH, LANES => LANES)
      port map (clk_fast => cap_clk, clk_div => sys_clk, rst => sys_rst, pins => serdes_pins, q => serdes_q, o => serdes_o);
    ja_g : if FINE_PINS generate
      serdes_pins(7 downto 0) <= pmod1;
      ja_in <= serdes_o(7 downto 0);
      ja_lane_g : for k in 0 to LANES - 1 generate
        ja_lanes((k + 1) * 8 - 1 downto k * 8) <= serdes_q(k * SERDES_CH + 7 downto k * SERDES_CH);
      end generate;
    end generate;
    no_ja_g : if not FINE_PINS generate
      ja_in <= pmod1;
      ja_lanes <= (others => '0');
    end generate;
    in_g : if INPUTS > 0 generate
      serdes_pins(SERDES_CH - 1 downto JA_SERDES) <= la_in(INPUTS - 1 downto 0);
      in_lane_g : for k in 0 to LANES - 1 generate
        la_in_lanes((k + 1) * INPUTS - 1 downto k * INPUTS) <=
          serdes_q(k * SERDES_CH + SERDES_CH - 1 downto k * SERDES_CH + JA_SERDES);
      end generate;
    end generate;
    no_in_g : if INPUTS = 0 generate
      la_in_lanes <= (others => '0');
    end generate;
  end generate;
  no_lanes_g : if SERDES_CH = 0 generate
    la_in_lanes <= (others => '0');
    ja_lanes <= (others => '0');
    ja_in <= pmod1;
  end generate;
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
    mem_wr_pending <= (others => '0');
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
                errors => mem_errors, wr_pending => mem_wr_pending,
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
              mem_errors => mem_errors,
              ps_bus_rd => ps_bus_rd, ps_bus_wr => ps_bus_wr, ps_bus_addr => ps_bus_addr, ps_bus_wdata => ps_bus_wdata,
              ps_bus_rdata => ps_bus_rdata);

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
    -- 構成の機能はデザインの生成パッケージから全部渡す（ケイパビリティと回路を一致させる。tests/test_core_generics.py）
    -- RESET_SPLIT: 最悪の経路が reset_sync の出力の扇出（約 3500）だった（docs/reports/fprobe-core-notes.md §2）
    -- MEM_SAME_CLOCK: 深いバッファの mem_clk は sys_clk（下の port map）
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
                 CLK_HZ => SYS_CLK_HZ, LA_INTERNAL => not DEEP, LA_FINE_PINS => FINE_PINS, RESET_SPLIT => true, MEM_SAME_CLOCK => true)
    port map (clk => sys_clk, cap_clk => cap_clk, rst => sys_rst,
              ctrl_in_valid => req_valid, ctrl_in_ready => req_ready, ctrl_in_data => req_data,
              ctrl_in_last => req_last,
              ctrl_out_valid => rsp_valid, ctrl_out_ready => rsp_ready, ctrl_out_data => rsp_data,
              ctrl_out_last => rsp_last,
              packet_size => MAX_PAYLOAD,
              dap_in_valid => dreq_valid, dap_in_ready => dreq_ready, dap_in_data => dreq_data, dap_in_last => dreq_last,
              dap_out_valid => drsp_valid, dap_out_ready => drsp_ready, dap_out_data => drsp_data,
              dap_out_last => drsp_last,
              swclk_o => swclk_o, swclk_oe => swclk_oe, swdio_o => swdio_o, swdio_oe => swdio_oe,
              swdio_o_lead => swdio_o_lead,
              swdio_i => ja_in(DBG_SWDIO), nreset_low => nreset_low, nreset_i => ja_in(DBG_NRESET),
              tdi_o => tdi_o, tdi_oe => tdi_oe, tdo_i => ja_in(DBG_SWO) or claimed(DBG_SWO),
              swo_i => ja_in(DBG_SWO) or claimed(DBG_SWO), swo_out_valid => swo_valid, swo_out_ready => swo_ready,
              swo_out_data => swo_data, swo_flush => open,
              uart_baud => line_rate, uart_baud_load => line_changed,
              uart_in_valid => uo_valid, uart_in_ready => uo_ready, uart_in_data => uo_data,
              uart_out_valid => ui_valid, uart_out_ready => ui_ready, uart_out_data => ui_data,
              uart_rx => ja_in(DBG_UART_RX) or claimed(DBG_UART_RX), uart_tx => uart_tx, uart_tx_oe => uart_tx_oe,
              uart_host_rts => '0', uart_host_break => '0', uart_cts_n => uart_cts_n,
              uart_rts_n => uart_rts_n, uart_rts_claim => rts_claim, uart_rts_pin => rts_pin,
              uart_cts_claim => cts_claim, uart_cts_pin => cts_pin, uart_cts => open,
              la_pins => ja_in, la_pin_lanes => ja_lanes,
              la_inputs => la_in(maximum(work.design_caps_pkg.LA_INPUTS, 1) - 1 downto 0), la_input_lanes => la_in_lanes,
              gpio_drive => gpio_drive, gpio_enable => gpio_enable, gpio_level => ja_in,
              xout_o => xout_o, xout_oe => xout_oe, xout_claim => xout_claim, xout_pin => xout_pin,
              mem_clk => sys_clk, mem_rst => sys_rst, mem_ready => mem_ready,
              mem_cmd_valid => mem_cmd_valid, mem_cmd_ready => mem_cmd_ready, mem_cmd_write => mem_cmd_write,
              mem_cmd_addr => mem_cmd_addr, mem_cmd_data => mem_cmd_data, mem_rd_valid => mem_rd_valid,
              mem_rd_data => mem_rd_data, mem_wr_pending => mem_wr_pending,
              stream_out_valid => st_valid, stream_out_ready => st_ready, stream_out_data => st_data,
              ps_bus_rd => ps_bus_rd, ps_bus_wr => ps_bus_wr, ps_bus_addr => ps_bus_addr, ps_bus_wdata => ps_bus_wdata,
              ps_bus_rdata => ps_bus_rdata,
              obs_tck => obs_tck_o, obs_tms => obs_tms_o, obs_tdi => obs_tdi_o, obs_tdo => obs_tdo,
              obs_oe => obs_oe, sync_in => sync_in, sync_out => sync_o, testgen_out => testgen_q, testgen_oe => testgen_oe);
  process (sys_clk)
  begin
    if rising_edge(sys_clk) then
      sync_pad <= sync_o;
    end if;
  end process;
  sync_out <= sync_pad when work.design_caps_pkg.SYNC else 'Z';
  testgen_pin <= testgen_q when testgen_oe = '1' else 'Z';
  obs_tck <= obs_tck_o when obs_oe = '1' else 'Z';
  obs_tms <= obs_tms_o when obs_oe = '1' else 'Z';
  obs_tdi <= obs_tdi_o when obs_oe = '1' else 'Z';

  -- デバッグポートの機能ピン（それ以外のピンは入力のまま）と、UART の RTS/CTS が取った端子
  pin_func_i : entity work.debug_pin_func
    generic map (DBG_SWCLK => DBG_SWCLK, DBG_TDI => DBG_TDI, DBG_NRESET => DBG_NRESET, DBG_SWDIO => DBG_SWDIO,
                 DBG_UART_TX => DBG_UART_TX, FLOW_PIN_CHOICES => (UART_FLOW_PIN_CHOICES(0), UART_FLOW_PIN_CHOICES(1)))
    port map (swclk_o => swclk_o, swclk_oe => swclk_oe, swdio_o => swdio_o, swdio_oe => swdio_oe, nreset_low => nreset_low,
              tdi_o => tdi_o, tdi_oe => tdi_oe, uart_tx => uart_tx, uart_tx_oe => uart_tx_oe,
              rts_claim => rts_claim, rts_pin => rts_pin, uart_rts_n => uart_rts_n, cts_claim => cts_claim,
              cts_pin => cts_pin, pins_in => ja_in, func_o => func_o, func_oe => func_oe, uart_cts_n => uart_cts_n);

  pin_select_i : entity work.pin_select
    generic map (PINS => 8, ACTIONS => 2, PIN_CHOICES => (XOUT_PIN_CHOICES(0), XOUT_PIN_CHOICES(1), XOUT_PIN_CHOICES(2), XOUT_PIN_CHOICES(3)))
    port map (gpio_drive => gpio_drive, gpio_enable => gpio_enable, func_o => func_o, func_oe => func_oe,
              xout_o => xout_o, xout_oe => xout_oe, xout_claim => xout_claim, xout_pin => xout_pin,
              pin_o => pin_o, pin_oe => pin_oe, claimed => claimed);

  -- 周期の前半の値: 機能の出力のうち SWDIO / TMS だけ前半の値に替え、同じ選択を通す（gpio・クロストリガの出力は前半も同じ）
  process (all)
  begin
    func_o_lead <= func_o;
    func_o_lead(DBG_SWDIO) <= swdio_o_lead;
  end process;
  pin_select_lead_i : entity work.pin_select
    generic map (PINS => 8, ACTIONS => 2, PIN_CHOICES => (XOUT_PIN_CHOICES(0), XOUT_PIN_CHOICES(1), XOUT_PIN_CHOICES(2), XOUT_PIN_CHOICES(3)))
    port map (gpio_drive => gpio_drive, gpio_enable => gpio_enable, func_o => func_o_lead, func_oe => func_oe,
              xout_o => xout_o, xout_oe => xout_oe, xout_claim => xout_claim, xout_pin => xout_pin,
              pin_o => pin_o_lead, pin_oe => open, claimed => open);

  oddr_i : entity work.xilinx_oddr
    generic map (WIDTH => 8)
    port map (clk => sys_clk, d1 => pin_o_lead, d2 => pin_o, q => pad_q);

  process (sys_clk)
  begin
    if rising_edge(sys_clk) then
      pad_t <= not pin_oe;
    end if;
  end process;

  pins : for i in pmod1'range generate
    pmod1(i) <= pad_q(i) when pad_t(i) = '0' else 'Z';
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
