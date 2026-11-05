-- ロジアナの深いバッファの開始・中止の受け渡し（sys → mem）。仕様: docs/superpowers/specs/2026-11-03-ps-stream-position-design.md
--
-- sys: run の 0→1 ごとに流れの番号 epoch を 1 進め、同じ辺で開始の設定（stream・ps_read・gaps・post_beats）を写す。
--   run_q は run を 1 周期遅らせたもの（epoch・設定と同じ辺で変わるので、組が揃う）。ps_read は stream と同時のときだけ 1。
--   gaps は呼ぶ側が stream と組み合わせて渡す（la_deep_buffer の gap_en）。
-- mem: 番号・run・設定の組を流し続けて受け、届いた番号が自分の番号と違えば新しい流れを始める（start を 1 周期）。
--   同じ番号で run = '0' になったら止める（stop を 1 周期）。mem が見る前に止められていた流れ（届いた run = '0'）は、
--   始めて次の周期に止める（拍の無い流れ。番号と状態が揃う）。状態を渡すので、間の値を飛ばしても始めと止めを取り違えない
--   （前は run の辺の 1 周期の読み込みで、近い 2 つの辺で開始が消えた。C5）。
-- リセット（C8）: rst と mem_rst は同時にかける。リセットの後、番号は 0 で、mem は最初に届いた番号 0 を受けるだけで始めない。
library ieee;
use ieee.std_logic_1164.all;
use ieee.numeric_std.all;

entity la_run_cdc is
  generic (
    POST_BITS  : positive;
    SAME_CLOCK : boolean := false
  );
  port (
    -- sys
    clk        : in  std_logic;
    rst        : in  std_logic;
    run        : in  std_logic;
    stream     : in  std_logic;
    ps_read    : in  std_logic;
    gaps       : in  std_logic;
    post_beats : in  unsigned(POST_BITS - 1 downto 0);
    epoch      : out unsigned(15 downto 0);
    run_q      : out std_logic;
    ps_last    : out std_logic;                               -- 最後に始めた流れは PS が読む流れ
    -- mem
    mem_clk    : in  std_logic;
    mem_rst    : in  std_logic;
    m_start    : out std_logic;
    m_stop     : out std_logic;
    m_epoch    : out unsigned(15 downto 0);                   -- いま受け持つ流れの番号（m_start の周期に変わる）
    m_stream   : out std_logic;                               -- 以下、始めた流れの設定
    m_ps_read  : out std_logic;
    m_gaps     : out std_logic;
    m_post     : out unsigned(POST_BITS - 1 downto 0)
  );
end entity;

architecture rtl of la_run_cdc is
  constant W : natural := 16 + 4 + POST_BITS;
  -- 受け渡しの語 x_src の並び（上位から）: 流れの番号 16 | run | gaps | ps_read | stream | post_beats（POST_BITS）
  constant X_RUN    : natural := POST_BITS + 3;
  constant X_GAPS   : natural := POST_BITS + 2;
  constant X_PSREAD : natural := POST_BITS + 1;
  constant X_STREAM : natural := POST_BITS;
  signal run_prev, run_r : std_logic := '0';
  signal epoch_r : unsigned(15 downto 0) := (others => '0');
  signal c_stream, c_ps_read, c_gaps : std_logic := '0';
  signal c_post : unsigned(POST_BITS - 1 downto 0) := (others => '0');
  signal x_src, x_dst : std_logic_vector(W - 1 downto 0);
  signal x_new : std_logic;
  -- mem
  signal r_epoch : unsigned(15 downto 0) := (others => '0');
  signal r_run, r_start, r_stop, stop_next : std_logic := '0';
  signal r_cfg : std_logic_vector(W - 1 downto 0) := (others => '0');
begin
  process (clk)
  begin
    if rising_edge(clk) then
      run_prev <= run;
      run_r    <= run;
      if rst = '1' then
        epoch_r   <= (others => '0');
        run_r     <= '0';
        c_stream  <= '0';
        c_ps_read <= '0';
        c_gaps    <= '0';
        c_post    <= (others => '0');
      elsif run = '1' and run_prev = '0' then
        epoch_r   <= epoch_r + 1;
        c_stream  <= stream;
        c_ps_read <= ps_read and stream;
        c_gaps    <= gaps;
        c_post    <= post_beats;
      end if;
    end if;
  end process;
  epoch   <= epoch_r;
  run_q   <= run_r;
  ps_last <= c_ps_read;

  -- 組は流し続ける（mem は番号の変化で始め、run = '0' で止める。間の値を飛ばしてよい）
  x_src <= std_logic_vector(epoch_r) & run_r & c_gaps & c_ps_read & c_stream & std_logic_vector(c_post);
  x_i : entity work.cdc_value
    generic map (WIDTH => W, SAME_CLOCK => SAME_CLOCK)
    port map (src_clk => clk, src_rst => rst, src_load => '1', src_value => x_src, src_ready => open,
              dst_clk => mem_clk, dst_rst => mem_rst, dst_value => x_dst, dst_changed => x_new);

  process (mem_clk)
    variable rx_epoch : unsigned(15 downto 0);
    variable rx_run : std_logic;
  begin
    if rising_edge(mem_clk) then
      rx_epoch := unsigned(x_dst(W - 1 downto W - 16));
      rx_run   := x_dst(X_RUN);
      r_start   <= '0';
      r_stop    <= stop_next;
      stop_next <= '0';
      if mem_rst = '1' then
        r_run   <= '0';
        r_epoch <= (others => '0');
        r_cfg   <= (others => '0');
        r_stop  <= '0';
      elsif x_new = '1' then
        if rx_epoch /= r_epoch then
          r_epoch   <= rx_epoch;
          r_cfg     <= x_dst;
          r_start   <= '1';
          stop_next <= not rx_run;
          r_run     <= rx_run;
        elsif rx_run = '0' and r_run = '1' then
          r_stop <= '1';
          r_run  <= '0';
        end if;
      end if;
    end if;
  end process;
  m_start   <= r_start;
  m_stop    <= r_stop;
  m_epoch   <= r_epoch;
  m_gaps    <= r_cfg(X_GAPS);
  m_ps_read <= r_cfg(X_PSREAD);
  m_stream  <= r_cfg(X_STREAM);
  m_post    <= unsigned(r_cfg(POST_BITS - 1 downto 0));
end architecture;
