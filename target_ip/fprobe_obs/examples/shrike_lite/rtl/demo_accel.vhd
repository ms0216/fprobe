-- SPDX-License-Identifier: Apache-2.0
-- Shrike-Lite のデモ: RP2040 が SPI で使う計算器。A × B（8 ビット）を B 回の足し算で求める。足し算は STEP_CYCLES 周期に
-- 1 回（既定 256。25 MHz で B = 255 なら約 2.6 ms。MCU が終わりを待つ時間が出る）。
--
-- レジスタ（16 ビット）: 0x00 ID（0x5D01）、0x01 CTRL（書いて bit0 = 1 で開始）、0x02 A、0x03 B、0x04 RESULT、
--   0x06 STATUS（bit0 = 計算中、bit1 = 終わった）、0x07 残りの回数、0x08 MODE、0x09 S2_MASK、0x0A EVENTS。
--   読み出しは rd_addr の 1 周期後。
--
-- 題材の仕込み（fprobe で MCU と FPGA を並べて見つけるための不具合。MODE で実行中に入れ外しする。リセット後 0 = なし）:
--   bit0 = S1: 計算中に開始すると、残りの回数は数え直すが結果を 0 に戻さない（途中の和に足し続ける）。
--              CLEAR_ON_RESTART = true なら無効（直した版）。
--   bit1 = S2: 16 ビットの LFSR の下位 S2_MASK ビットがすべて 0 の周期に来た A・B の書き込みを捨てる（S2_MASK = 0 は
--              捨てない）。MCU からは「たまに結果が違う」としか見えない。
--   bit2 = S4: A = 0xFF のときに計算中の開始が来ると、計算中のまま進まなくなる（固まり）。MODE を書くと解ける。
--   EVENTS: bit3:0 = 捨てた回数、bit7:4 = 固まった回数（下位。書くと 0 に）。
library ieee;
use ieee.std_logic_1164.all;
use ieee.numeric_std.all;

entity demo_accel is
    generic (
        CLEAR_ON_RESTART : boolean  := false;
        STEP_CYCLES      : positive := 256         -- 足し算 1 回の周期数（2 の累乗でなくてよい）
    );
    port (
        clk      : in  std_logic;
        rst      : in  std_logic;
        wr_valid : in  std_logic;
        wr_addr  : in  std_logic_vector(6 downto 0);
        wr_data  : in  std_logic_vector(15 downto 0);
        rd_addr  : in  std_logic_vector(6 downto 0);
        rd_data  : out std_logic_vector(15 downto 0);
        -- 観測用
        state    : out std_logic_vector(1 downto 0);     -- 0 = 待ち、1 = 計算中、2 = 終わった
        restart  : out std_logic;                        -- 計算中に開始された（1 周期）
        left     : out std_logic_vector(7 downto 0);     -- 残りの回数
        dropped  : out std_logic;                        -- S2 で書き込みを捨てた（1 周期）
        stuck    : out std_logic;                        -- S4 で固まっている
        mode     : out std_logic_vector(2 downto 0)      -- 題材の仕込み（MODE）
    );
end entity;

architecture rtl of demo_accel is
    constant ID : std_logic_vector(15 downto 0) := x"5D01";
    type state_t is (S_IDLE, S_BUSY, S_DONE);
    signal st    : state_t;
    signal a, b  : unsigned(7 downto 0);
    signal sum   : unsigned(15 downto 0);
    signal count : unsigned(7 downto 0);
    signal step  : natural range 0 to STEP_CYCLES - 1;
    signal rd    : std_logic_vector(15 downto 0);
    signal again : std_logic;
    signal mode_r  : std_logic_vector(2 downto 0);
    signal s2_mask : natural range 0 to 16;
    signal s2_bits : std_logic_vector(15 downto 0);          -- 下位 s2_mask ビットが 1（比べる LFSR のビット）
    signal lfsr    : std_logic_vector(15 downto 0);
    signal drop_n  : unsigned(3 downto 0);
    signal stuck_n : unsigned(3 downto 0);
    signal stuck_r : std_logic;
    signal drop_r  : std_logic;
begin
    dropped <= drop_r;
    stuck <= stuck_r;
    mode <= mode_r;
    rd_data <= rd;
    left <= std_logic_vector(count);
    restart <= again;
    with st select state <=
        "01" when S_BUSY,
        "10" when S_DONE,
        "00" when others;

    process (clk)
        variable start  : boolean;
        variable status : std_logic_vector(15 downto 0);
        variable lose   : boolean;
        variable keep   : boolean;
    begin
        if rising_edge(clk) then
            start := wr_valid = '1' and unsigned(wr_addr) = 1 and wr_data(0) = '1';
            -- S2: LFSR の下位 s2_mask ビットがすべて 0 の周期の A・B の書き込みを捨てる
            lose := mode_r(1) = '1' and s2_mask > 0 and (lfsr and s2_bits) = x"0000";
            again <= '0';
            drop_r <= '0';
            lfsr <= lfsr(14 downto 0) & (lfsr(15) xor lfsr(13) xor lfsr(12) xor lfsr(10));
            if wr_valid = '1' then
                case to_integer(unsigned(wr_addr)) is
                    when 2 | 3 =>
                        if lose then
                            drop_r <= '1';
                            drop_n <= drop_n + 1;
                        elsif unsigned(wr_addr) = 2 then
                            a <= unsigned(wr_data(7 downto 0));
                        else
                            b <= unsigned(wr_data(7 downto 0));
                        end if;
                    when 8 =>
                        mode_r <= wr_data(2 downto 0);
                        if stuck_r = '1' then                 -- MODE を書くと固まりが解ける
                            stuck_r <= '0';
                            st <= S_IDLE;
                        end if;
                    when 9 =>
                        if unsigned(wr_data) > 16 then
                            s2_mask <= 16;
                            s2_bits <= (others => '1');
                        else
                            s2_mask <= to_integer(unsigned(wr_data(4 downto 0)));
                            for i in 0 to 15 loop
                                if i < to_integer(unsigned(wr_data(4 downto 0))) then
                                    s2_bits(i) <= '1';
                                else
                                    s2_bits(i) <= '0';
                                end if;
                            end loop;
                        end if;
                    when 10 =>
                        drop_n <= (others => '0');
                        stuck_n <= (others => '0');
                    when others => null;
                end case;
            end if;

            keep := mode_r(0) = '1' and not CLEAR_ON_RESTART;
            if start then
                count <= b;
                step <= 0;
                st <= S_BUSY;
                if st /= S_BUSY or not keep then
                    sum <= (others => '0');
                end if;
                if st = S_BUSY then
                    again <= '1';
                    if mode_r(2) = '1' and a = x"FF" then  -- S4: 固まる
                        stuck_r <= '1';
                        stuck_n <= stuck_n + 1;
                    end if;
                end if;
            elsif stuck_r = '1' then
                null;                                      -- 計算中のまま進まない
            elsif st = S_BUSY then
                if count = 0 then
                    st <= S_DONE;
                elsif step = STEP_CYCLES - 1 then
                    step <= 0;
                    sum <= sum + a;
                    count <= count - 1;
                else
                    step <= step + 1;
                end if;
            end if;

            status := (others => '0');
            if st = S_BUSY then
                status(0) := '1';
            end if;
            if st = S_DONE then
                status(1) := '1';
            end if;
            case to_integer(unsigned(rd_addr)) is
                when 0 => rd <= ID;
                when 2 => rd <= x"00" & std_logic_vector(a);
                when 3 => rd <= x"00" & std_logic_vector(b);
                when 4 => rd <= std_logic_vector(sum);
                when 6 => rd <= status;
                when 7 => rd <= x"00" & std_logic_vector(count);
                when 8 => rd <= x"000" & '0' & mode_r;
                when 9 => rd <= std_logic_vector(to_unsigned(s2_mask, 16));
                when 10 => rd <= x"00" & std_logic_vector(stuck_n) & std_logic_vector(drop_n);
                when others => rd <= (others => '0');
            end case;

            if rst = '1' then
                st <= S_IDLE;
                a <= (others => '0');
                b <= (others => '0');
                sum <= (others => '0');
                count <= (others => '0');
                step <= 0;
                rd <= (others => '0');
                again <= '0';
                mode_r <= (others => '0');
                s2_mask <= 0;
                s2_bits <= (others => '0');
                lfsr <= x"ACE1";
                drop_n <= (others => '0');
                stuck_n <= (others => '0');
                stuck_r <= '0';
                drop_r <= '0';
            end if;
        end if;
    end process;
end architecture;
