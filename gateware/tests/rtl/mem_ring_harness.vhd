-- テスト専用: la_mem_writer と mem_reader を 1 つの mem_port に載せる（use_reader で切り替え）。容量 2^ADDR_BITS 拍。
library ieee;
use ieee.std_logic_1164.all;
use ieee.numeric_std.all;

entity mem_ring_harness is
  generic (
    ADDR_BITS : positive := 10
  );
  port (
    clk          : in  std_logic;
    rst          : in  std_logic;
    use_reader   : in  std_logic;
    -- 書き込み
    w_start      : in  std_logic;
    w_stop       : in  std_logic;
    post_beats   : in  unsigned(ADDR_BITS - 1 downto 0);
    in_valid     : in  std_logic;
    in_ready     : out std_logic;
    in_data      : in  std_logic_vector(127 downto 0);
    in_kind      : in  std_logic := '0';
    ev_enable    : in  std_logic := '0';
    in_trig      : in  std_logic;
    in_slot      : in  unsigned(2 downto 0);
    running      : out std_logic;
    triggered    : out std_logic;
    done         : out std_logic;
    trigger_beat : out unsigned(ADDR_BITS - 1 downto 0);
    trigger_slot : out unsigned(2 downto 0);
    written      : out unsigned(ADDR_BITS downto 0);
    ev_written   : out unsigned(31 downto 0);
    -- 読み出し
    r_start      : in  std_logic;
    region_base  : in  unsigned(ADDR_BITS - 1 downto 0);
    region_size  : in  unsigned(ADDR_BITS downto 0);
    first        : in  unsigned(ADDR_BITS - 1 downto 0);
    count        : in  unsigned(ADDR_BITS downto 0);
    r_busy       : out std_logic;
    out_valid    : out std_logic;
    out_ready    : in  std_logic;
    out_data     : out std_logic_vector(127 downto 0);
    -- mem_port
    cmd_valid    : out std_logic;
    cmd_ready    : in  std_logic;
    cmd_write    : out std_logic;
    cmd_addr     : out unsigned(ADDR_BITS - 1 downto 0);
    cmd_data     : out std_logic_vector(127 downto 0);
    rd_valid     : in  std_logic;
    rd_data      : in  std_logic_vector(127 downto 0)
  );
end entity;

architecture rtl of mem_ring_harness is
  signal w_valid, w_ready, w_write, r_valid, r_ready, r_write : std_logic;
  signal w_addr, r_addr : unsigned(ADDR_BITS - 1 downto 0);
  signal w_data, r_data : std_logic_vector(127 downto 0);
begin
  writer_i : entity work.la_mem_writer
    generic map (ADDR_BITS => ADDR_BITS)
    port map (clk => clk, rst => rst, start => w_start, stop => w_stop, post_beats => post_beats, stream => '0',
              ev_enable => ev_enable,
              in_valid => in_valid, in_ready => in_ready, in_data => in_data, in_kind => in_kind, in_trig => in_trig,
              in_slot => in_slot,
              running => running, triggered => triggered, done => done, trigger_beat => trigger_beat,
              trigger_slot => trigger_slot, written => written, total => open, ev_written => ev_written,
              cmd_valid => w_valid, cmd_ready => w_ready, cmd_write => w_write, cmd_addr => w_addr, cmd_data => w_data);

  reader_i : entity work.mem_reader
    generic map (ADDR_BITS => ADDR_BITS, LANDING => 16)
    port map (clk => clk, rst => rst, start => r_start, region_base => region_base, region_size => region_size,
              first => first, count => count, busy => r_busy,
              out_valid => out_valid, out_ready => out_ready, out_data => out_data,
              cmd_valid => r_valid, cmd_ready => r_ready, cmd_write => r_write, cmd_addr => r_addr, cmd_data => r_data,
              rd_valid => rd_valid, rd_data => rd_data);

  cmd_valid <= r_valid when use_reader = '1' else w_valid;
  cmd_write <= r_write when use_reader = '1' else w_write;
  cmd_addr  <= r_addr when use_reader = '1' else w_addr;
  cmd_data  <= r_data when use_reader = '1' else w_data;
  r_ready   <= cmd_ready and use_reader;
  w_ready   <= cmd_ready and not use_reader;
end architecture;
