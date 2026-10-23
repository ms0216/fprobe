-- SPDX-License-Identifier: Apache-2.0
-- fprobe_obs: JTAG の端子（TCK・TMS・TDI）を対象の clk で標本化し、TCK の立ち上がり・立ち下がりを 1 周期の印にする
-- （obs_soft_tap・obs_core の 1 クロック版の前段。TAP も観測の回路も FIFO も clk だけで動く）。
--
-- 3 本とも同じ 2 段で同期するので、rise の周期の tms・tdi は TCK が上がったときの値（JTAG の送り手は TCK の
-- 立ち下がりで TMS・TDI を変える）。TCK は clk の 1/8 以下であること（TDO は立ち下がりを見てから変わるので、
-- 送り手が TDO を読む立ち上がりまでに clk で 3 周期と出力の遅れが要る）。
library ieee;
use ieee.std_logic_1164.all;

entity obs_tck_sampler is
    port (
        clk   : in  std_logic;
        tck   : in  std_logic;
        tms   : in  std_logic;
        tdi   : in  std_logic;
        rise  : out std_logic;              -- この周期の終わりの辺を TCK の立ち上がりとして扱う
        fall  : out std_logic;              -- 同じく立ち下がり
        tms_s : out std_logic;
        tdi_s : out std_logic
    );
end entity;

architecture rtl of obs_tck_sampler is
    signal s_tck : std_logic_vector(2 downto 0) := (others => '0');
    signal s_tms : std_logic_vector(1 downto 0) := (others => '1');
    signal s_tdi : std_logic_vector(1 downto 0) := (others => '0');
begin
    rise <= s_tck(1) and not s_tck(2);
    fall <= s_tck(2) and not s_tck(1);
    tms_s <= s_tms(1);
    tdi_s <= s_tdi(1);

    process (clk)
    begin
        if rising_edge(clk) then
            s_tck <= s_tck(1 downto 0) & tck;
            s_tms <= s_tms(0) & tms;
            s_tdi <= s_tdi(0) & tdi;
        end if;
    end process;
end architecture;
