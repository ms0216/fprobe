-- DAP サンプラの周期の時計とゆらぎ（dap_sampler から分けた部品）。巡回を始める合図 due だけを出す。
-- 周期の時計は下りに数える（period を積んで 1 まで数える = period 周期ごと）。満了は due に登録し、巡回を始めるのは
-- 次の周期（31 ビットの 0 の検出から本体の slot・state 等への経路を切る。周期の長さは変わらない）。
-- run = '0' か period = 0 の間は period を積み直し、due を出さない。
-- jitter（k > 0）: 周期に 0〜2^k − 1 の乱数（16 ビットの LFSR）を足し、2^(k−1) を引く（平均の周期は変わらない）。
-- 周期的な処理（割り込み等）と読みの周期が噛み合って割合が偏る（エイリアシング）のを防ぐ（PC サンプリング）。
-- ゆらぎの計算は 3 段に分ける（1 段だと シフト → マスク → 32 ビットの加減算 が sys_clk に入らなかった）。
library ieee;
use ieee.std_logic_1164.all;
use ieee.numeric_std.all;

entity dap_sampler_timer is
  port (
    clk    : in  std_logic;
    rst    : in  std_logic;
    run    : in  std_logic;
    period : in  unsigned(31 downto 0);           -- 巡回の周期（clk の周期数。0 = 止める）
    jitter : in  natural range 0 to 15;           -- 周期のゆらぎ（2^jitter 周期の幅。0 = なし）
    due    : out std_logic                        -- 1 周期: 周期が満ちた（次の周期に巡回を始める）
  );
end entity;

architecture rtl of dap_sampler_timer is
  signal timer      : unsigned(31 downto 0) := (others => '0');
  signal due_r      : std_logic := '0';
  signal reload     : unsigned(31 downto 0) := (others => '0');   -- 次の周期（ゆらぎを足した period）
  signal jit_mask   : unsigned(31 downto 0) := (others => '0');   -- 2^jitter − 1
  signal jit_base   : unsigned(31 downto 0) := (others => '0');   -- period − 2^(jitter−1)
  signal jit_on     : boolean := false;
  signal jit_masked : unsigned(31 downto 0) := (others => '0');   -- 乱数 & jit_mask
  signal lfsr       : std_logic_vector(15 downto 0) := x"ACE1";
begin

  process (clk)
  begin
    if rising_edge(clk) then
      -- ゆらぎ: LFSR（x^16 + x^14 + x^13 + x^11 + 1）を毎周期進め、次の周期 = period + 乱数 − 2^(jitter−1)
      lfsr <= lfsr(14 downto 0) & (lfsr(15) xor lfsr(13) xor lfsr(12) xor lfsr(10));
      jit_on     <= jitter /= 0 and period > shift_left(to_unsigned(1, 32), jitter);
      jit_mask   <= shift_left(to_unsigned(1, 32), jitter) - 1;
      if jitter = 0 then
        jit_base <= period;
      else
        jit_base <= period - shift_left(to_unsigned(1, 32), jitter - 1);
      end if;
      jit_masked <= resize(unsigned(lfsr), 32) and jit_mask;
      if jit_on then
        reload <= jit_base + jit_masked;
      else
        reload <= period;
      end if;

      due_r <= '0';
      if run = '0' or period = 0 then
        timer <= period;
      elsif timer(31 downto 1) = 0 then
        timer <= reload;
        due_r <= '1';
      else
        timer <= timer - 1;
      end if;
      if rst = '1' then
        timer <= (others => '0');
        due_r <= '0';
        lfsr  <= x"ACE1";
      end if;
    end if;
  end process;

  due <= due_r;
end architecture;
