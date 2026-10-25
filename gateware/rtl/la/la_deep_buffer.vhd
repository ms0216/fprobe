-- ロジアナの深いバッファ: サンプル列を符号化して外部メモリにリングとして書き、指定範囲を読み出してバイト列で出す。
-- 仕様: docs/superpowers/specs/2026-10-16-m8-ddr3-design.md §5
--
--   sys: la_front → la_encoder → 非同期 FIFO ─→ mem: la_mem_writer ─┐
--   sys: バイト列 ← stream_unpack ← 非同期 FIFO ← mem: mem_reader ←─┴ mem_port（書き込みと読み出しは排他）
--
-- run の 0→1 で捕捉を始め（前段の設定 rate / trig_* は捕捉中は変えないこと）、トリガ後 post_beats 拍で止まる。
-- 開始から pretrigger サンプルまではトリガ（条件・外部とも）を受け付けない（トリガ前の記録を必ず残す。内蔵版と同じ）。
-- run の 1→0 で中止。結果（トリガの拍・番号・書いた拍の総数）は止まってから有効。read（1 周期）で first から count 拍を
-- 読み、out にバイト列（拍 = 16 バイト、下位から）を出す。読み出しは捕捉していないときに行う。
-- trigger_time はトリガを符号器が見た時点のタイムベース（サンプルの数周期後）。
--
-- DAP のイベント（ev_valid + ev_data、128 ビット）も同じ経路で外部メモリのイベント用リングに書く
-- （ev_enable の間。仕様: docs/superpowers/specs/2026-10-16-m9b-dap-events-design.md）。ロジアナの拍を優先し、
-- イベントは 1 件の保持レジスタで待たせる。空かずに失ったイベントは ev_lost に数える。
-- 読み出しは read_region で領域を選ぶ（'0' = ロジアナのリング LA_BEATS 拍、'1' = イベントのリング EV_BEATS 拍）。
--
-- 連続ストリーミング（stream = '1' で run の 0→1。仕様: 2026-10-16-m8c-la-streaming-design.md）: 書き込みはトリガで
-- 止まらず、書いた総数と読んだ総数の差（未読）があれば mem_reader に最大 STREAM_CHUNK 拍ずつ読ませて out に流す
-- （外部メモリを FIFO として使う）。書き込みと読み出しが同時に mem_port を求めたら交互に通す。未読がリング −
-- STREAM_CHUNK を超えたら書き込みを止めて overrun を立てる。run の 1→0 で書き込みを止め、未読を出し切って終わる
-- （その間 reading = '1'）。stream_beats はストリームで書いた拍の総数。
--
-- イベントも流す（STREAM_EVENTS = true、R1。仕様: 2026-10-19-r1-unified-stream-design.md）: ストリームを「見出し 1 拍 +
-- 中身」のかたまりの列にし、ロジアナの拍とイベントのリングの未読を交互に載せる（読み出し器が空くたびに、両方に未読が
-- あれば前回と反対の種類）。見出し = [7:0] 種類（1 = ロジアナ、2 = イベント）、[31:16] 拍数、[63:32] 通し番号
-- （ロジアナ: ストリームの先頭からの拍の番号、イベント: 最初のイベントのリングの位置）、[127:64] 印 "FPRBSEG"。
-- 最初のイベントの位置は run の 0→1 の時点の post_beats（ストリームでは使わない）。イベントの未読（書き位置 − 読み
-- 位置）がリングの半分に達したら、ロジアナの書き込みを止めて overrun を立て、以後イベントは流さない。止めたら、
-- 止めた時点の書き位置までのイベントとロジアナの未読を出し切って終わる。stream_beats は出すと決めた拍の総数
-- （見出し・イベントを含む）。
library ieee;
use ieee.std_logic_1164.all;
use ieee.numeric_std.all;

entity la_deep_buffer is
  generic (
    WIDTH         : positive := 16;
    ADDR_BITS     : positive := 23;
    STREAM_EVENTS : boolean := false                       -- ストリームにイベントも見出し付きで流す
  );
  port (
    -- sys ドメイン
    clk          : in  std_logic;
    rst          : in  std_logic;
    probes       : in  std_logic_vector(WIDTH - 1 downto 0);
    ext_trigger  : in  std_logic;
    time         : in  unsigned(63 downto 0);
    rate         : in  unsigned(15 downto 0);
    trig_mask    : in  std_logic_vector(WIDTH - 1 downto 0);
    trig_value   : in  std_logic_vector(WIDTH - 1 downto 0);
    trig_rise    : in  std_logic_vector(WIDTH - 1 downto 0);
    trig_fall    : in  std_logic_vector(WIDTH - 1 downto 0);
    pw_enable    : in  std_logic := '0';      -- パルス幅のトリガ（la_front）
    pw_event     : in  std_logic := '0';
    run          : in  std_logic;
    rle          : in  std_logic;
    stream       : in  std_logic;                          -- run の 0→1 の時点で取り込む
    post_beats   : in  unsigned(ADDR_BITS - 1 downto 0);     -- STREAM_EVENTS のストリームでは最初のイベントの位置
    pretrigger   : in  unsigned(31 downto 0);                -- トリガを受け付けるまでのサンプル数
    capturing    : out std_logic;
    triggered    : out std_logic;                          -- 符号器がトリガを見た（クロストリガの源）
    done         : out std_logic;
    overrun      : out std_logic;
    trigger_beat : out unsigned(ADDR_BITS - 1 downto 0);
    trigger_slot : out unsigned(2 downto 0);
    written      : out unsigned(ADDR_BITS downto 0);
    trigger_time : out unsigned(63 downto 0);
    stream_beats : out unsigned(31 downto 0);
    -- DAP のイベント
    ev_enable    : in  std_logic;
    ev_valid     : in  std_logic;
    ev_data      : in  std_logic_vector(127 downto 0);
    ev_written   : out unsigned(31 downto 0);                -- 書いたイベントの総数
    ev_lost      : out unsigned(31 downto 0);                -- 記録できなかったイベントの数
    la_beats     : out unsigned(ADDR_BITS downto 0);         -- ロジアナのリングの拍数
    ev_beats     : out unsigned(ADDR_BITS downto 0);         -- イベントのリングの拍数
    read         : in  std_logic;
    read_region  : in  std_logic;
    read_first   : in  unsigned(ADDR_BITS - 1 downto 0);
    read_count   : in  unsigned(ADDR_BITS downto 0);
    reading      : out std_logic;
    out_valid    : out std_logic;
    out_ready    : in  std_logic;
    out_data     : out std_logic_vector(7 downto 0);
    -- mem ドメイン（mem_port）
    mem_clk      : in  std_logic;
    mem_rst      : in  std_logic;
    mem_active   : out std_logic;                          -- mem_port を使っている（書き込み中・読み出し中）
    cmd_valid    : out std_logic;
    cmd_ready    : in  std_logic;
    cmd_write    : out std_logic;
    cmd_addr     : out unsigned(ADDR_BITS - 1 downto 0);
    cmd_data     : out std_logic_vector(127 downto 0);
    rd_valid     : in  std_logic;
    rd_data      : in  std_logic_vector(127 downto 0)
  );
end entity;

architecture rtl of la_deep_buffer is
  constant STREAM_CHUNK : natural := 256;
  constant EV_BITS : natural := ADDR_BITS - 4;
  -- STREAM_EVENTS: かたまりは未読がこれだけ溜まるか、溜まり始めて AGE_BITS の周期が経つか、書き込みが止まってから
  -- 出す（1 拍ずつのかたまりでは見出しで流れが 2 倍になる）。イベントはリングの半分（溢れ）より十分小さく
  constant LA_MIN   : natural := 64;
  constant EV_MIN   : natural := minimum(64, 2 ** (EV_BITS - 2));
  constant AGE_BITS : natural := 14;                                          -- 2^13 周期（約 82 us）
  -- 見出しの印と種類（fprobe_gw.csr.fprobe_map の STREAM_SEG_* と一致させる）
  constant SEG_MAGIC  : std_logic_vector(63 downto 0) := x"0047455342525046";   -- "FPRBSEG\0"（リトルエンディアン）
  constant SEG_LA     : std_logic_vector(7 downto 0) := x"01";
  constant SEG_EVENTS : std_logic_vector(7 downto 0) := x"02";
  constant EV_SIZE : unsigned(ADDR_BITS downto 0) := shift_left(to_unsigned(1, ADDR_BITS + 1), ADDR_BITS - 4);
  constant LA_SIZE : unsigned(ADDR_BITS downto 0) := shift_left(to_unsigned(1, ADDR_BITS + 1), ADDR_BITS) - EV_SIZE;
  -- 1 つのかたまりで読む拍数（未読 n。最大 STREAM_CHUNK 拍）
  function chunk_of(n : unsigned(31 downto 0)) return unsigned is
  begin
    if n > STREAM_CHUNK then
      return to_unsigned(STREAM_CHUNK, ADDR_BITS + 1);
    end if;
    return resize(n, ADDR_BITS + 1);
  end function;
  -- sys
  signal s_valid, s_hit, s_hit_raw, enc_trig, enc_triggered, enc_valid, enc_ready : std_logic;
  signal pre_left : unsigned(31 downto 0) := (others => '0');
  signal armed : std_logic := '0';
  signal s_data : std_logic_vector(WIDTH - 1 downto 0);
  signal enc_data : std_logic_vector(127 downto 0);
  signal enc_slot : unsigned(2 downto 0);
  signal run_prev, trig_prev : std_logic := '0';
  signal time_r : unsigned(63 downto 0) := (others => '0');
  signal ctl_src, ctl_dst : std_logic_vector(ADDR_BITS + 1 downto 0);   -- run, stream, post_beats
  signal enc_overrun, ovf_s : std_logic;
  signal w_total_dst : std_logic_vector(31 downto 0);
  signal ctl_load : std_logic := '0';
  signal rd_src, rd_dst : std_logic_vector(2 * ADDR_BITS + 1 downto 0);
  -- イベントの合流
  signal ev_in_valid, evq_in_ready, evq_ready, m_in_valid, m_in_ready : std_logic;
  signal evq_valid : std_logic := '0';
  signal evq_data : std_logic_vector(127 downto 0) := (others => '0');
  signal m_in : std_logic_vector(132 downto 0);
  signal lost : unsigned(31 downto 0) := (others => '0');
  signal ev_en_m : std_logic;
  signal m_ev_written : unsigned(31 downto 0);
  signal ev_tick : unsigned(3 downto 0) := (others => '0');
  signal ev_cnt_load : std_logic := '0';
  signal ev_cnt_dst : std_logic_vector(31 downto 0);
  signal beat_valid, beat_ready : std_logic;
  signal beat_data : std_logic_vector(127 downto 0);
  signal status_dst : std_logic_vector(2 * ADDR_BITS + 6 downto 0);
  signal status_new : std_logic;
  signal reading_s, pending_read, pending_run, ev_en_prev : std_logic := '0';
  signal drain : natural range 0 to 31 := 0;            -- 下流（非同期 FIFO・分解）が空になるまでの余裕
  -- mem
  signal m_ctl_new, m_rd_new : std_logic;
  signal region_base : unsigned(ADDR_BITS - 1 downto 0);
  signal region_size : unsigned(ADDR_BITS downto 0);
  signal m_start, m_stop : std_logic := '0';
  signal f_valid, f_ready, f_trig : std_logic;
  signal f_data : std_logic_vector(132 downto 0);
  -- FIFO（ブロック RAM の読み出し）と書き込みの間の受け渡しの段（mem の頭打ちの経路: ブロック RAM の出力から書き込みの
  -- 選択を通って DDR3 の受け渡しまでを、レジスタで切る）
  signal q_valid, q_ready : std_logic;
  signal q_data : std_logic_vector(132 downto 0);
  signal w_running, w_triggered, w_done : std_logic;
  signal w_beat : unsigned(ADDR_BITS - 1 downto 0);
  signal w_slot : unsigned(2 downto 0);
  signal w_written : unsigned(ADDR_BITS downto 0);
  signal w_valid, w_ready, w_write, r_valid, r_ready, r_write, r_busy, r_rd_valid : std_logic;
  signal w_addr, r_addr : unsigned(ADDR_BITS - 1 downto 0);
  signal w_data, r_data : std_logic_vector(127 downto 0);
  signal ro_valid, ro_ready : std_logic;
  signal ro_data : std_logic_vector(127 downto 0);
  signal m_state, m_state_prev : std_logic_vector(2 downto 0) := "000";
  -- ストリーミング（mem）
  signal w_total, r_total : unsigned(31 downto 0) := (others => '0');
  signal s_on, s_start, m_ovf, m_full_stop, m_busy : std_logic := '0';
  type stream_state_t is (T_IDLE, T_SUM, T_HDR, T_GO, T_SETTLE, T_IDLE2);
  signal s_state : stream_state_t := T_IDLE;
  signal unread_r : unsigned(31 downto 0) := (others => '0');
  signal next_sum : unsigned(ADDR_BITS downto 0) := (others => '0');
  signal r_ptr : unsigned(ADDR_BITS - 1 downto 0) := (others => '0');
  signal rdr_start : std_logic;
  signal rdr_first : unsigned(ADDR_BITS - 1 downto 0) := (others => '0');
  signal rdr_count : unsigned(ADDR_BITS downto 0) := (others => '0');
  signal rdr_region : std_logic := '0';
  signal sel_r : std_logic := '0';
  signal rdr_first_mux : unsigned(ADDR_BITS - 1 downto 0);
  signal rdr_count_mux : unsigned(ADDR_BITS downto 0);
  signal m_status_load : std_logic := '0';
  signal status_src : std_logic_vector(2 * ADDR_BITS + 6 downto 0);
  -- イベントも流す（STREAM_EVENTS。false なら定数のまま）
  signal ev_wpos : unsigned(EV_BITS - 1 downto 0);                             -- イベントの次の書き位置
  signal ev_rpos, ev_lim : unsigned(EV_BITS - 1 downto 0) := (others => '0');  -- 読み位置・流す終わり
  signal ev_unread_r, ev_back_r : unsigned(EV_BITS - 1 downto 0) := (others => '0');
  signal ev_on, ev_en_mprev : std_logic := '0';
  signal seg_kind, last_kind : std_logic := '0';                              -- '0' = ロジアナ、'1' = イベント
  signal hdr_valid : std_logic := '0';
  signal hdr_lo : std_logic_vector(63 downto 0) := (others => '0');
  signal o_total : unsigned(31 downto 0) := (others => '0');                  -- 出すと決めた拍の総数
  signal age : unsigned(AGE_BITS - 1 downto 0) := (others => '0');            -- 前のかたまりからの周期
  signal sb_src : unsigned(31 downto 0);
  signal ts_valid, ts_ready : std_logic;
  signal ts_data : std_logic_vector(127 downto 0);
begin
  ---------------------------------------------------------------------------------------------
  -- sys: 前段・符号器
  ---------------------------------------------------------------------------------------------
  front_i : entity work.la_front
    generic map (WIDTH => WIDTH)
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

  encoder_i : entity work.la_encoder
    port map (clk => clk, rst => rst, run => run, rle => rle, sample_valid => s_valid,
              sample => std_logic_vector(resize(unsigned(s_data), 16)), sample_hit => s_hit,
              out_valid => enc_valid, out_ready => enc_ready, out_data => enc_data, out_trig => enc_trig,
              out_slot => enc_slot, triggered => enc_triggered, overrun => enc_overrun);
  overrun <= enc_overrun or ovf_s;

  -- 開始・中止とトリガの時刻
  process (clk)
  begin
    if rising_edge(clk) then
      run_prev  <= run;
      trig_prev <= enc_triggered;
      ctl_load  <= run xor run_prev;
      if run = '1' and enc_triggered = '1' and trig_prev = '0' then
        time_r <= time;
      end if;
    end if;
  end process;
  ctl_src <= run & stream & std_logic_vector(post_beats);
  ctl_i : entity work.cdc_value
    generic map (WIDTH => ADDR_BITS + 2)
    port map (src_clk => clk, src_load => ctl_load, src_value => ctl_src,
              dst_clk => mem_clk, dst_value => ctl_dst, dst_changed => m_ctl_new);

  -- イベント: 1 件の保持レジスタで待たせ、ロジアナの拍が無い周期に合流する。出すのと同じ周期に次を受ける
  -- （イベントは連続する周期に来ることがある: DAP の転送の直後のバイト等）。空く前に来たものは lost に数える
  ev_in_valid  <= ev_valid and ev_enable;
  evq_in_ready <= not evq_valid or evq_ready;
  process (clk)
  begin
    if rising_edge(clk) then
      if rst = '1' then
        evq_valid <= '0';
      elsif evq_in_ready = '1' then
        evq_valid <= ev_in_valid;
        evq_data  <= ev_data;
      end if;
      if rst = '1' or (ev_enable = '1' and ev_en_prev = '0') then
        lost <= (others => '0');
      elsif ev_valid = '1' and ev_enable = '1' and evq_in_ready = '0' and lost /= x"FFFFFFFF" then
        lost <= lost + 1;
      end if;
      ev_en_prev <= ev_enable;
    end if;
  end process;
  ev_lost <= lost;

  m_in_valid <= enc_valid or evq_valid;
  m_in       <= '0' & enc_trig & std_logic_vector(enc_slot) & enc_data when enc_valid = '1' else
                '1' & "0000" & evq_data;
  enc_ready  <= m_in_ready;
  evq_ready  <= m_in_ready and not enc_valid;

  to_mem_i : entity work.async_fifo
    generic map (WIDTH => 133, DEPTH => 512)
    port map (in_clk => clk, in_rst => rst, in_valid => m_in_valid, in_ready => m_in_ready, in_data => m_in,
              out_clk => mem_clk, out_rst => mem_rst, out_valid => f_valid, out_ready => f_ready, out_data => f_data);
  to_mem_slice_i : entity work.stream_slice
    generic map (WIDTH => 133)
    port map (clk => mem_clk, rst => mem_rst, in_valid => f_valid, in_ready => f_ready, in_data => f_data,
              out_valid => q_valid, out_ready => q_ready, out_data => q_data);

  ---------------------------------------------------------------------------------------------
  -- mem: リング書き込みと読み出し
  ---------------------------------------------------------------------------------------------
  process (mem_clk)
  begin
    if rising_edge(mem_clk) then
      m_start <= m_ctl_new and ctl_dst(ADDR_BITS + 1);
      m_stop  <= m_ctl_new and not ctl_dst(ADDR_BITS + 1);
    end if;
  end process;

  writer_i : entity work.la_mem_writer
    generic map (ADDR_BITS => ADDR_BITS)
    port map (clk => mem_clk, rst => mem_rst, start => m_start, stop => m_stop or m_full_stop,
              post_beats => unsigned(ctl_dst(ADDR_BITS - 1 downto 0)), stream => ctl_dst(ADDR_BITS),
              ev_enable => ev_en_m,
              in_valid => q_valid, in_ready => q_ready, in_data => q_data(127 downto 0), in_kind => q_data(132),
              in_trig => q_data(131), in_slot => unsigned(q_data(130 downto 128)),
              running => w_running, triggered => w_triggered, done => w_done, trigger_beat => w_beat,
              trigger_slot => w_slot, written => w_written, total => w_total, ev_written => m_ev_written,
              ev_pos => ev_wpos,
              cmd_valid => w_valid, cmd_ready => w_ready, cmd_write => w_write, cmd_addr => w_addr, cmd_data => w_data);

  ev_en_i : entity work.sync_bit
    port map (clk => mem_clk, d => ev_enable, q => ev_en_m);

  -- 書いたイベントの数は 16 周期ごとに sys へ渡す（常に変わりうる）
  process (mem_clk)
  begin
    if rising_edge(mem_clk) then
      ev_tick     <= ev_tick + 1;
      ev_cnt_load <= '1' when ev_tick = 0 else '0';
    end if;
  end process;
  ev_cnt_i : entity work.cdc_value
    generic map (WIDTH => 32)
    port map (src_clk => mem_clk, src_load => ev_cnt_load, src_value => std_logic_vector(m_ev_written),
              dst_clk => clk, dst_value => ev_cnt_dst, dst_changed => open);
  ev_written <= unsigned(ev_cnt_dst);
  la_beats   <= LA_SIZE;
  ev_beats   <= EV_SIZE;

  rd_src <= read_region & std_logic_vector(read_first) & std_logic_vector(read_count);
  rd_i : entity work.cdc_value
    generic map (WIDTH => 2 * ADDR_BITS + 2)
    port map (src_clk => clk, src_load => read, src_value => rd_src,
              dst_clk => mem_clk, dst_value => rd_dst, dst_changed => m_rd_new);

  -- ストリーム: 未読（w_total - r_total）があり読み出し器が空いていれば、最大 STREAM_CHUNK 拍を読ませる。
  -- 未読・拍数・次の番地は 1 周期ずつに分けて求める（起動は STREAM_CHUNK 拍に 1 回程度なので数周期は問題ない）。
  -- STREAM_EVENTS: イベントのリングの未読（ev_lim - ev_rpos）も同じように読み、どちらのかたまりの前にも見出しを送る
  -- （T_HDR）。ev_lim は書き込み中は書き位置を追い、止まったらそこで止まる（止めた時点までを流す）
  process (mem_clk)
    variable wrapped : unsigned(ADDR_BITS downto 0);
    variable take_ev, take_la, ripe, la_ready, ev_ready : boolean;
  begin
    if rising_edge(mem_clk) then
      s_start     <= '0';
      m_full_stop <= '0';
      unread_r    <= w_total - r_total;
      ev_unread_r <= ev_lim - ev_rpos;
      ev_back_r   <= ev_wpos - ev_rpos;                        -- 読んでいない書き込み（上書きの判定）
      ev_en_mprev <= ev_en_m;
      if w_running = '1' then
        ev_lim <= ev_wpos;
      end if;
      if mem_rst = '1' then
        s_on      <= '0';
        m_ovf     <= '0';
        ev_on     <= '0';
        hdr_valid <= '0';
        s_state   <= T_IDLE;
      elsif m_start = '1' then
        s_on     <= ctl_dst(ADDR_BITS);
        m_ovf    <= '0';
        r_total  <= (others => '0');
        r_ptr    <= (others => '0');
        unread_r <= (others => '0');                           -- 前の捕捉の総数の差を見せない
        o_total  <= (others => '0');
        age      <= (others => '0');
        hdr_valid <= '0';
        last_kind <= '0';
        seg_kind  <= '0';
        if STREAM_EVENTS then
          ev_on   <= ctl_dst(ADDR_BITS);
          ev_rpos <= unsigned(ctl_dst(EV_BITS - 1 downto 0));          -- post_beats = 最初のイベントの位置
          ev_lim  <= unsigned(ctl_dst(EV_BITS - 1 downto 0));
        end if;
        ev_unread_r <= (others => '0');
        ev_back_r   <= (others => '0');
        s_state  <= T_SETTLE;                                  -- 総数の 0 戻しが unread_r に届くまで判定しない
      elsif s_on = '1' then
        if w_running = '1' and s_state /= T_SETTLE and s_state /= T_IDLE2
           and unread_r > resize(LA_SIZE, 32) - STREAM_CHUNK then
          m_full_stop <= '1';                                  -- 未読がリングに迫った: 書き込みを止める
          m_ovf       <= '1';
        end if;
        if STREAM_EVENTS and ev_on = '1' and s_state /= T_SETTLE and s_state /= T_IDLE2
           and ev_back_r(EV_BITS - 1) = '1' then
          m_full_stop <= '1';                                  -- イベントの未読がリングの半分: 止め、以後流さない
          m_ovf       <= '1';
          ev_on       <= '0';
        end if;
        if STREAM_EVENTS and ev_en_m = '1' and ev_en_mprev = '0' then
          ev_rpos <= (others => '0');                          -- イベントの記録が始め直された（書き位置が 0 に戻る）
        end if;
        case s_state is
          when T_IDLE =>                                       -- unread_r は r_total の更新から 2 周期後に正しい
            ripe     := not STREAM_EVENTS or age(AGE_BITS - 1) = '1' or w_running = '0';
            la_ready := unread_r /= 0 and (ripe or unread_r >= LA_MIN);
            ev_ready := STREAM_EVENTS and ev_on = '1' and ev_unread_r /= 0
                        and (ripe or resize(ev_unread_r, 32) >= EV_MIN);
            take_ev  := ev_ready and (not la_ready or last_kind = '0');
            take_la  := not take_ev and la_ready;
            if STREAM_EVENTS and age(AGE_BITS - 1) = '0' then
              age <= age + 1;
            end if;
            if r_busy = '0' then
              if take_ev then
                seg_kind  <= '1';
                rdr_count <= chunk_of(resize(ev_unread_r, 32));
                s_state   <= T_SUM;
              elsif take_la then
                seg_kind  <= '0';
                rdr_count <= chunk_of(unread_r);
                s_state   <= T_SUM;
              elsif w_running = '0' then                       -- （止まっていれば ripe なので、未読は両方 0）
                s_on <= '0';                                   -- 書き込みが止まり、出し切った
              end if;
            end if;
          when T_SUM =>
            next_sum <= resize(r_ptr, ADDR_BITS + 1) + rdr_count;
            if STREAM_EVENTS then
              if seg_kind = '1' then
                hdr_lo <= std_logic_vector(resize(ev_rpos, 32)) & std_logic_vector(resize(rdr_count, 16))
                          & x"00" & SEG_EVENTS;
              else
                hdr_lo <= std_logic_vector(r_total) & std_logic_vector(resize(rdr_count, 16)) & x"00" & SEG_LA;
              end if;
              hdr_valid <= '1';
              s_state   <= T_HDR;
            else
              hdr_lo  <= (others => '0');                      -- 見出しの無い構成でも駆動する（EX1998）
              s_state <= T_GO;
            end if;
          when T_HDR =>                                        -- 見出しが mem → sys の FIFO に入るまで
            if ts_ready = '1' then
              hdr_valid <= '0';
              s_state   <= T_GO;
            end if;
          when T_GO =>
            s_start   <= '1';
            o_total   <= o_total + rdr_count + 1;              -- 見出しと中身
            age       <= (others => '0');
            last_kind <= seg_kind;
            if seg_kind = '1' then
              rdr_first <= resize(ev_rpos, ADDR_BITS);
              ev_rpos   <= ev_rpos + rdr_count(EV_BITS - 1 downto 0);   -- リングの中で折り返す（2 の冪）
            else
              rdr_first <= r_ptr;
              r_total   <= r_total + rdr_count;
              if next_sum >= LA_SIZE then
                wrapped := next_sum - LA_SIZE;
              else
                wrapped := next_sum;
              end if;
              r_ptr <= wrapped(ADDR_BITS - 1 downto 0);
            end if;
            s_state <= T_SETTLE;
          when T_SETTLE =>                                     -- 読み出し器の busy と unread_r が追いつくまで
            s_state <= T_IDLE2;
          when T_IDLE2 =>
            s_state <= T_IDLE;
        end case;
      end if;
    end if;
  end process;

  rdr_start  <= m_rd_new or s_start;
  rdr_first_mux <= rdr_first when s_start = '1' else unsigned(rd_dst(2 * ADDR_BITS downto ADDR_BITS + 1));
  rdr_count_mux <= rdr_count when s_start = '1' else unsigned(rd_dst(ADDR_BITS downto 0));
  rdr_region <= rd_dst(2 * ADDR_BITS + 1) when s_on = '0' else seg_kind;
  region_base <= resize(LA_SIZE, ADDR_BITS) when rdr_region = '1' else (others => '0');
  region_size <= EV_SIZE when rdr_region = '1' else LA_SIZE;

  reader_i : entity work.mem_reader
    generic map (ADDR_BITS => ADDR_BITS)
    port map (clk => mem_clk, rst => mem_rst, start => rdr_start,
              region_base => region_base, region_size => region_size,
              first => rdr_first_mux, count => rdr_count_mux, busy => r_busy,
              out_valid => ro_valid, out_ready => ro_ready, out_data => ro_data,
              cmd_valid => r_valid, cmd_ready => r_ready, cmd_write => r_write, cmd_addr => r_addr, cmd_data => r_data,
              rd_valid => r_rd_valid, rd_data => rd_data);

  -- mem_port: 書き込みと読み出しが両方求めていれば交互に通す（ストリーミング中は同時に動く）。
  -- どちらに通すか（sel_r）は 1 周期前に決めてレジスタに持つ（128 ビットの切り替えの選択を短い経路にする）。
  -- 通す側が求めていなければその周期は空く（帯域 500 MB/s に対して使うのは ≤ 80 MB/s なので問題ない）。
  -- 読み出しの結果は読み出し器だけが受ける（書き込み側は読まない）
  cmd_valid  <= (r_valid and sel_r) or (w_valid and not sel_r);
  cmd_write  <= r_write when sel_r = '1' else w_write;
  cmd_addr   <= r_addr when sel_r = '1' else w_addr;
  cmd_data   <= r_data when sel_r = '1' else w_data;
  r_ready    <= cmd_ready and sel_r;
  w_ready    <= cmd_ready and not sel_r;                       -- 選ばれていない側は待つ
  r_rd_valid <= rd_valid;
  process (mem_clk)
  begin
    if rising_edge(mem_clk) then
      -- 次の周期に通す側: 片方だけが求めていればそちら、両方なら今と反対（受理されたら交代）
      if r_valid = '1' and w_valid = '0' then
        sel_r <= '1';
      elsif w_valid = '1' and r_valid = '0' then
        sel_r <= '0';
      elsif r_valid = '1' and w_valid = '1' and cmd_ready = '1' then
        sel_r <= not sel_r;
      end if;
    end if;
  end process;
  m_busy     <= r_busy or s_on;
  mem_active <= m_busy or w_running or ev_en_m;               -- イベントの記録中も使う

  -- mem → sys: 書き込みの状態（変わるたびに束ねて渡す）
  m_state <= w_running & w_triggered & w_done;
  process (mem_clk)
  begin
    if rising_edge(mem_clk) then
      m_state_prev  <= m_state;
      m_status_load <= '1' when m_state /= m_state_prev else '0';
    end if;
  end process;
  status_src <= m_state & std_logic_vector(w_slot) & std_logic_vector(w_beat) & std_logic_vector(w_written);
  status_i : entity work.cdc_value
    generic map (WIDTH => 2 * ADDR_BITS + 7)
    port map (src_clk => mem_clk, src_load => m_status_load, src_value => status_src,
              dst_clk => clk, dst_value => status_dst, dst_changed => status_new);

  -- 読み出した拍（と STREAM_EVENTS の見出し）→ sys → バイト列。見出しは読み出し器が空いている間にだけ送る
  -- （T_HDR は r_busy = '0' の後）ので、読み出し器の出力とぶつからない
  ts_valid <= ro_valid or hdr_valid;
  ts_data  <= SEG_MAGIC & hdr_lo when hdr_valid = '1' else ro_data;
  ro_ready <= ts_ready and not hdr_valid;
  to_sys_i : entity work.async_fifo
    generic map (WIDTH => 128, DEPTH => 64)
    port map (in_clk => mem_clk, in_rst => mem_rst, in_valid => ts_valid, in_ready => ts_ready, in_data => ts_data,
              out_clk => clk, out_rst => rst, out_valid => beat_valid, out_ready => beat_ready, out_data => beat_data);

  unpack_i : entity work.stream_unpack
    generic map (BYTES => 16)
    port map (clk => clk, rst => rst, in_valid => beat_valid, in_ready => beat_ready, in_data => beat_data,
              out_valid => out_valid, out_ready => out_ready, out_data => out_data);

  -- sys の状態
  reading_i : entity work.sync_bit
    port map (clk => clk, d => m_busy, q => reading_s);
  ovf_i : entity work.sync_bit
    port map (clk => clk, d => m_ovf, q => ovf_s);
  -- stream_beats: ストリームで書いた拍の総数。STREAM_EVENTS では出すと決めた拍の総数（見出し・イベントを含む）
  sb_src <= o_total when STREAM_EVENTS else w_total;
  w_total_i : entity work.cdc_value
    generic map (WIDTH => 32)
    port map (src_clk => mem_clk, src_load => ev_cnt_load, src_value => std_logic_vector(sb_src),
              dst_clk => clk, dst_value => w_total_dst, dst_changed => open);
  stream_beats <= unsigned(w_total_dst);
  process (clk)
  begin
    if rising_edge(clk) then
      if rst = '1' then
        pending_read <= '0';
      elsif read = '1' then
        pending_read <= '1';                                   -- mem 側が busy になるまで
      elsif reading_s = '1' then
        pending_read <= '0';
      end if;
      -- 開始から mem 側の状態が届くまでは捕捉中（前回の done を見せない）
      if rst = '1' or run = '0' then
        pending_run <= '0';
      elsif run_prev = '0' then
        pending_run <= '1';
      elsif status_new = '1' and status_dst(2 * ADDR_BITS + 6) = '1' then
        pending_run <= '0';                                    -- 書き込みが始まったと届いた
      end if;
    end if;
  end process;
  -- 読み出し中: mem 側の読み出し（ストリームを含む）に加え、sys 側の FIFO と分解に拍が残っている間と、
  -- その後の数周期（mem 側の busy が落ちてから拍が FIFO を渡ってくるまでの遅れを覆う）
  process (clk)
  begin
    if rising_edge(clk) then
      if reading_s = '1' or beat_valid = '1' or out_valid = '1' then
        drain <= 31;
      elsif drain /= 0 then
        drain <= drain - 1;
      end if;
    end if;
  end process;
  reading      <= '1' when reading_s = '1' or pending_read = '1' or drain /= 0 else '0';
  capturing    <= status_dst(2 * ADDR_BITS + 6) or pending_run;
  triggered    <= enc_triggered;
  done         <= status_dst(2 * ADDR_BITS + 4) and not pending_run;
  trigger_slot <= unsigned(status_dst(2 * ADDR_BITS + 3 downto 2 * ADDR_BITS + 1));
  trigger_beat <= unsigned(status_dst(2 * ADDR_BITS downto ADDR_BITS + 1));
  written      <= unsigned(status_dst(ADDR_BITS downto 0));
  trigger_time <= time_r;
end architecture;
