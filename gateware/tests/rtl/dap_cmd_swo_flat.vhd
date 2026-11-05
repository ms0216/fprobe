-- 試験用: dap_cmd_swo の dap_packet 側の record ポート（unit_req_t / unit_rsp_t）を、平たいポートにして出す。
-- GHDL の VPI は record のポートを駆動できないため、cocotb の toplevel はこれにする（tests/test_swo.py）。
library ieee;
use ieee.std_logic_1164.all;
use ieee.numeric_std.all;
use work.dap_pkg.all;

entity dap_cmd_swo_flat is
  generic (
    CLK_HZ         : positive;
    HAS_MANCHESTER : boolean := true
  );
  port (
    clk         : in  std_logic;
    rst         : in  std_logic;
    command     : in  natural range 0 to 255;
    start       : in  std_logic;
    done        : out std_logic;
    get_req     : out std_logic;
    get_n       : out natural range 1 to 8;
    get_ack     : in  std_logic;
    arg         : in  std_logic_vector(63 downto 0);
    put_req     : out std_logic;
    put_n       : out natural range 1 to 8;
    put_val     : out std_logic_vector(63 downto 0);
    put_ack     : in  std_logic;
    swo_run     : out std_logic;
    swo_divisor : out unsigned(15 downto 0);
    swo_manchester : out std_logic;
    swo_error   : in  std_logic;
    swo_overrun : in  std_logic;
    div_start   : out std_logic;
    div_num     : out unsigned(31 downto 0);
    div_den     : out unsigned(31 downto 0);
    div_done    : in  std_logic;
    div_quot    : in  unsigned(31 downto 0)
  );
end entity;

architecture rtl of dap_cmd_swo_flat is
  signal req : unit_req_t;
  signal rsp : unit_rsp_t;
begin
  req.command <= command;
  req.start   <= start;
  req.get_ack <= get_ack;
  req.arg     <= arg;
  req.put_ack <= put_ack;
  done    <= rsp.done;
  get_req <= rsp.get_req;
  get_n   <= rsp.get_n;
  put_req <= rsp.put_req;
  put_n   <= rsp.put_n;
  put_val <= rsp.put_val;

  swo_i : entity work.dap_cmd_swo
    generic map (CLK_HZ => CLK_HZ, HAS_MANCHESTER => HAS_MANCHESTER)
    port map (clk => clk, rst => rst, req => req, rsp => rsp,
              swo_run => swo_run, swo_divisor => swo_divisor, swo_manchester => swo_manchester,
              swo_error => swo_error, swo_overrun => swo_overrun,
              div_start => div_start, div_num => div_num, div_den => div_den,
              div_done => div_done, div_quot => div_quot);
end architecture;
