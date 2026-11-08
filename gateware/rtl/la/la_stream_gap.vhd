-- ロジアナの垂れ流しの欠けの区間（ボード非依存、clk = sys のドメイン）: 符号器の拍と外部メモリへの非同期 FIFO の間に置く。
-- 仕様: docs/superpowers/specs/2026-11-01-la-deep-fast-design.md §12（段 B）
--
-- enable = '0'（run の 0→1 の時点）: 何もしない（入力をそのまま出す。in_ready = out_ready。今までの「溢れたら止まる」形）。
-- enable = '1': 符号器を止めない（in_ready = '1'）。拍を出せない（下流の受け口が空かない・hold = '1'）周期の拍は捨て、捨てた
--   サンプルの数を数える（RAW: 1 拍 = 2^(SPB_LOG + pack) サンプル、RLE: 拍の 4 レコードの長さ（数 + 1）の和）。hold = '0'・
--   room = '1'（下流の FIFO が半分未満）・受け口が空いた周期に、欠けの記録（イベント、out_kind = '1'）を 1 件出して拍を出すのを
--   再開する。欠けは拍の境で始まり拍の境で終わる（捨てたのは拍まるごと）ので、ホストは記録の位置 b（その前に出した拍の数）と
--   長さ q で、届いた拍の列の中の欠けの位置を一意に決められる（埋めない・補わない）。
--   記録（128 ビット。種類 EVENT_KIND_LA_GAP = event_pkg。定義元は fprobe_gw/events.py）:
--     [47:0] 拍を捨て始めた周期の time、[79:48] b（この流れで欠けの前に出したロジアナの拍の数。32 ビットで折り返す。
--     ストリームの見出しのロジアナの通し番号と同じ数え）、[119:80] q（捨てたサンプルの数。流れのサンプルの単位）、[127:120] 種類。
--   q が 2^(Q_BITS − 1) に達したら、その時点までを 1 件にして（同じ b のまま）次の件を続ける（記録の 40 ビットの欄に収める）。
--   それでも欄（2^40）を超える（下流が受けないまま 2^39 サンプルの後も記録を出せない）なら lost。
--   捨て始めるとき・記録を出すときに ev_enable = '0'（イベントのリングに書かれない）なら、欠けを記録できないので lost を立てて以後は何も出さない
--   （黙って欠けを作らない。受け手は overrun として扱う）。run = '0' の後は記録を出さない（止めた後の欠けは流れの外）。
-- gaps = この流れで出した記録の件数、last_beat = 最後に出した記録の b（ホストは届いた拍の数を受け取った後に last_beat → gaps の
--   順に読めば、届いた拍に関わる記録の件数が分かる。仕様 §12）。gap_open = 今、拍を捨てている（止めた後も次の開始まで残る）。
-- out_tag は in_tag をそのまま運ぶ（記録の周期は 0）。
-- パイプライン（enable = '1'）: 入力の拍の長さを求める段 → 出す・捨てるを決めて出力のレジスタに入れる段。
--
-- トークン形式（tok = '1' かつ rle = '1'。docs/superpowers/specs/2026-11-07-la-rle2-design.md §3.6）:
--   捨てたサンプルの数 q は拍の長さの和でなく、符号器が拍ごとに付ける「拍の最後のトークンの時刻」in_pos の差:
--   q = 捨てた最後の拍の in_pos − 直前に出した拍の in_pos（記録の位置 b・時刻・種類・欄は同じ。split の件も同じ位置からの差で続ける）。
--   TOGGLE は差分なので、捨てたあとは値が分からない。そこで欠けの区間は、捨てた拍が再提示の拍でない（in_kf = 0）たびに resync_req を
--   1 周期立てて、符号器に「次のサンプルを VALUE（値の再提示）にして新しい拍を始めよ」と頼み続ける。欠けを閉じるときは、閉じる拍（その拍も
--   捨てる）の次の拍から出す。ただしその拍が再提示の拍（in_kf = 1）でなければ出さず、また欠けを始める（同じ位置 b の続きの件。
--   q は途切れずに続く）。再提示の拍の先頭は VALUE（長い間隔の前置きの WAIT があればその後ろ）で、その VALUE の間隔は捨てた最後のトークン
--   からの時間なので、ホストは時刻を pos_e + q（直前に出した拍の in_pos + 欠けの q）から続ける。
library ieee;
use ieee.std_logic_1164.all;
use ieee.numeric_std.all;
use work.event_pkg.all;
use work.la_pkg.all;

entity la_stream_gap is
  generic (
    SPB_LOG : natural := 3;                 -- RAW の 1 拍のサンプルの数の log2（pack = 0 のとき）
    Q_BITS  : positive := 40                -- 件を分ける長さ 2^(Q_BITS − 1)（2〜40。試験は小さくして件の分け方を確かめる）
  );
  port (
    clk        : in  std_logic;
    rst        : in  std_logic;
    run        : in  std_logic;
    enable     : in  std_logic;                       -- run の 0→1 の周期に取り込む
    rle        : in  std_logic;                       -- 同
    pack       : in  unsigned(2 downto 0);            -- 同（RAW の 1 拍 = 2^(SPB_LOG + pack) サンプル）
    time       : in  unsigned(63 downto 0);
    tok        : in  std_logic := '0';                -- rle = 1 のとき、トークン形式（run の 0→1 の周期に取り込む）
    in_pos     : in  unsigned(47 downto 0) := (others => '0');   -- トークン形式: 入力の拍の最後のトークンの時刻
    in_kf      : in  std_logic := '0';                           -- トークン形式: 入力の拍は再提示の VALUE から始まる
    resync_req : out std_logic;                       -- トークン形式: 符号器に再提示を頼む（1 周期のパルス）
    hold       : in  std_logic;                       -- 1 = 拍を出さない（外部メモリのリングの未読が迫った）
    room       : in  std_logic;                       -- 1 = 下流の FIFO に半分以上の空き（欠けを閉じる条件）
    ev_enable  : in  std_logic;                       -- イベントの記録が有効（欠けの記録が書かれる）
    in_valid   : in  std_logic;
    in_ready   : out std_logic;
    in_data    : in  std_logic_vector(127 downto 0);
    in_tag     : in  std_logic_vector(3 downto 0);
    out_valid  : out std_logic;
    out_ready  : in  std_logic;
    out_kind   : out std_logic;                       -- '0' = ロジアナの拍、'1' = 欠けの記録（イベント）
    out_data   : out std_logic_vector(127 downto 0);
    out_tag    : out std_logic_vector(3 downto 0);
    gaps       : out unsigned(31 downto 0);
    last_beat  : out unsigned(31 downto 0);
    gap_open   : out std_logic;
    lost       : out std_logic
  );
end entity;

architecture rtl of la_stream_gap is
  signal run_prev : std_logic := '0';
  signal en_r, rle_r, tok_r : std_logic := '0';
  signal pack_r : unsigned(2 downto 0) := (others => '0');
  -- 段 A（入力の拍と長さ）
  signal a_valid : std_logic := '0';
  signal a_data : std_logic_vector(127 downto 0) := (others => '0');
  signal a_tag : std_logic_vector(3 downto 0) := (others => '0');
  signal a_len : unsigned(18 downto 0) := (others => '0');
  signal a_pos : unsigned(47 downto 0) := (others => '0');
  signal a_kf : std_logic := '0';
  -- 段 B（出す・捨てる）
  signal gapping : std_logic := '0';
  signal need_kf : std_logic := '0';                              -- トークン形式: 欠けを閉じた。次に出す拍は再提示の拍でなければならない
  signal pos_e, pos_d, pos_ref : unsigned(47 downto 0) := (others => '0');   -- 出した最後の拍・捨てた最後の拍・今の件の起点の in_pos
  signal rs_r : std_logic := '0';
  signal q : unsigned(40 downto 0) := (others => '0');            -- 捨てたサンプルの数（bit40 = 欄を超えた）
  signal b_gap, beats : unsigned(31 downto 0) := (others => '0');
  signal t_gap : unsigned(47 downto 0) := (others => '0');
  signal o_valid, o_kind : std_logic := '0';
  signal o_data : std_logic_vector(127 downto 0) := (others => '0');
  signal o_tag : std_logic_vector(3 downto 0) := (others => '0');
  signal gaps_r, last_r : unsigned(31 downto 0) := (others => '0');
  signal lost_r : std_logic := '0';

  -- 拍のサンプルの数
  function beat_len(d : std_logic_vector(127 downto 0); is_rle : std_logic; p : unsigned(2 downto 0)) return unsigned is
    variable n : unsigned(18 downto 0);
  begin
    if is_rle = '1' then
      n := to_unsigned(4, 19);
      for r in 0 to 3 loop
        n := n + resize(rle_run_minus_1(d, r), 19);
      end loop;
    else
      n := shift_left(to_unsigned(1, 19), SPB_LOG + to_integer(p));
    end if;
    return n;
  end function;

  function record_of(t : unsigned(47 downto 0); b : unsigned(31 downto 0); n : unsigned) return std_logic_vector is
  begin
    return EVENT_KIND_LA_GAP & std_logic_vector(resize(n, 40)) & std_logic_vector(b) & std_logic_vector(t);
  end function;
begin
  assert Q_BITS >= 2 and Q_BITS <= 40 and SPB_LOG + 4 <= 18 report "la_stream_gap: 2 <= Q_BITS <= 40, SPB_LOG <= 14" severity failure;

  process (clk)
    variable free : boolean;
    variable qn : unsigned(40 downto 0);
    variable close_now, split_now : boolean;
    variable pass_ok, disc : boolean;
    variable dn, qd : unsigned(47 downto 0);
  begin
    if rising_edge(clk) then
      run_prev <= run;
      if o_valid = '1' and out_ready = '1' then
        o_valid <= '0';
      end if;
      free := o_valid = '0' or out_ready = '1';

      -- 段 A
      a_valid <= '0';
      if in_valid = '1' and en_r = '1' then
        a_valid <= '1';
        a_data  <= in_data;
        a_tag   <= in_tag;
        a_len   <= beat_len(in_data, rle_r, pack_r);
        a_pos   <= in_pos;
        a_kf    <= in_kf;
      end if;
      disc := false;

      -- 段 B
      if en_r = '1' and lost_r = '0' then
        if gapping = '0' then
          if a_valid = '1' then
            pass_ok := free and hold = '0' and (tok_r = '0' or need_kf = '0' or a_kf = '1');
            if pass_ok then
              o_valid <= '1';
              o_kind  <= '0';
              o_data  <= a_data;
              o_tag   <= a_tag;
              beats   <= beats + 1;
              pos_e   <= a_pos;
              need_kf <= '0';
            elsif ev_enable = '0' and run = '1' then
              lost_r <= '1';                                 -- 欠けを記録できない: 捨て始めずに止める（今までの溢れと同じ）
            else
              gapping <= '1';                                -- この拍から捨てる
              q       <= resize(a_len, 41);
              b_gap   <= beats;
              t_gap   <= time(47 downto 0);
              pos_ref <= pos_e;
              pos_d   <= a_pos;
              disc    := true;
            end if;
          end if;
        else
          qn := q;
          dn := pos_d;
          if a_valid = '1' then
            qn := q + resize(a_len, 41);                     -- この周期の拍も捨てる
            dn := a_pos;
            disc := true;
          end if;
          if tok_r = '1' then
            qd := dn - pos_ref;                              -- トークン形式: 捨てた最後の拍の時刻 − 起点
            qn := qd(40 downto 0);
            if qd(47 downto 41) /= 0 then qn(40) := '1'; end if;
          end if;
          pos_d <= dn;
          close_now := hold = '0' and room = '1';
          split_now := qn(40 downto Q_BITS - 1) /= 0;
          if close_now and qn = 0 then
            gapping <= '0';                                  -- 分けた件の後に捨てた拍が無い: 記録は要らない
            if tok_r = '1' then
              need_kf <= '1';
              pos_e   <= dn;
            end if;
          elsif run = '1' and free and (close_now or split_now) then
            if ev_enable = '1' then
              o_valid <= '1';
              o_kind  <= '1';
              o_data  <= record_of(t_gap, b_gap, qn);
              o_tag   <= (others => '0');
              gaps_r  <= gaps_r + 1;
              last_r  <= b_gap;
              if close_now then
                gapping <= '0';                              -- 次の拍から出す
                if tok_r = '1' then
                  need_kf <= '1';                            -- トークン形式: 次の拍は再提示の拍でなければ出さない
                  pos_e   <= dn;
                end if;
              else
                q     <= (others => '0');                    -- 同じ位置の続きの件
                t_gap <= time(47 downto 0);
                pos_ref <= dn;
              end if;
            else
              lost_r <= '1';                                 -- 記録が書かれない: 欠けを黙って作らない
            end if;
          else
            q <= qn;
            pos_d <= dn;
            if qn(40) = '1' then
              lost_r <= '1';                                 -- 欄に収まらない（下流が長く受けない）
            end if;
          end if;
        end if;
      end if;

      rs_r <= '0';
      if disc and a_kf = '0' and tok_r = '1' then
        rs_r <= '1';                                         -- 捨てた拍が再提示の拍でない: 符号器に次の再提示を頼む
      end if;

      if run = '1' and run_prev = '0' then                   -- 開始: 設定を取り込み、前の流れの残りを捨てる
        en_r    <= enable;
        rle_r   <= rle;
        tok_r   <= tok and rle;
        need_kf <= '0';
        pos_e   <= (others => '0');
        rs_r    <= '0';
        pack_r  <= pack;
        a_valid <= '0';
        o_valid <= '0';
        gapping <= '0';
        q       <= (others => '0');
        beats   <= (others => '0');
        gaps_r  <= (others => '0');
        last_r  <= (others => '0');
        lost_r  <= '0';
      end if;
      if rst = '1' then
        en_r    <= '0';
        tok_r   <= '0';
        need_kf <= '0';
        rs_r    <= '0';
        a_valid <= '0';
        o_valid <= '0';
        gapping <= '0';
        q       <= (others => '0');
        beats   <= (others => '0');
        gaps_r  <= (others => '0');
        last_r  <= (others => '0');
        lost_r  <= '0';
      end if;
    end if;
  end process;

  in_ready  <= '1' when en_r = '1' else out_ready;
  out_valid <= o_valid when en_r = '1' else in_valid;
  out_kind  <= o_kind when en_r = '1' else '0';
  out_data  <= o_data when en_r = '1' else in_data;
  out_tag   <= o_tag when en_r = '1' else in_tag;
  gaps      <= gaps_r;
  last_beat <= last_r;
  gap_open  <= gapping;
  resync_req <= rs_r;
  lost      <= lost_r;
end architecture;
