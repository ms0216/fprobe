-- 同期リセットの扇出の分割。入力 rst（アクティブハイ・clk に同期済み）を COUNT 個のレジスタで 1 周期遅らせ、
-- 回路の塊ごとに別のレジスタのリセットを使えるようにする。
--
-- 理由: 1 本の reset_sync の出力が数千個の FF につながると、配線だけで 8 ns 超になる（実測: PYNQ-Z2 の
-- fprobe_pynq / fprobe_pynq_deep で、sys の最悪の setup 経路がどちらも reset_sync の出力 → 各 CSR の R で、
-- 論理 0 段・配線 8.3〜8.6 ns・扇出 約 3500、余裕 0.30 / 0.49 ns）。出力を分けると 1 本あたりの扇出が 1/COUNT になる。
--
-- 振る舞い: rst_out の各ビットは rst を 1 周期遅らせたもの（アサートも解除も全ビットで同じだけ 1 周期遅れる）。
-- 使う側の塊をすべて rst_out にすれば、塊の間の関係は変わらない。入力のストリームはスライス（リセット中は ready = 0）
-- が受けるので、解除が 1 周期遅れる間にデータを取りこぼさない。
--
-- 同じ値の FF が合成で 1 つに統合されると意味がなくなるので、残す属性を付ける
-- （Gowin: syn_preserve。Vivado: keep と dont_touch。reset_sync / sync_bit と同じ理由）。
library ieee;
use ieee.std_logic_1164.all;

entity reset_fanout is
  generic (
    COUNT : positive := 4
  );
  port (
    clk     : in  std_logic;
    rst     : in  std_logic;
    rst_out : out std_logic_vector(COUNT - 1 downto 0)
  );
end entity;

architecture rtl of reset_fanout is
  signal copy : std_logic_vector(COUNT - 1 downto 0) := (others => '1');
  attribute syn_preserve : integer;
  attribute keep : string;
  attribute dont_touch : string;
  attribute syn_preserve of copy : signal is 1;
  attribute keep of copy : signal is "true";
  attribute dont_touch of copy : signal is "true";
begin
  process (clk)
  begin
    if rising_edge(clk) then
      copy <= (others => rst);
    end if;
  end process;
  rst_out <= copy;
end architecture;
