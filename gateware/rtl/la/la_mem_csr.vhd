-- ロジアナのバッファを CSR 窓として読む（1 サンプル = 1 ワード、上位は 0）。書き込みは無視する。
-- la_capture の同期読み出し（1 周期遅れ）に合わせ、要求から 3 周期目に応答する。
library ieee;
use ieee.std_logic_1164.all;
use ieee.numeric_std.all;
use work.csr_bus_pkg.all;

entity la_mem_csr is
  generic (
    WIDTH     : positive;
    ADDR_BITS : positive
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
begin
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
  bus_rdata <= (31 downto WIDTH => '0') & rd_data when ack = '1' else (others => '0');   -- csr_bus_pkg の約束
end architecture;
