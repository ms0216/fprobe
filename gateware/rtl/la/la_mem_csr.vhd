-- ロジアナのバッファを CSR 窓として読む。書き込みは無視する。
-- SLICES = 1: 1 語 = 1 ワード（WIDTH ≤ 32、上位は 0）。ワード番号の下位 ADDR_BITS ビットが語の番号。
-- SLICES > 1（1 周期に複数のサンプルの la_lane_capture。WIDTH = 32 × SLICES）: ワード番号 = j × 2**ADDR_BITS + 語の番号 で、
--   語のビット 32j〜32j + 31（レーン 2j・2j + 1）を返す（窓 la_lanes_mem）。
-- la_capture の同期読み出し（1 周期遅れ）に合わせ、要求から 3 周期目に応答する。
library ieee;
use ieee.std_logic_1164.all;
use ieee.numeric_std.all;
use work.csr_bus_pkg.all;
use work.util_pkg.all;

entity la_mem_csr is
  generic (
    WIDTH     : positive;
    ADDR_BITS : positive;
    SLICES    : positive := 1
  );
  port (
    clk       : in  std_logic;
    rst       : in  std_logic;
    bus_req   : in  std_logic;
    bus_addr  : in  csr_addr_t;
    bus_ack   : out std_logic;
    bus_rdata : out csr_word_t;
    rd_addr   : out unsigned(ADDR_BITS - 1 downto 0);
    rd_data   : in  std_logic_vector(WIDTH - 1 downto 0)
  );
end entity;

architecture rtl of la_mem_csr is
  signal stage : natural range 0 to 3 := 0;
  signal ack   : std_logic := '0';
  signal addr  : unsigned(ADDR_BITS - 1 downto 0) := (others => '0');
  signal slice : natural range 0 to 2 ** clog2(SLICES) - 1 := 0;   -- SLICES 以上（使わない番地）は 0 を返す
  signal word  : csr_word_t;
begin
  assert SLICES = 1 or WIDTH = 32 * SLICES report "la_mem_csr: WIDTH must be 32 * SLICES" severity failure;
  assert SLICES > 1 or WIDTH <= 32 report "la_mem_csr: WIDTH must be at most 32" severity failure;

  process (clk)
  begin
    if rising_edge(clk) then
      ack <= '0';
      if rst = '1' then
        stage <= 0;
      else
        case stage is
          when 0 =>
            if bus_req = '1' and ack = '0' then
              addr  <= bus_addr(ADDR_BITS - 1 downto 0);
              if SLICES > 1 then
                slice <= to_integer(bus_addr(ADDR_BITS + clog2(SLICES) - 1 downto ADDR_BITS));
              end if;
              stage <= 1;
            end if;
          when 1 =>
            stage <= 2;                         -- la_capture が rd_addr を取り込む
          when others =>
            ack   <= '1';                       -- rd_data が有効
            stage <= 0;
        end case;
      end if;
    end if;
  end process;

  rd_addr   <= addr;
  bus_ack   <= ack;
  one_g : if SLICES = 1 generate
    word <= std_logic_vector(resize(unsigned(rd_data), 32));
  end generate;
  many_g : if SLICES > 1 generate
    process (all)
    begin
      word <= (others => '0');
      for j in 0 to SLICES - 1 loop
        if slice = j then
          word <= rd_data(32 * j + 31 downto 32 * j);
        end if;
      end loop;
    end process;
  end generate;
  bus_rdata <= word when ack = '1' else (others => '0');   -- csr_bus_pkg の約束
end architecture;
