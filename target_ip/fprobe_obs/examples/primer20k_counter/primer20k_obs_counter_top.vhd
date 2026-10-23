-- SPDX-License-Identifier: Apache-2.0
-- fprobe_obs の例: Tang Primer 20K で 27 MHz のカウンタを観測する（fprobe の FPGA 観測の実機の確かめ、O1・O2）。
--
-- 観測の口は拡張ヘッダ J12 のふつうのピン 4 本（obs_soft_tap）: J12 の 5 番 TCK・6 番 TMS・7 番 TDI・8 番 TDO。
-- 観測する値（GROUPS = 2）: 組 0 = カウンタの 27〜12 ビット（4096 周期 = 約 152 µs ごとに変わる）、組 1 = 43〜28 ビット。
-- 時刻の照合用に、組 0 の最下位（カウンタの 12 ビット）を J12 の 9 番（obs_mark）にも出す（fprobe のロジアナで同じ辺を見る）。
-- LED は動いている印（カウンタの上位）。
library ieee;
use ieee.std_logic_1164.all;
use ieee.numeric_std.all;

entity primer20k_obs_counter_top is
  port (
    clk27    : in  std_logic;
    led      : out std_logic_vector(5 downto 0);
    obs_tck  : in  std_logic;
    obs_tms  : in  std_logic;
    obs_tdi  : in  std_logic;
    obs_tdo  : out std_logic;                -- 3 ステート（Shift-IR・Shift-DR の間だけ出す）
    obs_mark : out std_logic                 -- カウンタの 12 ビット（時刻の照合用）
  );
end entity;

architecture rtl of primer20k_obs_counter_top is
  signal por      : unsigned(3 downto 0) := (others => '0');   -- 電源投入のリセット（構成の直後は 0）
  signal rst      : std_logic;
  signal counter  : unsigned(43 downto 0);
  signal probes   : std_logic_vector(31 downto 0);
  signal tdo, tdo_oe : std_logic;
  signal u_tdi, u_tdo, u_sel, u_capture, u_shift, u_update, u_tlr : std_logic;
begin
  process (clk27)
  begin
    if rising_edge(clk27) then
      if por /= 15 then
        por <= por + 1;
      end if;
      if rst = '1' then
        counter <= (others => '0');
      else
        counter <= counter + 1;
      end if;
    end if;
  end process;
  rst <= '0' when por = 15 else '1';

  probes <= std_logic_vector(counter(43 downto 28)) & std_logic_vector(counter(27 downto 12));

  tap : entity work.obs_soft_tap
    port map (tck => obs_tck, tms => obs_tms, tdi => obs_tdi, tdo => tdo, tdo_oe => tdo_oe,
              user_tdi => u_tdi, user_tdo => u_tdo, user_sel => u_sel, user_capture => u_capture,
              user_shift => u_shift, user_update => u_update, user_tlr => u_tlr);

  core : entity work.obs_core
    generic map (GROUPS => 2, FIFO_DEPTH => 512, CLOCK_HZ => 27_000_000)
    port map (clk => clk27, rst => rst, probes => probes, tck => obs_tck, tdi => u_tdi, tdo => u_tdo,
              sel => u_sel, capture => u_capture, shift => u_shift, update => u_update, tlr => u_tlr);

  obs_tdo <= tdo when tdo_oe = '1' else 'Z';
  obs_mark <= counter(12);
  led <= std_logic_vector(counter(26 downto 21));
end architecture;
