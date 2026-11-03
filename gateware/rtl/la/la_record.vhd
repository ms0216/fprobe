-- ロジアナの記録（ボード非依存、clk のドメイン）: 前段（la_front・la_lane_front）が出すサンプルの列とトリガの印を BRAM に溜める。
-- 仕様: docs/superpowers/specs/2026-10-16-m7-logic-analyzer-design.md §2、2026-11-01-la-resolution-design.md §3.3
-- （la_capture から記録の部分を分けたもの。la_capture = la_front + la_record、la_lane_capture = la_lane_front + la_record）
--
-- 1 語 = WIDTH ビット（la_capture: 1 サンプル、la_lane_capture: 1 周期の N サンプル）。数える単位は語。
-- run の 0→1 で開始: プリトリガ分（pretrigger 語）を溜め、トリガを待ち（その間はリングで上書き）、
-- トリガ後に残り（DEPTH - pretrigger 語。トリガの語を含む）を溜めて完了。run の 1→0 で中止。
-- valid = 1 の周期の data を 1 語として溜める。hit = 1 ならその語でトリガが成立した（valid と同じ周期）。
-- arm = 1 の間（トリガ待ち）だけ、前段は外部トリガを報告する（プリトリガを溜める間の外部トリガは使わない）。
-- 完了後、バッファは start_index から DEPTH 語が時間順（rd_addr で同期読み出し、1 周期遅れ。RD_REG なら 2 周期遅れ）。
-- トリガの語を受けた周期の time・phase を trigger_time・trigger_phase に持つ（phase は前段が決める細かい位置）。
-- 速いクロック（300 MHz 級）のため、幅の広い比較・加減算を毎周期の経路に置かない: pretrigger は DEPTH − 1 以下（下位 ADDR_BITS ビットだけ使う）。
-- FAST = true（取り込み領域）: トリガの時刻・位相・start_index はトリガを受けた周期の 1 周期後に確定する（done の 1 周期後。読み出しは
-- done を見てから始めるので影響しない）。run も 1 周期遅らせて使う（前段も FAST にする）。FAST = false は元の回路と周期まで同じ。
library ieee;
use ieee.std_logic_1164.all;
use ieee.numeric_std.all;
use work.util_pkg.all;

entity la_record is
  generic (
    WIDTH  : positive := 16;
    DEPTH  : positive := 8192;                  -- 語。2 の冪
    RD_REG : boolean := false;
    FAST   : boolean := false
  );
  port (
    clk           : in  std_logic;
    rst           : in  std_logic;
    run           : in  std_logic;
    pretrigger    : in  unsigned(31 downto 0);   -- DEPTH - 1 まで（語）
    valid         : in  std_logic;
    data          : in  std_logic_vector(WIDTH - 1 downto 0);
    hit           : in  std_logic;
    time          : in  unsigned(63 downto 0);
    phase         : in  unsigned(3 downto 0);
    arm           : out std_logic;
    capturing     : out std_logic;
    triggered     : out std_logic;
    done          : out std_logic;
    start_index   : out unsigned(31 downto 0);
    trigger_index : out unsigned(31 downto 0);
    trigger_time  : out unsigned(63 downto 0);
    trigger_phase : out unsigned(3 downto 0);
    rd_addr       : in  unsigned(clog2(DEPTH) - 1 downto 0);
    rd_data       : out std_logic_vector(WIDTH - 1 downto 0)
  );
end entity;

architecture rtl of la_record is
  constant ADDR_BITS : natural := clog2(DEPTH);
  type state_t is (S_IDLE, S_PRE, S_WAIT, S_POST, S_DONE);
  type memory_t is array (0 to DEPTH - 1) of std_logic_vector(WIDTH - 1 downto 0);

  signal memory    : memory_t;
  signal state     : state_t := S_IDLE;
  signal run_prev  : std_logic := '0';
  signal run_q, run_i : std_logic := '0';                                  -- FAST: run を 1 周期遅らせたもの（設定の pre_zero などが run の開始の周期に間に合う）
  signal wp        : unsigned(ADDR_BITS - 1 downto 0) := (others => '0');
  signal pre_low   : unsigned(ADDR_BITS - 1 downto 0) := (others => '0');   -- pretrigger の下位
  signal post_len : unsigned(ADDR_BITS - 1 downto 0) := (others => '0');   -- DEPTH − pretrigger − 1
  signal first : boolean := false;                                          -- 開始してから最初の語まで（それまで left を pre_low から取り直す）
  signal pre_one, post_one, left1 : boolean := false;                      -- pre_low = 1、post_len = 1、left = 1（left の更新と同時に決めるフラグ）
  signal pre_zero  : boolean := true;
  signal post_none : boolean := false;
  signal left      : unsigned(ADDR_BITS - 1 downto 0) := (others => '0');
  signal ev        : boolean := false;                    -- トリガを受けた（1 周期後に時刻などを確定する）
  signal time_d    : unsigned(63 downto 0) := (others => '0');
  signal phase_d   : unsigned(3 downto 0) := (others => '0');
  signal q2        : std_logic_vector(WIDTH - 1 downto 0) := (others => '0');
  signal start_r   : unsigned(ADDR_BITS - 1 downto 0) := (others => '0');
  signal trig_r    : unsigned(ADDR_BITS - 1 downto 0) := (others => '0');
  signal time_r    : unsigned(63 downto 0) := (others => '0');
  signal phase_r   : unsigned(3 downto 0) := (others => '0');
  signal q         : std_logic_vector(WIDTH - 1 downto 0) := (others => '0');
begin
  assert 2 ** ADDR_BITS = DEPTH report "la_record: DEPTH must be a power of two" severity failure;

  arm <= '1' when state = S_WAIT else '0';

  process (clk)
  begin
    if rising_edge(clk) then
      if valid = '1' and (state = S_PRE or state = S_WAIT or state = S_POST) then
        memory(to_integer(wp)) <= data;
      end if;
      q <= memory(to_integer(rd_addr));
    end if;
  end process;

  run_i <= run_q when FAST else run;

  -- 設定（pretrigger）から求める値はレジスタに置く（毎回の 32 ビットの加減算と比較は、sys を上げると遅い経路だった。
  -- 2026-10-29）。pretrigger は止めている間に変える設定なので、1 周期遅れても振る舞いは変わらない
  process (clk)
  begin
    if rising_edge(clk) then
      pre_low   <= pretrigger(ADDR_BITS - 1 downto 0);
      pre_one   <= pretrigger(ADDR_BITS - 1 downto 0) = 1;
      post_one  <= (not pretrigger(ADDR_BITS - 1 downto 0)) = 1;
      post_len  <= not pretrigger(ADDR_BITS - 1 downto 0);               -- DEPTH − 1 − pretrigger（DEPTH − 1 は全ビット 1）
      post_none <= pretrigger(ADDR_BITS - 1 downto 0) = (ADDR_BITS - 1 downto 0 => '1');
      pre_zero  <= pretrigger = 0;
      time_d    <= time;                                                 -- ev の 1 周期後にトリガの周期の値を取るため、1 周期遅らせて持つ
      phase_d   <= phase;
    end if;
  end process;

  process (clk)
  begin
    if rising_edge(clk) then
      run_q    <= run;
      run_prev <= run_i;
      ev       <= false;
      if ev then
        time_r  <= time_d;
        phase_r <= phase_d;
        start_r <= trig_r - pre_low;
      end if;
      if rst = '1' then
        state <= S_IDLE;
      elsif run_i = '0' then
        if state /= S_DONE then
          state <= S_IDLE;                                   -- 中止（完了した結果は残す）
        end if;
      elsif run_prev = '0' then
        if (pre_zero and FAST) or (pretrigger = 0 and not FAST) then   -- FAST: pre_zero は pretrigger の 1 周期後、run_i は run の 1 周期後なので、同じ周期に変えても間に合う
          state <= S_WAIT;                                   -- 開始（溜める語が無ければ 0 番から受け付ける）
        else
          state <= S_PRE;
        end if;
        wp     <= (others => '0');
        left   <= pre_low;                                   -- S_PRE の残り（left = 1 が最後の語）
        left1  <= pre_one;
        first  <= true;
      elsif first and valid = '0' then
        left   <= pre_low;                                   -- 設定は最初の語までに落ち着く
        left1  <= pre_one;
      elsif valid = '1' then
        first  <= false;
        case state is
          when S_PRE =>
            wp     <= wp + 1;
            left   <= left - 1;
            left1  <= left = 2;
            if left1 then                                    -- pretrigger 個目の語（pretrigger ≥ 1）
              state <= S_WAIT;
            end if;
          when S_WAIT =>
            if hit = '1' then
              trig_r  <= wp;
              if FAST then
                ev      <= true;
              else
                time_r  <= time;
                phase_r <= phase;
                start_r <= wp - pre_low;
              end if;
              left    <= post_len;
              left1   <= post_one;
              if post_none then
                state <= S_DONE;
              else
                state <= S_POST;
              end if;
            end if;
            wp     <= wp + 1;
          when S_POST =>
            wp   <= wp + 1;
            left <= left - 1;
            left1 <= left = 2;
            if left1 then
              state <= S_DONE;
            end if;
          when others =>
            null;
        end case;
      end if;
    end if;
  end process;

  capturing     <= '1' when state = S_PRE or state = S_WAIT or state = S_POST else '0';
  triggered     <= '1' when state = S_POST or state = S_DONE else '0';
  done          <= '1' when state = S_DONE else '0';
  start_index   <= resize(start_r, 32);
  trigger_index <= resize(trig_r, 32);
  trigger_time  <= time_r;
  trigger_phase <= phase_r;
  rd_data       <= q2 when RD_REG else q;

  process (clk)
  begin
    if rising_edge(clk) then
      q2 <= q;
    end if;
  end process;
end architecture;
