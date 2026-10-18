-- メモリ試験（mem_test）を sys ドメインから使う: 起動・引数と結果をクロックドメイン間で受け渡す。
-- 仕様: docs/superpowers/specs/2026-10-16-m8-ddr3-design.md §2
--
-- start で引数を mem ドメインへ渡して試験を始める。結果（busy・done・数値）は mem ドメインで状態が変わるたびに
-- 束ねて sys へ渡す。start から新しい結果が届くまでは busy（前回の done を見せない）。
-- TESTER = false なら試験回路を持たず（状態の同期だけ）、メモリ I/F には何も出さない。
library ieee;
use ieee.std_logic_1164.all;
use ieee.numeric_std.all;

entity mem_test_sys is
  generic (
    ADDR_BITS : positive := 24;
    DATA_BITS : positive := 128;
    TESTER    : boolean := true
  );
  port (
    -- sys ドメイン
    clk          : in  std_logic;
    rst          : in  std_logic;
    start        : in  std_logic;
    base         : in  unsigned(ADDR_BITS - 1 downto 0);
    count        : in  unsigned(ADDR_BITS downto 0);
    seed         : in  std_logic_vector(31 downto 0);
    ready        : out std_logic;
    in_reset     : out std_logic;                          -- 診断: mem 側がリセット中
    clock_tick   : out std_logic;                          -- 診断: mem_clk で数えた計数の 1 ビット
    busy         : out std_logic;
    done         : out std_logic;
    errors       : out unsigned(31 downto 0);
    first_error  : out unsigned(ADDR_BITS - 1 downto 0);
    write_cycles : out unsigned(31 downto 0);
    read_cycles  : out unsigned(31 downto 0);
    -- mem ドメイン（mem_port）
    mem_clk      : in  std_logic;
    mem_rst      : in  std_logic;
    mem_ready    : in  std_logic;
    cmd_valid    : out std_logic;
    cmd_ready    : in  std_logic;
    cmd_write    : out std_logic;
    cmd_addr     : out unsigned(ADDR_BITS - 1 downto 0);
    cmd_data     : out std_logic_vector(DATA_BITS - 1 downto 0);
    rd_valid     : in  std_logic;
    rd_data      : in  std_logic_vector(DATA_BITS - 1 downto 0)
  );
end entity;

architecture rtl of mem_test_sys is
  constant ARGS_BITS   : positive := 2 * ADDR_BITS + 1 + 32;
  constant RESULT_BITS : positive := 2 + 32 + ADDR_BITS + 64;

  signal args_src, args_dst : std_logic_vector(ARGS_BITS - 1 downto 0);
  signal m_start  : std_logic;
  signal m_busy, m_done : std_logic;
  signal m_errors, m_wc, m_rc : unsigned(31 downto 0);
  signal m_first  : unsigned(ADDR_BITS - 1 downto 0);
  signal m_state, m_state_prev : std_logic_vector(1 downto 0) := "00";
  signal m_load   : std_logic := '0';
  signal result_src, result_dst : std_logic_vector(RESULT_BITS - 1 downto 0);
  signal result_new : std_logic;
  signal pending  : std_logic := '0';
  signal ticks    : unsigned(19 downto 0) := (others => '0');
begin
  no_tester : if not TESTER generate
    busy <= '0'; done <= '0'; errors <= (others => '0'); first_error <= (others => '0');
    write_cycles <= (others => '0'); read_cycles <= (others => '0');
    cmd_valid <= '0'; cmd_write <= '0'; cmd_addr <= (others => '0'); cmd_data <= (others => '0');
  end generate;

  tester_g : if TESTER generate
  -- sys → mem: 引数（start で保持して渡す。届いた周期が mem 側の start）
  args_src <= std_logic_vector(base) & std_logic_vector(count) & seed;
  args_i : entity work.cdc_value
    generic map (WIDTH => ARGS_BITS)
    port map (src_clk => clk, src_load => start, src_value => args_src,
              dst_clk => mem_clk, dst_value => args_dst, dst_changed => m_start);

  test_i : entity work.mem_test
    generic map (ADDR_BITS => ADDR_BITS, DATA_BITS => DATA_BITS)
    port map (clk => mem_clk, rst => mem_rst, start => m_start,
              base => unsigned(args_dst(ARGS_BITS - 1 downto ADDR_BITS + 33)),
              count => unsigned(args_dst(ADDR_BITS + 32 downto 32)), seed => args_dst(31 downto 0),
              busy => m_busy, done => m_done, errors => m_errors, first_error => m_first,
              write_cycles => m_wc, read_cycles => m_rc,
              cmd_valid => cmd_valid, cmd_ready => cmd_ready, cmd_write => cmd_write, cmd_addr => cmd_addr,
              cmd_data => cmd_data, rd_valid => rd_valid, rd_data => rd_data);

  -- mem → sys: 状態が変わるたびに結果を束ねて渡す（変化は試験 1 回に数回だけ）
  m_state <= m_busy & m_done;
  process (mem_clk)
  begin
    if rising_edge(mem_clk) then
      m_state_prev <= m_state;
      m_load       <= '1' when m_state /= m_state_prev else '0';
    end if;
  end process;
  result_src <= m_state & std_logic_vector(m_errors) & std_logic_vector(m_first) & std_logic_vector(m_wc)
                & std_logic_vector(m_rc);
  result_i : entity work.cdc_value
    generic map (WIDTH => RESULT_BITS)
    port map (src_clk => mem_clk, src_load => m_load, src_value => result_src,
              dst_clk => clk, dst_value => result_dst, dst_changed => result_new);

  -- start から、試験が終わった結果が届くまでは busy
  process (clk)
  begin
    if rising_edge(clk) then
      if rst = '1' then
        pending <= '0';
      elsif start = '1' then
        pending <= '1';
      elsif result_new = '1' and result_dst(RESULT_BITS - 2) = '1' then
        pending <= '0';
      end if;
    end if;
  end process;

  busy         <= pending or result_dst(RESULT_BITS - 1);
  done         <= result_dst(RESULT_BITS - 2) and not pending;
  errors       <= unsigned(result_dst(RESULT_BITS - 3 downto RESULT_BITS - 34));
  first_error  <= unsigned(result_dst(64 + ADDR_BITS - 1 downto 64));
  write_cycles <= unsigned(result_dst(63 downto 32));
  read_cycles  <= unsigned(result_dst(31 downto 0));
  end generate;

  ready_i : entity work.sync_bit
    port map (clk => clk, d => mem_ready, q => ready);
  reset_i : entity work.sync_bit
    port map (clk => clk, d => mem_rst, q => in_reset);
  process (mem_clk)
  begin
    if rising_edge(mem_clk) then
      ticks <= ticks + 1;
    end if;
  end process;
  tick_i : entity work.sync_bit
    port map (clk => clk, d => ticks(ticks'high), q => clock_tick);
end architecture;
