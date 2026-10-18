-- ロジックアナライザの捕捉（ボード非依存、clk のドメイン）。
-- 仕様: docs/superpowers/specs/2026-10-16-m7-logic-analyzer-design.md §2
--
-- 前段（同期化・サンプル間隔・トリガ判定）は la_front。サンプルは rate + 1 周期ごと。
-- run の 0→1 で開始: プリトリガ分（pretrigger サンプル）を溜め、トリガを待ち（その間はリングで上書き）、
-- トリガ後に残り（DEPTH - pretrigger サンプル。トリガのサンプルを含む）を溜めて完了。run の 1→0 で中止。
-- トリガ = 値一致 ((s and mask) = (value and mask)) かつ エッジ条件（rise / fall で指定したチャンネルの
-- どれかが変化。指定なしなら真）、または ext_trigger（1 周期のパルスでよい。トリガ待ちの間に来たものを
-- 次のサンプルで使う）。判定は 1 段のパイプライン: サンプル n の判定は次の周期に、1 周期遅らせたサンプル列と揃えて使う。
-- 完了後、バッファは start_index から DEPTH サンプルが時間順（rd_addr で同期読み出し、1 周期遅れ）。
library ieee;
use ieee.std_logic_1164.all;
use ieee.numeric_std.all;
use work.util_pkg.all;

entity la_capture is
  generic (
    WIDTH : positive := 16;
    DEPTH : positive := 8192                    -- 2 の冪
  );
  port (
    clk           : in  std_logic;
    rst           : in  std_logic;
    probes        : in  std_logic_vector(WIDTH - 1 downto 0);
    time          : in  unsigned(63 downto 0);
    ext_trigger   : in  std_logic;
    -- 設定（捕捉中は変えないこと）
    run           : in  std_logic;
    rate          : in  unsigned(15 downto 0);
    pretrigger    : in  unsigned(31 downto 0);   -- DEPTH - 1 まで
    trig_mask     : in  std_logic_vector(WIDTH - 1 downto 0);
    trig_value    : in  std_logic_vector(WIDTH - 1 downto 0);
    trig_rise     : in  std_logic_vector(WIDTH - 1 downto 0);
    trig_fall     : in  std_logic_vector(WIDTH - 1 downto 0);
    pw_enable     : in  std_logic := '0';      -- パルス幅のトリガ（la_front）
    pw_event      : in  std_logic := '0';
    -- 状態
    capturing     : out std_logic;
    triggered     : out std_logic;
    done          : out std_logic;
    start_index   : out unsigned(31 downto 0);
    trigger_index : out unsigned(31 downto 0);
    trigger_time  : out unsigned(63 downto 0);
    -- 読み出し
    rd_addr       : in  unsigned(clog2(DEPTH) - 1 downto 0);
    rd_data       : out std_logic_vector(WIDTH - 1 downto 0)
  );
end entity;

architecture rtl of la_capture is
  constant ADDR_BITS : natural := clog2(DEPTH);
  type state_t is (S_IDLE, S_PRE, S_WAIT, S_POST, S_DONE);
  type memory_t is array (0 to DEPTH - 1) of std_logic_vector(WIDTH - 1 downto 0);

  signal memory    : memory_t;
  signal state     : state_t := S_IDLE;
  signal run_prev  : std_logic := '0';
  signal arm       : std_logic;
  signal valid     : std_logic;
  signal data      : std_logic_vector(WIDTH - 1 downto 0);
  signal hit       : std_logic;
  signal wp        : unsigned(ADDR_BITS - 1 downto 0) := (others => '0');
  signal filled    : unsigned(31 downto 0) := (others => '0');
  signal left      : unsigned(31 downto 0) := (others => '0');
  signal start_r   : unsigned(ADDR_BITS - 1 downto 0) := (others => '0');
  signal trig_r    : unsigned(ADDR_BITS - 1 downto 0) := (others => '0');
  signal time_r    : unsigned(63 downto 0) := (others => '0');
  signal q         : std_logic_vector(WIDTH - 1 downto 0) := (others => '0');
begin
  assert 2 ** ADDR_BITS = DEPTH report "la_capture: DEPTH must be a power of two" severity failure;

  arm <= '1' when state = S_WAIT else '0';             -- プリトリガを溜める間の外部トリガは使わない
  front_i : entity work.la_front
    generic map (WIDTH => WIDTH)
    port map (clk => clk, rst => rst, probes => probes, ext_trigger => ext_trigger, run => run, arm => arm,
              rate => rate, trig_mask => trig_mask, trig_value => trig_value, trig_rise => trig_rise,
              trig_fall => trig_fall, pw_enable => pw_enable, pw_event => pw_event, sample_valid => valid, sample => data, sample_hit => hit);

  process (clk)
  begin
    if rising_edge(clk) then
      if valid = '1' and (state = S_PRE or state = S_WAIT or state = S_POST) then
        memory(to_integer(wp)) <= data;
      end if;
      q <= memory(to_integer(rd_addr));
    end if;
  end process;

  process (clk)
  begin
    if rising_edge(clk) then
      run_prev <= run;
      if rst = '1' then
        state <= S_IDLE;
      elsif run = '0' then
        if state /= S_DONE then
          state <= S_IDLE;                                   -- 中止（完了した結果は残す）
        end if;
      elsif run_prev = '0' then
        state  <= S_PRE;                                     -- 開始
        wp     <= (others => '0');
        filled <= (others => '0');
      elsif valid = '1' then
        case state is
          when S_PRE =>
            wp     <= wp + 1;
            filled <= filled + 1;
            if filled + 1 >= pretrigger then
              state <= S_WAIT;
            end if;
          when S_WAIT =>
            if hit = '1' then
              trig_r  <= wp;
              time_r  <= time;
              start_r <= wp - pretrigger(ADDR_BITS - 1 downto 0);
              left    <= to_unsigned(DEPTH, 32) - pretrigger - 1;
              if to_unsigned(DEPTH, 32) - pretrigger - 1 = 0 then
                state <= S_DONE;
              else
                state <= S_POST;
              end if;
            end if;
            wp     <= wp + 1;
            filled <= filled + 1;
          when S_POST =>
            wp   <= wp + 1;
            left <= left - 1;
            if left = 1 then
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
  rd_data       <= q;
end architecture;
