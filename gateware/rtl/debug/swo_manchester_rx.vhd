-- SWO の Manchester 方式の受信器（ARM の TPIU / ITM の SWO、CMSIS-DAP の DAP_SWO_Mode = 2）。
--
-- 線の形（Arm CoreSight の SWO。Black Magic Probe・Orbuculum の解読と同じ）:
--   * 休止は Low。フレームは開始ビット（値 1）、データ（バイトの LSB から）、休止（Low が 1 ビット以上）。
--   * ビットの値はビットの中央の変化の向き: 1 = High → Low（前半 High）、0 = Low → High（前半 Low）。
--     同じ値が続くと、ビットの境目にも変化が入る（向きに意味は無い）。
--   * 1 フレームのバイト数は決まっていない（8 ビットごとに 1 バイトを出す）。
--
-- 解読: 中央の変化ごとに時計を合わせ直す（ビットごとに同期するので、送り手と速さがずれても溜まらない）。
--   T = divisor（1 ビットの clk 周期数）。直前の中央の変化からの経過 t で次の変化を分ける:
--     t <  T - floor(T/4)   … 境目の変化（無視する。境目は T/2 に来る）
--     t >= T - floor(T/4)   … 中央の変化（ビットの値 = 向き）
--     t >= T + floor(T/4) で変化が無い … フレームの終わり。8 ビットに満たない端数があれば frame_error
--   変化の検出は 2 段の同期化を通すので、時刻は ±1 周期ずれる。境目（T/2 + 1 未満）と中央（T - 1 超）を分けられる
--   最小は T = 4（100 MHz で 25 Mbit/s）。余裕（線のひずみ・速さのずれ）は T が大きいほど増える（swo_receiver の説明）。
--   開始ビットは休止（Low）からの立ち上がりで見つけ、その中央（立ち下がり）を最初の同期点にする。立ち上がったまま
--   T - floor(T/4) の間 下がらなければ Manchester の開始ではない（線が High のまま = つないでいない等）: Low に戻るまで待つ。
-- divisor は 4 以上で使う（dap_cmd_swo が制限する）。受信中に変えない。
library ieee;
use ieee.std_logic_1164.all;
use ieee.numeric_std.all;

entity swo_manchester_rx is
  generic (
    DIVISOR_BITS : positive := 16
  );
  port (
    clk         : in  std_logic;
    rst         : in  std_logic;
    divisor     : in  unsigned(DIVISOR_BITS - 1 downto 0);   -- 1 ビットの clk 周期数（4 以上）
    rx          : in  std_logic;                             -- 非同期でよい
    out_valid   : out std_logic;                             -- 1 周期（受け手は毎周期取り出す）
    out_data    : out std_logic_vector(7 downto 0);
    frame_error : out std_logic                              -- 1 周期: 端数のビットでフレームが終わった
  );
end entity;

architecture rtl of swo_manchester_rx is
  type state_t is (S_IDLE, S_START, S_BITS, S_WAIT_LOW);
  signal state     : state_t := S_IDLE;
  signal rx_sync   : std_logic;
  signal rx_prev   : std_logic := '0';
  signal timer     : unsigned(DIVISOR_BITS downto 0) := (others => '0');    -- 直前の同期点からの周期数（飽和）
  signal mid_from  : unsigned(DIVISOR_BITS downto 0) := (others => '0');    -- T - floor(T/4)
  signal end_at    : unsigned(DIVISOR_BITS downto 0) := (others => '0');    -- T + floor(T/4)
  signal bit_count : unsigned(2 downto 0) := (others => '0');
  signal shreg     : std_logic_vector(7 downto 0) := (others => '0');
  signal valid_r   : std_logic := '0';
  signal error_r   : std_logic := '0';
  signal edge      : boolean;
begin
  sync_i : entity work.sync_bit
    generic map (INIT => '0')
    port map (clk => clk, d => rx, q => rx_sync);

  edge <= rx_sync /= rx_prev;

  process (clk)
    variable t, q : unsigned(DIVISOR_BITS downto 0);
  begin
    if rising_edge(clk) then
      -- 閾値は divisor から作る（受信中は変わらない。1 周期遅れてよい）
      t        := resize(divisor, DIVISOR_BITS + 1);
      q        := shift_right(t, 2);
      mid_from <= t - q;
      end_at   <= t + q;

      rx_prev <= rx_sync;
      valid_r <= '0';
      error_r <= '0';
      if timer /= (timer'range => '1') then
        timer <= timer + 1;
      end if;

      if rst = '1' then
        state     <= S_IDLE;
        rx_prev   <= '0';
        timer     <= (others => '0');
        bit_count <= (others => '0');
        shreg     <= (others => '0');
      else
        case state is
          when S_IDLE =>
            if rx_sync = '1' and rx_prev = '0' then         -- 開始ビットの前半（High）の始まり
              timer <= to_unsigned(1, timer'length);
              state <= S_START;
            elsif rx_sync = '1' then                        -- High のまま（リセット直後等）: Low を待つ
              state <= S_WAIT_LOW;
            end if;

          when S_START =>
            if rx_sync = '0' then                           -- 開始ビットの中央 = 最初の同期点
              timer     <= to_unsigned(1, timer'length);
              bit_count <= (others => '0');
              state     <= S_BITS;
            elsif timer >= mid_from then                    -- 半ビットより長く High: Manchester の開始ではない
              state <= S_WAIT_LOW;
            end if;

          when S_BITS =>
            if edge and timer >= mid_from then              -- 中央の変化: 下がった = 1、上がった = 0
              timer     <= to_unsigned(1, timer'length);
              shreg     <= (not rx_sync) & shreg(7 downto 1);
              bit_count <= bit_count + 1;
              if bit_count = 7 then
                valid_r <= '1';
              end if;
            elsif timer >= end_at then                      -- 変化が来ない: フレームの終わり
              if bit_count /= 0 then
                error_r <= '1';
              end if;
              state <= S_IDLE when rx_sync = '0' else S_WAIT_LOW;
            end if;

          when S_WAIT_LOW =>
            if rx_sync = '0' then
              state <= S_IDLE;
            end if;
        end case;
      end if;
    end if;
  end process;

  out_valid   <= valid_r;
  out_data    <= shreg;
  frame_error <= error_r;
end architecture;
