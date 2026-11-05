-- テスト専用: 生成器の試験ブロック csr_probe（tests/test_csr_generator.py の TEST_BLOCK）を、フラットなポートで包む。
-- 生成されたバンクは要求・応答を record（csr_cmd_t・csr_rsp_t）で受け渡すが、GHDL の VPI は record のポートを
-- 駆動できないので、cocotb の toplevel にはこのラッパーを使う。
library ieee;
use ieee.std_logic_1164.all;
use work.csr_bus_pkg.all;

entity csr_probe_harness is
  port (
    clk          : in  std_logic;
    rst          : in  std_logic;
    bus_req      : in  std_logic;
    bus_we       : in  std_logic;
    bus_addr     : in  csr_addr_t;
    bus_wdata    : in  csr_word_t;
    bus_ack      : out std_logic;
    bus_err      : out std_logic;
    bus_rdata    : out csr_word_t;
    ctrl_enable  : out std_logic;
    ctrl_mode    : out std_logic_vector(2 downto 0);
    status_busy  : in  std_logic;
    status_count : in  std_logic_vector(15 downto 0);
    word         : out std_logic_vector(31 downto 0);
    snap         : in  std_logic_vector(31 downto 0);
    snap_read    : out std_logic;
    go_start     : out std_logic;
    go_lanes     : out std_logic_vector(3 downto 0);
    pins         : in  std_logic_vector(7 downto 0)
  );
end entity;

architecture rtl of csr_probe_harness is
  signal bus_cmd : csr_cmd_t;
  signal bus_rsp : csr_rsp_t;
begin
  bus_cmd <= (we => bus_we, addr => bus_addr, wdata => bus_wdata);
  bus_ack   <= bus_rsp.ack;
  bus_err   <= bus_rsp.err;
  bus_rdata <= bus_rsp.rdata;

  dut_i : entity work.csr_probe
    port map (clk => clk, rst => rst, bus_req => bus_req, bus_cmd => bus_cmd, bus_rsp => bus_rsp,
              ctrl_enable => ctrl_enable, ctrl_mode => ctrl_mode, status_busy => status_busy,
              status_count => status_count, word => word, snap => snap, snap_read => snap_read,
              go_start => go_start, go_lanes => go_lanes, pins => pins);
end architecture;
