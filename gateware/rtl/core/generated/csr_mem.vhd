-- 自動生成ファイル。直接編集しないこと（python -m fprobe_gw gen で再生成）。
-- CSR ブロック mem（0x00000600, 64 バイト）: 外部メモリ（DDR3 等）の状態とメモリ試験（docs/superpowers/specs/2026-10-16-m8-ddr3-design.md）。番地・数は拍（MEM_BEAT_BYTES バイト）単位
-- CSR バスのスレーブ（csr_bus_pkg）。要求の次の周期に応答する。未定義のワードはエラー、
-- RO / CONST への書き込みは無視する。応答の周期以外の bus_rdata・bus_err は 0。
library ieee;
use ieee.std_logic_1164.all;
use ieee.numeric_std.all;
use work.csr_bus_pkg.all;

entity csr_mem is
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
    status_ready : in  std_logic;   -- status.ready（RO）
    status_busy : in  std_logic;   -- status.busy（RO）
    status_done : in  std_logic;   -- status.done（RO）
    status_in_reset : in  std_logic;   -- status.in_reset（RO）
    status_clock_tick : in  std_logic;   -- status.clock_tick（RO）
    command_test : out std_logic;   -- command.test（PULSE: 1 を書いた次の周期に 1）
    test_base : out std_logic_vector(31 downto 0);   -- test_base（RW）
    test_count : out std_logic_vector(31 downto 0);   -- test_count（RW）
    test_seed : out std_logic_vector(31 downto 0);   -- test_seed（RW）
    errors : in  std_logic_vector(31 downto 0);   -- errors（RO）
    first_error : in  std_logic_vector(31 downto 0);   -- first_error（RO）
    write_cycles : in  std_logic_vector(31 downto 0);   -- write_cycles（RO）
    read_cycles : in  std_logic_vector(31 downto 0)   -- read_cycles（RO）
  );
end entity;

architecture rtl of csr_mem is
  signal ack   : std_logic := '0';
  signal err   : std_logic := '0';
  signal rdata : csr_word_t := (others => '0');
  signal start : boolean;
  signal index : natural range 0 to 15;
  signal command_test_r : std_logic := '0';
  signal test_base_r : std_logic_vector(31 downto 0) := "00000000000000000000000000000000";
  signal test_count_r : std_logic_vector(31 downto 0) := "00000000000000000000000000000001";
  signal test_seed_r : std_logic_vector(31 downto 0) := "00000000000000000000000000000000";
begin
  start <= bus_req = '1' and ack = '0';
  index <= to_integer(bus_addr(3 downto 0));

  process (clk)
  begin
    if rising_edge(clk) then
      ack   <= '0';
      err   <= '0';
      rdata <= (others => '0');                 -- 応答の周期以外は 0（csr_decoder が窓の応答の論理和を取る）
      command_test_r <= '0';
      if rst = '1' then
        test_base_r <= "00000000000000000000000000000000";
        test_count_r <= "00000000000000000000000000000001";
        test_seed_r <= "00000000000000000000000000000000";
      elsif start then
        ack   <= '1';
        case index is
          when 0 =>   -- status
            rdata(0) <= status_ready;
            rdata(1) <= status_busy;
            rdata(2) <= status_done;
            rdata(3) <= status_in_reset;
            rdata(4) <= status_clock_tick;
          when 1 =>   -- command
            if bus_we = '1' then
              command_test_r <= bus_wdata(0);
            end if;
          when 2 =>   -- test_base
            rdata(31 downto 0) <= test_base_r;
            if bus_we = '1' then
              test_base_r <= bus_wdata(31 downto 0);
            end if;
          when 3 =>   -- test_count
            rdata(31 downto 0) <= test_count_r;
            if bus_we = '1' then
              test_count_r <= bus_wdata(31 downto 0);
            end if;
          when 4 =>   -- test_seed
            rdata(31 downto 0) <= test_seed_r;
            if bus_we = '1' then
              test_seed_r <= bus_wdata(31 downto 0);
            end if;
          when 5 =>   -- errors
            rdata(31 downto 0) <= errors;
          when 6 =>   -- first_error
            rdata(31 downto 0) <= first_error;
          when 7 =>   -- write_cycles
            rdata(31 downto 0) <= write_cycles;
          when 8 =>   -- read_cycles
            rdata(31 downto 0) <= read_cycles;
          when others =>
            err <= '1';
        end case;
      end if;
    end if;
  end process;

  bus_ack   <= ack;
  bus_err   <= err;
  bus_rdata <= rdata;
  command_test <= command_test_r;
  test_base <= test_base_r;
  test_count <= test_count_r;
  test_seed <= test_seed_r;
end architecture;
