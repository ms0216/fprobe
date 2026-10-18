-- デバッグコネクタの端子ごとの駆動の選択（ボード非依存、組み合わせ回路）。
-- 仕様: docs/superpowers/specs/2026-10-18-u41-xtrig-pin-output-design.md §3.5
--
-- クロストリガの出力の動作 k（0〜ACTIONS-1）は、端子の候補 PIN_CHOICES(xout_pin(k)) を動かす（claim のとき）。
-- 端子 i の優先: gpio（enable）> 動作 k（claim で端子 i を選んでいる。oe の間だけ駆動。番号の小さい動作が優先）
--               > 機能（func）。
-- claimed(i) = 端子 i をクロストリガの出力に使っている。トップはその端子の機能の入力に休止の値 1 を入れる
-- （例: uart_rx => pin(i) or claimed(i)）。claim した端子の機能の出力（func_oe）は使わない。
-- 候補に無い番号（xout_pin >= PIN_CHOICES'length）を選んだ動作は、どの端子も動かさない。
library ieee;
use ieee.std_logic_1164.all;
use ieee.numeric_std.all;

entity pin_select is
  generic (
    PINS     : positive := 8;
    ACTIONS  : positive := 2;                     -- クロストリガの出力の動作の数
    PIN_CHOICES : integer_vector := (4, 5, 6, 7)  -- 端子の候補 j の端子の番号（候補は 4 本まで = xout_pin の 2 ビット）
  );
  port (
    gpio_drive  : in  std_logic_vector(PINS - 1 downto 0);
    gpio_enable : in  std_logic_vector(PINS - 1 downto 0);
    func_o      : in  std_logic_vector(PINS - 1 downto 0);
    func_oe     : in  std_logic_vector(PINS - 1 downto 0);
    xout_o      : in  std_logic_vector(ACTIONS - 1 downto 0);
    xout_oe     : in  std_logic_vector(ACTIONS - 1 downto 0);
    xout_claim  : in  std_logic_vector(ACTIONS - 1 downto 0);
    xout_pin    : in  std_logic_vector(2 * ACTIONS - 1 downto 0);   -- 動作 k の候補の番号（2 ビットずつ）
    pin_o       : out std_logic_vector(PINS - 1 downto 0);
    pin_oe      : out std_logic_vector(PINS - 1 downto 0);
    claimed     : out std_logic_vector(PINS - 1 downto 0)
  );
end entity;

architecture rtl of pin_select is
  constant CHOICES : integer_vector(0 to PIN_CHOICES'length - 1) := PIN_CHOICES;
begin
  process (all)
    variable o, oe, c : std_logic_vector(PINS - 1 downto 0);
    variable sel      : natural range 0 to 3;
  begin
    o  := func_o;
    oe := func_oe;
    c  := (others => '0');
    for k in ACTIONS - 1 downto 0 loop                 -- 番号の大きい動作から書き、小さい動作で上書き（優先）
      sel := to_integer(unsigned(xout_pin(2 * k + 1 downto 2 * k)));
      for j in CHOICES'range loop
        if xout_claim(k) = '1' and sel = j then
          c(CHOICES(j))  := '1';
          o(CHOICES(j))  := xout_o(k);
          oe(CHOICES(j)) := xout_oe(k);
        end if;
      end loop;
    end loop;
    for i in 0 to PINS - 1 loop
      if gpio_enable(i) = '1' then
        o(i)  := gpio_drive(i);
        oe(i) := '1';
      end if;
    end loop;
    pin_o   <= o;
    pin_oe  <= oe;
    claimed <= c;
  end process;
end architecture;
