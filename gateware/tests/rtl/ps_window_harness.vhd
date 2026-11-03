-- テスト専用: zynq_frame_link（AXI4-Lite の窓）+ la_ps_port（PS が DDR を直接読む流れのレジスタ）。PYNQ の fprobe_core と同じ
-- つなぎ方（窓の ps_bus_* → la_ps_port）。la_deep_buffer の側（流れの番号・公開された組・読み出しの要求）はテストベンチが入れる。
library ieee;
use ieee.std_logic_1164.all;
use ieee.numeric_std.all;

entity ps_window_harness is
  port (
    clk            : in  std_logic;
    rst            : in  std_logic;
    s_axi_awaddr   : in  std_logic_vector(11 downto 0);
    s_axi_awvalid  : in  std_logic;
    s_axi_awready  : out std_logic;
    s_axi_wdata    : in  std_logic_vector(31 downto 0);
    s_axi_wstrb    : in  std_logic_vector(3 downto 0);
    s_axi_wvalid   : in  std_logic;
    s_axi_wready   : out std_logic;
    s_axi_bresp    : out std_logic_vector(1 downto 0);
    s_axi_bvalid   : out std_logic;
    s_axi_bready   : in  std_logic;
    s_axi_araddr   : in  std_logic_vector(11 downto 0);
    s_axi_arvalid  : in  std_logic;
    s_axi_arready  : out std_logic;
    s_axi_rdata    : out std_logic_vector(31 downto 0);
    s_axi_rresp    : out std_logic_vector(1 downto 0);
    s_axi_rvalid   : out std_logic;
    s_axi_rready   : in  std_logic;
    epoch          : in  unsigned(15 downto 0);
    pub_epoch      : in  unsigned(15 downto 0);
    pub_active     : in  std_logic;
    pub_writing    : in  std_logic;
    pub_overrun    : in  std_logic;
    pub_la_written : in  unsigned(31 downto 0);
    pub_la_wptr    : in  unsigned(31 downto 0);
    pub_ev_wpos    : in  unsigned(31 downto 0);
    pub_ev_start   : in  unsigned(31 downto 0);
    rd_epoch       : in  unsigned(7 downto 0);
    rd_region      : in  std_logic;
    rd_first       : in  unsigned(31 downto 0);
    rd_count       : in  unsigned(31 downto 0);
    ret_epoch      : out unsigned(15 downto 0);
    la_consumed    : out unsigned(31 downto 0);
    ev_consumed    : out unsigned(31 downto 0);
    out_beats      : out unsigned(31 downto 0);
    rd_ack         : out unsigned(7 downto 0)
  );
end entity;

architecture rtl of ps_window_harness is
  signal bus_rd, bus_wr : std_logic;
  signal bus_addr : unsigned(7 downto 0);
  signal bus_wdata, bus_rdata : std_logic_vector(31 downto 0);
begin
  link_i : entity work.zynq_frame_link
    generic map (MEM_BYTES => 32 * 1024 * 1024)
    port map (clk => clk, rst => rst,
              s_axi_awaddr => s_axi_awaddr, s_axi_awvalid => s_axi_awvalid, s_axi_awready => s_axi_awready,
              s_axi_wdata => s_axi_wdata, s_axi_wstrb => s_axi_wstrb, s_axi_wvalid => s_axi_wvalid,
              s_axi_wready => s_axi_wready, s_axi_bresp => s_axi_bresp, s_axi_bvalid => s_axi_bvalid,
              s_axi_bready => s_axi_bready, s_axi_araddr => s_axi_araddr, s_axi_arvalid => s_axi_arvalid,
              s_axi_arready => s_axi_arready, s_axi_rdata => s_axi_rdata, s_axi_rresp => s_axi_rresp,
              s_axi_rvalid => s_axi_rvalid, s_axi_rready => s_axi_rready,
              rx_we => open, rx_waddr => open, rx_wdata => open, rx_commit => open, rx_len => open, rx_busy => '0',
              tx_valid => '0', tx_len => 1, tx_raddr => open, tx_rdata => (others => '0'), tx_done => open,
              mem_base => open, mem_errors => (others => '0'),
              ps_bus_rd => bus_rd, ps_bus_wr => bus_wr, ps_bus_addr => bus_addr, ps_bus_wdata => bus_wdata,
              ps_bus_rdata => bus_rdata);
  port_i : entity work.la_ps_port
    port map (clk => clk, rst => rst, bus_rd => bus_rd, bus_wr => bus_wr, bus_addr => bus_addr, bus_wdata => bus_wdata,
              bus_rdata => bus_rdata, epoch => epoch, pub_epoch => pub_epoch, pub_active => pub_active,
              pub_writing => pub_writing, pub_overrun => pub_overrun, pub_la_written => pub_la_written,
              pub_la_wptr => pub_la_wptr, pub_ev_wpos => pub_ev_wpos, pub_ev_start => pub_ev_start,
              rd_epoch => rd_epoch, rd_region => rd_region, rd_first => rd_first, rd_count => rd_count,
              ret_epoch => ret_epoch, la_consumed => la_consumed, ev_consumed => ev_consumed, out_beats => out_beats,
              rd_ack => rd_ack);
end architecture;
