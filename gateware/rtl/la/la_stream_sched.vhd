-- ロジアナの深いバッファの連続ストリーミングの読み出しの段取り（mem ドメイン）。外部メモリのリングを FIFO として使う。
-- 仕様: docs/superpowers/specs/2026-10-16-m8c-la-streaming-design.md、2026-10-19-r1-unified-stream-design.md、
--       2026-11-01-la-deep-fast-design.md §12、2026-11-03-ps-stream-position-design.md
--
-- start（流れの開始。設定 stream・gaps・ps_read・ev_first はその周期の値）から、stream = '1' の流れの間（active）:
--   ・未読（書き込み器の総数 w_total − 読ませた総数）があり読み出し器が空いていれば、最大 STREAM_CHUNK 拍を読ませる
--     （rd_start を 1 周期。rd_region・rd_first・rd_count はその周期の値）。未読・拍数・次の番地は 1 周期ずつに分けて求める
--     （起動は STREAM_CHUNK 拍に 1 回程度なので数周期は問題ない）。
--   ・STREAM_EVENTS: イベントのリングの未読（ev_lim − ev_rpos）も同じように読み、どちらのかたまりの前にも見出しを出す
--     （hdr_valid / hdr_ready。見出しは読み出し器が空いている間にだけ出すので、読み出し器の出力とぶつからない）。
--     両方に未読があれば前回と反対の種類。かたまりは未読がこれだけ溜まるか（LA_MIN・EV_MIN）、溜まり始めて 2^(AGE_BITS−1)
--     周期が経つか、書き込みが止まってから出す（1 拍ずつのかたまりでは見出しで流れが 2 倍になる）。ev_lim は書き込み中は
--     書き位置を追い、止まったらそこで止まる（止めた時点までを流す）。
--     見出し = [7:0] 種類（1 = ロジアナ、2 = イベント）、[31:16] 拍数、[63:32] 通し番号（ロジアナ: 流れの先頭からの拍の番号、
--     イベント: 最初のイベントのリングの位置）、[127:64] 印 "FPRBSEG"。
--   ・未読がリング − STREAM_CHUNK を超えたら書き込みを止めて（full_stop）溢れ（ovf）。イベントの未読がリングの半分に達したら
--     同じく止め、以後イベントは流さない。
--   ・gaps: 未読が LA_SIZE − GAP_HOLD を超えたら hold を立て（書き込みは止めない）、LA_SIZE − GAP_HOLD − GAP_ROOM 以下に減ったら
--     下ろす（sys の la_stream_gap が拍を捨てる。GAP_HOLD・GAP_ROOM の意味は la_deep_buffer の注）。
--   ・ps_read: 読み出し器に読ませず、読み終えた位置は PS が返す（ret_*。この流れの番号 ret_epoch = epoch の値だけを使う。
--     書いた総数より先の値は使わない）。PS に公開するイベントの書き位置 ev_pub は、止めた後は ev_lim で頭打ち（止めた後の
--     イベントは流れに入れず、リングに残す。§2.9。PS が ev_lim を越えて読むと未読が 0 にならず、流れが終わらなかった。B1）。
--   ・書き込みが止まり、未読を出し切ったら（PS が読む流れは PS が読み終えたら）active を下ろす。
-- 判定（溢れ・hold）は、未読が読ませたかたまりの後の総数に追いついてから（T_WAIT の間はしない）。
-- 数え: unread（と同じ周期の w_total = unread_total）、peak（流れの開始からの未読の最大。unread を含める）、out_total（出すと
-- 決めた拍の総数。見出し・イベントを含む）。総数（書いた・読んだ）は 32 ビットで折り返す（mod_diff）。
library ieee;
use ieee.std_logic_1164.all;
use ieee.numeric_std.all;
use work.util_pkg.all;
use work.mem_ring_pkg.all;

entity la_stream_sched is
  generic (
    ADDR_BITS     : positive;
    STREAM_EVENTS : boolean := false;
    TOTAL_START_BELOW_4G : natural := 0                      -- 試験用: 読んだ総数を 2**32 のこの数だけ手前から（la_mem_writer と同じ値）
  );
  port (
    clk          : in  std_logic;
    rst          : in  std_logic;
    -- 流れの開始と設定（la_run_cdc の mem 側）
    start        : in  std_logic;
    epoch        : in  unsigned(15 downto 0);
    stream       : in  std_logic;
    gaps         : in  std_logic;
    ps_read      : in  std_logic;
    ev_first     : in  unsigned(ring_ev_bits(ADDR_BITS) - 1 downto 0);   -- 最初に読むイベントの位置
    -- 書き込み器
    w_total      : in  unsigned(31 downto 0);
    w_running    : in  std_logic;
    ev_wpos      : in  unsigned(ring_ev_bits(ADDR_BITS) - 1 downto 0);
    ev_safe      : in  unsigned(ring_ev_bits(ADDR_BITS) - 1 downto 0);   -- 応答の来た書きまでのイベントの書き位置
    full_stop    : out std_logic;                                        -- 1 周期: 書き込みを止める
    -- PS の読み終えた位置
    ret_epoch    : in  unsigned(15 downto 0);
    ret_la       : in  unsigned(31 downto 0);
    ret_ev       : in  unsigned(ring_ev_bits(ADDR_BITS) - 1 downto 0);
    -- 読み出し器
    rd_busy      : in  std_logic;
    rd_start     : out std_logic;
    rd_region    : out std_logic;                                        -- '0' = ロジアナ、'1' = イベント
    rd_first     : out unsigned(ADDR_BITS - 1 downto 0);
    rd_count     : out unsigned(ADDR_BITS downto 0);
    -- 見出し
    hdr_valid    : out std_logic;
    hdr_ready    : in  std_logic;
    hdr_data     : out std_logic_vector(127 downto 0);
    -- 状態と数え
    active       : out std_logic;                                        -- 流れの最中
    ps_on        : out std_logic;                                        -- この流れは PS が読む
    ev_keep      : out std_logic;                                        -- イベントのリングを読む流れの最中（la_mem_writer の ev_keep。C3）
    hold         : out std_logic;
    ovf          : out std_logic;
    stream_epoch : out unsigned(15 downto 0);                            -- 流れの状態（総数・active 等）の流れの番号
    ev_start     : out unsigned(ring_ev_bits(ADDR_BITS) - 1 downto 0);   -- この流れの最初のイベントの位置
    ev_pub       : out unsigned(ring_ev_bits(ADDR_BITS) - 1 downto 0);   -- PS に公開するイベントの書き位置
    unread       : out unsigned(31 downto 0);
    unread_total : out unsigned(31 downto 0);
    peak         : out unsigned(31 downto 0);
    out_total    : out unsigned(31 downto 0)
  );
end entity;

architecture rtl of la_stream_sched is
  constant STREAM_CHUNK : natural := 256;
  constant EV_BITS : natural := ring_ev_bits(ADDR_BITS);
  constant LA_MIN   : natural := 64;
  constant EV_MIN   : natural := minimum(64, 2 ** (EV_BITS - 2));
  constant AGE_BITS : natural := 14;                                          -- 2^13 周期（約 82 us）
  -- 見出しの印と種類（fprobe_gw.csr.fprobe_map の STREAM_SEG_* と一致させる）
  constant SEG_MAGIC  : std_logic_vector(63 downto 0) := x"0047455342525046";   -- "FPRBSEG\0"（リトルエンディアン）
  constant SEG_LA     : std_logic_vector(7 downto 0) := x"01";
  constant SEG_EVENTS : std_logic_vector(7 downto 0) := x"02";
  constant LA_SIZE : unsigned(ADDR_BITS downto 0) := ring_la_beats(ADDR_BITS);
  constant TOTAL_START : unsigned(31 downto 0) := to_unsigned(0, 32) - to_unsigned(TOTAL_START_BELOW_4G, 32);
  constant GAP_HOLD : natural := minimum(1024, to_integer(LA_SIZE) / 4);
  constant GAP_ROOM : natural := minimum(4096, to_integer(LA_SIZE) / 4);
  constant LA_SIZE32 : unsigned(31 downto 0) := resize(LA_SIZE, 32);

  -- 1 つのかたまりで読む拍数（未読 n。最大 STREAM_CHUNK 拍）
  function chunk_of(n : unsigned(31 downto 0)) return unsigned is
  begin
    if n > STREAM_CHUNK then
      return to_unsigned(STREAM_CHUNK, ADDR_BITS + 1);
    end if;
    return resize(n, ADDR_BITS + 1);
  end function;

  -- T_WAIT: 読み出し器が始めを受けた（busy）のを待つ。その時点で r_total の更新が unread_r に届いている
  type state_t is (T_IDLE, T_SUM, T_HDR, T_GO, T_WAIT);
  signal state : state_t := T_IDLE;
  signal s_on, gaps_m, hold_m, ps_m, ovf_m, stop_r, start_r : std_logic := '0';
  signal epoch_r : unsigned(15 downto 0) := (others => '0');
  signal r_total : unsigned(31 downto 0) := (others => '0');
  signal r_ptr : unsigned(ADDR_BITS - 1 downto 0) := (others => '0');
  signal unread_r, unread_t, peak_r, o_total : unsigned(31 downto 0) := (others => '0');
  signal next_sum : unsigned(ADDR_BITS downto 0) := (others => '0');
  signal first_r : unsigned(ADDR_BITS - 1 downto 0) := (others => '0');
  signal count_r : unsigned(ADDR_BITS downto 0) := (others => '0');
  -- イベント（STREAM_EVENTS。false なら定数のまま）
  signal ev_on : std_logic := '0';
  signal ev_rpos, ev_lim, ev_unread_r, ev_back_r, ev_start_r : unsigned(EV_BITS - 1 downto 0) := (others => '0');
  signal seg_kind, last_kind : std_logic := '0';                              -- '0' = ロジアナ、'1' = イベント
  signal hdr_v : std_logic := '0';
  signal hdr_lo : std_logic_vector(63 downto 0) := (others => '0');
  signal age : unsigned(AGE_BITS - 1 downto 0) := (others => '0');            -- 前のかたまりからの周期
begin
  process (clk)
    variable wrapped : unsigned(ADDR_BITS downto 0);
    variable take_ev, take_la, ripe, la_ready, ev_ready, judge : boolean;
  begin
    if rising_edge(clk) then
      start_r     <= '0';
      stop_r      <= '0';
      unread_r    <= mod_diff(w_total, r_total);
      unread_t    <= w_total;
      ev_unread_r <= ev_lim - ev_rpos;
      ev_back_r   <= ev_wpos - ev_rpos;                        -- 読んでいない書き込み（上書きの判定）
      if w_running = '1' then
        ev_lim <= ev_wpos;
      end if;
      if start = '1' then
        peak_r <= (others => '0');
      elsif s_on = '1' and unread_r > peak_r then
        peak_r <= unread_r;
      end if;

      if rst = '1' then
        s_on       <= '0';
        gaps_m     <= '0';
        hold_m     <= '0';
        ps_m       <= '0';
        ovf_m      <= '0';
        ev_on      <= '0';
        hdr_v      <= '0';
        ev_start_r <= (others => '0');
        epoch_r    <= (others => '0');
        state      <= T_IDLE;
      elsif start = '1' then
        epoch_r    <= epoch;                                   -- 書き込み器の総数の 0 戻しと同じ辺（状態の組が揃う）
        s_on       <= stream;
        gaps_m     <= gaps and stream;
        hold_m     <= '0';
        ps_m       <= ps_read and stream;
        ev_start_r <= ev_first;
        ovf_m      <= '0';
        r_total    <= TOTAL_START;
        r_ptr      <= (others => '0');
        unread_r   <= (others => '0');                         -- 前の捕捉の総数の差を見せない（次の周期は 0 − 0）
        o_total    <= (others => '0');
        age        <= (others => '0');
        hdr_v      <= '0';
        last_kind  <= '0';
        seg_kind   <= '0';
        if STREAM_EVENTS then
          ev_on   <= stream;
          ev_rpos <= ev_first;
          ev_lim  <= ev_first;
        end if;
        ev_unread_r <= (others => '0');
        ev_back_r   <= (others => '0');
        state       <= T_IDLE;
      elsif s_on = '1' then
        judge := state /= T_WAIT;
        if ps_m = '1' and ret_epoch = epoch then
          if mod_le(ret_la, w_total) then
            r_total <= ret_la;
          end if;
          if STREAM_EVENTS then
            ev_rpos <= ev_start_r + ret_ev;
          end if;
        end if;
        if gaps_m = '1' and judge then
          if unread_r > LA_SIZE32 - GAP_HOLD then
            hold_m <= '1';
          elsif unread_r <= LA_SIZE32 - (GAP_HOLD + GAP_ROOM) then
            hold_m <= '0';
          end if;
        end if;
        if w_running = '1' and judge and unread_r > LA_SIZE32 - STREAM_CHUNK then
          stop_r <= '1';                                       -- 未読がリングに迫った: 書き込みを止める
          ovf_m  <= '1';
        end if;
        if STREAM_EVENTS and ev_on = '1' and judge and ev_back_r(EV_BITS - 1) = '1' then
          stop_r <= '1';                                       -- イベントの未読がリングの半分: 止め、以後流さない
          ovf_m  <= '1';
          ev_on  <= '0';
        end if;
        case state is
          when T_IDLE =>                                       -- unread_r は r_total の更新から 2 周期後に正しい
            ripe     := not STREAM_EVENTS or age(AGE_BITS - 1) = '1' or w_running = '0';
            la_ready := ps_m = '0' and unread_r /= 0 and (ripe or unread_r >= LA_MIN);   -- PS が読む流れでは読ませない
            ev_ready := ps_m = '0' and STREAM_EVENTS and ev_on = '1' and ev_unread_r /= 0
                        and (ripe or resize(ev_unread_r, 32) >= EV_MIN);
            take_ev  := ev_ready and (not la_ready or last_kind = '0');
            take_la  := not take_ev and la_ready;
            if STREAM_EVENTS and age(AGE_BITS - 1) = '0' then
              age <= age + 1;
            end if;
            if rd_busy = '0' then
              if take_ev then
                seg_kind <= '1';
                count_r  <= chunk_of(resize(ev_unread_r, 32));
                state    <= T_SUM;
              elsif take_la then
                seg_kind <= '0';
                count_r  <= chunk_of(unread_r);
                state    <= T_SUM;
              elsif w_running = '0' and (ps_m = '0' or (unread_r = 0 and ev_unread_r = 0)) then
                -- （止まっていれば ripe なので、未読は両方 0）。PS が読む流れは、PS が読み終えてから
                s_on <= '0';                                   -- 書き込みが止まり、出し切った
              end if;
            end if;
          when T_SUM =>
            next_sum <= resize(r_ptr, ADDR_BITS + 1) + count_r;
            if STREAM_EVENTS then
              if seg_kind = '1' then
                hdr_lo <= std_logic_vector(resize(ev_rpos, 32)) & std_logic_vector(resize(count_r, 16)) & x"00" & SEG_EVENTS;
              else
                hdr_lo <= std_logic_vector(r_total) & std_logic_vector(resize(count_r, 16)) & x"00" & SEG_LA;
              end if;
              hdr_v <= '1';
              state <= T_HDR;
            else
              hdr_lo <= (others => '0');                       -- 見出しの無い構成でも駆動する（EX1998）
              state  <= T_GO;
            end if;
          when T_HDR =>                                        -- 見出しが受けられるまで
            if hdr_ready = '1' then
              hdr_v <= '0';
              state <= T_GO;
            end if;
          when T_GO =>
            start_r   <= '1';
            o_total   <= o_total + count_r + 1;                -- 見出しと中身
            age       <= (others => '0');
            last_kind <= seg_kind;
            if seg_kind = '1' then
              first_r <= resize(ev_rpos, ADDR_BITS);
              ev_rpos <= ev_rpos + count_r(EV_BITS - 1 downto 0);   -- リングの中で折り返す（2 の冪）
            else
              first_r <= r_ptr;
              r_total <= r_total + count_r;
              if next_sum >= LA_SIZE then
                wrapped := next_sum - LA_SIZE;
              else
                wrapped := next_sum;
              end if;
              r_ptr <= wrapped(ADDR_BITS - 1 downto 0);
            end if;
            state <= T_WAIT;
          when T_WAIT =>                                       -- 読み出し器が始めを受けた（busy）: unread_r も新しい r_total のもの
            if rd_busy = '1' then
              state <= T_IDLE;
            end if;
        end case;
      end if;
    end if;
  end process;

  -- PS に公開するイベントの書き位置（状態の組と同じ時点の値）。止めた後の PS の流れでは、読み位置からの距離が ev_lim より
  -- 遠ければ ev_lim にする
  ev_pub <= ev_lim when ps_m = '1' and s_on = '1' and w_running = '0' and ev_safe - ev_rpos > ev_lim - ev_rpos else ev_safe;

  full_stop    <= stop_r;
  rd_start     <= start_r;
  rd_region    <= seg_kind;
  rd_first     <= first_r;
  rd_count     <= count_r;
  hdr_valid    <= hdr_v;
  hdr_data     <= SEG_MAGIC & hdr_lo;
  active       <= s_on;
  ps_on        <= ps_m;
  ev_keep      <= s_on and ev_on;
  hold         <= hold_m;
  ovf          <= ovf_m;
  stream_epoch <= epoch_r;
  ev_start     <= ev_start_r;
  unread       <= unread_r;
  unread_total <= unread_t;
  peak         <= unread_r when s_on = '1' and unread_r > peak_r else peak_r;   -- 最大は unread を含める（同じ時点）
  out_total    <= o_total;
end architecture;
