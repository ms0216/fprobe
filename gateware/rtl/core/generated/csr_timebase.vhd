-- 自動生成ファイル。直接編集しないこと（python -m fprobe_gw gen で再生成）。
-- CSR ブロック timebase（0x00000100, 64 バイト）: 64 ビットのタイムベース（周波数はケイパビリティ TIMEBASE_HZ）
-- CSR バスのスレーブ（csr_bus_pkg）。要求の次の周期に応答する。未定義のワードはエラー、
-- RO / CONST への書き込みは無視する。応答の周期以外の bus_rdata・bus_err は 0。
library ieee;
use ieee.std_logic_1164.all;
use ieee.numeric_std.all;
use work.csr_bus_pkg.all;

entity csr_timebase is
  port (
    clk       : in  std_logic;
    rst       : in  std_logic;
    bus_req   : in  std_logic;
    bus_we    : in  std_logic;
    bus_addr  : in  csr_addr_t;
    bus_wdata : in  csr_word_t;
    bus_ack   : out std_logic;
    bus_err   : out std_logic;
    bus_rdata : out csr_word_t;
    time_lo : in  std_logic_vector(31 downto 0);   -- time_lo（RO）
    time_lo_read : out std_logic;   -- time_lo を読んだ周期に 1
    time_hi : in  std_logic_vector(31 downto 0)   -- time_hi（RO）
  );
end entity;

architecture rtl of csr_timebase is
  signal ack   : std_logic := '0';
  signal err   : std_logic := '0';
  signal rdata : csr_word_t := (others => '0');
  signal start : boolean;
  signal index : natural range 0 to 15;
begin
  start <= bus_req = '1' and ack = '0';
  index <= to_integer(bus_addr(3 downto 0));
  time_lo_read <= '1' when start and bus_we = '0' and index = 0 else '0';

  process (clk)
  begin
    if rising_edge(clk) then
      ack   <= '0';
      err   <= '0';
      rdata <= (others => '0');                 -- 応答の周期以外は 0（csr_decoder が窓の応答の論理和を取る）
      if rst = '1' then
        null;
      elsif start then
        ack   <= '1';
        case index is
          when 0 =>   -- time_lo
            rdata(31 downto 0) <= time_lo;
          when 1 =>   -- time_hi
            rdata(31 downto 0) <= time_hi;
          when others =>
            err <= '1';
        end case;
      end if;
    end if;
  end process;

  bus_ack   <= ack;
  bus_err   <= err;
  bus_rdata <= rdata;
end architecture;
