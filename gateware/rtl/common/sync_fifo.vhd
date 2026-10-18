-- 同期 FIFO（First-Word Fall-Through）。
--
-- * 容量はちょうど DEPTH 語（level は格納語数 0..DEPTH）。
-- * メモリは同期読み出しで記述し、ブロック RAM に割り当てられるようにする。
--   先頭語は出力レジスタに先読みされ、out_valid と同時に out_data が有効になる。
-- * ストリーム I/F: in_valid/in_ready/in_data, out_valid/out_ready/out_data。
library ieee;
use ieee.std_logic_1164.all;
use ieee.numeric_std.all;
use work.util_pkg.all;

entity sync_fifo is
  generic (
    WIDTH : positive;
    DEPTH : positive   -- 2 の冪
  );
  port (
    clk       : in  std_logic;
    rst       : in  std_logic;
    in_valid  : in  std_logic;
    in_ready  : out std_logic;
    in_data   : in  std_logic_vector(WIDTH - 1 downto 0);
    out_valid : out std_logic;
    out_ready : in  std_logic;
    out_data  : out std_logic_vector(WIDTH - 1 downto 0);
    level     : out unsigned(31 downto 0)
  );
end entity;

architecture rtl of sync_fifo is
  constant ADDR_BITS : natural := clog2(DEPTH);

  type memory_t is array (0 to DEPTH - 1) of std_logic_vector(WIDTH - 1 downto 0);
  signal memory : memory_t;

  signal wr_ptr    : unsigned(ADDR_BITS - 1 downto 0) := (others => '0');
  signal rd_ptr    : unsigned(ADDR_BITS - 1 downto 0) := (others => '0');
  signal mem_count : natural range 0 to DEPTH := 0;   -- メモリ内（出力レジスタを除く）の語数
  signal out_full  : std_logic := '0';                 -- 出力レジスタに語がある
  signal out_reg   : std_logic_vector(WIDTH - 1 downto 0) := (others => '0');

  signal total     : natural range 0 to DEPTH + 1;
  signal push      : boolean;
  signal pop_out   : boolean;   -- 出力レジスタから外部へ
  signal fetch     : boolean;   -- メモリから出力レジスタへ
begin
  assert 2 ** ADDR_BITS = DEPTH report "sync_fifo: DEPTH must be a power of two" severity failure;

  total   <= mem_count + to_natural(out_full = '1');
  push    <= in_valid = '1' and total < DEPTH;
  pop_out <= out_full = '1' and out_ready = '1';
  fetch   <= mem_count > 0 and (out_full = '0' or pop_out);

  -- メモリ本体（書き込みと同期読み出し。リセットを持たないので RAM に推論される）
  process (clk)
  begin
    if rising_edge(clk) then
      if push then
        memory(to_integer(wr_ptr)) <= in_data;
      end if;
      if fetch then
        out_reg <= memory(to_integer(rd_ptr));
      end if;
    end if;
  end process;

  process (clk)
  begin
    if rising_edge(clk) then
      if rst = '1' then
        wr_ptr    <= (others => '0');
        rd_ptr    <= (others => '0');
        mem_count <= 0;
        out_full  <= '0';
      else
        if push then
          wr_ptr <= wr_ptr + 1;
        end if;
        if fetch then
          rd_ptr <= rd_ptr + 1;
        end if;
        mem_count <= mem_count + to_natural(push) - to_natural(fetch);
        if fetch then
          out_full <= '1';
        elsif pop_out then
          out_full <= '0';
        end if;
      end if;
    end if;
  end process;

  in_ready  <= '1' when total < DEPTH else '0';
  out_valid <= out_full;
  out_data  <= out_reg;
  level     <= to_unsigned(total, level'length);
end architecture;
