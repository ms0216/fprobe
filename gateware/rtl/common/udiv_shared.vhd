-- 2 つの使い手で共有する割り算器（udiv + 調停）。仕様: docs/superpowers/specs/2026-10-18-b22-fpga-area-design.md §3.3
--
-- 使い手の約束: pN_req を 1 にし、pN_num / pN_den を保ったまま pN_done（1 周期）を待つ。done の周期の quotient が
-- 結果。done を見たら req を下ろす（次の周期から。下ろし忘れると同じ計算をもう一度する）。
-- 調停: 割り算器が空いていれば p0 を優先して取り込む。待たされた要求は失われない（req を保っている限り）。
-- 使い手が 1 つなら p1_req = '0' につなぐ（選択の回路は消え、ただの udiv になる）。
library ieee;
use ieee.std_logic_1164.all;
use ieee.numeric_std.all;

entity udiv_shared is
  generic (
    WIDTH : positive := 32
  );
  port (
    clk      : in  std_logic;
    rst      : in  std_logic;
    p0_req   : in  std_logic;
    p0_num   : in  unsigned(WIDTH - 1 downto 0);
    p0_den   : in  unsigned(WIDTH - 1 downto 0);
    p0_done  : out std_logic;
    p1_req   : in  std_logic := '0';
    p1_num   : in  unsigned(WIDTH - 1 downto 0) := (others => '0');
    p1_den   : in  unsigned(WIDTH - 1 downto 0) := (others => '0');
    p1_done  : out std_logic;
    quotient : out unsigned(WIDTH - 1 downto 0)       -- done の周期に有効（両方の使い手に共通）
  );
end entity;

architecture rtl of udiv_shared is
  signal running : std_logic := '0';             -- 割り算中（owner の計算）
  signal owner   : std_logic := '0';             -- '0' = p0、'1' = p1
  signal start   : std_logic;
  signal pick_p1 : std_logic;                    -- 取り込む要求（空いているときの選択）
  signal num, den : unsigned(WIDTH - 1 downto 0);
  signal done    : std_logic;
begin
  -- 空いていれば、いま出ている要求を取り込む（udiv は start の周期に被除数・除数を取り込む）
  pick_p1 <= not p0_req;
  start   <= not running and (p0_req or p1_req);
  num     <= p1_num when pick_p1 = '1' else p0_num;
  den     <= p1_den when pick_p1 = '1' else p0_den;

  div_i : entity work.udiv
    generic map (WIDTH => WIDTH)
    port map (clk => clk, rst => rst, start => start, dividend => num, divisor => den,
              busy => open, done => done, quotient => quotient, remainder => open);

  process (clk)
  begin
    if rising_edge(clk) then
      if rst = '1' then
        running <= '0';
        owner   <= '0';
      elsif start = '1' then
        running <= '1';
        owner   <= pick_p1;
      elsif done = '1' then
        running <= '0';                          -- 次の周期から次の要求を取り込める（使い手は req を下ろしている）
      end if;
    end if;
  end process;

  p0_done <= done and not owner;
  p1_done <= done and owner;
end architecture;
