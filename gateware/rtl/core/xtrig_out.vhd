-- クロストリガの動作「出力の端子を動かす」（ボード非依存、clk のドメイン）。
-- 仕様: docs/superpowers/specs/2026-10-18-u41-xtrig-pin-output-design.md §3
--
-- 動作 k（0〜COUNT-1）の条件 c(k) = 端子の並び or (sources and src(k)) /= 0
--   端子の並び（mask(k) = 0 なら不成立。pins は 2 段で同期化）:
--     any(k) = 0: (pins and mask) = (value and mask)   -- 見る端子がすべて値に一致
--     any(k) = 1: ((pins xnor value) and mask) /= 0    -- 見る端子のどれかが値に一致
-- 形 mode(k): 0 = 脈（c の立ち上がりで length × 刻み）、1 = 保持（c の立ち上がりから無効にするまで）、
--             2・3 = 条件の間（c が成り立つ間）。
-- 刻み = 16^unit(k) 周期（time の bit 4·8·…·28 の変化。unit ≥ 1 では最初の刻みの位置の分だけ短くなりうる）。
-- 動いている間 oe(k) = 1・o(k) = level(k)、それ以外は oe(k) = 0（高インピーダンス）。
-- enable(k) = 0 で状態を消す。c の前の値は enable に関係なく追う（有効にした時点で成り立つ条件は立ち上がりでない）。
-- 遅れ（条件の間）: 端子 → 同期化 2 段 → 条件のレジスタ → 出力のレジスタ = 4 周期。
library ieee;
use ieee.std_logic_1164.all;
use ieee.numeric_std.all;

entity xtrig_out is
  generic (
    COUNT : positive := 2;                 -- 動作の数（端子への割り当ては pin_select）
    WIDTH : positive := 8                  -- 端子の並びで見る端子の数（ロジアナの ch 0 から）
  );
  port (
    clk     : in  std_logic;
    rst     : in  std_logic;
    pins    : in  std_logic_vector(WIDTH - 1 downto 0);        -- 非同期でよい
    sources : in  std_logic_vector(5 downto 0);                -- クロストリガの源（1 周期の脈）
    time    : in  unsigned(31 downto 0);                       -- タイムベース（脈の長さの刻み）
    -- 設定（出力 k はビット k、または k 番目の組）
    enable  : in  std_logic_vector(COUNT - 1 downto 0);
    mode    : in  std_logic_vector(2 * COUNT - 1 downto 0);
    level   : in  std_logic_vector(COUNT - 1 downto 0);
    any     : in  std_logic_vector(COUNT - 1 downto 0);
    mask    : in  std_logic_vector(WIDTH * COUNT - 1 downto 0);
    value   : in  std_logic_vector(WIDTH * COUNT - 1 downto 0);
    src     : in  std_logic_vector(6 * COUNT - 1 downto 0);
    length  : in  std_logic_vector(8 * COUNT - 1 downto 0);
    unit    : in  std_logic_vector(3 * COUNT - 1 downto 0);
    -- 出力
    o       : out std_logic_vector(COUNT - 1 downto 0);
    oe      : out std_logic_vector(COUNT - 1 downto 0)
  );
end entity;

architecture rtl of xtrig_out is
  signal p1, s : std_logic_vector(WIDTH - 1 downto 0) := (others => '0');
  -- 刻み: tick(u) = time の bit 4u の変化（u = 0 は毎周期）
  signal tbits, tprev : std_logic_vector(7 downto 1) := (others => '0');
  signal tick : std_logic_vector(7 downto 0) := (others => '0');
  signal cond, cond_prev, held, drive : std_logic_vector(COUNT - 1 downto 0) := (others => '0');
  type count_array_t is array (0 to COUNT - 1) of unsigned(7 downto 0);
  signal left : count_array_t := (others => (others => '0'));
begin
  for_tbits : for u in 1 to 7 generate
    tbits(u) <= time(4 * u);
  end generate;
  tick <= (tbits xor tprev) & '1';

  process (clk)
    variable m, v, hit : std_logic_vector(WIDTH - 1 downto 0);
    variable pattern   : boolean;
    variable rise      : boolean;
    variable n         : unsigned(7 downto 0);
    variable h         : std_logic;
  begin
    if rising_edge(clk) then
      p1    <= pins;
      s     <= p1;
      tprev <= tbits;
      for k in 0 to COUNT - 1 loop
        m   := mask(WIDTH * (k + 1) - 1 downto WIDTH * k);
        v   := value(WIDTH * (k + 1) - 1 downto WIDTH * k);
        hit := not (s xor v) and m;
        if any(k) = '1' then
          pattern := hit /= (hit'range => '0');
        else
          pattern := m /= (m'range => '0') and hit = m;
        end if;
        if pattern or (sources and src(6 * k + 5 downto 6 * k)) /= "000000" then
          cond(k) <= '1';
        else
          cond(k) <= '0';
        end if;
        cond_prev(k) <= cond(k);

        -- 次の状態（脈の残り n、保持 h）を求め、駆動（drive）もレジスタから出す（端子に組み合わせの瞬間の
        -- ひげを出さない）
        rise := cond(k) = '1' and cond_prev(k) = '0';
        n    := (others => '0');
        h    := '0';
        if enable(k) = '1' and rst = '0' then
          case mode(2 * k + 1 downto 2 * k) is
            when "00" =>                                   -- 脈（途中の立ち上がりは読み直す）
              if rise then
                n := unsigned(length(8 * k + 7 downto 8 * k));
              elsif left(k) /= 0 and tick(to_integer(unsigned(unit(3 * k + 2 downto 3 * k)))) = '1' then
                n := left(k) - 1;
              else
                n := left(k);
              end if;
            when "01" =>                                   -- 保持
              if rise then
                h := '1';
              else
                h := held(k);
              end if;
            when others =>                                 -- 条件の間
              null;
          end case;
        end if;
        left(k) <= n;
        held(k) <= h;
        if enable(k) = '1' and rst = '0' and mode(2 * k + 1) = '1' then
          drive(k) <= cond(k);
        elsif n /= 0 or h = '1' then
          drive(k) <= '1';
        else
          drive(k) <= '0';
        end if;
      end loop;
    end if;
  end process;

  oe <= drive;
  o  <= level;
end architecture;
