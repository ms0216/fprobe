-- 非同期の 1 ビット信号を clk に同期させる 2 段フリップフロップ。
-- SAME_CLOCK = true（d が clk と同じクロックの領域の信号）: 同期化は要らないので、そのまま出す（遅れ 0）。
library ieee;
use ieee.std_logic_1164.all;

entity sync_bit is
  generic (
    INIT       : std_logic := '0';   -- リセット後・コンフィギュレーション直後の出力値
    SAME_CLOCK : boolean := false
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
  -- Vivado: 同期化の FF として隣に置き、準安定の解析の対象にする（UG901 の ASYNC_REG）。入口の経路は sync_bit.xdc で時間を測らない
  attribute ASYNC_REG : string;
  attribute ASYNC_REG of stage : signal is "TRUE";
begin
  cross_g : if not SAME_CLOCK generate
    process (clk)
    begin
      if rising_edge(clk) then
        stage <= stage(0) & d;
      end if;
    end process;
    q <= stage(1);
  end generate;
  same_g : if SAME_CLOCK generate
    stage <= (others => INIT);                 -- 使わない（駆動はする）
    q <= d;
  end generate;
end architecture;
