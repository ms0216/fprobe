-- 非同期の 1 ビット信号を clk に同期させる 2 段フリップフロップ。
library ieee;
use ieee.std_logic_1164.all;

entity sync_bit is
  generic (
    INIT : std_logic := '0'   -- リセット後・コンフィギュレーション直後の出力値
  );
  port (
    clk : in  std_logic;
    d   : in  std_logic;
    q   : out std_logic
  );
end entity;

architecture rtl of sync_bit is
  signal stage : std_logic_vector(1 downto 0) := (others => INIT);
  -- 同期化の FF: 最適化で消さず（syn_preserve）、シフトレジスタとして記憶素子にまとめない（syn_srlstyle）。
  -- GowinSynthesis の syn_keep は配線にしか効かない（SUG550 §5）
  attribute syn_preserve : integer;
  attribute syn_srlstyle : string;
  attribute syn_preserve of stage : signal is 1;
  attribute syn_srlstyle of stage : signal is "registers";
begin
  process (clk)
  begin
    if rising_edge(clk) then
      stage <= stage(0) & d;
    end if;
  end process;
  q <= stage(1);
end architecture;
