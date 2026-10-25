-- 観測の値の一致（obs_jtag_engine から切り出し。クロストリガの源 bit4。
-- docs/superpowers/specs/2026-10-21-system-analysis-design.md §4.1）:
-- 観測した値（obs_valid が 1 の周期の obs_v）ごとに、前の値から match_change のどれかが変わり、かつ
-- (値 xor match_value) and match_mask = 0 なら match を 1 周期のパルスにして match_count を数える。
-- match_change = 0 なら一致に入った瞬間（前は不一致・今は一致）。match_on = 0 でも前の値は追う。
-- match_on の立ち上がりで数え直す。restart（観測を開き直す）で前の値を忘れる（最初の値では発火しない）。
library ieee;
use ieee.std_logic_1164.all;
use ieee.numeric_std.all;

entity obs_match is
  port (
    clk          : in  std_logic;
    rst          : in  std_logic;
    restart      : in  std_logic;
    obs_valid    : in  std_logic;
    obs_v        : in  std_logic_vector(15 downto 0);
    match_mask   : in  std_logic_vector(15 downto 0);
    match_value  : in  std_logic_vector(15 downto 0);
    match_change : in  std_logic_vector(15 downto 0);
    match_on     : in  std_logic;
    match        : out std_logic;
    match_count  : out unsigned(31 downto 0)
  );
end entity;

architecture rtl of obs_match is
  signal m_last     : std_logic_vector(15 downto 0);   -- 前の値
  signal m_hit_last : std_logic;                       -- 前の値が一致していた
  signal m_seen     : std_logic;                       -- 前の値がある（開き直すと 0）
  signal m_count    : unsigned(31 downto 0);
  signal m_on_prev  : std_logic;
  signal match_r    : std_logic;
begin
  match       <= match_r;
  match_count <= m_count;

  process (clk)
    variable hit, fire : boolean;
  begin
    if rising_edge(clk) then
      match_r <= '0';
      m_on_prev <= match_on;
      if match_on = '1' and m_on_prev = '0' then         -- 一致を有効にしたら数え直す
        m_count <= (others => '0');
        m_seen <= '0';                                   -- 最初の値は覚えるだけ（無効の間に古い条件で追った状態で成立させない）
      end if;
      if obs_valid = '1' then
        hit := ((obs_v xor match_value) and match_mask) = x"0000";
        if match_change = x"0000" then
          fire := hit and m_hit_last = '0';
        else
          fire := hit and ((obs_v xor m_last) and match_change) /= x"0000";
        end if;
        if fire and m_seen = '1' and match_on = '1' and m_on_prev = '1' then     -- 有効にした周期は数え直しと重ねない
          match_r <= '1';
          m_count <= m_count + 1;
        end if;
        m_last <= obs_v;
        if hit then
          m_hit_last <= '1';
        else
          m_hit_last <= '0';
        end if;
        m_seen <= '1';
      end if;
      if restart = '1' then                              -- 開き直した: 前の値を忘れる
        m_seen <= '0';
      end if;

      if rst = '1' then
        m_last <= (others => '0');
        m_hit_last <= '0';
        m_seen <= '0';
        m_count <= (others => '0');
        match_r <= '0';
      end if;
    end if;
  end process;
end architecture;
