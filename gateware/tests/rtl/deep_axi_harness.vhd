-- テスト専用: la_deep_buffer（ロジアナの深いバッファ）の外部メモリの口を axi_mem_port で AXI3 にする（PYNQ の P3 と同じ
-- つなぎ。mem_clk = clk）。深いバッファの取り込み・読み戻しを、AXI3 のメモリの模型を相手に端から端まで確かめる。
library ieee;
use ieee.std_logic_1164.all;
use ieee.numeric_std.all;

entity deep_axi_harness is
  generic (
    WIDTH     : positive := 16;
    ADDR_BITS : positive := 10
  );
  port (
    clk          : in  std_logic;
    rst          : in  std_logic;
    probes       : in  std_logic_vector(WIDTH - 1 downto 0);
    ext_trigger  : in  std_logic;
    time         : in  unsigned(63 downto 0);
    rate         : in  unsigned(15 downto 0);
    trig_mask    : in  std_logic_vector(WIDTH - 1 downto 0);
    trig_value   : in  std_logic_vector(WIDTH - 1 downto 0);
    trig_rise    : in  std_logic_vector(WIDTH - 1 downto 0);
    trig_fall    : in  std_logic_vector(WIDTH - 1 downto 0);
    pw_enable    : in  std_logic;
    pw_event     : in  std_logic;
    run          : in  std_logic;
    rle          : in  std_logic;
    stream       : in  std_logic;
    post_beats   : in  unsigned(ADDR_BITS - 1 downto 0);
    pretrigger   : in  unsigned(31 downto 0);
    capturing    : out std_logic;
    triggered    : out std_logic;
    done         : out std_logic;
    overrun      : out std_logic;
    trigger_beat : out unsigned(ADDR_BITS - 1 downto 0);
    trigger_slot : out unsigned(2 downto 0);
    written      : out unsigned(ADDR_BITS downto 0);
    trigger_time : out unsigned(63 downto 0);
    stream_beats : out unsigned(31 downto 0);
    ev_enable    : in  std_logic;
    ev_valid     : in  std_logic;
    ev_data      : in  std_logic_vector(127 downto 0);
    ev_written   : out unsigned(31 downto 0);
    ev_lost      : out unsigned(31 downto 0);
    la_beats     : out unsigned(ADDR_BITS downto 0);
    ev_beats     : out unsigned(ADDR_BITS downto 0);
    read         : in  std_logic;
    read_region  : in  std_logic;
    read_first   : in  unsigned(ADDR_BITS - 1 downto 0);
    read_count   : in  unsigned(ADDR_BITS downto 0);
    reading      : out std_logic;
    out_valid    : out std_logic;
    out_ready    : in  std_logic;
    out_data     : out std_logic_vector(7 downto 0);
    base          : in  unsigned(31 downto 0);
    mem_errors    : out unsigned(15 downto 0);
    m_axi_awaddr  : out std_logic_vector(31 downto 0);
    m_axi_awlen   : out std_logic_vector(3 downto 0);
    m_axi_awsize  : out std_logic_vector(2 downto 0);
    m_axi_awburst : out std_logic_vector(1 downto 0);
    m_axi_awvalid : out std_logic;
    m_axi_awready : in  std_logic;
    m_axi_wdata   : out std_logic_vector(63 downto 0);
    m_axi_wstrb   : out std_logic_vector(7 downto 0);
    m_axi_wlast   : out std_logic;
    m_axi_wvalid  : out std_logic;
    m_axi_wready  : in  std_logic;
    m_axi_bresp   : in  std_logic_vector(1 downto 0);
    m_axi_bvalid  : in  std_logic;
    m_axi_bready  : out std_logic;
    m_axi_araddr  : out std_logic_vector(31 downto 0);
    m_axi_arlen   : out std_logic_vector(3 downto 0);
    m_axi_arsize  : out std_logic_vector(2 downto 0);
    m_axi_arburst : out std_logic_vector(1 downto 0);
    m_axi_arvalid : out std_logic;
    m_axi_arready : in  std_logic;
    m_axi_rdata   : in  std_logic_vector(63 downto 0);
    m_axi_rresp   : in  std_logic_vector(1 downto 0);
    m_axi_rlast   : in  std_logic;
    m_axi_rvalid  : in  std_logic;
    m_axi_rready  : out std_logic
  );
end entity;

architecture rtl of deep_axi_harness is
  signal cmd_valid, cmd_ready, cmd_write, rd_valid : std_logic;
  signal cmd_addr : unsigned(ADDR_BITS - 1 downto 0);
  signal cmd_data, rd_data : std_logic_vector(127 downto 0);
begin
  deep_i : entity work.la_deep_buffer
    generic map (WIDTH => WIDTH, ADDR_BITS => ADDR_BITS)
    port map (clk => clk,
              rst => rst,
              probes => probes,
              ext_trigger => ext_trigger,
              time => time,
              rate => rate,
              trig_mask => trig_mask,
              trig_value => trig_value,
              trig_rise => trig_rise,
              trig_fall => trig_fall,
              pw_enable => pw_enable,
              pw_event => pw_event,
              run => run,
              rle => rle,
              stream => stream,
              post_beats => post_beats,
              pretrigger => pretrigger,
              capturing => capturing,
              triggered => triggered,
              done => done,
              overrun => overrun,
              trigger_beat => trigger_beat,
              trigger_slot => trigger_slot,
              written => written,
              trigger_time => trigger_time,
              stream_beats => stream_beats,
              ev_enable => ev_enable,
              ev_valid => ev_valid,
              ev_data => ev_data,
              ev_written => ev_written,
              ev_lost => ev_lost,
              la_beats => la_beats,
              ev_beats => ev_beats,
              read => read,
              read_region => read_region,
              read_first => read_first,
              read_count => read_count,
              reading => reading,
              out_valid => out_valid,
              out_ready => out_ready,
              out_data => out_data,
              mem_clk => clk, mem_rst => rst, mem_active => open,
              cmd_valid => cmd_valid, cmd_ready => cmd_ready, cmd_write => cmd_write, cmd_addr => cmd_addr,
              cmd_data => cmd_data, rd_valid => rd_valid, rd_data => rd_data);

  mem_i : entity work.axi_mem_port
    generic map (ADDR_BITS => ADDR_BITS)
    port map (clk => clk, rst => rst, base => base, ready => open,
              cmd_valid => cmd_valid, cmd_ready => cmd_ready, cmd_write => cmd_write, cmd_addr => cmd_addr,
              cmd_data => cmd_data, rd_valid => rd_valid, rd_data => rd_data, errors => mem_errors,
              m_axi_awaddr => m_axi_awaddr,
              m_axi_awlen => m_axi_awlen,
              m_axi_awsize => m_axi_awsize,
              m_axi_awburst => m_axi_awburst,
              m_axi_awvalid => m_axi_awvalid,
              m_axi_awready => m_axi_awready,
              m_axi_wdata => m_axi_wdata,
              m_axi_wstrb => m_axi_wstrb,
              m_axi_wlast => m_axi_wlast,
              m_axi_wvalid => m_axi_wvalid,
              m_axi_wready => m_axi_wready,
              m_axi_bresp => m_axi_bresp,
              m_axi_bvalid => m_axi_bvalid,
              m_axi_bready => m_axi_bready,
              m_axi_araddr => m_axi_araddr,
              m_axi_arlen => m_axi_arlen,
              m_axi_arsize => m_axi_arsize,
              m_axi_arburst => m_axi_arburst,
              m_axi_arvalid => m_axi_arvalid,
              m_axi_arready => m_axi_arready,
              m_axi_rdata => m_axi_rdata,
              m_axi_rresp => m_axi_rresp,
              m_axi_rlast => m_axi_rlast,
              m_axi_rvalid => m_axi_rvalid,
              m_axi_rready => m_axi_rready);
end architecture;
