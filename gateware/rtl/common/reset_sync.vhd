-- リセット同期化: 非同期にアサートし、clk に同期して解除する。
-- 出力 rst はアクティブハイ。設計内の全同期ロジックはこの出力を同期リセットとして使う。
library ieee;
use ieee.std_logic_1164.all;

entity reset_sync is
  generic (
    STAGES : positive := 3
  );
  port (
    clk      : in  std_logic;
    arst_in  : in  std_logic;   -- 非同期リセット要求（アクティブハイ。例: not pll_lock）
    rst      : out std_logic
  );
end entity;

architecture rtl of reset_sync is
  signal stage : std_logic_vector(STAGES - 1 downto 0) := (others => '1');
  -- 同期化の FF: 最適化で消さず（syn_preserve）、シフトレジスタとして記憶素子にまとめない（syn_srlstyle）。
  -- GowinSynthesis の syn_keep は配線にしか効かない（SUG550 §5）
  attribute syn_preserve : integer;
  attribute syn_srlstyle : string;
  attribute syn_preserve of stage : signal is 1;
  attribute syn_srlstyle of stage : signal is "registers";
begin
  process (clk, arst_in)
  begin
    if arst_in = '1' then
      stage <= (others => '1');
    elsif rising_edge(clk) then
      stage <= stage(STAGES - 2 downto 0) & '0';
    end if;
  end process;
  rst <= stage(STAGES - 1);
end architecture;
