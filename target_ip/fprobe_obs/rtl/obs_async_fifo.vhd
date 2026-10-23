-- SPDX-License-Identifier: Apache-2.0
-- fprobe_obs: 非同期 FIFO（書き = 対象のクロック、読み = JTAG の TCK）。グレイ符号の位置を 2 段で同期する。
--
-- 読み側の TCK はスキャンの間しか動かない。そのため:
--  * 読み側には reset を持たない（TCK が来ないうちは効かない）。書き側の reset は書きの位置を
--    「同期した読みの位置」に合わせて空にする（読み側を触らずに空にできる）。
--  * 読み出しは同期の RAM（MAX10 等の LUT の RAM を持たない FPGA でもブロック RAM に入る）。
--    r_data = mem(r_ptr + r_ahead) を TCK の毎周期で読む。r_ahead = '1' で先頭の次を前もって読める
--    （先頭を取り出すのと同じ周期に次の語を使う使い方のため）。
--  * r_count は読み側から見た記録の数（書きの位置の同期の遅れの分だけ少なく見えることはあるが、多く見えることはない）。
--  * w_free は書き側から見た空き（同じく、少なく見えることはあるが、多く見えることはない）。
--  * SAME_CLOCK = true（読みも書きも同じクロック。obs_core の 1 クロック版）では位置を同期せず、そのまま比べる。
library ieee;
use ieee.std_logic_1164.all;
use ieee.numeric_std.all;

entity obs_async_fifo is
    generic (
        WIDTH      : positive := 32;
        DEPTH_BITS : positive := 9;          -- 深さ = 2**DEPTH_BITS
        SAME_CLOCK : boolean  := false        -- w_clk と r_clk が同じ（位置の同期を省く）
    );
    port (
        -- 書き側
        w_clk   : in  std_logic;
        w_rst   : in  std_logic;
        w_valid : in  std_logic;              -- w_free > 0 のときだけ上げる（一杯のときの書き込みは捨てる）
        w_data  : in  std_logic_vector(WIDTH - 1 downto 0);
        w_free  : out unsigned(DEPTH_BITS downto 0);
        -- 読み側
        r_clk   : in  std_logic;
        r_pop   : in  std_logic;              -- r_count > 0 のときだけ上げる
        r_ahead : in  std_logic;
        r_data  : out std_logic_vector(WIDTH - 1 downto 0);
        r_count : out unsigned(DEPTH_BITS downto 0)
    );
end entity;

architecture rtl of obs_async_fifo is
    subtype ptr_t is unsigned(DEPTH_BITS downto 0);
    type mem_t is array (0 to 2 ** DEPTH_BITS - 1) of std_logic_vector(WIDTH - 1 downto 0);

    function to_gray(b : ptr_t) return ptr_t is
    begin
        return b xor shift_right(b, 1);
    end function;

    function from_gray(g : ptr_t) return ptr_t is
        variable b : ptr_t;
    begin
        b(b'high) := g(g'high);
        for i in b'high - 1 downto 0 loop
            b(i) := b(i + 1) xor g(i);
        end loop;
        return b;
    end function;

    signal mem : mem_t;

    -- 書き側
    signal w_ptr       : ptr_t := (others => '0');
    signal w_ptr_gray  : ptr_t := (others => '0');
    signal r_gray_w1   : ptr_t := (others => '0');
    signal r_gray_w2   : ptr_t := (others => '0');
    -- 読み側
    signal r_ptr       : ptr_t := (others => '0');
    signal r_ptr_gray  : ptr_t := (others => '0');
    signal w_gray_r1   : ptr_t := (others => '0');
    signal w_gray_r2   : ptr_t := (others => '0');
    signal r_data_q    : std_logic_vector(WIDTH - 1 downto 0) := (others => '0');
    signal r_seen      : ptr_t;                                     -- 書き側から見た読みの位置
    signal w_seen      : ptr_t;                                     -- 読み側から見た書きの位置
begin
    r_data <= r_data_q;

    two_g : if not SAME_CLOCK generate
        r_seen <= from_gray(r_gray_w2);
        w_seen <= from_gray(w_gray_r2);
    end generate;
    same_g : if SAME_CLOCK generate
        r_seen <= r_ptr;
        w_seen <= w_ptr;
    end generate;
    w_free <= to_unsigned(2 ** DEPTH_BITS, DEPTH_BITS + 1) - (w_ptr - r_seen);
    r_count <= w_seen - r_ptr;

    process (w_clk)
        variable next_ptr : ptr_t;
    begin
        if rising_edge(w_clk) then
            r_gray_w1 <= r_ptr_gray;
            r_gray_w2 <= r_gray_w1;
            if w_rst = '1' then
                next_ptr := r_seen;
            elsif w_valid = '1' and w_ptr - r_seen /= 2 ** DEPTH_BITS then
                mem(to_integer(w_ptr(DEPTH_BITS - 1 downto 0))) <= w_data;
                next_ptr := w_ptr + 1;
            else
                next_ptr := w_ptr;
            end if;
            w_ptr <= next_ptr;
            w_ptr_gray <= to_gray(next_ptr);
        end if;
    end process;

    process (r_clk)
        variable next_ptr : ptr_t;
        variable addr     : ptr_t;
    begin
        if rising_edge(r_clk) then
            w_gray_r1 <= w_ptr_gray;
            w_gray_r2 <= w_gray_r1;
            if r_pop = '1' then
                next_ptr := r_ptr + 1;
            else
                next_ptr := r_ptr;
            end if;
            r_ptr <= next_ptr;
            r_ptr_gray <= to_gray(next_ptr);
            if r_ahead = '1' then
                addr := next_ptr + 1;
            else
                addr := next_ptr;
            end if;
            r_data_q <= mem(to_integer(addr(DEPTH_BITS - 1 downto 0)));
        end if;
    end process;
end architecture;
