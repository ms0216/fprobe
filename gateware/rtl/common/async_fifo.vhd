-- 非同期 FIFO（クロックドメイン間のストリーム受け渡し。First-Word Fall-Through）。
--
-- * ポインタは 1 ビット余分に持ったグレイコードで相手ドメインへ渡し、2 段 FF で同期化する
--   （グレイコードは 1 回の更新で 1 ビットしか変わらないので、同期化で値が壊れない）。
-- * 満杯/空の判定は相手側ポインタの同期化遅れの分だけ保守的になる（溢れ・空読みは起きない）。
-- * メモリは書き込み側クロックで書き、読み出し側クロックで同期読み出しする（デュアルポート RAM に推論）。
-- * 容量は DEPTH + 1 語（メモリ DEPTH 語 + 読み出し側の出力レジスタに先読みした 1 語）。
-- * in_rst / out_rst はそれぞれのドメインで同期化済みのリセット。両方を同じ機会にかけること。
library ieee;
use ieee.std_logic_1164.all;
use ieee.numeric_std.all;
use work.util_pkg.all;

entity async_fifo is
  generic (
    WIDTH : positive;
    DEPTH : positive    -- 2 の冪
  );
  port (
    in_clk    : in  std_logic;
    in_rst    : in  std_logic;
    in_valid  : in  std_logic;
    in_ready  : out std_logic;
    in_data   : in  std_logic_vector(WIDTH - 1 downto 0);
    out_clk   : in  std_logic;
    out_rst   : in  std_logic;
    out_valid : out std_logic;
    out_ready : in  std_logic;
    out_data  : out std_logic_vector(WIDTH - 1 downto 0);
    -- 読み出し側で 1 にした周期に、読み出し側に届いている書き込み（同期化の遅れの分より前に書かれた語）を
    -- すべて捨てる（B10: SWO の受信の開始で前の残りを捨てる）。その周期の out_valid は 0
    out_flush : in  std_logic := '0'
  );
end entity;

architecture rtl of async_fifo is
  constant ADDR_BITS : natural := clog2(DEPTH);
  subtype ptr_t is unsigned(ADDR_BITS downto 0);

  type memory_t is array (0 to DEPTH - 1) of std_logic_vector(WIDTH - 1 downto 0);
  signal memory : memory_t;

  function to_gray(b : ptr_t) return ptr_t is
  begin
    return b xor shift_right(b, 1);
  end function;

  function from_gray(g : ptr_t) return ptr_t is
    variable b : ptr_t;
  begin
    b(b'high) := g(g'high);
    for i in g'high - 1 downto 0 loop
      b(i) := b(i + 1) xor g(i);
    end loop;
    return b;
  end function;

  -- 書き込みドメイン
  signal wr_bin      : ptr_t := (others => '0');
  signal wr_gray     : ptr_t := (others => '0');
  signal rd_gray_s1  : ptr_t := (others => '0');
  signal rd_gray_s2  : ptr_t := (others => '0');
  -- 読み出しドメイン
  signal rd_bin      : ptr_t := (others => '0');
  signal rd_gray     : ptr_t := (others => '0');
  signal wr_gray_s1  : ptr_t := (others => '0');
  signal wr_gray_s2  : ptr_t := (others => '0');
  signal out_full    : std_logic := '0';
  signal out_reg     : std_logic_vector(WIDTH - 1 downto 0) := (others => '0');

  -- 同期化の FF: 最適化で消さず（syn_preserve）、シフトレジスタとして記憶素子にまとめない（syn_srlstyle）。
  -- GowinSynthesis の syn_keep は配線にしか効かない（SUG550 §5）
  attribute syn_preserve : integer;
  attribute syn_srlstyle : string;
  attribute syn_preserve of rd_gray_s1, rd_gray_s2, wr_gray_s1, wr_gray_s2 : signal is 1;
  attribute syn_srlstyle of rd_gray_s1, rd_gray_s2, wr_gray_s1, wr_gray_s2 : signal is "registers";

  signal full  : boolean;
  signal empty : boolean;
  signal push  : boolean;
  signal pop   : boolean;
  signal fetch : boolean;
begin
  assert 2 ** ADDR_BITS = DEPTH report "async_fifo: DEPTH must be a power of two" severity failure;

  -- 満杯: 書き込みポインタが読み出しポインタのちょうど 1 周先（グレイでは上位 2 ビットが反転）
  full <= wr_gray = (not rd_gray_s2(ADDR_BITS downto ADDR_BITS - 1))
                    & rd_gray_s2(ADDR_BITS - 2 downto 0) when ADDR_BITS >= 2 else
          wr_gray = not rd_gray_s2;
  push <= in_valid = '1' and not full;

  process (in_clk)
  begin
    if rising_edge(in_clk) then
      if push then
        memory(to_integer(wr_bin(ADDR_BITS - 1 downto 0))) <= in_data;
      end if;
    end if;
  end process;

  process (in_clk)
  begin
    if rising_edge(in_clk) then
      rd_gray_s1 <= rd_gray;
      rd_gray_s2 <= rd_gray_s1;
      if in_rst = '1' then
        wr_bin  <= (others => '0');
        wr_gray <= (others => '0');
      elsif push then
        wr_bin  <= wr_bin + 1;
        wr_gray <= to_gray(wr_bin + 1);
      end if;
    end if;
  end process;

  in_ready <= '0' when full else '1';

  -- 読み出しドメイン
  empty <= rd_gray = wr_gray_s2;
  pop   <= out_full = '1' and out_ready = '1';
  fetch <= not empty and (out_full = '0' or pop);

  process (out_clk)
  begin
    if rising_edge(out_clk) then
      if fetch then
        out_reg <= memory(to_integer(rd_bin(ADDR_BITS - 1 downto 0)));
      end if;
    end if;
  end process;

  process (out_clk)
  begin
    if rising_edge(out_clk) then
      wr_gray_s1 <= wr_gray;
      wr_gray_s2 <= wr_gray_s1;
      if out_rst = '1' then
        rd_bin   <= (others => '0');
        rd_gray  <= (others => '0');
        out_full <= '0';
      elsif out_flush = '1' then
        rd_bin   <= from_gray(wr_gray_s2);               -- 届いている書き込みまで読んだことにする
        rd_gray  <= wr_gray_s2;
        out_full <= '0';
      else
        if fetch then
          rd_bin   <= rd_bin + 1;
          rd_gray  <= to_gray(rd_bin + 1);
          out_full <= '1';
        elsif pop then
          out_full <= '0';
        end if;
      end if;
    end if;
  end process;

  out_valid <= out_full and not out_flush;
  out_data  <= out_reg;
end architecture;
