-- 自動生成ファイル。直接編集しないこと（python -m fprobe_gw gen で再生成）。
-- CSR ブロック inject（0x00000540, 64 バイト）: 割り込み書き込みの語の列（docs/superpowers/specs/2026-10-29-halt-watch-design.md §3.2）。語 0 は xtrig の inject_addr / inject_value、語 1〜extra はこの表。要求 1 回で語 0〜extra を順に書き、最後に CTRL/STAT を読んで書き込みの誤りを確かめる（JTAG-DP は FAULT を返さない）。故障注入のある構成だけ（無ければ読むとエラー）
-- CSR バスのスレーブ（csr_bus_pkg）。要求の次の周期に応答する。未定義のワードはエラー、
-- RO / CONST への書き込みは無視する。応答の周期以外の bus_rsp の rdata・err は 0。
library ieee;
use ieee.std_logic_1164.all;
use ieee.numeric_std.all;
use work.csr_bus_pkg.all;

entity csr_inject is
  port (
    clk       : in  std_logic;
    rst       : in  std_logic;
    bus_req   : in  std_logic;
    bus_cmd   : in  csr_cmd_t;
    bus_rsp : out csr_rsp_t;
    control_extra : out std_logic_vector(2 downto 0);   -- control.extra（RW）
    entry : out std_logic_vector(2 downto 0);   -- entry（RW）
    addr : out std_logic_vector(31 downto 0);   -- addr（RW）
    value : out std_logic_vector(31 downto 0);   -- value（RW）
    entry_write : out std_logic;   -- entry_write（PULSE: 1 を書いた次の周期に 1）
    status_written_words : in  std_logic_vector(3 downto 0);   -- status.written_words（RO）
    first_time_lo : in  std_logic_vector(31 downto 0);   -- first_time_lo（RO）
    first_time_hi : in  std_logic_vector(31 downto 0);   -- first_time_hi（RO）
    start_first : out std_logic_vector(2 downto 0);   -- start.first（RW）
    start_last : out std_logic_vector(2 downto 0);   -- start.last（RW）
    start_go : out std_logic;   -- start.go（PULSE: 1 を書いた次の周期に 1）
    start_status_pending : in  std_logic;   -- start_status.pending（RO）
    start_status_result : in  std_logic_vector(1 downto 0);   -- start_status.result（RO）
    start_status_written_words : in  std_logic_vector(3 downto 0);   -- start_status.written_words（RO）
    start_status_count : in  std_logic_vector(15 downto 0);   -- start_status.count（RO）
    start_first_time_lo : in  std_logic_vector(31 downto 0);   -- start_first_time_lo（RO）
    start_first_time_hi : in  std_logic_vector(31 downto 0);   -- start_first_time_hi（RO）
    start_last_time_lo : in  std_logic_vector(31 downto 0);   -- start_last_time_lo（RO）
    start_last_time_hi : in  std_logic_vector(31 downto 0)   -- start_last_time_hi（RO）
  );
end entity;

architecture rtl of csr_inject is
  signal ack   : std_logic := '0';
  signal err   : std_logic := '0';
  signal rdata : csr_word_t := (others => '0');
  signal start : boolean;
  signal index : natural range 0 to 15;
  signal control_extra_r : std_logic_vector(2 downto 0) := (others => '0');
  signal entry_r : std_logic_vector(2 downto 0) := (others => '0');
  signal addr_r : std_logic_vector(31 downto 0) := (others => '0');
  signal value_r : std_logic_vector(31 downto 0) := (others => '0');
  signal entry_write_r : std_logic := '0';
  signal start_first_r : std_logic_vector(2 downto 0) := (others => '0');
  signal start_last_r : std_logic_vector(2 downto 0) := (others => '0');
  signal start_go_r : std_logic := '0';
begin
  start <= bus_req = '1' and ack = '0';
  index <= to_integer(bus_cmd.addr(3 downto 0));

  process (clk)
  begin
    if rising_edge(clk) then
      ack   <= '0';
      err   <= '0';
      rdata <= (others => '0');                 -- 応答の周期以外は 0（csr_decoder が窓の応答の論理和を取る）
      entry_write_r <= '0';
      start_go_r <= '0';
      if rst = '1' then
        control_extra_r <= (others => '0');
        entry_r <= (others => '0');
        addr_r <= (others => '0');
        value_r <= (others => '0');
        start_first_r <= (others => '0');
        start_last_r <= (others => '0');
      elsif start then
        ack   <= '1';
        case index is
          when 0 =>   -- control
            rdata(2 downto 0) <= control_extra_r;
            if bus_cmd.we = '1' then
              control_extra_r <= bus_cmd.wdata(2 downto 0);
            end if;
          when 1 =>   -- entry
            rdata(2 downto 0) <= entry_r;
            if bus_cmd.we = '1' then
              entry_r <= bus_cmd.wdata(2 downto 0);
            end if;
          when 2 =>   -- addr
            rdata(31 downto 0) <= addr_r;
            if bus_cmd.we = '1' then
              addr_r <= bus_cmd.wdata(31 downto 0);
            end if;
          when 3 =>   -- value
            rdata(31 downto 0) <= value_r;
            if bus_cmd.we = '1' then
              value_r <= bus_cmd.wdata(31 downto 0);
            end if;
          when 4 =>   -- entry_write
            if bus_cmd.we = '1' then
              entry_write_r <= bus_cmd.wdata(0);
            end if;
          when 5 =>   -- status
            rdata(3 downto 0) <= status_written_words;
          when 6 =>   -- first_time_lo
            rdata(31 downto 0) <= first_time_lo;
          when 7 =>   -- first_time_hi
            rdata(31 downto 0) <= first_time_hi;
          when 8 =>   -- start
            rdata(2 downto 0) <= start_first_r;
            rdata(6 downto 4) <= start_last_r;
            if bus_cmd.we = '1' then
              start_first_r <= bus_cmd.wdata(2 downto 0);
              start_last_r <= bus_cmd.wdata(6 downto 4);
              start_go_r <= bus_cmd.wdata(8);
            end if;
          when 9 =>   -- start_status
            rdata(0) <= start_status_pending;
            rdata(5 downto 4) <= start_status_result;
            rdata(11 downto 8) <= start_status_written_words;
            rdata(31 downto 16) <= start_status_count;
          when 10 =>   -- start_first_time_lo
            rdata(31 downto 0) <= start_first_time_lo;
          when 11 =>   -- start_first_time_hi
            rdata(31 downto 0) <= start_first_time_hi;
          when 12 =>   -- start_last_time_lo
            rdata(31 downto 0) <= start_last_time_lo;
          when 13 =>   -- start_last_time_hi
            rdata(31 downto 0) <= start_last_time_hi;
          when others =>
            err <= '1';
        end case;
      end if;
    end if;
  end process;

  bus_rsp.ack   <= ack;
  bus_rsp.err   <= err;
  bus_rsp.rdata <= rdata;
  control_extra <= control_extra_r;
  entry <= entry_r;
  addr <= addr_r;
  value <= value_r;
  entry_write <= entry_write_r;
  start_first <= start_first_r;
  start_last <= start_last_r;
  start_go <= start_go_r;
end architecture;
