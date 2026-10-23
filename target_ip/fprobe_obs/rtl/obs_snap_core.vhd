-- SPDX-License-Identifier: Apache-2.0
-- fprobe_obs: 極小の FPGA 向けの標本型の観測の回路（docs/superpowers/specs/2026-10-20-fpga-observation-v2-design.md の
-- 「標本型」）。FIFO も記録も命令も持たず、JTAG のスキャンごとに今の値を読ませる（ForgeFPGA SLG47910 等、記録型の
-- obs_core が入らない FPGA 用。ブロックで 20 個前後）。
--
-- JTAG の 4 本（tck・tms・tdi・tdo）を clk で標本化して動く（obs_tck_sampler + obs_soft_tap の 1 クロック版。TCK は
-- clk の 1/8 以下）。USER（IR 0x2）の DR は 32 ビット:
--   31:24 = 0xA7（標本型の印。記録型の見出しは 0xA6。fprobe はこれで型を見分ける）
--   23:16 = 前の Capture-DR からの値の変化の回数（255 で止める。2 以上なら標本の間に見えない変化があった）
--   15:0  = Capture-DR の周期の probes
-- fprobe は Capture-DR の瞬間の自分の時刻を付けて値にする（時刻合わせは要らない。標本の間隔 = スキャンの間隔）。
library ieee;
use ieee.std_logic_1164.all;
use ieee.numeric_std.all;

entity obs_snap_core is
    generic (
        IDCODE : std_logic_vector(31 downto 0) := x"0F0B5001"
    );
    port (
        clk    : in  std_logic;
        rst    : in  std_logic;
        probes : in  std_logic_vector(15 downto 0);
        tck    : in  std_logic;
        tms    : in  std_logic;
        tdi    : in  std_logic;
        tdo    : out std_logic;
        tdo_oe : out std_logic
    );
end entity;

architecture rtl of obs_snap_core is
    constant MARK : std_logic_vector(7 downto 0) := x"A7";
    signal rise, fall, tms_s, tdi_s : std_logic;
    signal u_tdi, u_sel, u_capture, u_shift, u_update, u_tlr : std_logic;
    signal prev    : std_logic_vector(15 downto 0);
    signal changes : unsigned(7 downto 0);
    signal dr      : std_logic_vector(31 downto 0);
begin
    sampler : entity work.obs_tck_sampler
        port map (clk => clk, tck => tck, tms => tms, tdi => tdi, rise => rise, fall => fall, tms_s => tms_s,
                  tdi_s => tdi_s);
    tap : entity work.obs_soft_tap
        generic map (IDCODE => IDCODE, ONE_CLOCK => true)
        port map (tck => clk, tck_ce => rise, tck_fall_ce => fall, tms => tms_s, tdi => tdi_s, tdo => tdo,
                  tdo_oe => tdo_oe, user_tdi => u_tdi, user_tdo => dr(0), user_sel => u_sel,
                  user_capture => u_capture, user_shift => u_shift, user_update => u_update, user_tlr => u_tlr);

    process (clk)
        variable captured : boolean;
        variable counted  : unsigned(7 downto 0);   -- この周期の変化まで数えた回数（255 で止める）
    begin
        if rising_edge(clk) then
            captured := rise = '1' and u_sel = '1' and u_capture = '1';
            counted := changes;
            if probes /= prev and changes /= 255 then
                counted := changes + 1;
            end if;
            prev <= probes;
            -- Capture-DR の周期の値を取り込むので、その周期の変化までをこの標本の回数にする（値と回数の範囲を揃える）
            if captured then
                dr <= MARK & std_logic_vector(counted) & probes;
                changes <= (others => '0');
            else
                changes <= counted;
                if rise = '1' and u_sel = '1' and u_shift = '1' then
                    dr <= u_tdi & dr(31 downto 1);
                end if;
            end if;
            if rst = '1' then
                prev <= probes;
                changes <= (others => '0');
                dr <= (others => '0');
            end if;
        end if;
    end process;
end architecture;
