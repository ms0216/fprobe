-- 自動生成ファイル。直接編集しないこと（python -m fprobe_gw gen で再生成）。
-- CSR ブロック events（0x00000800, 64 バイト）: DAP トランザクションのイベント記録（docs/superpowers/specs/2026-10-16-m9b-dap-events-design.md）。1 件 = 1 拍。読み出しは la_deep の read_region = 1
-- CSR バスのスレーブ（csr_bus_pkg）。要求の次の周期に応答する。未定義のワードはエラー、
-- RO / CONST への書き込みは無視する。応答の周期以外の bus_rsp の rdata・err は 0。
library ieee;
use ieee.std_logic_1164.all;
use ieee.numeric_std.all;
use work.csr_bus_pkg.all;

entity csr_events is
  port (
    clk       : in  std_logic;
    rst       : in  std_logic;
    bus_req   : in  std_logic;
    bus_cmd   : in  csr_cmd_t;
    bus_rsp : out csr_rsp_t;
    control_enable : out std_logic;   -- control.enable（RW）
    written : in  std_logic_vector(31 downto 0);   -- written（RO）
    lost : in  std_logic_vector(31 downto 0);   -- lost（RO）
    capacity : in  std_logic_vector(31 downto 0);   -- capacity（RO）
    written_hi : in  std_logic_vector(31 downto 0)   -- written_hi（RO）
  );
end entity;

architecture rtl of csr_events is
  signal ack   : std_logic := '0';
  signal err   : std_logic := '0';
  signal rdata : csr_word_t := (others => '0');
  signal start : boolean;
  signal index : natural range 0 to 15;
  signal control_enable_r : std_logic := '0';
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
        control_enable_r <= '0';
      elsif start then
        ack   <= '1';
        case index is
          when 0 =>   -- control
            rdata(0) <= control_enable_r;
            if bus_cmd.we = '1' then
              control_enable_r <= bus_cmd.wdata(0);
            end if;
          when 1 =>   -- written
            rdata(31 downto 0) <= written;
          when 2 =>   -- lost
            rdata(31 downto 0) <= lost;
          when 3 =>   -- capacity
            rdata(31 downto 0) <= capacity;
          when 4 =>   -- written_hi
            rdata(31 downto 0) <= written_hi;
          when others =>
            err <= '1';
        end case;
      end if;
    end if;
  end process;

  bus_rsp.ack   <= ack;
  bus_rsp.err   <= err;
  bus_rsp.rdata <= rdata;
  control_enable <= control_enable_r;
end architecture;
