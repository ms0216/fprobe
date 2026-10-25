-- 外部メモリの拍の範囲を読んでストリームに出す（mem ドメイン）。仕様: docs/superpowers/specs/2026-10-16-m8-ddr3-design.md §5
--
-- start で、領域（region_base から region_size 拍）の中の first 拍目から count 拍を読む（領域の中で折り返す）。mem_port の読み出しの結果は止められないので、
-- 受け皿（同期 FIFO、LANDING 拍）に入れ、受け皿の空き（発行済みで出し終えていない数）を見ながらコマンドを出す。
-- 読み出しの途中で新しい start が来たとき、前の読みで発行済みでまだ届いていない結果は、届いた順に捨てる（drop 個）。
-- first が領域の大きさ以上なら、領域の大きさを引いて領域の中に収める（reducing、数サイクル以上かかる。busy は count /= 0 の間 1）。
-- region_size = 0 は 0 拍の読み。
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
  signal outst     : natural range 0 to LANDING := 0;                   -- 発行済みで結果がまだ届いていない（前の読みの分を含む）
  signal drop      : natural range 0 to LANDING := 0;                   -- outst のうち、前の読みの分（届いたら捨てる）
  -- to_issue /= 0 と in_flight + drop < LANDING をレジスタに持つ（cmd_valid は受け渡しの 152 ビットの CE に広がるので、
  -- 幅の広い比べを組み合わせのまま出さない。値は数と同じ周期に更新する）
  signal more      : boolean := false;
  signal room      : boolean := true;
  signal reducing  : boolean := false;                                  -- first を領域の中に収めている間
  signal issue     : boolean;
  signal pop       : boolean;
  signal fifo_valid : std_logic;
  signal land_valid : std_logic;
begin
  issue     <= more and room and cmd_ready = '1';
  cmd_valid <= '1' when more and room else '0';
  cmd_write <= '0';
  cmd_addr  <= addr;
  cmd_data  <= (others => '0');
  busy      <= '1' when to_output /= 0 else '0';

  land_valid <= rd_valid when drop = 0 else '0';
  landing_i : entity work.sync_fifo
    generic map (WIDTH => 128, DEPTH => LANDING)
    port map (clk => clk, rst => rst or start, in_valid => land_valid, in_ready => open, in_data => rd_data,
              out_valid => fifo_valid, out_ready => out_ready, out_data => out_data, level => open);
  out_valid <= fifo_valid;
  pop       <= fifo_valid = '1' and out_ready = '1';

  process (clk)
    variable flight, o_next, d_next : natural range 0 to LANDING;
  begin
    if rising_edge(clk) then
      -- 発行（このサイクルにメモリが受けたコマンド。start と同じサイクルのものも数える）と到着
      o_next := outst;
      if issue and rd_valid = '0' then
        o_next := outst + 1;
      elsif rd_valid = '1' and not issue then
        o_next := outst - 1;
      end if;
      d_next := drop;
      if rd_valid = '1' and drop /= 0 then
        d_next := drop - 1;
      end if;
      outst <= o_next;
      if rst = '1' then
        to_issue  <= (others => '0');
        to_output <= (others => '0');
        in_flight <= 0;
        outst     <= 0;
        drop      <= 0;
        more      <= false;
        room      <= true;
        reducing  <= false;
      elsif start = '1' then
        base      <= region_base;
        size      <= region_size;
        offset    <= resize(first, ADDR_BITS + 1);
        addr      <= region_base + first;
        if region_size = 0 then
          to_issue  <= (others => '0');
          to_output <= (others => '0');
        else
          to_issue  <= count;
          to_output <= count;
        end if;
        in_flight <= 0;
        drop      <= o_next;                                -- 発行済みで未着の読みは、ぜんぶ捨てる対象
        if region_size /= 0 and resize(first, ADDR_BITS + 1) < region_size then
          more     <= count /= 0;                           -- first が領域の中: そのまま発行できる
          reducing <= false;
        else
          more     <= false;                                -- first を領域の中に収めてから発行する
          reducing <= true;
        end if;
        room      <= o_next < LANDING;
      else
        drop <= d_next;
        if reducing then
          if size = 0 or offset < size then
            addr     <= base + resize(offset, ADDR_BITS);
            more     <= to_issue /= 0;
            reducing <= false;
          else
            offset   <= offset - size;
          end if;
        elsif issue then
          if offset + 1 = size then                          -- 領域の終わり → 先頭へ
            offset <= (others => '0');
            addr   <= base;
          else
            offset <= offset + 1;
            addr   <= addr + 1;
          end if;
          to_issue <= to_issue - 1;
          more     <= to_issue /= 1;
        end if;
        flight := in_flight;
        if issue and not pop then
          flight := in_flight + 1;
        elsif pop and not issue then
          flight := in_flight - 1;
        end if;
        in_flight <= flight;
        room      <= flight + d_next < LANDING;
        if pop then
          to_output <= to_output - 1;
        end if;
      end if;
    end if;
  end process;
end architecture;
