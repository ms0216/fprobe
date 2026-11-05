-- 自動生成ファイル。直接編集しないこと（python -m fprobe_gw gen で再生成）。
-- CSR ブロック system（0x00000000, 64 バイト）: 識別と疎通確認
-- CSR バスのスレーブ（csr_bus_pkg）。要求の次の周期に応答する。未定義のワードはエラー、
-- RO / CONST への書き込みは無視する。応答の周期以外の bus_rsp の rdata・err は 0。
library ieee;
use ieee.std_logic_1164.all;
use ieee.numeric_std.all;
use work.csr_bus_pkg.all;

entity csr_system is
  port (
    clk       : in  std_logic;
    rst       : in  std_logic;
    bus_req   : in  std_logic;
    bus_cmd   : in  csr_cmd_t;
    bus_rsp : out csr_rsp_t;
    scratch : out std_logic_vector(31 downto 0)   -- scratch（RW）
  );
end entity;

architecture rtl of csr_system is
  signal ack   : std_logic := '0';
  signal err   : std_logic := '0';
  signal rdata : csr_word_t := (others => '0');
  signal start : boolean;
  signal index : natural range 0 to 15;
  signal scratch_r : std_logic_vector(31 downto 0) := (others => '0');
begin
  start <= bus_req = '1' and ack = '0';
  index <= to_integer(bus_cmd.addr(3 downto 0));

  process (clk)
  begin
    if rising_edge(clk) then
      ack   <= '0';
      err   <= '0';
      rdata <= (others => '0');                 -- 応答の周期以外は 0（csr_decoder が窓の応答の論理和を取る）
      if rst = '1' then
        scratch_r <= (others => '0');
      elsif start then
        ack   <= '1';
        case index is
          when 0 =>   -- magic
            rdata(31 downto 0) <= x"42525046";
          when 1 =>   -- version
            rdata(31 downto 0) <= x"00000002";
          when 2 =>   -- scratch
            rdata(31 downto 0) <= scratch_r;
            if bus_cmd.we = '1' then
              scratch_r <= bus_cmd.wdata(31 downto 0);
            end if;
          when others =>
            err <= '1';
        end case;
      end if;
    end if;
  end process;

  bus_rsp.ack   <= ack;
  bus_rsp.err   <= err;
  bus_rsp.rdata <= rdata;
  scratch <= scratch_r;
end architecture;
