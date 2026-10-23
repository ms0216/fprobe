-- 自動生成ファイル。直接編集しないこと（python -m fprobe_gw gen で再生成）。
-- CSR ブロック debug（0x00000900, 64 バイト）: SWD / JTAG の物理層の調整
-- CSR バスのスレーブ（csr_bus_pkg）。要求の次の周期に応答する。未定義のワードはエラー、
-- RO / CONST への書き込みは無視する。応答の周期以外の bus_rdata・bus_err は 0。
library ieee;
use ieee.std_logic_1164.all;
use ieee.numeric_std.all;
use work.csr_bus_pkg.all;

entity csr_debug is
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
    sample_delay_swd : out std_logic_vector(1 downto 0);   -- sample_delay.swd（RW）
    sample_delay_jtag : out std_logic_vector(2 downto 0)   -- sample_delay.jtag（RW）
  );
end entity;

architecture rtl of csr_debug is
  signal ack   : std_logic := '0';
  signal err   : std_logic := '0';
  signal rdata : csr_word_t := (others => '0');
  signal start : boolean;
  signal index : natural range 0 to 15;
  signal sample_delay_swd_r : std_logic_vector(1 downto 0) := "01";
  signal sample_delay_jtag_r : std_logic_vector(2 downto 0) := "000";
begin
  start <= bus_req = '1' and ack = '0';
  index <= to_integer(bus_addr(3 downto 0));

  process (clk)
  begin
    if rising_edge(clk) then
      ack   <= '0';
      err   <= '0';
      rdata <= (others => '0');                 -- 応答の周期以外は 0（csr_decoder が窓の応答の論理和を取る）
      if rst = '1' then
        sample_delay_swd_r <= "01";
        sample_delay_jtag_r <= "000";
      elsif start then
        ack   <= '1';
        case index is
          when 0 =>   -- sample_delay
            rdata(1 downto 0) <= sample_delay_swd_r;
            rdata(10 downto 8) <= sample_delay_jtag_r;
            if bus_we = '1' then
              sample_delay_swd_r <= bus_wdata(1 downto 0);
              sample_delay_jtag_r <= bus_wdata(10 downto 8);
            end if;
          when others =>
            err <= '1';
        end case;
      end if;
    end if;
  end process;

  bus_ack   <= ack;
  bus_err   <= err;
  bus_rdata <= rdata;
  sample_delay_swd <= sample_delay_swd_r;
  sample_delay_jtag <= sample_delay_jtag_r;
end architecture;
