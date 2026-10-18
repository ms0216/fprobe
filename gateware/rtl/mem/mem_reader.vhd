-- 外部メモリの拍の範囲を読んでストリームに出す（mem ドメイン）。仕様: docs/superpowers/specs/2026-10-16-m8-ddr3-design.md §5
--
-- start で、領域（region_base から region_size 拍）の中の first 拍目から count 拍を読む（領域の中で折り返す）。mem_port の読み出しの結果は止められないので、
-- 受け皿（同期 FIFO、LANDING 拍）に入れ、受け皿の空き（発行済みで出し終えていない数）を見ながらコマンドを出す。
library ieee;
use ieee.std_logic_1164.all;
use ieee.numeric_std.all;

entity mem_reader is
  generic (
    ADDR_BITS : positive := 23;
    LANDING   : positive := 64                                -- 受け皿の深さ（2 の冪）
  );
  port (
    clk       : in  std_logic;
    rst       : in  std_logic;
    start     : in  std_logic;                                -- 1 周期（以下を取り込む）
    region_base : in unsigned(ADDR_BITS - 1 downto 0);
    region_size : in unsigned(ADDR_BITS downto 0);
    first     : in  unsigned(ADDR_BITS - 1 downto 0);            -- 領域の中の位置
    count     : in  unsigned(ADDR_BITS downto 0);
    busy      : out std_logic;                                -- 出し終えるまで
    -- 出力
    out_valid : out std_logic;
    out_ready : in  std_logic;
    out_data  : out std_logic_vector(127 downto 0);
    -- メモリ（mem_port、読み出しのみ）
    cmd_valid : out std_logic;
    cmd_ready : in  std_logic;
    cmd_write : out std_logic;
    cmd_addr  : out unsigned(ADDR_BITS - 1 downto 0);
    cmd_data  : out std_logic_vector(127 downto 0);
    rd_valid  : in  std_logic;
    rd_data   : in  std_logic_vector(127 downto 0)
  );
end entity;

architecture rtl of mem_reader is
  signal addr      : unsigned(ADDR_BITS - 1 downto 0) := (others => '0');
  signal offset    : unsigned(ADDR_BITS downto 0) := (others => '0');
  signal base      : unsigned(ADDR_BITS - 1 downto 0) := (others => '0');
  signal size      : unsigned(ADDR_BITS downto 0) := (others => '0');
  signal to_issue  : unsigned(ADDR_BITS downto 0) := (others => '0');   -- まだ出していないコマンド
  signal to_output : unsigned(ADDR_BITS downto 0) := (others => '0');   -- まだ出力していない拍
  signal in_flight : natural range 0 to LANDING := 0;                   -- 発行済みで受け皿から出していない
  signal issue     : boolean;
  signal pop       : boolean;
  signal fifo_valid : std_logic;
begin
  issue     <= to_issue /= 0 and in_flight < LANDING and cmd_ready = '1';
  cmd_valid <= '1' when to_issue /= 0 and in_flight < LANDING else '0';
  cmd_write <= '0';
  cmd_addr  <= addr;
  cmd_data  <= (others => '0');
  busy      <= '1' when to_output /= 0 else '0';

  landing_i : entity work.sync_fifo
    generic map (WIDTH => 128, DEPTH => LANDING)
    port map (clk => clk, rst => rst or start, in_valid => rd_valid, in_ready => open, in_data => rd_data,
              out_valid => fifo_valid, out_ready => out_ready, out_data => out_data, level => open);
  out_valid <= fifo_valid;
  pop       <= fifo_valid = '1' and out_ready = '1';

  process (clk)
  begin
    if rising_edge(clk) then
      if rst = '1' then
        to_issue  <= (others => '0');
        to_output <= (others => '0');
        in_flight <= 0;
      elsif start = '1' then
        base      <= region_base;
        size      <= region_size;
        offset    <= resize(first, ADDR_BITS + 1);
        addr      <= region_base + first;
        to_issue  <= count;
        to_output <= count;
        in_flight <= 0;
      else
        if issue then
          if offset + 1 = size then                          -- 領域の終わり → 先頭へ
            offset <= (others => '0');
            addr   <= base;
          else
            offset <= offset + 1;
            addr   <= addr + 1;
          end if;
          to_issue <= to_issue - 1;
        end if;
        if issue and not pop then
          in_flight <= in_flight + 1;
        elsif pop and not issue then
          in_flight <= in_flight - 1;
        end if;
        if pop then
          to_output <= to_output - 1;
        end if;
      end if;
    end if;
  end process;
end architecture;
