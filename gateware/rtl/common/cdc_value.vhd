-- めったに変わらない多ビット値のクロックドメイン乗せ換え（保持 + トグルのハンドシェイク）。
-- 送り側: src_load（1 周期）で src_value を保持し、トグルを反転する。保持した値は次の src_load まで変わらない。
-- 受け側: トグルを 2 段で同期化し、変化を見たら保持値を取り込んで dst_changed を 1 周期出す。
-- src_load の間隔は受け側の数周期より長いこと（短いと途中の値を飛ばす。最後の値は必ず届く）。
library ieee;
use ieee.std_logic_1164.all;

entity cdc_value is
  generic (
    WIDTH : positive
  );
  port (
    src_clk     : in  std_logic;
    src_load    : in  std_logic;
    src_value   : in  std_logic_vector(WIDTH - 1 downto 0);
    dst_clk     : in  std_logic;
    dst_value   : out std_logic_vector(WIDTH - 1 downto 0);
    dst_changed : out std_logic
  );
end entity;

architecture rtl of cdc_value is
  signal held    : std_logic_vector(WIDTH - 1 downto 0) := (others => '0');
  signal toggle  : std_logic := '0';
  signal t_sync  : std_logic;
  signal t_prev  : std_logic := '0';
  signal value_r : std_logic_vector(WIDTH - 1 downto 0) := (others => '0');
  signal changed : std_logic := '0';
begin
  process (src_clk)
  begin
    if rising_edge(src_clk) then
      if src_load = '1' then
        held   <= src_value;
        toggle <= not toggle;
      end if;
    end if;
  end process;

  sync_i : entity work.sync_bit
    port map (clk => dst_clk, d => toggle, q => t_sync);

  process (dst_clk)
  begin
    if rising_edge(dst_clk) then
      t_prev  <= t_sync;
      changed <= '0';
      if t_sync /= t_prev then
        value_r <= held;                   -- held はトグルの前から安定している
        changed <= '1';
      end if;
    end if;
  end process;

  dst_value   <= value_r;
  dst_changed <= changed;
end architecture;
