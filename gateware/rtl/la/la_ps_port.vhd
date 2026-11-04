-- PS が DDR のリングを直接読む流れの、窓（zynq_frame_link）の PS 側のレジスタ（ボード非依存。clk = sys）。
-- 仕様: docs/superpowers/specs/2026-11-03-ps-stream-position-design.md。番地・ビットは ps_window_pkg（fprobe_gw/ps_window.py が正）。
--
-- la_deep_buffer と PS（中継 fprobe-relay）の「位置の約束」をここで守らせる:
--   - 位置（LA_CONSUMED・EV_CONSUMED・STREAM_BEATS）は流れの番号 cons_epoch の値。la_deep_buffer の流れの番号 epoch が変わった次の
--     周期に、cons_epoch をそれにして位置を 0 に戻す。位置は cons_epoch と一緒に la_deep_buffer に返す（ret_*）。
--   - 位置の書きは、PS が添えた番号（POS_TAG。書いたまま残る）が cons_epoch と同じで、その周期に番号が変わっていないときだけ受ける。
--     違えば捨てて STALE_WRITES に数える（中継が新しい流れに気づく前に書いた、前の流れの位置を使わない。窓は書きを 1 つずつ順に
--     処理するので、POS_TAG → 位置の順の書きは割り込まれない）。
--   - STREAM_EPOCH の読みは、la_deep_buffer が 1 つの受け渡しで揃えて公開した組（流れの番号・状態・書き位置）を写し、以後の
--     LA_WRITTEN・LA_WPTR・EV_WPOS・EV_START の読みは写しを返す（別々の時点の値が混ざらない。C10）。値の CURRENT は、
--     公開された番号が cons_epoch と同じ（= 位置のレジスタがその流れのもの）。
--   - RD_EPOCH の読みは、PS への読み出しの要求（RD_REGION・RD_FIRST・RD_COUNT）を写す。
-- 窓のアクセス（bus_rd・bus_wr）は 1 周期。bus_rdata は bus_addr の読みの値（組み合わせ。窓がその周期に取る）。
library ieee;
use ieee.std_logic_1164.all;
use ieee.numeric_std.all;
use work.util_pkg.all;
use work.ps_window_pkg.all;

entity la_ps_port is
  port (
    clk            : in  std_logic;
    rst            : in  std_logic;
    -- 窓（zynq_frame_link）からのアクセス。bus_addr は語の番号（WIN_*）
    bus_rd         : in  std_logic;
    bus_wr         : in  std_logic;
    bus_addr       : in  unsigned(7 downto 0);
    bus_wdata      : in  std_logic_vector(31 downto 0);
    bus_rdata      : out std_logic_vector(31 downto 0);
    -- la_deep_buffer
    epoch          : in  unsigned(15 downto 0);             -- 流れの番号（ps_epoch）
    pub_epoch      : in  unsigned(15 downto 0);             -- 公開された組（ps_pub_epoch 〜 ps_ev_start）
    pub_active     : in  std_logic;
    pub_writing    : in  std_logic;
    pub_overrun    : in  std_logic;
    pub_la_written : in  unsigned(31 downto 0);
    pub_la_wptr    : in  unsigned(31 downto 0);
    pub_ev_wpos    : in  unsigned(31 downto 0);
    pub_ev_start   : in  unsigned(31 downto 0);
    rd_epoch       : in  unsigned(7 downto 0);              -- PS への読み出しの要求（4 つは同じ周期に変わる）
    rd_region      : in  std_logic;
    rd_first       : in  unsigned(31 downto 0);
    rd_count       : in  unsigned(31 downto 0);
    ret_epoch      : out unsigned(15 downto 0);             -- 下の 3 つの値の流れの番号
    la_consumed    : out unsigned(31 downto 0);
    ev_consumed    : out unsigned(31 downto 0);
    out_beats      : out unsigned(31 downto 0);
    rd_ack         : out unsigned(7 downto 0)
  );
end entity;

architecture rtl of la_ps_port is
  signal cons_epoch : unsigned(15 downto 0) := (others => '0');
  signal tag : unsigned(15 downto 0) := (others => '0');
  signal la_r, ev_r, beats_r : unsigned(31 downto 0) := (others => '0');
  signal stale : unsigned(31 downto 0) := (others => '0');
  signal rd_ack_r : unsigned(7 downto 0) := (others => '0');
  -- 写し
  signal state_word, s_state : std_logic_vector(31 downto 0) := (others => '0');
  signal s_la_written, s_la_wptr, s_ev_wpos, s_ev_start : unsigned(31 downto 0) := (others => '0');
  signal s_rd_region : std_logic := '0';
  signal s_rd_first, s_rd_count : unsigned(31 downto 0) := (others => '0');
  signal pos_write : boolean;
begin
  -- STREAM_EPOCH の読みの値（公開された組から。CURRENT は位置のレジスタが同じ流れのもの）
  process (all)
  begin
    state_word <= (others => '0');
    state_word(WIN_EPOCH_BITS - 1 downto 0) <= std_logic_vector(pub_epoch);
    state_word(WIN_STATE_ACTIVE)  <= pub_active;
    state_word(WIN_STATE_WRITING) <= pub_writing;
    state_word(WIN_STATE_OVERRUN) <= pub_overrun;
    if pub_epoch = cons_epoch and epoch = cons_epoch then
      state_word(WIN_STATE_CURRENT) <= '1';
    end if;
  end process;

  pos_write <= bus_wr = '1' and (to_integer(bus_addr) = WIN_LA_CONSUMED or to_integer(bus_addr) = WIN_EV_CONSUMED
                                 or to_integer(bus_addr) = WIN_STREAM_BEATS);

  process (clk)
  begin
    if rising_edge(clk) then
      if rst = '1' then
        cons_epoch <= (others => '0');
        tag        <= (others => '0');
        la_r       <= (others => '0');
        ev_r       <= (others => '0');
        beats_r    <= (others => '0');
        stale      <= (others => '0');
        rd_ack_r   <= (others => '0');
        s_state    <= (others => '0');
        s_la_written <= (others => '0');
        s_la_wptr    <= (others => '0');
        s_ev_wpos    <= (others => '0');
        s_ev_start   <= (others => '0');
        s_rd_region  <= '0';
        s_rd_first   <= (others => '0');
        s_rd_count   <= (others => '0');
      else
        if epoch /= cons_epoch then
          -- 新しい流れ: 位置を 0 に戻す（この周期の位置の書きは捨てる。番号は前の流れのもの）
          cons_epoch <= epoch;
          la_r       <= (others => '0');
          ev_r       <= (others => '0');
          beats_r    <= (others => '0');
          if pos_write then
            stale <= sat_inc(stale);
          end if;
        elsif pos_write then
          if tag = cons_epoch then
            if to_integer(bus_addr) = WIN_LA_CONSUMED then
              la_r <= unsigned(bus_wdata);
            elsif to_integer(bus_addr) = WIN_EV_CONSUMED then
              ev_r <= unsigned(bus_wdata);
            else
              beats_r <= unsigned(bus_wdata);
            end if;
          else
            stale <= sat_inc(stale);                         -- 前の流れの番号の書き（捨てる）
          end if;
        end if;
        if bus_wr = '1' and to_integer(bus_addr) = WIN_POS_TAG then
          tag <= unsigned(bus_wdata(WIN_EPOCH_BITS - 1 downto 0));
        end if;
        if bus_wr = '1' and to_integer(bus_addr) = WIN_RD_ACK then
          rd_ack_r <= unsigned(bus_wdata(7 downto 0));
        end if;
        if bus_rd = '1' and to_integer(bus_addr) = WIN_STREAM_EPOCH then
          s_state      <= state_word;
          s_la_written <= pub_la_written;
          s_la_wptr    <= pub_la_wptr;
          s_ev_wpos    <= pub_ev_wpos;
          s_ev_start   <= pub_ev_start;
        end if;
        if bus_rd = '1' and to_integer(bus_addr) = WIN_RD_EPOCH then
          s_rd_region <= rd_region;
          s_rd_first  <= rd_first;
          s_rd_count  <= rd_count;
        end if;
      end if;
    end if;
  end process;

  -- 読みの値（写しのレジスタは最後に写した値。STREAM_EPOCH・RD_EPOCH はその周期の値 = 写す値）
  process (all)
  begin
    bus_rdata <= (others => '0');
    case to_integer(bus_addr) is
      when WIN_STREAM_EPOCH => bus_rdata <= state_word;
      when WIN_LA_WRITTEN   => bus_rdata <= std_logic_vector(s_la_written);
      when WIN_LA_WPTR      => bus_rdata <= std_logic_vector(s_la_wptr);
      when WIN_EV_WPOS      => bus_rdata <= std_logic_vector(s_ev_wpos);
      when WIN_EV_START     => bus_rdata <= std_logic_vector(s_ev_start);
      when WIN_LA_CONSUMED  => bus_rdata <= std_logic_vector(la_r);
      when WIN_EV_CONSUMED  => bus_rdata <= std_logic_vector(ev_r);
      when WIN_STREAM_BEATS => bus_rdata <= std_logic_vector(beats_r);
      when WIN_POS_TAG      => bus_rdata(WIN_EPOCH_BITS - 1 downto 0) <= std_logic_vector(tag);
      when WIN_STALE_WRITES => bus_rdata <= std_logic_vector(stale);
      when WIN_RD_EPOCH     => bus_rdata(7 downto 0) <= std_logic_vector(rd_epoch);
      when WIN_RD_REGION    => bus_rdata(0) <= s_rd_region;
      when WIN_RD_FIRST     => bus_rdata <= std_logic_vector(s_rd_first);
      when WIN_RD_COUNT     => bus_rdata <= std_logic_vector(s_rd_count);
      when WIN_RD_ACK       => bus_rdata(7 downto 0) <= std_logic_vector(rd_ack_r);
      when others           => null;
    end case;
  end process;

  ret_epoch   <= cons_epoch;
  la_consumed <= la_r;
  ev_consumed <= ev_r;
  out_beats   <= beats_r;
  rd_ack      <= rd_ack_r;
end architecture;
