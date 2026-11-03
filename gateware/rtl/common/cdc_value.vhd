-- 多ビット値のクロックドメイン乗せ換え（保持 + トグルの握手）。値の組（WIDTH ビット）は 1 つの時点のものとして揃って届く。
--
-- 送り側: src_load（1 周期）で src_value を送る。前の値がまだ届いていない（受け側の受け取りの返事が戻っていない）間の
--   src_load は、最新の 1 つを控え、返事が戻ったらそれを送る（間の値は飛ばす。最後に src_load した値は必ず届く）。
--   送っている値 held は、受け側が受け取って返事が戻るまで変えない（握手。受け側が取り込む間に値が変わって、ビットごとに
--   古い値と新しい値が混ざることが無い）。src_ready = 1 は、送っているものも控えも無い（いま src_load した値がすぐ送られる）。
--   値を流し続けるときは src_load = '1' のままにする（返事が戻るたびに、その周期の src_value を送る。受け渡しの速さの上限で
--   最新の値が届く）。
-- 受け側: トグルを 2 段で同期化し、変化を見たら保持値を取り込んで dst_changed を 1 周期出す。返事は「最後に見たトグル」
--   （t_prev）そのもの。電源投入の値がどうであっても、送り側と受け側の見方は数周期で揃う（握手が止まらない）。
-- リセット（任意。同期・アクティブハイ）: src_rst は送りかけ・控えを捨てて保持値を 0 に、dst_rst は受けた値を 0 にする。
--   両方を同時にかけると、解いた後に前の値は届かない（受けた値は 0 から始まる）。片方だけでも握手は数周期で揃う。
-- SAME_CLOCK = true（src_clk と dst_clk が同じクロック）: 同期化を使わず 1 周期で渡す（dst_value は src_load の周期の
--   src_value を次の周期に出し、dst_changed も同じ周期に出す。src_ready は常に 1）。
library ieee;
use ieee.std_logic_1164.all;

entity cdc_value is
  generic (
    WIDTH      : positive;
    SAME_CLOCK : boolean := false
  );
  port (
    src_clk     : in  std_logic;
    src_rst     : in  std_logic := '0';
    src_load    : in  std_logic;
    src_value   : in  std_logic_vector(WIDTH - 1 downto 0);
    src_ready   : out std_logic;
    dst_clk     : in  std_logic;
    dst_rst     : in  std_logic := '0';
    dst_value   : out std_logic_vector(WIDTH - 1 downto 0);
    dst_changed : out std_logic
  );
end entity;

architecture rtl of cdc_value is
  signal value_r : std_logic_vector(WIDTH - 1 downto 0) := (others => '0');
  signal changed : std_logic := '0';
begin
  dst_value   <= value_r;
  dst_changed <= changed;

  same_g : if SAME_CLOCK generate
    process (dst_clk)
    begin
      if rising_edge(dst_clk) then
        changed <= src_load;
        if src_load = '1' then
          value_r <= src_value;
        end if;
        if dst_rst = '1' or src_rst = '1' then
          changed <= '0';
          value_r <= (others => '0');
        end if;
      end if;
    end process;
    src_ready <= '1';
  end generate;

  cross_g : if not SAME_CLOCK generate
    signal held    : std_logic_vector(WIDTH - 1 downto 0) := (others => '0');
    signal next_v  : std_logic_vector(WIDTH - 1 downto 0) := (others => '0');   -- 送っている間に来た最新の値
    signal pend    : std_logic := '0';                                          -- next_v を送る
    signal toggle  : std_logic := '0';
    signal ack_s   : std_logic;                                                 -- 受け側の返事（送り側に同期化）
    signal busy    : std_logic;
    signal t_sync  : std_logic;
    signal t_prev  : std_logic := '0';
  begin
    busy <= toggle xor ack_s;

    process (src_clk)
    begin
      if rising_edge(src_clk) then
        if src_rst = '1' then
          held   <= (others => '0');
          pend   <= '0';
          toggle <= '0';
        elsif busy = '0' then
          if src_load = '1' then
            held   <= src_value;
            toggle <= not toggle;
            pend   <= '0';
          elsif pend = '1' then
            held   <= next_v;
            toggle <= not toggle;
            pend   <= '0';
          end if;
        elsif src_load = '1' then
          next_v <= src_value;
          pend   <= '1';
        end if;
      end if;
    end process;
    src_ready <= not busy and not pend;

    -- 送っている値は、受け側の返事が戻るまで変えない（上の busy = '0' の時だけ held を書く）
    -- synthesis translate_off
    process (src_clk)
      variable held_prev : std_logic_vector(WIDTH - 1 downto 0);
      variable busy_prev : std_logic := '0';
    begin
      if rising_edge(src_clk) then
        assert not (busy_prev = '1' and busy = '1' and held /= held_prev and src_rst = '0')
          report "cdc_value: held changed while the value was in flight" severity failure;
        held_prev := held;
        busy_prev := busy;
      end if;
    end process;
    -- synthesis translate_on

    t_sync_i : entity work.sync_bit
      port map (clk => dst_clk, d => toggle, q => t_sync);
    ack_sync_i : entity work.sync_bit
      port map (clk => src_clk, d => t_prev, q => ack_s);

    process (dst_clk)
    begin
      if rising_edge(dst_clk) then
        t_prev  <= t_sync;
        changed <= '0';
        if dst_rst = '1' then
          value_r <= (others => '0');
        elsif t_sync /= t_prev then
          value_r <= held;                   -- held はトグルの前から、返事が戻るまで変わらない
          changed <= '1';
        end if;
      end if;
    end process;
  end generate;
end architecture;
