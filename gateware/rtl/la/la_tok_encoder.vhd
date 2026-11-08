-- ロジアナの RLE のトークン形式の符号器（1 周期 1 サンプル。ボード非依存、clk = sys のドメイン）: サンプル列を 16 バイトの拍に詰める。
-- 仕様: docs/superpowers/specs/2026-11-07-la-rle2-design.md §3・§5・§3.7（段階 2: REPEAT なし、段階 2b: REPEAT あり）。ホストの参照符号器
--   host/fprobe/la_tokens.py の encode_tokens(samples, repeat, tick_samples, resync_at) とバイト単位で一致する
--   （repeat = 入力 repeat（run の 0→1 で取り込む）、REPEAT_EN = false の構成では repeat = False と一致）。
--   符号の定数は fprobe_gw/csr/fprobe_map.py の LA_TOK_CONSTANTS（生成パッケージ la_tok_pkg）。
--
-- 構成（順序は全部サンプルの順。判断はサンプル列と tick・resync だけで決まる）:
--   前段（feed-forward、5 段）: サンプル → 直前との XOR（変化した ch）→ 先頭 3 本の ch の位置・k >= 4 の印 → 1 サンプル 1 項目（entry）。
--   合流（段 5）: 変化の無いサンプルの連なりを数え、次の出来事（または停止）の項目に「その前に変化の無いサンプルが n 個」と付ける。
--   溜め（FIFO、16 項目）: 後段が数周期かかる場合（長い間隔の WAIT・tick の WAIT・再同期）に、1 周期 1 サンプルの入力を受けとめる。溢れたら overrun。
--     後段は変化の無いサンプルの連なりを 1〜2 周期で進める（長い無変化のあとの数周期の遅れは、その連なりの間に取り戻す）。
--   後段（P）: 項目を 1 つずつ処理して拍に詰める。ふつうの項目（直前の出来事から 8198 サンプル以内の TOGGLE・2047 以内の VALUE）は
--     1 周期。それより長い間隔・tick の WAIT・再同期は数周期かかる（有限状態機械）。拍の詰め方は参照符号器の _Packer と同じ:
--     束（グループ）は拍をまたがない。入らなければ残りを NOP で埋めて拍を出す。
-- REPEAT（REPEAT_EN = true の構成で、run の 0→1 の時点の入力 repeat = 1 のとき。仕様 §3.7）: 1 つの TOGGLE だけの束（間隔が 8198 以下 = 3 バイト以下）が
--   完全に同じものが続くのを、直前の B（1 か 2）個の束 + REPEAT にまとめる。後段の中の「束のまとめ」（R）が、束を拍に詰める前に直近の 2 つ
--   （保持 H0・H1）を持ち、同じ束が続く間は拍に置かず数える（r_n）。違う束・長い間隔の束・VALUE・複数 ch の束・tick・再同期・停止が来たら、
--   まとめを閉じて（S_DR 以降の数周期）拍に置く: REPEAT にして短くなれば 対象 + REPEAT を 1 つの塊（S_RC*）、そうでなければ元の束を順に（S_RL）。
--   束の時刻（out_pos 用）は直近 5 つの出来事の時刻の窓 pw で持つ。まとめの中は拍に何も置かない（保持している間、束は後に回るだけで順序は変わらない）。
-- run の 0→1 の周期のサンプルは使わず（la_encoder と同じ）、その次の周期のサンプルから符号にする。1→0 で停止の項目を流し、
--   作りかけの拍を NOP で埋めて出す（現行のレコード形式と違い、端数を捨てない）。
-- tick（サンプル数 = sys のクロック数 ÷ (la.rate + 1)、0 = 無し。run の 0→1 で取り込む）: 参照符号器の tick_samples と同じ。
--   最後の拍を出してから tick 経った（拍が満ちて出たときも数え直す）ら、作りかけの拍を NOP で埋めて出す。作りかけが空なら先に WAIT を出す。
--   作りかけが空でなければ追加の周期は要らない（次の出来事の束を入れる周期に拍の払いを兼ねる）。空のときの WAIT は数周期かかる。
--   小さすぎる tick（約 8 未満）で変化が続くと後段の処理が追いつかず溜めが溢れる（overrun。黙って捨てない）。
-- resync（1 周期のパルス。保持して次のサンプルで使う）: 次の sample_valid のサンプルを「再同期」にする。そのサンプルは変化の有無にかかわらず
--   VALUE（現在の値の再提示）になり、直前に作りかけの拍を NOP で埋めて出す（新しい拍の先頭から始める）。その拍の out_kf = 1（欠けの後の再開。
--   la_stream_gap が使う）。
-- 出力は 1 拍の保持レジスタ（out_valid / out_ready）。保持中に次の拍が出たら、その拍を捨てて overrun を立てる（la_encoder と同じ。
--   la_stream_gap が有効なら in_ready = 1 なので起きない）。out_pos = その拍の最後のトークンの時刻（サンプルの番号。最初のサンプル = 0。
--   la_stream_gap が欠けの長さ q を出すのに使う）。
-- 間隔が 2^47 サンプルを超える出来事の前は overrun を立てる（時刻の数えが 48 ビットなので、黙って狂わせない）。WAIT で分けるのは、
--   間隔が上限を超えたとき（TOGGLE: 7 + 2^TOGGLE_DELTA_BITS − 1、VALUE: 2^VALUE_DELTA_BITS − 1）に出来事の前で行う（参照符号器と同じ）。
-- generic の TOGGLE_DELTA_BITS・VALUE_DELTA_BITS・WAIT_BITS は符号の定義の値が既定。試験が小さくして長い間隔の分割を短いシミュレーション
-- で確かめる（その試験では参照符号器の同じ定数も同じ値にする）。
library ieee;
use ieee.std_logic_1164.all;
use ieee.numeric_std.all;
use work.la_tok_pkg.all;

entity la_tok_encoder is
  generic (
    TOGGLE_DELTA_BITS : positive range 4 to 41 := LA_TOK_PAYLOAD_BITS;                 -- TOGGLE_LONG の P の幅 = 間隔の上限 7 + 2^n − 1
    VALUE_DELTA_BITS  : positive range 2 to 25 := LA_TOK_PAYLOAD_BITS - 16;            -- VALUE の間隔の幅
    WAIT_BITS         : positive range 2 to 40 := LA_TOK_WAIT_BITS;                    -- 1 つの WAIT の時間の幅
    REPEAT_EN         : boolean := false;                                              -- REPEAT の回路を持つ（false なら面積を足さない）
    REPEAT_N_BITS     : positive range 3 to 40 := LA_TOK_PAYLOAD_BITS - 1              -- 1 つの REPEAT の繰り返しの回数の上限 2^n（参照符号器の REPEAT_N_MAX）
  );
  port (
    clk          : in  std_logic;
    rst          : in  std_logic;
    run          : in  std_logic;
    sample_valid : in  std_logic;
    sample       : in  std_logic_vector(15 downto 0);
    tick         : in  unsigned(31 downto 0);                         -- run の 0→1 で取り込む。0 = 無し
    repeat       : in  std_logic := '0';                              -- 1 = REPEAT を使う（REPEAT_EN の構成だけ。run の 0→1 で取り込む）
    resync       : in  std_logic;                                     -- パルス（次のサンプルを再同期にする。次のサンプルが来るまで保つ）
    out_valid    : out std_logic;
    out_ready    : in  std_logic;
    out_data     : out std_logic_vector(127 downto 0);
    out_pos      : out unsigned(47 downto 0);
    out_kf       : out std_logic;
    overrun      : out std_logic;
    pad_bytes    : out unsigned(31 downto 0);                         -- NOP で埋めたバイトの総数（流れの開始から）
    keyframes    : out unsigned(31 downto 0)                          -- 再同期のサンプルの数
  );
end entity;

architecture rtl of la_tok_encoder is
  constant NOPS   : std_logic_vector(127 downto 0) := x"07070707070707070707070707070707";   -- LA_TOK_NOP を 16 個

  -- 間隔の上限・WAIT の最大（48 ビット）。TOGGLE: 7 + 2^n − 1、VALUE: 2^n − 1、WAIT: 2^n − 1
  constant LT_U : unsigned(47 downto 0) := to_unsigned(LA_TOK_DELTA_EXT, 48) + shift_left(to_unsigned(1, 48), TOGGLE_DELTA_BITS) - 1;
  constant LV_U : unsigned(47 downto 0) := shift_left(to_unsigned(1, 48), VALUE_DELTA_BITS) - 1;
  constant WM_U : unsigned(47 downto 0) := shift_left(to_unsigned(1, 48), WAIT_BITS) - 1;

  -- 速い道（1 周期）の間隔の上限。TOGGLE: 頭 + 尾 2 個（P < 2^13 → 間隔 <= 8198）、VALUE: 尾 4 個（間隔 < 2048）まで。
  -- 小さい generic では上限（LT・LV）を超えない（間隔が上限を超えたら WAIT で分ける遅い道）
  function fast_limit(bits : positive; max_fast : natural; bias : natural) return natural is
  begin
    if bits >= 30 then
      return max_fast;
    elsif bias + 2 ** bits - 1 < max_fast then
      return bias + 2 ** bits - 1;
    else
      return max_fast;
    end if;
  end function;
  constant FAST_T : natural := fast_limit(TOGGLE_DELTA_BITS, 2 ** (LA_TOK_T1_BITS + LA_TOK_TAIL_BITS) + LA_TOK_DELTA_EXT - 1, LA_TOK_DELTA_EXT);   -- 8198
  constant FAST_V : natural := fast_limit(VALUE_DELTA_BITS, 2 ** 11 - 1, 0);
  constant CAP    : natural := 8199;                                -- dsmall の上限（FAST_T + 1 以上）

  function ctz16(x : std_logic_vector(15 downto 0)) return unsigned is
    variable r : natural := 0;
  begin
    for i in 15 downto 0 loop
      if x(i) = '1' then r := i; end if;
    end loop;
    return to_unsigned(r, 4);
  end function;

  function clear_low(x : std_logic_vector(15 downto 0)) return std_logic_vector is
  begin
    return x and std_logic_vector(unsigned(x) - 1);
  end function;

  -- 特殊トークン（WAIT・REPEAT）の束: 頭 head + 尾（T1 は特殊の印）。尾の数は nt（P が収まる最小）
  function special_bytes(head : std_logic_vector(7 downto 0); pay : unsigned(40 downto 0); nt : natural) return std_logic_vector is
    variable r : std_logic_vector(71 downto 0) := (others => '0');
  begin
    r(7 downto 0) := head;
    r(15 downto 8) := "11" & std_logic_vector(pay(5 downto 0));
    for k in 2 to 6 loop
      if k <= nt then
        r(8 * k + 7 downto 8 * k) := '1' & std_logic_vector(pay(LA_TOK_T1_BITS + LA_TOK_TAIL_BITS * (k - 2) + 6 downto
                                                              LA_TOK_T1_BITS + LA_TOK_TAIL_BITS * (k - 2)));
      end if;
    end loop;
    return r;
  end function;

  -- P の尾の数（1〜6）: P < 2^6 なら 1、< 2^13 なら 2、…（参照符号器の _tails）
  function tail_count(p : unsigned(40 downto 0)) return natural is
    variable n : natural := 1;
  begin
    for k in 1 to 5 loop
      if p(40 downto LA_TOK_T1_BITS + LA_TOK_TAIL_BITS * (k - 1)) /= 0 then n := k + 1; end if;
    end loop;
    return n;
  end function;

  -- 前段の項目（FIFO の 1 件）: [40] n = 1（後段が次の項目の無変化の 1 サンプルを前の項目の完了に畳むための印）、[39] REPEAT でまとめられる形（ev かつ stop・usev・resync でなく nt = 1）、[38] n /= 0（後段の判断を浅くするために前もって求めておく）、[37:22] この出来事（または停止）の前の変化の無いサンプルの数 n、[21] stop、[20] ev、[19] usev、[18] resync、
  -- [17:16] nt - 1、[15:0] VALUE の値、または {pos2, pos1, pos0, 0000}。ev = stop = 0 の項目は n 個の変化の無いサンプルだけ（n = 65535 で分ける）
  subtype entry_t is std_logic_vector(40 downto 0);
  type fifo_t is array (0 to 15) of entry_t;
  signal fifo : fifo_t := (others => (others => '0'));
  signal f_wr, f_rd : unsigned(4 downto 0) := (others => '0');

  signal run_prev : std_logic := '0';
  signal tick_r : unsigned(31 downto 0) := (others => '0');
  -- 前段
  signal s0_valid, s0_stop, s0_resync, s0_first : std_logic := '0';
  signal s0_sample : std_logic_vector(15 downto 0) := (others => '0');
  signal prev_s : std_logic_vector(15 downto 0) := (others => '0');
  signal prev_ok : std_logic := '0';
  signal rs_pend : std_logic := '0';                    -- 再同期の要求を保持中
  signal s1_valid, s1_stop, s1_forced, s1_resync : std_logic := '0';
  signal s1_x, s1_val : std_logic_vector(15 downto 0) := (others => '0');
  signal s2_valid, s2_stop, s2_ev, s2_forced, s2_resync : std_logic := '0';
  signal s2_x1, s2_val : std_logic_vector(15 downto 0) := (others => '0');
  signal s2_p0 : unsigned(3 downto 0) := (others => '0');
  signal s3_valid, s3_stop, s3_ev, s3_forced, s3_resync : std_logic := '0';
  signal s3_x2, s3_val : std_logic_vector(15 downto 0) := (others => '0');
  signal s3_p0, s3_p1 : unsigned(3 downto 0) := (others => '0');
  signal s3_two : std_logic := '0';
  signal s4_valid : std_logic := '0';
  signal s4_entry : std_logic_vector(21 downto 0) := (others => '0');
  signal s4_cs : std_logic := '0';                      -- s4_entry が REPEAT でまとめられる形（1 つの TOGGLE だけ）
  signal s5_valid : std_logic := '0';
  signal s5_entry : entry_t := (others => '0');
  signal m_idle : unsigned(15 downto 0) := (others => '0');

  -- 後段 P
  -- 閉じ役（tick で束のまとめを閉じる処理を、前段の処理と並行して行う。下の説明）
  type cstate_t is (C_IDLE, C_P, C_N, C_T, C_R, C_PART, C_F);
  signal cst : cstate_t := C_IDLE;
  signal sn_h0_b, sn_h1_b : std_logic_vector(23 downto 0) := (others => '0');
  signal sn_h0_l, sn_h1_l : unsigned(1 downto 0) := (others => '0');
  signal sn_n : unsigned(REPEAT_N_BITS downto 0) := (others => '0');
  signal sn_b2, sn_part : std_logic := '0';
  signal sn_pos_a, sn_pos_b : unsigned(47 downto 0) := (others => '0');
  type state_t is (S_NORMAL, S_TICKF, S_TW0, S_SD, S_SC1, S_SC2, S_SW1, S_SW2, S_SW3, S_SG1, S_SG2, S_SG3, S_DONE,
                   S_DR, S_RC1, S_RC2, S_RC3, S_RC4, S_RP, S_RL);
  signal st : state_t := S_DONE;
  signal cur : std_logic_vector(127 downto 0) := NOPS;
  signal fill : unsigned(4 downto 0) := (others => '0');
  signal cur_pos : unsigned(47 downto 0) := (others => '0');
  signal cur_kf : std_logic := '0';
  signal now_t, lastt : unsigned(47 downto 0) := (others => '0');
  signal dsmall : unsigned(13 downto 0) := (others => '0');
  signal pcnt : unsigned(12 downto 0) := (others => '0');
  signal to_due : unsigned(31 downto 0) := (others => '0');   -- tick まで残りのサンプル数（tick = 0 のときは使わない）
  signal hd_v : std_logic := '0';                       -- 溜めの先頭（登録済み）が有効
  signal hd_r : entry_t := (others => '0');
  signal due, pre_done, flushed_acc : std_logic := '0';
  signal loaded : std_logic := '0';                      -- 先頭の項目の変化の無いサンプルの数を rem に取り込み済み
  signal rem_n : unsigned(15 downto 0) := (others => '0');
  signal rem_nz : std_logic := '0';                       -- rem_n /= 0
  signal fast_t_r, fast_v_r : std_logic := '1';           -- dsmall が速い道（TOGGLE: FAST_T 以下、VALUE: FAST_V 以下）に収まる（dsmall と一緒に更新）
  -- 遅い道
  signal d_reg, dm1_reg, step_reg, newt_reg : unsigned(47 downto 0) := (others => '0');
  signal gt_r : std_logic := '0';
  signal pay_reg : unsigned(40 downto 0) := (others => '0');
  signal nt_reg : natural range 0 to 6 := 0;
  signal tick_wait : std_logic := '0';                  -- 遅い道の WAIT が tick のもの
  signal direct_r : std_logic := '0';                   -- TOGGLE の先頭が 1 バイトで書ける
  -- REPEAT（束のまとめ R）。REPEAT_EN = false の構成では全部 0 に駆動する
  type pw_t is array (0 to 4) of unsigned(47 downto 0);
  signal pw : pw_t := (others => (others => '0'));      -- 直近 5 つの出来事の時刻（pw(0) = 最新）
  signal rep_r : std_logic := '0';                      -- この流れで REPEAT を使う
  signal r_pat : std_logic := '0';                      -- 繰り返しを開いている（H0（と B = 2 なら H1）が対象）
  signal r_b2 : std_logic := '0';                       -- B = 2
  signal r_ge8 : std_logic := '0';                      -- r_n >= 8（r_n と一緒に更新する印。閉じ役に任せられる条件を、長い経路に入れないための登録）
  signal r_part : std_logic := '0';                     -- B = 2 で、1 つ目が一致して 2 つ目を待っている
  signal r_hcnt : unsigned(1 downto 0) := (others => '0');   -- 繰り返しを開いていないとき、持っている束の数（0〜2）
  signal r_n : unsigned(REPEAT_N_BITS downto 0) := (others => '0');   -- 繰り返しの回数（対象の後に何回続いたか）
  signal h0_sig, h1_sig : std_logic_vector(17 downto 0) := (others => '0');   -- 束の署名 = ch（4）+ 間隔（14）
  signal h0_b, h1_b : std_logic_vector(23 downto 0) := (others => '0');       -- 束のバイト（3 バイト以下）
  signal h0_l, h1_l : unsigned(1 downto 0) := (others => '0');                -- 束のバイト数（1〜3）
  signal rl_m, rl_k : unsigned(2 downto 0) := (others => '0');                -- 元の束を順に置く: 総数・何個目
  signal rl_b2 : std_logic := '0';                      -- 順に置く束が H0・H1 の交互
  signal tick_dr : std_logic := '0';                    -- tick のためにまとめを閉じた
  -- 拍の詰めの段への要求（前の段が決めたことを 1 周期後に実行する）
  signal rq_v, rq_flush, rq_eb, rq_full, rq_kf : std_logic := '0';
  signal rq_pad, rq_off, rq_len : unsigned(4 downto 0) := (others => '0');
  signal rq_g : std_logic_vector(71 downto 0) := (others => '0');
  signal rq_pos : unsigned(47 downto 0) := (others => '0');
  -- 出力
  signal valid_r, kf_r, overrun_r : std_logic := '0';
  signal data_r : std_logic_vector(127 downto 0) := (others => '0');
  signal pos_r : unsigned(47 downto 0) := (others => '0');
  signal pad_r, kfr_r : unsigned(31 downto 0) := (others => '0');
begin
  out_valid  <= valid_r;
  out_data   <= data_r;
  out_pos    <= pos_r;
  out_kf     <= kf_r;
  overrun    <= overrun_r;
  pad_bytes  <= pad_r;
  keyframes  <= kfr_r;

  ---------------------------------------------------------------------------------------------
  -- 前段: サンプル → 項目（1 周期 1 件）
  ---------------------------------------------------------------------------------------------
  process (clk)
    variable x : std_logic_vector(15 downto 0);
    variable x3 : std_logic_vector(15 downto 0);
    variable usev : std_logic;
    variable nt : unsigned(1 downto 0);
    variable p2 : unsigned(3 downto 0);
    variable pl : std_logic_vector(15 downto 0);
  begin
    if rising_edge(clk) then
      run_prev <= run;
      -- 段 0: 入力を取る（開始の周期のサンプルは使わない）
      s0_valid  <= '0';
      s0_stop   <= '0';
      s0_resync <= '0';
      s0_first  <= '0';
      s0_sample <= sample;
      if run = '1' and run_prev = '1' and sample_valid = '1' then
        s0_valid  <= '1';
        s0_resync <= resync or rs_pend;
        s0_first  <= not prev_ok;
        prev_ok   <= '1';
        rs_pend   <= '0';
      elsif run = '0' and run_prev = '1' then
        s0_stop <= '1';
      elsif resync = '1' then
        rs_pend <= '1';
      end if;
      if run = '1' and run_prev = '0' then
        prev_ok <= '0';
        rs_pend <= '0';
      end if;

      -- 段 1: 直前との XOR
      s1_valid  <= s0_valid or s0_stop;
      s1_stop   <= s0_stop;
      s1_resync <= s0_resync;
      s1_forced <= s0_first or s0_resync;
      s1_val    <= s0_sample;
      if s0_valid = '1' then
        if s0_first = '1' then
          s1_x <= (others => '0');
        else
          s1_x <= s0_sample xor prev_s;
        end if;
        prev_s <= s0_sample;
      else
        s1_x <= (others => '0');
      end if;
      -- 段 1 の ev は段 2 で（x が登録されてから）求める
      -- 段 2: 最下位の変化 ch の位置と、それを消した集合
      s2_valid  <= s1_valid;
      s2_stop   <= s1_stop;
      s2_resync <= s1_resync;
      s2_forced <= s1_forced;
      s2_val    <= s1_val;
      if (s1_forced = '1' or s1_x /= x"0000") and s1_stop = '0' then
        s2_ev <= '1';
      else
        s2_ev <= '0';
      end if;
      s2_p0  <= ctz16(s1_x);
      s2_x1  <= clear_low(s1_x);
      -- 段 3: 2 番目の位置
      s3_valid  <= s2_valid;
      s3_stop   <= s2_stop;
      s3_resync <= s2_resync;
      s3_forced <= s2_forced;
      s3_ev     <= s2_ev;
      s3_val    <= s2_val;
      s3_p0     <= s2_p0;
      s3_p1     <= ctz16(s2_x1);
      s3_x2     <= clear_low(s2_x1);
      if s2_x1 /= x"0000" then s3_two <= '1'; else s3_two <= '0'; end if;
      -- 段 4: 3 番目の位置・k >= 4 の判定 → 項目
      s4_valid <= s3_valid;
      x3 := clear_low(s3_x2);
      if s3_forced = '1' or x3 /= x"0000" then usev := '1'; else usev := '0'; end if;
      if s3_x2 /= x"0000" then
        nt := "10";
      elsif s3_two = '1' then
        nt := "01";
      else
        nt := "00";
      end if;
      p2 := ctz16(s3_x2);
      if usev = '1' then
        pl := s3_val;
      else
        pl := std_logic_vector(p2) & std_logic_vector(s3_p1) & std_logic_vector(s3_p0) & "0000";
      end if;
      s4_entry <= s3_stop & s3_ev & usev & s3_resync & std_logic_vector(nt) & pl;
      if s3_ev = '1' and s3_stop = '0' and usev = '0' and s3_resync = '0' and nt = "00" then s4_cs <= '1'; else s4_cs <= '0'; end if;
      if run = '1' and run_prev = '0' then            -- 開始: 前の流れの残りを流さない
        s1_valid <= '0';
        s2_valid <= '0';
        s3_valid <= '0';
        s4_valid <= '0';
      end if;
      -- 段 5: 変化の無いサンプルを数える
      s5_valid <= '0';
      if s4_valid = '1' then
        if s4_entry(21) = '1' or s4_entry(20) = '1' then
          s5_valid <= '1';
          if m_idle = 1 then
            s5_entry <= '1' & s4_cs & '1' & std_logic_vector(m_idle) & s4_entry;
          elsif m_idle /= 0 then
            s5_entry <= '0' & s4_cs & '1' & std_logic_vector(m_idle) & s4_entry;
          else
            s5_entry <= '0' & s4_cs & '0' & std_logic_vector(m_idle) & s4_entry;
          end if;
          m_idle   <= (others => '0');
        elsif m_idle = x"FFFE" then
          s5_valid <= '1';
          s5_entry <= "001" & x"FFFF" & "0000000000000000000000";
          m_idle   <= (others => '0');
        else
          m_idle <= m_idle + 1;
        end if;
      end if;
      if run = '1' and run_prev = '0' then
        s5_valid <= '0';
        m_idle   <= (others => '0');
      end if;
    end if;
  end process;

  ---------------------------------------------------------------------------------------------
  -- 溜め + 後段
  ---------------------------------------------------------------------------------------------
  process (clk)
    variable e : entry_t;
    variable e_stop, e_ev, e_usev, e_resync : std_logic;
    variable e_nt : natural range 1 to 3;
    variable e_pl : std_logic_vector(15 downto 0);
    variable empty, full : boolean;
    variable pop : boolean;
    variable fl_now : boolean;                       -- この周期に拍を出した・払った（last_flush が今のサンプルになる）
    variable fast_ok : boolean;
    variable g : std_logic_vector(71 downto 0);
    variable glen : natural range 0 to 9;
    variable d11 : unsigned(10 downto 0);
    variable c0, c1, c2 : std_logic_vector(3 downto 0);
    variable l1 : natural range 1 to 3;
    variable ntl : natural range 0 to 6;
    variable p : unsigned(40 downto 0);
    variable tmp48 : unsigned(47 downto 0);
    variable e_n, n_cur : unsigned(15 downto 0);
    variable kk : unsigned(15 downto 0);
    variable to_due_v : unsigned(31 downto 0);
    variable dnew : unsigned(16 downto 0);
    variable hit : boolean;
    variable fold : boolean;
    variable td_a, td_b : unsigned(31 downto 0);
    variable hi_p1 : unsigned(31 downto 0);             -- 時刻の上位 32 ビット + 1（繰り上がりを選ぶだけにして、遅く分かる加える数の加算を下位 16 ビットに絞る）
    variable lo_s : unsigned(16 downto 0);
    variable n_nz : boolean;
    -- 拍に置く束（この周期の put の 1 か所の入口）と、項目の処理が終わったか
    variable p_en : boolean;
    variable p_g : std_logic_vector(71 downto 0);
    variable p_len, p_need : natural range 0 to 16;
    variable p_pos : unsigned(47 downto 0);
    variable p_kf, p_tf : std_logic;
    variable c_do, c_ev : boolean;
    variable f_req : boolean;
    variable f_pad : natural range 0 to 16;
    variable bfill : natural range 0 to 16;
    variable q_eb, q_full : boolean;
    variable q_off, q_pad : natural range 0 to 16;
    variable sb : std_logic_vector(127 downto 0);
    variable sk : std_logic;
    -- REPEAT
    variable cand, needs_r, r_full, shift_pw, match : boolean;
    variable eq0, eq1 : boolean;
    variable dsm_n : unsigned(13 downto 0);
    variable sig_new : std_logic_vector(17 downto 0);
    variable blen, nsm, mm : natural range 0 to 63;
    variable idx : natural range 0 to 7;
    variable b2i, parti : natural range 0 to 1;
    variable cbusy, pk_free, noput, bg_ok, cp : boolean;   -- 閉じ役が動いている・拍の詰めを使える（使わない項目か閉じ役が止まっている）・
                                                      -- この項目は拍の詰めを使わない・閉じ役に任せられる・この周期の拍への置きが閉じ役のもの

    -- 拍を出す（保持中なら捨てて overrun）
    procedure emit(data : std_logic_vector(127 downto 0); pos : unsigned(47 downto 0); kf : std_logic) is
    begin
      if valid_r = '1' and out_ready = '0' then
        overrun_r <= '1';
      else
        valid_r <= '1';
        data_r  <= data;
        pos_r   <= pos;
        kf_r    <= kf;
      end if;
    end procedure;

    -- 作りかけの拍を NOP で埋めて出す要求（空なら何もしない）。last_flush は呼び出し側で更新する。
    -- 拍の組み立て（cur）は 1 周期後の「拍の詰め」の段で行う。ここ（前の段）は、拍の中身の量 fill（影）だけを即座に進め、詰めの段への要求
    -- （どこに置くか・先に出すか・満ちたか）を決める。決めるのが前の段、データを動かすのが後の段（段を分けて 1 周期の論理の深さを抑える）
    procedure flush_cur is
    begin
      if fill /= 0 then
        f_req := true;
        f_pad := 16 - to_integer(fill);
        fill  <= (others => '0');
      end if;
    end procedure;

    -- 項目の処理が終わった周期の更新（時刻・間隔の数え・tick）
    -- 時刻を inc 進める（inc <= 65535）。上位は繰り上がるかどうかで、先に求めた + 1 を選ぶだけ
    procedure set_now(inc : unsigned(15 downto 0)) is
    begin
      lo_s := ('0' & now_t(15 downto 0)) + inc;
      if lo_s(16) = '1' then
        now_t <= hi_p1 & lo_s(15 downto 0);
      else
        now_t <= now_t(47 downto 16) & lo_s(15 downto 0);
      end if;
    end procedure;

    procedure complete(had_event : boolean) is
    begin
      -- 次の項目が「無変化のサンプルが 1 つだけ先行する」なら、その 1 サンプルの進めを畳むか（下）
      fold := had_event and f_wr /= f_rd and fifo(to_integer(f_rd(3 downto 0)))(40) = '1'
              and (tick_r = 0 or (tick_r >= 3 and to_due_v >= 3));
      if fold then
        set_now(to_unsigned(2, 16));
      else
        set_now(to_unsigned(1, 16));
      end if;
      if had_event then
        dsm_n  := to_unsigned(1, 14);
        fast_t_r <= '1';
        fast_v_r <= '1';
        pcnt   <= to_unsigned(8192 - 6, 13);
        lastt  <= now_t;
      elsif dsmall < CAP then
        dsm_n  := dsmall + 1;
        if to_integer(dsmall) + 1 <= FAST_T then fast_t_r <= '1'; else fast_t_r <= '0'; end if;
        if to_integer(dsmall) + 1 <= FAST_V then fast_v_r <= '1'; else fast_v_r <= '0'; end if;
        pcnt   <= pcnt + 1;
      end if;
      pre_done    <= '0';
      flushed_acc <= '0';
      loaded      <= '0';
      rem_n       <= (others => '0');
      rem_nz      <= '0';
      -- 次の項目が「無変化のサンプルが 1 つだけ先行する」なら、その 1 サンプルの進め（次の周期の n_nz の分岐）を今の完了に畳む
      -- （状態の進みは、その分岐を 1 周期で実行したものと同じ。tick の時刻に当たるときは畳まない。間隔 2 サンプルの出来事の連なりに処理の余裕を作る）。
      -- 畳むかは早く分かる信号だけで決め、tick の数えの 2 つの値（拍が出た周期の数え直し・数えを進める）を先に並べて、遅く分かる fl_now で選ぶだけにする
      if fold then
        td_a := tick_r - 1;
        td_b := to_due_v - 2;
      else
        td_a := tick_r;
        td_b := to_due_v - 1;
      end if;
      if fl_now or flushed_acc = '1' then
        to_due <= td_a;
        due    <= '0';
      else
        to_due <= td_b;
        if tick_r /= 0 and to_due_v = 1 then due <= '1'; else due <= '0'; end if;
      end if;
      pop := true;
      if fold then
        dsm_n  := to_unsigned(2, 14);
        pcnt   <= to_unsigned(8192 - 5, 13);
        loaded <= '1';
      end if;
    end procedure;

    -- 束のまとめを空にする
    procedure clear_r is
    begin
      r_pat  <= '0';
      r_b2   <= '0';
      r_part <= '0';
      r_hcnt <= (others => '0');
      r_n    <= (others => '0');
      r_ge8  <= '0';
    end procedure;

  begin
    if rising_edge(clk) then
      fl_now  := false;
      pop     := false;
      hi_p1   := now_t(47 downto 16) + 1;
      to_due_v := to_due;
      p_en    := false;
      p_g     := (others => '0');
      p_len   := 0;
      p_need  := 0;
      p_pos   := now_t;
      p_kf    := '0';
      p_tf    := '0';
      c_do    := false;
      c_ev    := false;
      shift_pw := false;
      f_req   := false;
      f_pad   := 0;
      cp      := false;
      dsm_n   := dsmall;
      if valid_r = '1' and out_ready = '1' then
        valid_r <= '0';
      end if;
      -- REPEAT の回路が無い構成: 束のまとめの信号は全部 0 に駆動する（この下の代入が有効な構成では上書きされる）
      if not REPEAT_EN then
        pw      <= (others => (others => '0'));
        rep_r   <= '0';
        r_pat   <= '0';
        r_b2    <= '0';
        r_part  <= '0';
        r_hcnt  <= (others => '0');
        r_n     <= (others => '0');
        r_ge8   <= '0';
        h0_sig  <= (others => '0');
        h1_sig  <= (others => '0');
        h0_b    <= (others => '0');
        h1_b    <= (others => '0');
        h0_l    <= (others => '0');
        h1_l    <= (others => '0');
        rl_m    <= (others => '0');
        rl_k    <= (others => '0');
        rl_b2   <= '0';
        tick_dr <= '0';
        cst     <= C_IDLE;
        sn_h0_b <= (others => '0');
        sn_h1_b <= (others => '0');
        sn_h0_l <= (others => '0');
        sn_h1_l <= (others => '0');
        sn_n    <= (others => '0');
        sn_b2   <= '0';
        sn_part <= '0';
        sn_pos_a <= (others => '0');
        sn_pos_b <= (others => '0');
      end if;

      -- 溜めの読み出し口（先頭）
      e      := hd_r;
      e_n    := unsigned(e(37 downto 22));
      e_stop := e(21); e_ev := e(20); e_usev := e(19); e_resync := e(18);
      e_nt   := to_integer(unsigned(e(17 downto 16))) + 1;
      e_pl   := e(15 downto 0);
      empty  := hd_v = '0';
      full   := (f_wr - f_rd) = 16;

      -- 溜めへの書き込み
      if s5_valid = '1' then
        if full then
          overrun_r <= '1';
        else
          fifo(to_integer(f_wr(3 downto 0))) <= s5_entry;
          f_wr <= f_wr + 1;
        end if;
      end if;

      -- 先頭の項目の値（速い道）
      d11 := dsmall(10 downto 0);
      c0 := e_pl(7 downto 4);
      c1 := e_pl(11 downto 8);
      c2 := e_pl(15 downto 12);
      -- TOGGLE の ch の位置は {pos2, pos1, pos0, 0000}: pos0 = [7:4]、pos1 = [11:8]、pos2 = [15:12]
      if e_usev = '1' then
        fast_ok := fast_v_r = '1';
      else
        fast_ok := fast_t_r = '1';
      end if;
      -- 速い道の束
      g := (others => '0');
      glen := 0;
      l1 := 1;
      if e_usev = '1' then
        g(7 downto 0)   := std_logic_vector(to_unsigned(LA_TOK_KIND_VALUE * 8 + LA_TOK_DELTA_EXT, 8));
        g(15 downto 8)  := "11" & e_pl(5 downto 0);
        g(23 downto 16) := '1' & e_pl(12 downto 6);
        g(31 downto 24) := '1' & std_logic_vector(d11(3 downto 0)) & e_pl(15 downto 13);
        if d11(10 downto 4) /= 0 then
          g(39 downto 32) := '1' & std_logic_vector(d11(10 downto 4));
          glen := 5;
        else
          glen := 4;
        end if;
      else
        if dsmall <= LA_TOK_DELTA_DIRECT_MAX then
          g(7 downto 0) := '0' & c0 & std_logic_vector(dsmall(2 downto 0));
          l1 := 1;
        else
          g(7 downto 0)  := '0' & c0 & "111";
          g(15 downto 8) := "10" & std_logic_vector(pcnt(5 downto 0));
          if pcnt(12 downto 6) /= 0 then
            g(23 downto 16) := '1' & std_logic_vector(pcnt(12 downto 6));
            l1 := 3;
          else
            l1 := 2;
          end if;
        end if;
        glen := l1;
        if e_nt >= 2 then
          g(8 * l1 + 7 downto 8 * l1) := '0' & c1 & "000";
          glen := l1 + 1;
        end if;
        if e_nt >= 3 then
          g(8 * l1 + 15 downto 8 * l1 + 8) := '0' & c2 & "000";
          glen := l1 + 2;
        end if;
      end if;

      -- REPEAT: 束のまとめの状態と、先頭の項目が「まとめられる束」（1 つの TOGGLE だけ、3 バイト以下）か
      sig_new := c0 & std_logic_vector(dsmall);
      eq0     := sig_new = h0_sig;
      eq1     := sig_new = h1_sig;
      cand    := REPEAT_EN and rep_r = '1' and e(39) = '1' and fast_ok;
      needs_r := REPEAT_EN and (r_pat = '1' or r_hcnt /= 0);
      r_full  := REPEAT_EN and r_pat = '1' and r_n(REPEAT_N_BITS) = '1';
      if r_b2 = '1' then b2i := 1; else b2i := 0; end if;
      if r_part = '1' then parti := 1; else parti := 0; end if;

      -- 閉じ役: 動いている間は、前段の処理（S_NORMAL）が拍の詰めを使う項目で待つ（順序は全部サンプルの順）
      cbusy := REPEAT_EN and cst /= C_IDLE;
      bg_ok := REPEAT_EN and r_pat = '1' and r_ge8 = '1';   -- 繰り返しが 8 回以上: REPEAT にして必ず短くなる
      -- 拍の詰めを使わずに進みうる項目: 変化の無いサンプルだけの項目、または、まとめに加わりうる束（実際に拍に置く・まとめを閉じる場合は、下の分岐の中で待つ）
      noput   := (e(20) = '0' and e(21) = '0') or (cand and not r_full);
      pk_free := not cbusy or noput;

      case st is
        when S_NORMAL =>
          if not empty then
            if loaded = '1' then n_cur := rem_n; n_nz := rem_nz = '1'; else n_cur := e_n; n_nz := e(38) = '1'; end if;
            if due = '1' then
              -- 前のサンプルの tick。まとめを持っていれば先に拍に置く（tick は持っているものを全部払う）。
              -- 作りかけがあって次が速い道の出来事（まとめられる束でない）なら、その束を入れる周期に払いを兼ねる（追加の周期なし）
              if cbusy then
                null;                              -- 前の tick の閉じ役が終わるまで待つ（tick の時刻はサンプルの数で決まるので変わらない）
              elsif bg_ok then
                -- 繰り返しが長く続いているまとめ: 閉じ役に渡して（REPEAT の頭 + 尾を作り、拍に置き、作りかけを払う。数周期）、前段の処理は止めずに進む。
                -- 払う時刻は tick のサンプルなので、数えはここで数え直す。次の項目がまとめられる束なら、この周期に新しいまとめの最初として取る
                cst      <= C_P;
                sn_h0_b  <= h0_b;  sn_h0_l <= h0_l;
                sn_h1_b  <= h1_b;  sn_h1_l <= h1_l;
                sn_n     <= r_n;   sn_b2   <= r_b2;  sn_part <= r_part;
                sn_pos_b <= pw(0);
                if r_part = '1' then sn_pos_a <= pw(1); else sn_pos_a <= pw(0); end if;
                clear_r;
                to_due      <= tick_r;
                due         <= '0';
                flushed_acc <= '0';
                if cand and not n_nz then
                  h0_sig <= sig_new; h0_b <= g(23 downto 0); h0_l <= to_unsigned(l1, 2);
                  r_hcnt <= to_unsigned(1, 2);
                  shift_pw := true;
                  to_due_v := tick_r;
                  c_do := true; c_ev := true;
                end if;
              elsif needs_r then
                tick_dr <= '1';
                if r_pat = '1' then
                  st <= S_DR;
                else                               -- 持っている束だけ: 元の束を順に置く（S_DR の周期を省く）
                  rl_m <= resize(r_hcnt, 3); rl_b2 <= '1'; rl_k <= (others => '0');
                  st   <= S_RL;
                end if;
              elsif tick_dr = '1' then
                st <= S_TICKF;                     -- まとめを払った: 出来事があったので WAIT は出さない
              elsif fill /= 0 and not n_nz and e_ev = '1' and e_stop = '0' and e_resync = '0' and fast_ok and not cand then
                p_en := true; p_g := g; p_len := glen; p_need := glen; p_pos := now_t; p_kf := '0'; p_tf := '1';
                to_due_v := tick_r;
                c_do := true; c_ev := true;
              elsif fill = 0 then
                st <= S_TW0;                       -- 作りかけが空: 先に WAIT を出す
              else
                st <= S_TICKF;
              end if;
            elsif n_nz then
              -- 変化の無いサンプルを tick の時刻まで（または全部）進める
              hit := tick_r /= 0 and to_due(31 downto 16) = 0 and to_due(15 downto 0) <= n_cur;
              if hit then kk := to_due(15 downto 0); else kk := n_cur; end if;
              set_now(kk);
              dnew  := ('0' & resize(dsmall, 16)) + resize(kk, 17);
              if dnew > CAP then
                dsm_n := to_unsigned(CAP, 14);
                fast_t_r <= '0';
                fast_v_r <= '0';
              else
                dsm_n := dnew(13 downto 0);
                if to_integer(dnew) <= FAST_T then fast_t_r <= '1'; else fast_t_r <= '0'; end if;
                if to_integer(dnew) <= FAST_V then fast_v_r <= '1'; else fast_v_r <= '0'; end if;
              end if;
              pcnt  <= pcnt + kk(12 downto 0);
              if tick_r /= 0 then to_due <= to_due - kk; end if;
              if hit then due <= '1'; end if;
              loaded <= '1';
              rem_n  <= n_cur - kk;
              if n_cur /= kk then rem_nz <= '1'; else rem_nz <= '0'; end if;
            elsif not pk_free then
              null;                                -- 閉じ役が拍に置いている間は、拍の詰めを使う項目は待つ
            elsif needs_r and (e_stop = '1' or (e_ev = '1' and (not cand or r_full))) then
              -- まとめを拍に置いてから（まとめられない束・停止・回数の上限）
              if r_pat = '1' then
                st <= S_DR;
              else                                 -- 持っている束だけ: 元の束を順に置く（S_DR の周期を省く）
                rl_m <= resize(r_hcnt, 3); rl_b2 <= '1'; rl_k <= (others => '0');
                st   <= S_RL;
              end if;
            elsif e_stop = '1' then
              flush_cur;
              st  <= S_DONE;
              pop := true;
              loaded <= '0';
            elsif e_ev = '0' then                  -- 変化の無いサンプルだけの項目（n = 65535 で分けたもの）を進め終えた
              pop := true;
              loaded <= '0';
            elsif e_resync = '1' and pre_done = '0' and fast_ok then
              -- 再同期の項目で速い道: 作りかけを払う（新しい拍の先頭から始める）のを、再提示の VALUE を入れる周期に兼ねる（追加の周期なし）
              p_en := true; p_g := g; p_len := glen; p_need := glen; p_pos := now_t; p_kf := '1'; p_tf := '1';
              fl_now := true;
              kfr_r  <= kfr_r + 1;
              c_do := true; c_ev := true;
            elsif e_resync = '1' and pre_done = '0' then
              flush_cur;
              pre_done    <= '1';
              flushed_acc <= '1';
              kfr_r       <= kfr_r + 1;
            elsif e_ev = '1' and not fast_ok then
              st <= S_SD;
            elsif cand then
              -- まとめられる束: 直近 2 つの束と比べ、同じものが続く間は拍に置かず数える（参照符号器の _Encoder.submit）
              c_do := true; c_ev := true;
              shift_pw := true;
              if r_pat = '1' then
                if r_b2 = '1' and r_part = '1' then match := eq1; else match := eq0; end if;
                if match then
                  if r_b2 = '0' then
                    r_n <= r_n + 1;
                    if r_n(REPEAT_N_BITS downto 3) /= 0 or r_n(2 downto 0) = "111" then r_ge8 <= '1'; end if;
                  elsif r_part = '0' then
                    r_part <= '1';
                  else
                    r_part <= '0';
                    r_n    <= r_n + 1;
                    if r_n(REPEAT_N_BITS downto 3) /= 0 or r_n(2 downto 0) = "111" then r_ge8 <= '1'; end if;
                  end if;
                else
                  c_do := false;                   -- 違う束: まとめを閉じてから、この束を最初から
                  shift_pw := false;
                  if not cbusy then st <= S_DR; end if;   -- 閉じ役が動いている間は待つ
                end if;
              elsif r_hcnt = 0 then
                h0_sig <= sig_new; h0_b <= g(23 downto 0); h0_l <= to_unsigned(l1, 2);
                r_hcnt <= to_unsigned(1, 2);
              elsif r_hcnt = 1 then
                if eq0 then                        -- B = 1: H0 の束の繰り返しが 1 回
                  r_pat  <= '1';
                  r_b2   <= '0';
                  r_part <= '0';
                  r_n    <= to_unsigned(1, REPEAT_N_BITS + 1);
                  r_hcnt <= (others => '0');
                else
                  h1_sig <= sig_new; h1_b <= g(23 downto 0); h1_l <= to_unsigned(l1, 2);
                  r_hcnt <= to_unsigned(2, 2);
                end if;
              elsif cbusy and (eq1 or not eq0) then
                c_do := false;                     -- 古い方の束を拍に置く場合は、閉じ役が終わるまで待つ
                shift_pw := false;
              else
                if eq1 then                        -- B = 1: 古い方（H0）は拍に置き、H1 の束の繰り返しが 1 回
                  p_en := true; p_g(23 downto 0) := h0_b; p_len := to_integer(h0_l); p_need := p_len; p_pos := pw(1);
                  h0_sig <= h1_sig; h0_b <= h1_b; h0_l <= h1_l;
                  r_pat  <= '1';
                  r_b2   <= '0';
                  r_part <= '0';
                  r_n    <= to_unsigned(1, REPEAT_N_BITS + 1);
                  r_hcnt <= (others => '0');
                elsif eq0 then                     -- B = 2: H0・H1 の繰り返し。今の束が 1 つ目に一致
                  r_pat  <= '1';
                  r_b2   <= '1';
                  r_part <= '1';
                  r_n    <= (others => '0');
                  r_hcnt <= (others => '0');
                else                               -- 古い方（H0）を拍に置き、今の束を新しい H1 に
                  p_en := true; p_g(23 downto 0) := h0_b; p_len := to_integer(h0_l); p_need := p_len; p_pos := pw(1);
                  h0_sig <= h1_sig; h0_b <= h1_b; h0_l <= h1_l;
                  h1_sig <= sig_new; h1_b <= g(23 downto 0); h1_l <= to_unsigned(l1, 2);
                end if;
              end if;
            else
              if e_ev = '1' then
                p_en := true; p_g := g; p_len := glen; p_need := glen; p_pos := now_t; p_kf := e_resync; p_tf := '0';
              end if;
              c_do := true; c_ev := e_ev = '1';
            end if;
          end if;

        when S_TICKF =>
          -- tick: 作りかけの拍を出す。last_flush が前のサンプルになる
          flush_cur;
          to_due  <= tick_r;
          due     <= '0';
          flushed_acc <= '0';
          tick_dr <= '0';
          st      <= S_NORMAL;

        when S_TW0 =>
          -- tick の WAIT の時間 = 前のサンプル（now - 1）− 直前の出来事の時刻
          step_reg  <= now_t - 1 - lastt;
          tick_wait <= '1';
          st        <= S_SW1;

        when S_SD =>
          d_reg <= now_t - lastt;
          st    <= S_SC1;

        when S_SC1 =>
          dm1_reg <= d_reg - 1;
          if e_usev = '1' then
            if d_reg > LV_U then gt_r <= '1'; else gt_r <= '0'; end if;
          else
            if d_reg > LT_U then gt_r <= '1'; else gt_r <= '0'; end if;
          end if;
          if d_reg(47) = '1' then
            overrun_r <= '1';                         -- 間隔が 2^47 サンプル以上: 時刻の数えが信用できない
          end if;
          tick_wait <= '0';
          st        <= S_SC2;

        when S_SC2 =>
          if gt_r = '1' then
            if dm1_reg > WM_U then step_reg <= WM_U; else step_reg <= dm1_reg; end if;
            st <= S_SW1;
          else
            st <= S_SG1;
          end if;

        when S_SW1 =>
          -- WAIT の準備: P = 時間、新しい時刻
          pay_reg <= step_reg(40 downto 0);
          newt_reg <= lastt + step_reg;
          if tick_wait = '0' then
            d_reg <= d_reg - step_reg;
          end if;
          st <= S_SW2;

        when S_SW2 =>
          nt_reg <= tail_count(pay_reg);
          st     <= S_SW3;

        when S_SW3 =>
          -- WAIT の束: 頭 0x07（種類 WAIT、間隔の欄 7）+ 尾（T1 は特殊の印）
          p_en := true;
          p_g  := special_bytes(std_logic_vector(to_unsigned(LA_TOK_KIND_WAIT * 8 + LA_TOK_DELTA_EXT, 8)), pay_reg, nt_reg);
          p_len := 1 + nt_reg; p_need := p_len; p_pos := newt_reg; p_kf := e_resync and not tick_wait; p_tf := '0';
          lastt <= newt_reg;
          if tick_wait = '1' then
            dsm_n  := to_unsigned(1, 14);
            fast_t_r <= '1';
            fast_v_r <= '1';
            pcnt   <= to_unsigned(8192 - 6, 13);
            st     <= S_TICKF;
          else
            st <= S_SC1;
          end if;

        when S_SG1 =>
          -- 出来事の束の準備（間隔 d_reg は上限以下）
          if e_usev = '1' then
            p := d_reg(24 downto 0) & unsigned(e_pl);
            direct_r <= '0';
          elsif d_reg <= LA_TOK_DELTA_DIRECT_MAX then
            p := (others => '0');
            direct_r <= '1';
          else
            tmp48 := d_reg - LA_TOK_DELTA_EXT;
            p := tmp48(40 downto 0);
            direct_r <= '0';
          end if;
          pay_reg <= p;
          st <= S_SG2;

        when S_SG2 =>
          if e_usev = '1' then
            nt_reg <= tail_count(pay_reg);
            if tail_count(pay_reg) < LA_TOK_VALUE_MIN_TAILS then nt_reg <= LA_TOK_VALUE_MIN_TAILS; end if;
          elsif direct_r = '1' then
            nt_reg <= 0;
          else
            nt_reg <= tail_count(pay_reg);
          end if;
          st <= S_SG3;

        when S_SG3 =>
          g := (others => '0');
          ntl := nt_reg;
          if e_usev = '1' then
            g(7 downto 0) := std_logic_vector(to_unsigned(LA_TOK_KIND_VALUE * 8 + LA_TOK_DELTA_EXT, 8));
            g(15 downto 8) := "11" & std_logic_vector(pay_reg(5 downto 0));
          elsif direct_r = '1' then
            g(7 downto 0) := '0' & c0 & std_logic_vector(d_reg(2 downto 0));
          else
            g(7 downto 0) := '0' & c0 & "111";
            g(15 downto 8) := "10" & std_logic_vector(pay_reg(5 downto 0));
          end if;
          for k in 2 to 6 loop
            if k <= ntl then
              g(8 * k + 7 downto 8 * k) := '1' & std_logic_vector(pay_reg(LA_TOK_T1_BITS + LA_TOK_TAIL_BITS * (k - 2) + 6 downto
                                                                        LA_TOK_T1_BITS + LA_TOK_TAIL_BITS * (k - 2)));
            end if;
          end loop;
          glen := 1 + ntl;
          if e_usev = '0' then
            if e_nt >= 2 then
              g(8 * glen + 7 downto 8 * glen) := '0' & c1 & "000";
              glen := glen + 1;
            end if;
            if e_nt >= 3 then
              g(8 * glen + 7 downto 8 * glen) := '0' & c2 & "000";
              glen := glen + 1;
            end if;
          end if;
          p_en := true; p_g := g; p_len := glen; p_need := glen; p_pos := now_t; p_kf := e_resync; p_tf := '0';
          c_do := true; c_ev := true;
          st <= S_NORMAL;

        when S_DR =>
          -- 束のまとめを閉じる（拍に置く）。繰り返しを開いていれば、REPEAT にして短くなるかを決める（S_RC1・S_RC2）。短くならないとき、
          -- 繰り返しを開いていないとき（持っている束だけ）、N = 0 の B = 2（3 つの束）は、元の束を順に置く（S_RL）
          if r_pat = '1' and r_n /= 0 then
            pay_reg <= resize(r_n - 1, 40) & r_b2;           -- P = (B − 1) + 2 (N − 1)
            st      <= S_RC1;
          else
            if r_pat = '1' then
              rl_m  <= to_unsigned(3, 3);
            else
              rl_m  <= resize(r_hcnt, 3);
            end if;
            rl_b2 <= '1';
            rl_k  <= (others => '0');
            st    <= S_RL;
          end if;

        when S_RC1 =>
          nt_reg <= tail_count(pay_reg);
          st     <= S_RC2;

        when S_RC2 =>
          -- 対象の B 個を N 回続けた分（N 回 × 対象のバイト数）が REPEAT（頭 + 尾）より長ければ REPEAT にする。N >= 8 なら必ず長い
          blen := to_integer(h0_l);
          if r_b2 = '1' then blen := blen + to_integer(h1_l); end if;
          if r_n(REPEAT_N_BITS downto 3) /= 0 then nsm := 8; else nsm := to_integer(r_n(3 downto 0)); end if;
          if 1 + nt_reg < nsm * blen then
            st <= S_RC3;
          else
            mm := (nsm + 1) * (1 + b2i) + parti;              -- 対象 + N 回の繰り返し（+ 途中まで一致した 1 つ）
            if mm > 7 then mm := 7; end if;
            rl_m  <= to_unsigned(mm, 3);
            rl_k  <= (others => '0');
            rl_b2 <= r_b2;
            st    <= S_RL;
          end if;

        when S_RC3 =>
          -- 対象の束（拍に REPEAT と一緒に入る長さを確保して置く）
          blen := to_integer(h0_l);
          p_g(23 downto 0) := h0_b;
          if r_b2 = '1' then
            case to_integer(h0_l) is
              when 1      => p_g(31 downto 8)  := h1_b;
              when 2      => p_g(39 downto 16) := h1_b;
              when others => p_g(47 downto 24) := h1_b;
            end case;
            blen := blen + to_integer(h1_l);
          end if;
          p_en := true; p_len := blen; p_need := blen + 1 + nt_reg;
          if r_part = '1' then p_pos := pw(1); else p_pos := pw(0); end if;
          st <= S_RC4;

        when S_RC4 =>
          -- REPEAT の束: 頭 0x17（種類 REPEAT、間隔の欄 7）+ 尾（T1 は特殊の印）
          p_en := true;
          p_g  := special_bytes(std_logic_vector(to_unsigned(LA_TOK_KIND_REPEAT * 8 + LA_TOK_DELTA_EXT, 8)), pay_reg, nt_reg);
          p_len := 1 + nt_reg; p_need := p_len;
          if r_part = '1' then p_pos := pw(1); else p_pos := pw(0); end if;
          if r_part = '1' then
            st <= S_RP;
          else
            clear_r;
            st <= S_NORMAL;
          end if;

        when S_RP =>
          -- 途中まで一致した束（B = 2 の 1 つ目）をそのまま置く
          p_en := true; p_g(23 downto 0) := h0_b; p_len := to_integer(h0_l); p_need := p_len; p_pos := pw(0);
          clear_r;
          st <= S_NORMAL;

        when S_RL =>
          -- 元の束を 1 つずつ順に置く（H0・H1 の交互、または H0 だけ）。時刻は窓の古い方から
          if rl_b2 = '1' and rl_k(0) = '1' then
            p_g(23 downto 0) := h1_b;
            p_len := to_integer(h1_l);
          else
            p_g(23 downto 0) := h0_b;
            p_len := to_integer(h0_l);
          end if;
          p_en := true; p_need := p_len;
          idx := to_integer(rl_m - 1 - rl_k);
          if idx > 4 then idx := 4; end if;
          p_pos := pw(idx);
          if rl_k + 1 >= rl_m then
            clear_r;
            st <= S_NORMAL;
          else
            rl_k <= rl_k + 1;
          end if;

        when S_DONE =>
          null;
      end case;

      -- 閉じ役（REPEAT の頭 + 尾を作り、対象と一緒に置き、作りかけの拍を払う。渡された時の写しだけを使う）
      if REPEAT_EN then
        case cst is
          when C_IDLE =>
            null;
          when C_P =>
            pay_reg <= resize(sn_n - 1, 40) & sn_b2;           -- P = (B − 1) + 2 (N − 1)
            cst     <= C_N;
          when C_N =>
            nt_reg <= tail_count(pay_reg);
            cst    <= C_T;
          when C_T =>
            -- 対象の束（拍に REPEAT と一緒に入る長さを確保して置く）。N >= 8 なので REPEAT にして必ず短くなる
            blen := to_integer(sn_h0_l);
            p_g(23 downto 0) := sn_h0_b;
            if sn_b2 = '1' then
              case to_integer(sn_h0_l) is
                when 1      => p_g(31 downto 8)  := sn_h1_b;
                when 2      => p_g(39 downto 16) := sn_h1_b;
                when others => p_g(47 downto 24) := sn_h1_b;
              end case;
              blen := blen + to_integer(sn_h1_l);
            end if;
            p_en := true; cp := true; p_len := blen; p_need := blen + 1 + nt_reg; p_pos := sn_pos_a;
            cst <= C_R;
          when C_R =>
            p_en := true; cp := true;
            p_g  := special_bytes(std_logic_vector(to_unsigned(LA_TOK_KIND_REPEAT * 8 + LA_TOK_DELTA_EXT, 8)), pay_reg, nt_reg);
            p_len := 1 + nt_reg; p_need := p_len; p_pos := sn_pos_a;
            if sn_part = '1' then cst <= C_PART; else cst <= C_F; end if;
          when C_PART =>
            p_en := true; cp := true;
            p_g(23 downto 0) := sn_h0_b; p_len := to_integer(sn_h0_l); p_need := p_len; p_pos := sn_pos_b;
            cst <= C_F;
          when C_F =>
            flush_cur;                             -- tick: 作りかけの拍を払う（数えは渡した時に数え直し済み）
            cst <= C_IDLE;
        end case;
      end if;

      -- 拍に置く要求（束を置くのはこの 1 か所）: 前の段。fill（影）で「先に今の拍を出すか・どこから置くか・満ちたか」を決めて、
      -- 詰めの段の要求レジスタ rq_* に入れる（データの移動は次の周期）。fl_now（拍が出た）はここで分かるので、tick の数えは遅れない
      rq_v <= '0';
      if p_en then
        bfill := to_integer(fill);
        if p_tf = '1' and bfill /= 0 then               -- 前のサンプルの tick の払いを兼ねる（出来事の束は新しい拍の先頭）
          q_eb := true;
          q_off := 0;
          q_pad := 16 - bfill;
        elsif bfill + p_need > 16 then                   -- 入らない: 残りを NOP で埋めて出し、新しい拍の先頭から
          q_eb := true;
          q_off := 0;
          q_pad := 16 - bfill;
          if not cp then fl_now := true; end if;
        else
          q_eb := false;
          q_off := bfill;
          q_pad := 0;
        end if;
        q_full := q_off + p_len = 16;
        rq_v     <= '1';
        rq_flush <= '0';
        if q_eb then rq_eb <= '1'; else rq_eb <= '0'; end if;
        if q_full then rq_full <= '1'; else rq_full <= '0'; end if;
        rq_pad   <= to_unsigned(q_pad, 5);
        rq_off   <= to_unsigned(q_off, 5);
        rq_len   <= to_unsigned(p_len, 5);
        rq_g     <= p_g;
        rq_pos   <= p_pos;
        rq_kf    <= p_kf;
        if q_full then
          fill   <= (others => '0');
          if not cp then fl_now := true; end if;
        else
          fill   <= to_unsigned(q_off + p_len, 5);
        end if;
      elsif f_req then
        rq_v     <= '1';
        rq_flush <= '1';
        rq_pad   <= to_unsigned(f_pad, 5);
      end if;

      -- 拍の詰めの段: 1 つ前の周期の要求どおりにデータを動かす（判断は要求の中にある）
      if rq_v = '1' then
        if rq_flush = '1' then
          emit(cur, cur_pos, cur_kf);
          pad_r  <= pad_r + resize(rq_pad, 32);
          cur    <= NOPS;
          cur_kf <= '0';
        else
          if rq_eb = '1' then
            emit(cur, cur_pos, cur_kf);
            pad_r  <= pad_r + resize(rq_pad, 32);
            sb     := NOPS;
          else
            sb     := cur;
          end if;
          if rq_eb = '1' or rq_off = 0 then sk := rq_kf; else sk := cur_kf; end if;
          for j in 0 to 15 loop                           -- バイト j に入るのは、束の j − off 番目（束の外は今の拍のまま）
            if j >= to_integer(rq_off) and j - to_integer(rq_off) < to_integer(rq_len) then
              sb(8 * j + 7 downto 8 * j) := rq_g(8 * (j - to_integer(rq_off)) + 7 downto 8 * (j - to_integer(rq_off)));
            end if;
          end loop;
          if rq_full = '1' then
            emit(sb, rq_pos, sk);
            cur    <= NOPS;
            cur_kf <= '0';
          else
            cur     <= sb;
            cur_pos <= rq_pos;
            cur_kf  <= sk;
          end if;
        end if;
      end if;
      -- 項目の処理が終わった周期の更新。終わらない周期（まとめを払うだけ等）に拍が出たら、終わる周期まで覚えておく
      if c_do then
        complete(c_ev);
      elsif fl_now then
        flushed_acc <= '1';
      end if;
      -- 出来事の時刻の窓
      if REPEAT_EN and shift_pw then
        pw(0) <= now_t;
        for i in 1 to 4 loop
          pw(i) <= pw(i - 1);
        end loop;
      end if;

      -- 溜めの先頭の登録: 取り出した（または空の）周期に次の項目を読む
      if hd_v = '0' or pop then
        if f_wr /= f_rd then
          hd_r <= fifo(to_integer(f_rd(3 downto 0)));
          hd_v <= '1';
          f_rd <= f_rd + 1;
        else
          hd_v <= '0';
        end if;
      end if;

      -- 開始: 前の流れの残り（溜め・作りかけ）を捨てる
      if run = '1' and run_prev = '0' then
        tick_r      <= tick;
        st          <= S_NORMAL;
        cst         <= C_IDLE;
        f_wr        <= (others => '0');
        f_rd        <= (others => '0');
        cur         <= NOPS;
        fill        <= (others => '0');
        rq_v        <= '0';
        cur_pos     <= (others => '0');
        cur_kf      <= '0';
        now_t       <= (others => '0');
        lastt       <= (others => '0');
        dsm_n       := (others => '0');
        fast_t_r    <= '1';
        fast_v_r    <= '1';
        rem_nz      <= '0';
        pcnt        <= to_unsigned(8192 - 7, 13);
        hd_v        <= '0';
        if tick /= 0 then to_due <= tick + 1; else to_due <= (others => '0'); end if;
        due         <= '0';
        pre_done    <= '0';
        flushed_acc <= '0';
        tick_wait   <= '0';
        overrun_r   <= '0';
        pad_r       <= (others => '0');
        kfr_r       <= (others => '0');
        if REPEAT_EN then
          rep_r     <= repeat;
          tick_dr   <= '0';
          clear_r;
        end if;
      end if;
      dsmall <= dsm_n;
      if rst = '1' then
        valid_r   <= '0';
        rq_v      <= '0';
        hd_v      <= '0';
        st        <= S_DONE;
        cst       <= C_IDLE;
        f_wr      <= (others => '0');
        f_rd      <= (others => '0');
        overrun_r <= '0';
      end if;
    end if;
  end process;
end architecture;
