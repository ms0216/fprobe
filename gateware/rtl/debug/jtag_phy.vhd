-- JTAG の物理層: TCK / TMS / TDI を駆動し TDO を取る。命令 I/F は swd_phy と同じで、JTAG-DP の違いをここで吸収する
-- （dap_processor の転送処理とクロストリガの割り込みを SWD と共通にするため）。
-- 仕様: docs/superpowers/specs/2026-10-16-m5-jtag-design.md §2、速さ（sys/2・ビットの間の空き 0）は
-- docs/superpowers/specs/2026-11-03-dap-line-rate-design.md
--
-- 前提: TAP は Run-Test/Idle にいる（JTAG_Sequence でホストが入れる）。チェーン上の位置は dr_before / dr_after
-- （選んだ装置より TDO 側 / TDI 側の装置数）と ir_before / ir_after（その IR の合計ビット数）、ir_len で与える。
--
-- ビットのタイミング: 1 ビット = 2 × half_period 周期（half_period ≥ 1。1 で TCK = sys / 2）: Low 相 half_period（TCK = 0）、
-- High 相 half_period（TCK = 1）。区間の境目・命令の境目・命令の間も同じ（空きなし）。命令が無ければ TCK = 1 のまま待つ。
-- High 相の最後の周期の辺で次のビットの TDI を出して TCK を下げる。次のビット（区間・番号）は前のビットの立ち上がりの辺で
-- 決めておく。TMS は Low 相の中ほどで変える（TCK の立ち下がり・TDI の変化と同じ辺に変えない）: half_period ≥ 2 は立ち下がりの
-- floor(half_period / 2) 周期後の辺。half_period = 1 は Low 相が 1 周期なので、tms は立ち下がりの辺で変え、tms_lead（その周期の
-- 前半の TMS = 前の値）を出す。ボードが ODDR（前半 tms_lead・後半 tms）で出せば TMS は立ち下がりの半周期（sys の）後に変わる。
-- ODDR の無いボードは tms だけを出す（half_period = 1 では TMS は立ち下がりと同じ辺で変わる）。
-- TMS を TCK の立ち下がり・TDI の変化と同じ辺に変えない理由: 実機（Primer 20K → Raspberry Pi 2B、2026-10-29）で、TCK の
-- 立ち下がりで TMS と TDI が同時に 0 → 1 になると、そのビットが化けた（周波数 100 kHz〜1 MHz に依らず毎回。片方だけの変化では
-- 化けない）。IR の DPACC / APACC / ABORT と、データの上位 2 ビットが 10 の DR の書き込みの最後のビットがこれに当たっていた。
-- 出力（tck / tms / tdi）はこの entity の FF（tck_r / tms_r / tdi_r）から直接出す（組み合わせ回路を通さない）。
--
-- TDO の取り込み: 次の立ち下がりの直前（次のビットの TCK を下げる辺の 1 周期前の辺のピン = din）。TAP は立ち上がりで
-- TMS / TDI を取り込み、立ち下がりで TDO を変えて次の立ち下がりまで保つので、TDO は立ち下がりから 1 ビットの長さ
-- （2 × half_period 周期）の 1 周期手前まで遅れて（配線の往復 + ターゲットの出力遅延）届いても正しく読める（Low 相の終わりで
-- 取ると半周期しか待てず、速い TCK で化けた。R16）。それより遅れて届く TDO のために、取り込みをさらに sample_delay 周期
-- 遅らせられる（遅延線。次のビット以降の途中で前のビットの値を取る）。取り込みはビットの順に来るので、DR の本体は TDO 用の
-- シフトレジスタに、シーケンスは取り込んだ数の位置に入れる。遅延線の各段は、そのビットがどの命令のどのスキャンの何番目か
-- （先頭・最後・応答を返すか）を運ぶ。TDO は TDI を出すシフトレジスタとは別（遅れた取り込みが次のビットの TDI を乱さない）。
-- 仕様: docs/superpowers/specs/2026-10-23-jtag-tdo-sample-design.md
--
-- 応答（rsp_valid）: その命令の最後の取り込み（シーケンスの最後のビット、転送の最後のスキャンの本体の最後のビット）の
-- 次の周期。線の上ではその命令の最後のビットの後に次の命令が始まっていることがある（取り込みの遅れの分）。応答の順は命令の
-- 順。rsp_* は rsp_valid の周期から次の rsp_valid まで保つ。OP_PINS は前の命令の応答が済んでから受け、受けた辺で終える。
--
-- 命令:
--   OP_SEQ_OUT  SWJ_Sequence: cmd_count ビットを cmd_data の下位から TMS に出す（TDI = 1）
--   OP_SEQ_IN   JTAG_Sequence: cmd_count ビット、TMS = cmd_request(0)、TDI = cmd_data、TDO を rsp_data の下位から
--   OP_TRANSFER SWD と同じ意味の転送（cmd_request: bit0 APnDP, bit1 RnW, bit2-3 A[3:2]）を JTAG-DP で行う:
--     AP → APACC。DP の A = 0: 読みは IDCODE 命令（32 ビット）、書きは ABORT。その他の DP → DPACC。
--     JTAG-DP は読み値が次のスキャンで返る。AP の読みは SWD と同じくポストのまま返し（dap_processor が RDBUFF で
--     回収する）、DP の読み（RDBUFF 以外）は続けて RDBUFF をスキャンして値を返す（SWD-DP と同じ見え方）。
--     cmd_count = COUNT_POSTED（2）の DP の読みは、続く RDBUFF をスキャンせず、その 1 スキャンの取り込み値（1 つ前の
--     転送の結果）を返す（ポストの DP の読み。読んだ DP のレジスタの値は次のスキャンの取り込みで返る）。dap_sampler が
--     「CTRL/STAT の読みのスキャンで前の AP の読みの値を受け取り、次の AP のスキャンで CTRL/STAT を受け取る」のに使う
--     （docs/superpowers/specs/2026-11-01-stop-precision-design.md §3.6）。ホストの転送は cmd_count = 1。
--     ACK: JTAG の OK/FAULT（010）→ "001"、WAIT（001）→ "010"、それ以外 → "111"（プロトコル異常）。
--     IDCODE・ABORT のスキャンに ACK は無いので "001"。
--     IR は前回と同じ（値も装置の位置も）なら省く。
--   OP_PINS     cmd_data(8/9/10) = 1 なら TCK / TMS / TDI を cmd_data(0/1/2) に。rsp_data(3..0) = TDO, TDI, TMS, TCK
--
-- 続けて受ける: 命令の最後のビットの High 相の最後の周期に cmd_valid なら、その周期に受け（cmd_ready = 1）、同じ辺で次の命令の
--   最初のビットを始める。受理は前の命令の応答より前になりうる（観測する側は受理と応答の対を高々 2 つ重ねて数える）。
-- cmd_cond（条件つきの命令）: '1' の命令は、直前の命令が OK の転送（ACK が OK/FAULT。IDCODE・ABORT は OK）か転送でない命令の
--   ときだけ受ける（swd_phy と同じ。dap_link が前の転送の結果を待たずに次を出しておくため）。
--
-- 線上の点の時刻（rsp_time。docs/superpowers/specs/2026-11-01-time-accuracy-design.md R5）: OP_TRANSFER はその転送の DR スキャン
--   （DP の読みで続けて RDBUFF をスキャンするときは最初のスキャン）の Update-DR を出る TCK の立ち上がり（tck が 1 になった
--   最初の周期）の now。JTAG-DP はここで AP / DP のアクセスを始める。IR を省いたか・続く RDBUFF のスキャンに依らない。
--   ほかの命令は命令を受理した周期の now。
library ieee;
use ieee.std_logic_1164.all;
use ieee.numeric_std.all;
use work.util_pkg.all;
use work.dap_pkg.all;

entity jtag_phy is
  port (
    clk         : in  std_logic;
    rst         : in  std_logic;
    -- 設定（命令の実行中は変えないこと）
    enable      : in  std_logic;
    half_period : in  unsigned(15 downto 0);                -- TCK の半周期（clk 周期数、1 以上）
    idle_cycles : in  natural range 0 to 255;
    dr_before   : in  natural range 0 to 7;
    dr_after    : in  natural range 0 to 7;
    ir_before   : in  natural range 0 to 63;
    ir_after    : in  natural range 0 to 63;
    ir_len      : in  natural range 1 to 32;
    sample_delay : in natural range 0 to 7;                 -- TDO の取り込みを立ち下がりの直前から遅らせる周期数
    now         : in  unsigned(63 downto 0);                -- タイムベース（rsp_time に取る）
    -- 命令（swd_phy と同じ）
    cmd_valid   : in  std_logic;
    cmd_ready   : out std_logic;
    cmd_cond    : in  std_logic := '0';                     -- 直前の命令が OK のときだけ受ける（先頭のコメント）
    cmd_op      : in  std_logic_vector(1 downto 0);
    cmd_count   : in  natural range 1 to 64;
    cmd_request : in  std_logic_vector(3 downto 0);
    cmd_data    : in  std_logic_vector(63 downto 0);
    rsp_valid   : out std_logic;
    rsp_ack     : out std_logic_vector(2 downto 0);
    rsp_data    : out std_logic_vector(63 downto 0);
    rsp_time    : out unsigned(63 downto 0);                -- 線上の点の時刻（先頭のコメント）
    -- ピン
    tck         : out std_logic;
    tms         : out std_logic;
    tms_lead    : out std_logic;                            -- その周期の前半の TMS（ODDR のボード用。先頭のコメント）
    tdi         : out std_logic;
    tdo         : in  std_logic
  );
end entity;

architecture rtl of jtag_phy is
  constant IR_ABORT    : std_logic_vector(3 downto 0) := x"8";
  constant IR_DPACC    : std_logic_vector(3 downto 0) := x"A";
  constant IR_APACC    : std_logic_vector(3 downto 0) := x"B";
  constant IR_IDCODE   : std_logic_vector(3 downto 0) := x"E";
  constant RDBUFF_READ : std_logic_vector(34 downto 0) := x"00000000" & "111";   -- DPACC、A = 0xC、読み

  -- ビット列は区間の並びとして 1 ビットずつ作る（ビット列全体を組み立てない）:
  --   IR スキャン: G_IR_HEAD（TMS 1,1,0,0）→ G_IR_BEFORE（1）→ G_IR_VALUE → G_IR_AFTER（1）→ G_IR_TAIL（TMS 1,0）
  --   DR スキャン: G_DR_HEAD（TMS 1,0,0）→ G_DR_BEFORE（0）→ G_DR_BODY（本体、TDO を取る）→ G_DR_AFTER（0）→ G_DR_TAIL
  --   シフトする区間（BEFORE / VALUE / BODY / AFTER）の最後のビットだけ TMS = 1（Exit1 へ）。G_IDLE は TMS = 0。
  --   G_SEQ はシーケンス命令（cmd_count ビット）。長さ 0 の区間（BEFORE / AFTER）は飛ばす。G_END = 命令のビットを出し終えた。
  type seg_t is (G_NONE, G_SEQ, G_IR_HEAD, G_IR_BEFORE, G_IR_VALUE, G_IR_AFTER, G_IR_TAIL,
                 G_DR_HEAD, G_DR_BEFORE, G_DR_BODY, G_DR_AFTER, G_DR_TAIL, G_IDLE, G_END);
  type half_t is (H_IDLE, H_LOW, H_HIGH);

  signal half      : half_t := H_IDLE;
  signal seg       : seg_t := G_NONE;                          -- 今のビット（H_LOW）/ 次のビット（立ち上がりの辺の後）の区間
  signal timer     : unsigned(15 downto 0) := (others => '0');
  signal expired   : boolean := false;                         -- 相の最後の周期（swd_phy と同じ）
  signal hp_one    : boolean;                                  -- half_period = 1
  signal index     : natural range 0 to 255 := 0;              -- 区間の中のビット番号
  signal seg_len   : natural range 0 to 255 := 0;              -- 今の区間の長さ（区間に入るときに決める）
  signal shifting  : natural range 0 to 255 := 0;              -- シフトの残りビット数（1 なら最後 = TMS 1）
  signal din       : std_logic := '1';
  signal tck_r, tms_r, tdi_r : std_logic := '1';
  signal tms_prev  : std_logic := '1';                         -- 1 周期前の tms_r（half_period = 1 の tms_lead）
  signal tms_next  : std_logic := '1';                         -- Low 相の中ほどで tms_r に移す（half_period ≥ 2）
  -- シーケンス
  signal seq_op    : std_logic_vector(1 downto 0) := OP_SEQ_OUT;
  signal seq_tms   : std_logic := '0';
  signal seq_out   : std_logic_vector(63 downto 0) := (others => '1');   -- 出すビット（下位から、毎ビットずらす）
  -- 転送（線を作る側）
  signal xfer      : boolean := false;                                   -- 今（か直前）の命令は転送
  signal ir        : std_logic_vector(3 downto 0) := (others => '0');
  signal ir_valid  : boolean := false;                                   -- TAP の IR が ir_cur になっている
  signal ir_cur    : std_logic_vector(3 downto 0) := (others => '0');
  -- IR を入れたときの装置の位置（装置が変われば同じ IR の値でも入れ直す）
  signal cur_before : natural range 0 to 63 := 0;
  signal cur_after  : natural range 0 to 63 := 0;
  signal cur_len    : natural range 1 to 32 := 4;
  signal dr_shift  : std_logic_vector(34 downto 0) := (others => '0');   -- 本体の出すビット（TDI。毎ビットずらす）
  signal short_dr  : boolean := false;                                   -- IDCODE（32 ビット、ヘッダなし）
  signal dp_read   : boolean := false;                                   -- このスキャンの後に RDBUFF をもう 1 回スキャンする
  signal marked    : boolean := false;                                   -- この転送の点を取った（続く RDBUFF のスキャンでは取らない）
  signal mark      : std_logic := '0';                                   -- 前の周期に Update-DR を出る TCK を上げた
  signal mark_par  : std_logic := '0';                                   -- その命令の cmd_par
  signal cmd_par   : std_logic := '0';                                   -- 命令ごとに反転（点の 2 面の選び）
  signal scan_par  : std_logic := '0';                                   -- スキャンごとに反転（ACK がどのスキャンのものか）
  type points_t is array (0 to 1) of unsigned(63 downto 0);
  signal point     : points_t := (others => (others => '0'));            -- 命令ごとの rsp_time（cmd_par で選ぶ）
  signal owed      : natural range 0 to 2 := 0;                          -- 受けて応答をまだ返していない命令の数
  -- 遅らせた取り込みの遅延線: take_base は前の周期の辺（次の立ち下がりの 1 周期前の辺）で予約したビット、段 k は k + 1 周期前
  type take_t is record
    valid  : std_logic;
    seq    : std_logic;                                                  -- '1' = シーケンス、'0' = DR の本体
    first  : std_logic;                                                  -- 区間の最初のビット
    last   : std_logic;                                                  -- 区間の最後のビット
    short  : std_logic;                                                  -- IDCODE のスキャン（ACK なし）
    abort  : std_logic;                                                  -- ABORT のスキャン（ACK を見ない）
    dpfirst : std_logic;                                                 -- RDBUFF が続くかもしれない DP の読みの最初のスキャン
    par    : std_logic;                                                  -- 命令の cmd_par
    spar   : std_logic;                                                  -- スキャンの scan_par
  end record;
  constant TAKE_NONE : take_t := (others => '0');
  type take_line_t is array (0 to 6) of take_t;
  signal take_line : take_line_t := (others => TAKE_NONE);
  signal take_base : take_t := TAKE_NONE;
  signal cur_take  : take_t := TAKE_NONE;                                -- 今のビットの取り込み（立ち上がりの辺で決める）
  -- 取り込む側
  signal seq_in    : std_logic_vector(63 downto 0) := (others => '0');   -- 取り込んだ TDO（取り込んだ数 seq_taken の位置へ）
  signal seq_taken : natural range 0 to 63 := 0;
  signal dr_in     : std_logic_vector(34 downto 0) := (others => '0');   -- 本体の取った TDO（上からずらし入れる）
  signal ack_cap   : std_logic_vector(2 downto 0) := (others => '0');    -- スキャンの本体の最初の 3 ビット（JTAG の ACK）
  signal cap_count : natural range 0 to 3 := 0;                          -- スキャンの本体の取り込んだ数（3 で飽和）
  signal cap_spar  : std_logic := '0';                                   -- 取り込み中のスキャンの scan_par
  signal resp_go   : boolean := false;                                   -- 前の周期に命令の最後の取り込みをした
  signal resp_take : take_t := TAKE_NONE;                                -- その最後のビット
  signal cap_pend  : boolean := false;                                   -- 結果（p_*）を控えている
  signal p_ack     : std_logic_vector(2 downto 0) := (others => '0');
  signal p_data    : std_logic_vector(63 downto 0) := (others => '0');
  signal p_par     : std_logic := '0';                                   -- その命令の cmd_par（点の面）
  signal wire_cnt  : natural range 0 to 2 := 0;                          -- ビットを出し終えて応答をまだ返していない命令の数
  signal done      : std_logic := '0';
  signal r_ack     : std_logic_vector(2 downto 0) := (others => '0');
  signal r_data    : std_logic_vector(63 downto 0) := (others => '0');
  signal r_time    : unsigned(63 downto 0) := (others => '0');

  function map_ack(a : std_logic_vector(2 downto 0)) return std_logic_vector is
  begin
    case a is
      when "010"  => return "001";                                       -- OK / FAULT
      when "001"  => return "010";                                       -- WAIT
      when others => return "111";
    end case;
  end function;

  function successor(s : seg_t) return seg_t is
  begin
    case s is
      when G_IR_HEAD   => return G_IR_BEFORE;
      when G_IR_BEFORE => return G_IR_VALUE;
      when G_IR_VALUE  => return G_IR_AFTER;
      when G_IR_AFTER  => return G_IR_TAIL;
      when G_IR_TAIL   => return G_DR_HEAD;
      when G_DR_HEAD   => return G_DR_BEFORE;
      when G_DR_BEFORE => return G_DR_BODY;
      when G_DR_BODY   => return G_DR_AFTER;
      when G_DR_AFTER  => return G_DR_TAIL;
      when others      => return G_END;                             -- G_DR_TAIL / G_IDLE / G_SEQ は手順で決める
    end case;
  end function;

  signal ack_ready : boolean;                                        -- 今のスキャンの ACK を取り込み終えた
  signal ok_cur    : boolean;                                        -- 直前（か今終わる）命令が OK（cmd_cond）
  signal ending    : boolean;                                        -- この周期の辺で命令の最後のビットの High 相が終わる
  signal accept    : boolean;                                        -- この周期の辺で命令を受ける
  signal ir_need   : boolean;                                        -- 受ける転送は IR のスキャンから
  signal cmd_ir    : std_logic_vector(3 downto 0);                   -- 受ける転送の IR
begin
  hp_one    <= half_period = 1;
  ack_ready <= cap_spar = scan_par and cap_count = 3;
  ok_cur    <= not xfer or short_dr or ir = IR_ABORT or (ack_ready and map_ack(ack_cap) = "001");
  ending    <= half = H_HIGH and expired and seg = G_END;
  accept    <= enable = '1' and rst = '0' and cmd_valid = '1' and (cmd_cond = '0' or ok_cur) and owed <= 1 and
               ((half = H_IDLE and (cmd_op /= OP_PINS or owed = 0)) or (ending and cmd_op /= OP_PINS));

  process (all)
  begin
    if cmd_request(0) = '1' then
      cmd_ir <= IR_APACC;
    elsif cmd_request(3 downto 2) = "00" and cmd_request(1) = '1' then
      cmd_ir <= IR_IDCODE;
    elsif cmd_request(3 downto 2) = "00" then
      cmd_ir <= IR_ABORT;
    else
      cmd_ir <= IR_DPACC;
    end if;
  end process;
  ir_need <= not (ir_valid and ir_cur = cmd_ir and cur_before = ir_before and cur_after = ir_after and cur_len = ir_len);

  cmd_ready <= '1' when accept else '0';
  rsp_valid <= done;
  rsp_ack   <= r_ack;
  rsp_data  <= r_data;
  rsp_time  <= r_time;
  tck       <= tck_r;
  tms       <= tms_r;
  tms_lead  <= tms_prev when hp_one else tms_r;
  tdi       <= tdi_r;

  process (clk)
    -- 半周期の時計: 積み直す（half_period 周期後に expired）・1 つ進める
    procedure half_restart is
    begin
      timer   <= half_period - 1;
      expired <= hp_one;
    end procedure;
    procedure half_tick is
    begin
      timer   <= timer - 1;
      expired <= timer = 1;
    end procedure;

    -- 区間 s の長さ
    impure function length_of(s : seg_t) return natural is
    begin
      case s is
        when G_IR_HEAD   => return 4;
        when G_IR_BEFORE => return ir_before;
        when G_IR_VALUE  => return ir_len;
        when G_IR_AFTER  => return ir_after;
        when G_IR_TAIL | G_DR_TAIL => return 2;
        when G_DR_HEAD   => return 3;
        when G_DR_BEFORE => return dr_before;
        when G_DR_BODY   =>
          if short_dr then
            return 32;
          end if;
          return 35;
        when G_DR_AFTER  => return dr_after;
        when G_IDLE      => return idle_cycles;
        when G_NONE | G_END => return 0;
        when G_SEQ       => return 0;              -- 入らない（シーケンスの長さは命令を受けたときに seg_len へ直接）
      end case;
    end function;

    -- 長さ 0 の区間（BEFORE / AFTER）を飛ばした次の区間（その次は長さ 1 以上）
    impure function next_seg(s : seg_t) return seg_t is
      variable n : seg_t;
    begin
      n := successor(s);
      if (n = G_IR_BEFORE or n = G_IR_AFTER or n = G_DR_BEFORE or n = G_DR_AFTER) and length_of(n) = 0 then
        n := successor(n);
      end if;
      return n;
    end function;

    procedure enter(s : seg_t) is
    begin
      seg     <= s;
      index   <= 0;
      seg_len <= length_of(s);
    end procedure;

    -- TMS を出す（half_period ≥ 2 は Low 相の中ほど、1 は立ち下がりの辺）
    procedure put_tms(v : std_logic) is
    begin
      tms_next <= v;
      if hp_one then
        tms_r <= v;
      end if;
    end procedure;

    -- TCK を下げて Low 相を始める
    procedure fall is
    begin
      tck_r   <= '0';
      half    <= H_LOW;
      half_restart;
    end procedure;

    -- 区間 s の i ビット目の TDI を出し、TCK を下げる（TMS は put_tms）
    procedure emit(s : seg_t; i : natural) is
    begin
      case s is
        when G_SEQ =>
          if seq_op = OP_SEQ_OUT then
            put_tms(seq_out(0));
            tdi_r <= '1';
          else
            put_tms(seq_tms);
            tdi_r <= seq_out(0);
          end if;
        when G_IR_HEAD =>
          put_tms(to_sl(i < 2));
          tdi_r <= '1';
        when G_IR_BEFORE | G_IR_AFTER =>
          put_tms(to_sl(shifting = 1));
          tdi_r <= '1';                                                -- 他の装置は BYPASS
        when G_IR_VALUE =>
          put_tms(to_sl(shifting = 1));
          tdi_r <= '1';                                                -- 4 ビットを超える IR の上位は 1
          for k in 0 to 3 loop
            if k = i then
              tdi_r <= ir(k);
            end if;
          end loop;
        when G_DR_HEAD =>
          put_tms(to_sl(i = 0));
          tdi_r <= '0';
        when G_DR_BEFORE | G_DR_AFTER =>
          put_tms(to_sl(shifting = 1));
          tdi_r <= '0';
        when G_DR_BODY =>
          put_tms(to_sl(shifting = 1));
          tdi_r <= dr_shift(0);
        when G_IR_TAIL | G_DR_TAIL =>
          put_tms(to_sl(i = 0));                                         -- → Update、→ Idle
          tdi_r <= '1';
        when others =>                                                -- G_IDLE
          put_tms('0');
          tdi_r <= '1';
      end case;
      fall;
    end procedure;

    -- 命令を受けて始める（最初のビットをこの辺で出す。OP_PINS はこの辺で終える）
    procedure start_command is
      variable par : std_logic;
    begin
      par     := not cmd_par;
      cmd_par <= par;
      point(to_integer(unsigned'('0' & par))) <= now;                 -- 転送でなければこの値（転送は Update-DR の点で書き直す）
      marked  <= false;
      xfer    <= cmd_op = OP_TRANSFER;
      case cmd_op is
        when OP_PINS =>
          r_data <= (63 downto 4 => '0') & din & tdi_r & tms_r & tck_r;
          r_ack  <= (others => '0');
          r_time <= now;
          if cmd_data(8) = '1' then
            tck_r <= cmd_data(0);
          end if;
          if cmd_data(9) = '1' then
            tms_r <= cmd_data(1);
          end if;
          if cmd_data(10) = '1' then
            tdi_r <= cmd_data(2);
          end if;
          ir_valid <= false;
          done     <= '1';
          seg      <= G_NONE;
          half     <= H_IDLE;
        when OP_SEQ_OUT | OP_SEQ_IN =>
          seq_op   <= cmd_op;
          seq_tms  <= cmd_request(0);
          seq_out  <= cmd_data;
          seg      <= G_SEQ;
          index    <= 0;
          seg_len  <= cmd_count;
          ir_valid <= false;
          if cmd_op = OP_SEQ_OUT then
            put_tms(cmd_data(0));
            tdi_r <= '1';
          else
            put_tms(cmd_request(0));
            tdi_r <= cmd_data(0);
          end if;
          fall;
        when others =>                                                -- OP_TRANSFER
          dr_shift <= cmd_data(31 downto 0) & cmd_request(3 downto 1);
          ir       <= cmd_ir;
          short_dr <= cmd_ir = IR_IDCODE;
          dp_read  <= cmd_ir = IR_DPACC and cmd_request(1) = '1' and cmd_request(3 downto 2) /= "11" and
                      cmd_count /= COUNT_POSTED;
          index    <= 0;
          put_tms('1');                                               -- IR_HEAD・DR_HEAD の最初は TMS = 1
          if ir_need then
            ir_cur     <= cmd_ir;
            cur_before <= ir_before;
            cur_after  <= ir_after;
            cur_len    <= ir_len;
            ir_valid   <= true;
            seg        <= G_IR_HEAD;
            seg_len    <= 4;
            tdi_r      <= '1';
          else
            seg        <= G_DR_HEAD;
            seg_len    <= 3;
            tdi_r      <= '0';
          end if;
          fall;
      end case;
    end procedure;

    variable now_take, take : take_t;
    variable final, reply, wire_end : boolean;
  begin
    if rising_edge(clk) then
      din      <= tdo;
      done     <= '0';
      tms_prev <= tms_r;
      mark     <= '0';
      if mark = '1' then
        point(to_integer(unsigned'('0' & mark_par))) <= now;            -- TCK が 1 になった最初の周期
      end if;
      now_take := TAKE_NONE;                                            -- この辺で予約するビット（下で決める）
      wire_end := false;

      if rst = '1' or enable = '0' then
        half     <= H_IDLE;
        seg      <= G_NONE;
        tck_r    <= '1';
        tms_r    <= '1';
        tdi_r    <= '1';
        ir_valid <= false;
        xfer     <= false;
      else
        case half is
          when H_IDLE =>
            if accept then
              start_command;
            end if;

          when H_LOW =>
            if not hp_one and timer = half_period(15 downto 1) then
              tms_r <= tms_next;                                      -- Low 相の中ほど（half_period = 2 なら立ち下がりの 1 周期後）
            end if;
            if not expired then
              half_tick;
            elsif seg = G_DR_TAIL and index + 1 = seg_len and dp_read and not ack_ready then
              -- RDBUFF を続けるかを決める ACK がまだ取り込めていない（本体の後に 2 ビット以上あるので実際には起きない
              -- 保険）: 立ち上げずに待つ（TMS = 0 の Low 相が延びるだけ）
              null;
            else
              -- Low 相の終わり: TCK を上げる。このビットの取り込みを決め、次のビットを決める
              tck_r   <= '1';
              half    <= H_HIGH;
              half_restart;
              if seg = G_SEQ then
                now_take := ('1', '1', to_sl(index = 0), to_sl(index + 1 = seg_len), '0', '0', '0', cmd_par, scan_par);
                seq_out <= '1' & seq_out(63 downto 1);
              elsif seg = G_DR_BODY then
                now_take := ('1', '0', to_sl(index = 0), to_sl(index + 1 = seg_len), to_sl(short_dr), to_sl(ir = IR_ABORT),
                             to_sl(dp_read), cmd_par, scan_par);
                dr_shift <= '0' & dr_shift(34 downto 1);
              end if;
              cur_take <= now_take;
              if not hp_one then
                now_take := TAKE_NONE;                                -- 予約は High 相の最後の周期の頭の辺
              end if;
              if seg = G_IR_BEFORE or seg = G_IR_VALUE or seg = G_IR_AFTER or
                 seg = G_DR_BEFORE or seg = G_DR_BODY or seg = G_DR_AFTER then
                shifting <= shifting - 1;
              end if;
              if seg = G_DR_TAIL and index = 1 and not marked then
                mark     <= '1';                                      -- Update-DR → Run-Test/Idle の立ち上がり（線上の点）
                mark_par <= cmd_par;
                marked   <= true;
              end if;
              -- 次のビット
              if index + 1 < seg_len then
                index <= index + 1;
              else
                case seg is
                  when G_SEQ | G_IDLE =>
                    enter(G_END);
                  when G_DR_TAIL =>
                    if dp_read and ack_ready and map_ack(ack_cap) = "001" then
                      dp_read   <= false;                             -- DPACC RDBUFF の読みで値を受け取る
                      dr_shift  <= RDBUFF_READ;
                      scan_par  <= not scan_par;
                      enter(G_DR_HEAD);
                    elsif idle_cycles /= 0 then
                      enter(G_IDLE);
                    else
                      enter(G_END);
                    end if;
                  when G_IR_HEAD =>
                    shifting <= ir_before + ir_len + ir_after;
                    enter(next_seg(seg));
                  when G_DR_HEAD =>
                    if short_dr then
                      shifting <= dr_before + 32 + dr_after;
                    else
                      shifting <= dr_before + 35 + dr_after;
                    end if;
                    enter(next_seg(seg));
                  when G_IR_TAIL =>
                    scan_par <= not scan_par;
                    enter(next_seg(seg));
                  when others =>
                    enter(next_seg(seg));
                end case;
              end if;
            end if;

          when H_HIGH =>
            if not expired then
              half_tick;
              if timer = 1 then
                now_take := cur_take;                                 -- High 相の最後の周期の頭の辺で予約する
              end if;
            elsif seg = G_END then
              wire_end := true;                                       -- この命令のビットを出し終えた
              if accept then
                start_command;
              else
                seg  <= G_NONE;
                half <= H_IDLE;
              end if;
            else
              emit(seg, index);
            end if;
        end case;
      end if;

      -- スキャンの始まり（DR_HEAD に入る前の IR_TAIL の終わりか、受けた転送の最初）の scan_par: 受けた転送は IR か DR の
      -- スキャンから始まる。DR のスキャンが始まるとき（IR を省いたとき）は受けた辺で反転する
      if accept and cmd_op = OP_TRANSFER and not ir_need then
        scan_par <= not scan_par;
      end if;

      -- 取り込み: din は 1 周期前のピン。sample_delay = 0 は前の周期の辺で予約したビット（この周期の終わりに TCK が下がる
      -- = 立ち下がりの直前）、k > 0 はさらに k 周期後
      if sample_delay = 0 then
        take := take_base;
      else
        take := take_line(sample_delay - 1);
      end if;
      resp_go   <= false;
      resp_take <= take;
      if take.valid = '1' then
        if take.seq = '1' then
          if take.first = '1' then
            seq_in    <= (0 => din, others => '0');
            seq_taken <= 1;
          else
            for k in 0 to 63 loop
              if k = seq_taken then
                seq_in(k) <= din;
              end if;
            end loop;
            if seq_taken /= 63 then
              seq_taken <= seq_taken + 1;
            end if;
          end if;
          resp_go <= take.last = '1';
        else
          dr_in <= din & dr_in(34 downto 1);
          if take.first = '1' then
            ack_cap(0) <= din;
            cap_count  <= 1;
            cap_spar   <= take.spar;
          elsif cap_count < 3 then
            ack_cap(cap_count) <= din;
            cap_count <= cap_count + 1;
          end if;
          resp_go <= take.last = '1';
        end if;
      end if;

      -- 命令の結果（最後の取り込みの次の周期に作って控える）。RDBUFF が続く DP の読みの最初のスキャンは、ACK が OK なら
      -- 結果にしない（続くスキャンの結果を待つ）
      final := resp_go;
      if resp_go and resp_take.seq = '0' and resp_take.dpfirst = '1' and map_ack(ack_cap) = "001" then
        final := false;
      end if;
      if final then
        cap_pend <= true;
        p_par    <= resp_take.par;
        if resp_take.seq = '1' then
          p_ack  <= (others => '0');
          p_data <= seq_in;
        elsif resp_take.short = '1' then
          p_ack  <= "001";                                            -- IDCODE: ACK は無い（32 ビットずらし入れた）
          p_data <= x"00000000" & dr_in(34 downto 3);
        elsif resp_take.abort = '1' then
          p_ack  <= "001";                                            -- ABORT の取り込み値は不定（見ない）
          p_data <= (others => '0');
        else
          p_ack  <= map_ack(ack_cap);
          p_data <= x"00000000" & dr_in(34 downto 3);
        end if;
      end if;

      -- 応答: 結果があり、その命令のビットを出し終えた（wire_cnt）とき。どちらも命令の順に来る（次の命令の取り込みは、
      -- 前の命令のビットを出し終えた後に始まる）ので、控えは 1 つで足りる
      reply := cap_pend and wire_cnt /= 0;
      if reply then
        done     <= '1';
        r_ack    <= p_ack;
        r_data   <= p_data;
        r_time   <= point(to_integer(unsigned'('0' & p_par)));        -- 線の点（Update-DR）はビットを出し終える前に取った
        if not final then
          cap_pend <= false;
        end if;
      end if;
      if wire_end and not reply then
        wire_cnt <= wire_cnt + 1;
      elsif reply and not wire_end then
        wire_cnt <= wire_cnt - 1;
      end if;

      -- 受けて応答をまだ返していない命令の数（OP_PINS は受けた辺で返す）
      if accept and cmd_op /= OP_PINS and not reply then
        owed <= owed + 1;
      elsif reply and not (accept and cmd_op /= OP_PINS) then
        owed <= owed - 1;
      end if;

      -- 遅延線を進める（遅れは命令の実行中に変えない）
      take_base    <= now_take;
      take_line(0) <= take_base;
      for k in 1 to 6 loop
        take_line(k) <= take_line(k - 1);
      end loop;
      if rst = '1' or enable = '0' then
        take_base <= TAKE_NONE;
        take_line <= (others => TAKE_NONE);
        cur_take  <= TAKE_NONE;
        resp_go   <= false;
        done      <= '0';
        owed      <= 0;
        cap_pend  <= false;
        wire_cnt  <= 0;
      end if;
    end if;
  end process;
end architecture;
