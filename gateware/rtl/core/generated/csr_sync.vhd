-- 自動生成ファイル。直接編集しないこと（python -m fprobe_gw gen で再生成）。
-- CSR ブロック sync（0x00000F00, 64 バイト）: ボードの間の時刻の同期（docs/superpowers/specs/2026-10-22-modular-design.md §3.2）。親は period ごとにパルスと自分の時刻を sync_out に出し、子は sync_in で受けた組（自分の時刻・親の時刻）をイベント 10 にする
-- CSR バスのスレーブ（csr_bus_pkg）。要求の次の周期に応答する。未定義のワードはエラー、
-- RO / CONST への書き込みは無視する。応答の周期以外の bus_rdata・bus_err は 0。
library ieee;
use ieee.std_logic_1164.all;
use ieee.numeric_std.all;
use work.csr_bus_pkg.all;

entity csr_sync is
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
    control_enable : out std_logic;   -- control.enable（RW）
    control_master : out std_logic;   -- control.master（RW）
    period : out std_logic_vector(31 downto 0);   -- period（RW）
    status_pairs : in  std_logic_vector(15 downto 0);   -- status.pairs（RO）
    status_crc_errors : in  std_logic_vector(15 downto 0)   -- status.crc_errors（RO）
  );
end entity;

architecture rtl of csr_sync is
  signal ack   : std_logic := '0';
  signal err   : std_logic := '0';
  signal rdata : csr_word_t := (others => '0');
  signal start : boolean;
  signal index : natural range 0 to 15;
  signal control_enable_r : std_logic := '0';
  signal control_master_r : std_logic := '0';
  signal period_r : std_logic_vector(31 downto 0) := "00000000000000011000011010100000";
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
        control_enable_r <= '0';
        control_master_r <= '0';
        period_r <= "00000000000000011000011010100000";
      elsif start then
        ack   <= '1';
        case index is
          when 0 =>   -- control
            rdata(0) <= control_enable_r;
            rdata(1) <= control_master_r;
            if bus_we = '1' then
              control_enable_r <= bus_wdata(0);
              control_master_r <= bus_wdata(1);
            end if;
          when 1 =>   -- period
            rdata(31 downto 0) <= period_r;
            if bus_we = '1' then
              period_r <= bus_wdata(31 downto 0);
            end if;
          when 2 =>   -- status
            rdata(15 downto 0) <= status_pairs;
            rdata(31 downto 16) <= status_crc_errors;
          when others =>
            err <= '1';
        end case;
      end if;
    end if;
  end process;

  bus_ack   <= ack;
  bus_err   <= err;
  bus_rdata <= rdata;
  control_enable <= control_enable_r;
  control_master <= control_master_r;
  period <= period_r;
end architecture;
