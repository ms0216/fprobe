-- SPDX-License-Identifier: Apache-2.0
-- fprobe_obs: ふつうの端子 4 本（TCK・TMS・TDI・TDO）で受ける JTAG の TAP（IEEE 1149.1）。
-- JTAG のユーザーの命令を FPGA の中から使えない FPGA（Gowin 等）や、FPGA の JTAG とは別の口で観測したいときに使う。
-- obs_core のユーザーの DR の口（BSCANE2 と同じ意味）を出す。
--
-- IR 4 ビット: 0x1 = IDCODE（Test-Logic-Reset の後）、0x2 = USER（obs_core）、0xF = BYPASS、ほかも BYPASS。
-- Capture-IR は "0001"（下位 2 ビットが 01）。TDO は TCK の立ち下がりで変わり、Shift-IR・Shift-DR の間だけ出す（tdo_oe）。
-- TRST は無い。状態は 16 個の 4 ビットの符号（どの値も正しい状態）なので、電源投入で何になっていても
-- TMS = 1 を 5 回送れば Test-Logic-Reset に戻る。
-- ONE_CLOCK = true のときは tck に対象の clk をつなぎ、tck_ce・tck_fall_ce で TCK の立ち上がり・立ち下がりを渡す
-- （obs_tck_sampler。tms・tdi も標本化したもの）。
library ieee;
use ieee.std_logic_1164.all;
use ieee.numeric_std.all;

entity obs_soft_tap is
    generic (
        IDCODE    : std_logic_vector(31 downto 0) := x"0F0B5001";  -- bit0 は 1（1149.1）
        ONE_CLOCK : boolean := false
    );
    port (
        -- 端子
        tck          : in  std_logic;
        tck_ce       : in  std_logic := '1';     -- 1 クロック版: この周期が TCK の立ち上がり
        tck_fall_ce  : in  std_logic := '0';     -- 1 クロック版: この周期が TCK の立ち下がり
        tms          : in  std_logic;
        tdi          : in  std_logic;
        tdo          : out std_logic;
        tdo_oe       : out std_logic;
        -- obs_core のユーザーの DR の口
        user_tdi     : out std_logic;
        user_tdo     : in  std_logic;
        user_sel     : out std_logic;
        user_capture : out std_logic;
        user_shift   : out std_logic;
        user_update  : out std_logic;
        user_tlr     : out std_logic
    );
end entity;

architecture rtl of obs_soft_tap is
    subtype state_t is std_logic_vector(3 downto 0);
    constant S_TLR     : state_t := x"F";
    constant S_RTI     : state_t := x"C";
    constant S_SEL_DR  : state_t := x"7";
    constant S_CAP_DR  : state_t := x"6";
    constant S_SH_DR   : state_t := x"2";
    constant S_EX1_DR  : state_t := x"1";
    constant S_PA_DR   : state_t := x"3";
    constant S_EX2_DR  : state_t := x"0";
    constant S_UP_DR   : state_t := x"5";
    constant S_SEL_IR  : state_t := x"4";
    constant S_CAP_IR  : state_t := x"E";
    constant S_SH_IR   : state_t := x"A";
    constant S_EX1_IR  : state_t := x"9";
    constant S_PA_IR   : state_t := x"B";
    constant S_EX2_IR  : state_t := x"8";
    constant S_UP_IR   : state_t := x"D";

    constant IR_IDCODE : std_logic_vector(3 downto 0) := x"1";
    constant IR_USER   : std_logic_vector(3 downto 0) := x"2";

    signal state    : state_t := S_TLR;
    signal ir       : std_logic_vector(3 downto 0) := IR_IDCODE;
    signal ir_shift : std_logic_vector(3 downto 0) := (others => '0');
    signal id_shift : std_logic_vector(31 downto 0) := (others => '0');
    signal bypass   : std_logic := '0';
    signal tdo_q    : std_logic := '0';
    signal tdo_oe_q : std_logic := '0';
    signal tdo_next    : std_logic;
    signal tdo_oe_next : std_logic;
begin
    user_tdi <= tdi;
    user_sel <= '1' when ir = IR_USER else '0';
    user_capture <= '1' when state = S_CAP_DR else '0';
    user_shift <= '1' when state = S_SH_DR else '0';
    user_update <= '1' when state = S_UP_DR else '0';
    user_tlr <= '1' when state = S_TLR else '0';
    tdo <= tdo_q;
    tdo_oe <= tdo_oe_q;

    process (tck)
    begin
        if rising_edge(tck) and tck_ce = '1' then
            -- 状態の移り（1149.1 の図 6-1）
            case state is
                when S_TLR    => if tms = '1' then state <= S_TLR;    else state <= S_RTI;    end if;
                when S_RTI    => if tms = '1' then state <= S_SEL_DR; else state <= S_RTI;    end if;
                when S_SEL_DR => if tms = '1' then state <= S_SEL_IR; else state <= S_CAP_DR; end if;
                when S_CAP_DR => if tms = '1' then state <= S_EX1_DR; else state <= S_SH_DR;  end if;
                when S_SH_DR  => if tms = '1' then state <= S_EX1_DR; else state <= S_SH_DR;  end if;
                when S_EX1_DR => if tms = '1' then state <= S_UP_DR;  else state <= S_PA_DR;  end if;
                when S_PA_DR  => if tms = '1' then state <= S_EX2_DR; else state <= S_PA_DR;  end if;
                when S_EX2_DR => if tms = '1' then state <= S_UP_DR;  else state <= S_SH_DR;  end if;
                when S_UP_DR  => if tms = '1' then state <= S_SEL_DR; else state <= S_RTI;    end if;
                when S_SEL_IR => if tms = '1' then state <= S_TLR;    else state <= S_CAP_IR; end if;
                when S_CAP_IR => if tms = '1' then state <= S_EX1_IR; else state <= S_SH_IR;  end if;
                when S_SH_IR  => if tms = '1' then state <= S_EX1_IR; else state <= S_SH_IR;  end if;
                when S_EX1_IR => if tms = '1' then state <= S_UP_IR;  else state <= S_PA_IR;  end if;
                when S_PA_IR  => if tms = '1' then state <= S_EX2_IR; else state <= S_PA_IR;  end if;
                when S_EX2_IR => if tms = '1' then state <= S_UP_IR;  else state <= S_SH_IR;  end if;
                when S_UP_IR  => if tms = '1' then state <= S_SEL_DR; else state <= S_RTI;    end if;
                when others   => state <= S_TLR;
            end case;

            -- 命令レジスタ
            case state is
                when S_TLR    => ir <= IR_IDCODE;
                when S_CAP_IR => ir_shift <= "0001";
                when S_SH_IR  => ir_shift <= tdi & ir_shift(3 downto 1);
                when S_UP_IR  => ir <= ir_shift;
                when others   => null;
            end case;

            -- IDCODE・BYPASS のデータレジスタ（USER は obs_core が持つ）
            if state = S_CAP_DR then
                id_shift <= IDCODE;
                bypass <= '0';
            elsif state = S_SH_DR then
                id_shift <= tdi & id_shift(31 downto 1);
                bypass <= tdi;
            end if;
        end if;
    end process;

    -- TDO は TCK の立ち下がりで変える（Shift-IR・Shift-DR の間だけ出す）
    process (all)
    begin
        tdo_next <= '0';
        tdo_oe_next <= '0';
        if state = S_SH_IR then
            tdo_next <= ir_shift(0);
            tdo_oe_next <= '1';
        elsif state = S_SH_DR then
            if ir = IR_IDCODE then
                tdo_next <= id_shift(0);
            elsif ir = IR_USER then
                tdo_next <= user_tdo;
            else
                tdo_next <= bypass;
            end if;
            tdo_oe_next <= '1';
        end if;
    end process;

    two_g : if not ONE_CLOCK generate
        process (tck)
        begin
            if falling_edge(tck) then
                tdo_q <= tdo_next;
                tdo_oe_q <= tdo_oe_next;
            end if;
        end process;
    end generate;
    one_g : if ONE_CLOCK generate
        process (tck)
        begin
            if rising_edge(tck) and tck_fall_ce = '1' then
                tdo_q <= tdo_next;
                tdo_oe_q <= tdo_oe_next;
            end if;
        end process;
    end generate;
end architecture;
