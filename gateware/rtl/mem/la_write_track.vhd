-- 応答の来た書きまでの位置（mem ドメイン）。仕様: docs/superpowers/specs/2026-11-03-ps-stream-position-design.md
--
-- AXI は書きと読みの順を保証しないので、PS に「読める」と教える位置は、書きの応答が来た所まで。メモリの口（axi_mem_port）の
-- 応答の来ていない書きの数 wr_pending は、書いた順に応答が返るので、いちばん新しく出した wr_pending 個の書き。
-- 出した書きの種類を新しい順の並び（bit 0 = 最後に出した書き）に持ち、その下位 wr_pending 個を種類ごとに数えて、書き込み器の
-- 位置（総数・書き位置）から引く。
--   kinds = '1': イベントの書き。own = '1': この流れのロジアナの拍（start より前に出したロジアナの書きは、この流れの総数に
--   入っていないので引かない）。
-- 書きの受理（wr_fire）は axi_mem_port が wr_pending を増やすのと同じ周期。wr_event はその書きの種類。
-- 出す値は入力と同じ周期の組み合わせ（総数・位置・wr_pending が同じ周期の値）。
library ieee;
use ieee.std_logic_1164.all;
use ieee.numeric_std.all;
use work.util_pkg.all;
use work.mem_ring_pkg.all;

entity la_write_track is
  generic (
    ADDR_BITS : positive
  );
  port (
    clk        : in  std_logic;
    rst        : in  std_logic;
    start      : in  std_logic;                                         -- 流れの開始
    wr_fire    : in  std_logic;
    wr_event   : in  std_logic;
    wr_pending : in  unsigned(3 downto 0);
    la_total   : in  unsigned(31 downto 0);                             -- 書き込み器の書いたロジアナの拍の総数
    la_ptr     : in  unsigned(ADDR_BITS - 1 downto 0);                  -- ロジアナのリングの次の書き位置
    ev_pos     : in  unsigned(ring_ev_bits(ADDR_BITS) - 1 downto 0);    -- イベントのリングの次の書き位置
    la_safe    : out unsigned(31 downto 0);
    la_ptr_safe : out unsigned(ADDR_BITS - 1 downto 0);
    ev_safe    : out unsigned(ring_ev_bits(ADDR_BITS) - 1 downto 0);
    ev_unacked : out unsigned(3 downto 0)                               -- 応答の来ていないイベントの書きの数（ev_safe = ev_pos - ev_unacked）
  );
end entity;

architecture rtl of la_write_track is
  constant EV_BITS : natural := ring_ev_bits(ADDR_BITS);
  constant LA_SIZE : unsigned(ADDR_BITS downto 0) := ring_la_beats(ADDR_BITS);
  signal kinds, own : std_logic_vector(14 downto 0) := (others => '0');
  signal la_pending, ev_pending : unsigned(3 downto 0);
begin
  process (clk)
  begin
    if rising_edge(clk) then
      if rst = '1' then
        kinds <= (others => '0');
        own   <= (others => '0');
      elsif wr_fire = '1' then
        kinds <= kinds(13 downto 0) & wr_event;
        own   <= (own(13 downto 0) & not wr_event) and not (14 downto 0 => start);
      elsif start = '1' then
        own   <= (others => '0');
      end if;
    end if;
  end process;

  process (kinds, own, wr_pending)
    variable nl, ne : unsigned(3 downto 0);
  begin
    nl := (others => '0');
    ne := (others => '0');
    for i in 0 to 14 loop
      if i < to_integer(wr_pending) then
        if kinds(i) = '1' then
          ne := ne + 1;
        elsif own(i) = '1' then
          nl := nl + 1;
        end if;
      end if;
    end loop;
    la_pending <= nl;
    ev_pending <= ne;
  end process;

  -- 総数は折り返す（mod_diff）。書き位置はリングの中で戻す
  process (la_total, la_ptr, ev_pos, la_pending, ev_pending)
    variable p : unsigned(ADDR_BITS downto 0);
  begin
    la_safe <= mod_diff(la_total, resize(la_pending, 32));
    p := resize(la_ptr, ADDR_BITS + 1) + LA_SIZE - resize(la_pending, ADDR_BITS + 1);
    if p >= LA_SIZE then
      p := p - LA_SIZE;
    end if;
    la_ptr_safe <= p(ADDR_BITS - 1 downto 0);
    ev_safe <= ev_pos - resize(ev_pending, EV_BITS);
    ev_unacked <= ev_pending;
  end process;
end architecture;
