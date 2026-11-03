-- 自動生成ファイル。直接編集しないこと（python -m fprobe_gw gen で再生成）。
-- CSR ブロック la_cap（0x00000C80, 64 バイト）: ロジアナの取り込みを sys の整数倍の速いクロックの領域で動かす構成（docs/superpowers/specs/2026-10-30-la-capture-clock-design.md）と、端子の直並列変換で sys の 1 周期に複数のサンプルを取る構成（2026-11-01-la-resolution-design.md）だけの情報。無い構成（比 1）では読むとエラー
-- CSR バスのスレーブ（csr_bus_pkg）。要求の次の周期に応答する。未定義のワードはエラー、
-- RO / CONST への書き込みは無視する。応答の周期以外の bus_rdata・bus_err は 0。
library ieee;
use ieee.std_logic_1164.all;
use ieee.numeric_std.all;
use work.csr_bus_pkg.all;

entity csr_la_cap is
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
    info_ratio : in  std_logic_vector(3 downto 0);   -- info.ratio（RO）
    info_trigger_phase : in  std_logic_vector(3 downto 0);   -- info.trigger_phase（RO）
    info_latency : in  std_logic_vector(7 downto 0);   -- info.latency（RO）
    info_lanes : in  std_logic_vector(3 downto 0);   -- info.lanes（RO）
    control_selftest : out std_logic   -- control.selftest（RW）
  );
end entity;

architecture rtl of csr_la_cap is
  signal ack   : std_logic := '0';
  signal err   : std_logic := '0';
  signal rdata : csr_word_t := (others => '0');
  signal start : boolean;
  signal index : natural range 0 to 15;
  signal control_selftest_r : std_logic := '0';
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
        control_selftest_r <= '0';
      elsif start then
        ack   <= '1';
        case index is
          when 0 =>   -- info
            rdata(3 downto 0) <= info_ratio;
            rdata(11 downto 8) <= info_trigger_phase;
            rdata(23 downto 16) <= info_latency;
            rdata(27 downto 24) <= info_lanes;
          when 1 =>   -- control
            rdata(0) <= control_selftest_r;
            if bus_we = '1' then
              control_selftest_r <= bus_wdata(0);
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
  control_selftest <= control_selftest_r;
end architecture;
