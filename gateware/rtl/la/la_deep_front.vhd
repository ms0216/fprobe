-- 深いバッファ（la_deep_buffer）の sys の前段: サンプル → 128 ビットの拍。
--   LANES = 1: la_front（トリガ・間引き・パルス幅）→ プリトリガの数え → la_pack（使う ch だけ詰める）→ la_encoder（RAW / RLE）。
--     連続記録の起点（start_time）は run の後の最初のサンプルの時刻
--   LANES > 1: la_lane_encoder（1 周期に LANES サンプル。トリガ・プリトリガ・前段は無い）
-- 拍の形・トリガの位置（out_trig・out_slot）・起点の時刻の意味は la_deep_buffer の先頭と同じ。la_deep_buffer の中だけで使う。
library ieee;
use ieee.std_logic_1164.all;
use ieee.numeric_std.all;
use work.timing_pkg.all;

entity la_deep_front is
  generic (
    WIDTH     : positive;
    LANES     : positive;
    IN_AGE    : natural;
    EXT_DELAY : natural
  );
  port (
    clk         : in  std_logic;
    rst         : in  std_logic;
    run         : in  std_logic;
    rle         : in  std_logic;
    pack        : in  std_logic_vector(2 downto 0);
    step        : in  unsigned(1 downto 0);
    first_ch    : in  unsigned(3 downto 0);
    probes      : in  std_logic_vector(WIDTH - 1 downto 0);
    lane_data   : in  std_logic_vector(LANES * WIDTH - 1 downto 0);
    ext_trigger : in  std_logic;
    time        : in  unsigned(63 downto 0);
    rate        : in  unsigned(15 downto 0);
    pretrigger  : in  unsigned(31 downto 0);
    trig_mask   : in  std_logic_vector(WIDTH - 1 downto 0);
    trig_value  : in  std_logic_vector(WIDTH - 1 downto 0);
    trig_rise   : in  std_logic_vector(WIDTH - 1 downto 0);
    trig_fall   : in  std_logic_vector(WIDTH - 1 downto 0);
    pw_enable   : in  std_logic;
    pw_event    : in  std_logic;
    -- 拍（符号器の出口）
    enc_valid     : out std_logic;
    enc_ready     : in  std_logic;
    enc_data      : out std_logic_vector(127 downto 0);
    enc_trig      : out std_logic;
    enc_slot      : out unsigned(2 downto 0);
    enc_triggered : out std_logic;
    enc_overrun   : out std_logic;
    start_time    : out unsigned(63 downto 0);
    start_valid   : out std_logic;
    latency       : out unsigned(7 downto 0);
    gap_pack      : out unsigned(2 downto 0)   -- 欠けの区間の RAW の 1 拍のサンプルの数の詰め（la_stream_gap）
  );
end entity;

architecture rtl of la_deep_front is
  signal run_prev : std_logic := '0';
  signal s_valid, s_hit, s_hit_raw : std_logic;
  signal pre_left : unsigned(31 downto 0) := (others => '0');
  signal armed : std_logic := '0';
  signal s_data : std_logic_vector(WIDTH - 1 downto 0);
  signal pack_eff : std_logic_vector(1 downto 0);
  signal pk_valid, pk_hit : std_logic;
  signal pk_data : std_logic_vector(15 downto 0);
  signal first_wait, first_seen, started_r : std_logic := '0';                -- LANES = 1 の連続記録の起点（R1）
  signal start_r : unsigned(63 downto 0) := (others => '0');
begin
  process (clk)
  begin
    if rising_edge(clk) then
      run_prev <= run;
    end if;
  end process;

  narrow_g : if LANES = 1 generate
    front_i : entity work.la_front
      generic map (WIDTH => WIDTH, EXT_DELAY => EXT_DELAY)
      port map (clk => clk, rst => rst, probes => probes, ext_trigger => ext_trigger, run => run, arm => armed,
                rate => rate, trig_mask => trig_mask, trig_value => trig_value, trig_rise => trig_rise,
                trig_fall => trig_fall, pw_enable => pw_enable, pw_event => pw_event, sample_valid => s_valid, sample => s_data, sample_hit => s_hit_raw);
    s_hit <= s_hit_raw and armed;

    -- 受け付けるまでの残りのサンプル数を数え、0 になったらトリガを受け付ける。armed は「このサンプルの前に残りが 0」
    -- （サンプル n が来る周期に armed = '1' ⟺ n >= pretrigger。レジスタの 1 周期遅れで 1 サンプル余計に溜めない）
    process (clk)
    begin
      if rising_edge(clk) then
        if run = '0' or run_prev = '0' then                  -- 止まっている間と開始の周期（run と同時に書かれた値も取る）
          pre_left <= pretrigger;
          if pretrigger = 0 and run = '1' then
            armed <= '1';
          else
            armed <= '0';
          end if;
        elsif s_valid = '1' and pre_left /= 0 then
          pre_left <= pre_left - 1;
          if pre_left = 1 then
            armed <= '1';
          end if;
        end if;
      end if;
    end process;

    pack_eff <= pack(1 downto 0) when rle = '0' else "00";                -- RLE は 16 ビットの値の繰り返しなので、詰めない
    pack_i : entity work.la_pack
      port map (clk => clk, rst => rst, run => run, pack => pack_eff, sample_valid => s_valid,
                sample => std_logic_vector(resize(unsigned(s_data), 16)), sample_hit => s_hit,
                out_valid => pk_valid, out_sample => pk_data, out_hit => pk_hit);

    encoder_i : entity work.la_encoder
      port map (clk => clk, rst => rst, run => run, rle => rle, sample_valid => pk_valid,
                sample => pk_data, sample_hit => pk_hit,
                out_valid => enc_valid, out_ready => enc_ready, out_data => enc_data, out_trig => enc_trig,
                out_slot => enc_slot, triggered => enc_triggered, overrun => enc_overrun);
    -- 連続記録の起点（R1。docs/superpowers/specs/2026-11-01-time-accuracy-design.md）: run の後の最初のサンプル（la_front の
    -- sample_valid）を、トリガ時刻と同じ段で時刻にする。トリガのサンプルが符号器に入った周期の次の周期の time が trigger_time
    -- なので、最初のサンプルが出た周期の次の周期の time を start_time にする（ピンの時刻 = start_time − LA_DEEP_DELAY − フィルタの段、
    -- trigger_time と同じ式。サンプル i は start_time + i × (rate + 1)）。start_valid は run の 0→1 で 0 に戻り、最初のサンプルで 1
    -- （止めた後も次の run まで残る）
    process (clk)
    begin
      if rising_edge(clk) then
        first_seen <= '0';
        if run_prev = '0' then                               -- 止まっている間と開始の周期（la_front はまだサンプルを出さない）
          first_wait <= '1';
        elsif s_valid = '1' and first_wait = '1' then
          first_wait <= '0';
          first_seen <= '1';
        end if;
        if rst = '1' or (run = '1' and run_prev = '0') then
          start_r  <= (others => '0');
          started_r <= '0';
        elsif first_seen = '1' then
          start_r  <= time;
          started_r <= '1';
        end if;
      end if;
    end process;
    start_time  <= start_r;
    start_valid <= started_r;
    latency     <= (others => '0');
    gap_pack    <= unsigned('0' & pack_eff);                 -- la_pack の詰め（1 拍 = 8 << pack サンプル）
  end generate;

  lanes_g : if LANES > 1 generate
    lane_enc_i : entity work.la_lane_encoder
      generic map (WIDTH => WIDTH, LANES => LANES, IN_AGE => IN_AGE)
      port map (clk => clk, rst => rst, run => run, rle => rle, step => step, pack => unsigned(pack), first_ch => first_ch, rate => rate,
                lane_data => lane_data, time => time, out_valid => enc_valid, out_ready => enc_ready, out_data => enc_data,
                overrun => enc_overrun, start_time => start_time, started => start_valid, latency => latency);
    -- トリガ・プリトリガ・前段は無い（垂れ流しだけ）。使わない信号も駆動する
    s_valid <= '0'; s_hit_raw <= '0'; s_hit <= '0'; s_data <= (others => '0');
    armed <= '0'; pre_left <= (others => '0');
    pack_eff <= "00"; pk_valid <= '0'; pk_hit <= '0'; pk_data <= (others => '0');
    enc_trig <= '0'; enc_slot <= (others => '0'); enc_triggered <= '0';
    first_wait <= '0'; first_seen <= '0'; start_r <= (others => '0'); started_r <= '0';
    gap_pack <= unsigned(pack);                                -- la_lane_raw の詰め（1 拍 = (128 / WIDTH) << pack サンプル）
  end generate;
end architecture;
