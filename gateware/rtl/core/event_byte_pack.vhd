-- イベントのバイトを詰める（1 つの源: SWO・UART の受信・UART の送信）。無損失: 1 件に最大 4 バイトと、各バイトの時刻を
-- 先頭のバイトの完全な時刻（48 ビット）+ 2〜4 バイト目の前のバイトからの差（各 12 ビット・時刻の刻み）で持つ。
-- 時刻は丸めない・捨てない: 差が 12 ビットに収まらないバイトは次の件にする。
-- 設計: docs/superpowers/specs/2026-11-01-bandwidth-observation-design.md 3.5。
--
-- 件の形（out_word。event_collector が上に種類 [127:120] を付ける）:
--   [47:0] 先頭のバイトの時刻、[79:48] バイト（i 番目を [48+8i+7 : 48+8i]）、[115:80] 差（i 番目のバイトの差を
--   [80+12(i-1)+11 : 80+12(i-1)]、i = 1〜3）、[117:116] バイトの数 − 1、[119:118] 0。使わないバイト・差の欄は 0。
--
-- バイトの時刻（in_time）は源が付けたもの（UART・SWO の受信は開始ビットの辺を見つけた周期の time。時刻の正しさの R3:
-- docs/superpowers/specs/2026-11-01-time-accuracy-design.md）で、受けた周期とは限らない。同じ源の時刻は受けた順に増える。
--
-- 件を切る条件（どれも「そうしないと詰められない・出せない」という事実で、経験則の閾値は無い）:
--   1. 4 バイト溜まった（欄が尽きた）。
--   2. 来たバイトの時刻と前のバイトの時刻の差が 12 ビット（0〜4095）に収まらない: 件を切り、そのバイトから次の件を始める。
--   3. 最後のバイトを受けてから 2**12 周期たった: 溜めておく時間の上限（後のバイトが同じ件に入れるかは 2 が決める）。
--      源の時刻が受けた周期と同じ（遅れが一定）なら、この周期以後のバイトの差は収まらないので、件を切っても失うものは無い。
--   よって溜める時間の上限は「先頭のバイトを受けてから 3 × 4095 + 4096 周期」（100 MHz で 163.8 µs）と、出口（event_collector の
--   1 周期 1 件）を待つ間。
-- 出口は 1 件の保持（ready）。切るべき件が出口の空きを待つ間に来たバイトは捨てて lost に数える（黙って捨てない）。
-- enable = '0'（記録を止めた）の周期に溜めていたバイトも、記録に残らないので lost に数えて捨てる。
-- lost は 1 周期遅れて出る（その周期に落とした数をレジスタに置く）。
library ieee;
use ieee.std_logic_1164.all;
use ieee.numeric_std.all;

entity event_byte_pack is
  port (
    clk, rst   : in  std_logic;
    enable     : in  std_logic;
    in_fire    : in  std_logic;                      -- この周期にバイトを受けた
    in_data    : in  std_logic_vector(7 downto 0);
    in_time    : in  unsigned(47 downto 0);          -- そのバイトの時刻（源が付けたもの）
    out_valid  : out std_logic;                      -- 出すのを待つ件がある
    out_taken  : in  std_logic;                      -- この周期に出した（out_valid = '1' の周期だけ）
    out_word   : out std_logic_vector(119 downto 0);
    lost       : out natural range 0 to 8            -- 前の周期に捨てたバイトの数
  );
end entity;

architecture rtl of event_byte_pack is
  constant DELTA_BITS : positive := 12;
  constant GAP_LIMIT  : natural := 2 ** DELTA_BITS;  -- この周期数たったら、後のバイトは同じ件に入らない
  signal acc_n      : natural range 0 to 4 := 0;      -- 詰めている件のバイトの数
  signal acc_time   : std_logic_vector(47 downto 0) := (others => '0');
  signal acc_bytes  : std_logic_vector(31 downto 0) := (others => '0');
  signal acc_deltas : std_logic_vector(35 downto 0) := (others => '0');
  signal gap        : natural range 0 to GAP_LIMIT := GAP_LIMIT;   -- 最後のバイトを受けてからの周期数（GAP_LIMIT で止まる）
  signal last_time  : unsigned(47 downto 0) := (others => '0');    -- 詰めている件の最後のバイトの時刻
  signal delta      : unsigned(47 downto 0);                         -- 来たバイトの時刻 − last_time
  signal fits       : boolean;                                       -- 差が 12 ビットに収まる
  signal ready      : std_logic := '0';
  signal ready_n    : natural range 0 to 4 := 0;
  signal ready_word : std_logic_vector(119 downto 0) := (others => '0');
  signal lost_r     : natural range 0 to 8 := 0;
begin
  delta <= in_time - last_time;
  fits  <= delta < GAP_LIMIT;                        -- 前より小さい時刻（来ないはず）は大きな数になり、収まらない
  out_valid <= ready;
  out_word  <= ready_word;
  lost      <= lost_r;

  process (clk)
    variable n       : natural range 0 to 4;
    variable lost_v  : natural range 0 to 8;
    variable free    : boolean;
    variable closing : boolean;
    variable left    : natural range 0 to 4;   -- この周期の終わりに ready に残る件のバイトの数
  begin
    if rising_edge(clk) then
      lost_v := 0;
      n := acc_n;
      free := ready = '0' or out_taken = '1';
      left := 0;
      if ready = '1' and out_taken = '0' then
        left := ready_n;
      end if;
      closing := acc_n = 4 or (acc_n > 0 and gap = GAP_LIMIT)
                 or (acc_n > 0 and in_fire = '1' and enable = '1' and not fits);
      if out_taken = '1' then
        ready <= '0';
      end if;
      if closing and free then
        ready      <= '1';
        ready_n    <= acc_n;
        ready_word <= "00" & std_logic_vector(to_unsigned(acc_n - 1, 2)) & acc_deltas & acc_bytes & acc_time;
        left := acc_n;
        n := 0;
      end if;
      if in_fire = '1' and enable = '1' then
        if n = 0 then
          acc_time   <= std_logic_vector(in_time);
          acc_bytes  <= x"000000" & in_data;
          acc_deltas <= (others => '0');
          last_time  <= in_time;
          n := 1;
          gap <= 1;
        elsif not closing then                       -- n = 1〜3、差は 12 ビットに収まる（fits）
          for i in 1 to 3 loop                       -- 欄の位置は定数（変数の範囲の切り出しを合成に渡さない）
            if n = i then
              acc_bytes(8 * i + 7 downto 8 * i) <= in_data;
              acc_deltas(DELTA_BITS * (i - 1) + DELTA_BITS - 1 downto DELTA_BITS * (i - 1))
                <= std_logic_vector(delta(DELTA_BITS - 1 downto 0));
            end if;
          end loop;
          last_time <= in_time;
          n := n + 1;
          gap <= 1;
        else                                         -- 切るべき件が出口の空きを待っている
          lost_v := 1;
          if gap < GAP_LIMIT then
            gap <= gap + 1;
          end if;
        end if;
      elsif gap < GAP_LIMIT then
        gap <= gap + 1;
      end if;
      acc_n <= n;
      if enable = '0' then                           -- 記録を止めた: 溜めていたバイトは記録に残らない
        lost_v := n + left;
        acc_n <= 0;
        ready <= '0';
      end if;
      if rst = '1' then
        acc_n  <= 0;
        ready  <= '0';
        gap    <= GAP_LIMIT;
        lost_v := 0;
      end if;
      lost_r <= lost_v;
    end if;
  end process;
end architecture;
