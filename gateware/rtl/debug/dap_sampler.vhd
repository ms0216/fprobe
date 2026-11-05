-- DAP サンプラ（F4: 変数のライブ観測）: ターゲットを止めずに、最大 8 か所（各 1〜16 語の連続）のメモリを周期的に読み、
-- 誤りが無いと確かめた値を 1 語ずつ出す（イベントの件 rec_* を組み、event_collector がそのまま種類 7・17・18 にする）。
-- 仕様: docs/superpowers/specs/2026-10-17-f4-live-sampler-design.md
--       docs/superpowers/specs/2026-10-18-b17-sampler-lazy-restore-design.md（B17: 遅延の戻し）
--       docs/superpowers/specs/2026-11-01-stop-precision-design.md §3.6（先読みの巡回）・§3.7（受け渡しの控え）
--       docs/superpowers/specs/2026-11-01-time-accuracy-design.md R6（時刻と窓）
--       docs/superpowers/specs/2026-11-01-bandwidth-observation-design.md 2.3（JTAG の K 語の確認）
--
-- swd_injector と同じく、上流（dap_processor と swd_injector）と swd_phy の間に入るアービタ。上流の命令の境界
-- （up_busy = '0' で、PHY に上流の命令が残っていない）で PHY を借り（held）、か所を 1 つずつ読む:
--   借りて最初だけ: SELECT ← ap（bank 0）/（控えが無ければ CSW・TAR を読んで保存）/ CSW ← 32 ビット・自動増加 single
--     （有効なか所が 1 つで 1 語なら自動増加なし = 同じ番地を読み続け、TAR も借りて最初の 1 回だけ）
--   か所ごと: TAR ← 番地 / DRW 読み × n
-- 先読み（§3.6）: AP の読みの値は次の転送で返る（ポスト）。その値を受け取る転送を、次の読みそのものにする:
--   SWD: 語の値は次の DRW の読みで返る（か所をまたいでも。TAR の書きは読みの値を変えない）。誤りの確認も次の AP の転送の
--     ACK に入る（SW-DP は STICKYERR の間、AP の転送に FAULT を返す）。よって「値を返した DRW の ACK が OK」= 値は正しい。
--     1 語 1 転送、1 か所 1 + n 転送。
--   JTAG: ACK に FAULT が無いので、CTRL/STAT で確かめる。check_words = K 回の DRW の読みごとに CTRL/STAT を 1 スキャンだけ
--     読み（jtag_phy のポストの DP の読み。そのスキャンで直前の DRW の値を受け取る）、CTRL/STAT の値は次のスキャン（次の語の
--     DRW・次のか所の TAR の書き・回収の RDBUFF）で受け取る。STICKYERR が 0 なら、前の確認から後に受け取った値を出す。
--     K = 1（語ごと）: 1 語 = DRW + CTRL/STAT（IR の切り替え 2 回）、1 か所 1 語 = TAR + DRW + CTRL/STAT = 3 スキャン + IR 2 回。
--     停止の見張りは K = 1（まとめると、止まったコアに気づいてから値を出すまでが延びる。設計書 §3.6）。K > 1 は使う側が明示する
--     流量の型（帯域の設計書 2.3）: 同じ番地を続けて読むなら 1 語 = DRW の 1 スキャン + K 語ごとに CTRL/STAT と IR 2 回。
--     確認が通るまで値は出さない（buf に溜める。最大 CHECK_MAX 語。K は CHECK_MAX で止める）。
--   値を受け取っていない読み・確認していない値が残るのは、か所の境界・巡回の終わり。線を渡す前（戻す・直接渡す）と、巡回の後に
--   休むときは、回収して確かめてから（SWD: RDBUFF・CTRL/STAT、JTAG: CTRL/STAT（まだ読んでいなければ）・RDBUFF のスキャンで
--   CTRL/STAT を受け取る）。間を空けずに次の巡回を始めるときは、回収せずに次の巡回の最初の読みで受け取る。
--   JTAG で次のか所の TAR の書きが値を出したときは、そのか所の DRW を読む前に境界（S_READ_BOUNDARY）を置く: 値の一致から
--   割り込み書き込みの要求（up_busy）が来るのを待ってから読むので、止まったコアの標本の次は、どのか所も読まずに線を渡す。
-- 遅延の戻し: ホストが線を使っていない間は SELECT・CSW・TAR を戻さず、線を持ったまま次のか所・次の巡回を読む。
-- 上流が PHY に命令を出したら（up_cmd_valid）、それを待たせたまま（up_cmd_ready = '0'）次の境界（か所の終わり・
-- 巡回の外）で 1 度だけ CSW・TAR・SELECT をホストの値に戻して渡す（ホストがキャッシュしている値なので、ホストからは
-- 見えない）。止めた（run = '0'）とき・持っている間にか所の表が変わったときも戻して渡す（AP 等の設定は止めてから
-- 変える）。きっかけを PHY の命令にするのは、PHY を使わない DAP の命令（SWJ_Clock・切断等）が PHY の設定を変える
-- 間に戻しの転送を重ねないため。切断（enable = '0'）・接続の方式の切り替え（jtag が借りたときと違う）では戻さずに放す
-- （ホストは線をリセットして書き直す。回収していない値は出さず、誤りの区間（線を失った）にする）。
-- 直接の受け渡し（docs/superpowers/specs/2026-10-29-halt-watch-design.md §3.3）: 上流の swd_injector が線を借りたら
-- （up_injector_held。割り込み書き込みは、こちらが放すのを待ってから最初の転送を出す）、その AP がサンプラの AP と同じで、
-- ホストの控え（shadow_valid、shadow_ap）もその AP のものなら、戻さずに渡す（handoffs に数える）。swd_injector は終わる
-- ときに CSW・TAR・SELECT をその控えに戻すので、ホストからは同じに見える。止めた・表が変わった・条件が合わないときは、
-- 今までどおり戻してから渡す。
-- 受け渡しの控え（§3.7）: 直接渡したとき、控え（CSW）から CSW を作って借りていたなら（fast）、line_handed = '1'
-- （線の SELECT は ap の bank 0、CSW は控えの CSW の Size を 32 ビットにした値（AddrInc は none か single）で、渡した後に
-- 誰も線を使っていない）。swd_injector はこれを見て SELECT・CSW の書き直しを省く。上流の命令が 1 つ線を通ったら、
-- 借りたら、戻したら、切断したら 0。
-- 割り込み書き込みの要求が来ている間（up_busy に含める。fprobe_core の inj_claim）は次のか所を始めないので、一致した
-- 標本のか所を読み終えた境界で渡す（docs/superpowers/specs/2026-11-01-stop-precision-design.md §3.2）。
-- 戻すきっかけ（give_back）と「有効なか所が 1 つ」（single）は 1 周期前に登録する。持っている間は上流の命令は PHY に
-- 届かないので、きっかけが 1 周期遅れてもホストの命令は追い越されない（判定の組み合わせの段を sys_clk に収める。
-- B17 の前回の版は標準の構成でタイミングを満たさなかった）。状態は 1 ビットの印（tar_ok 等）だけで持ち、値は比べない。
-- 番地の自動増加は 1 KB の境界をまたげない（ADIv5）。ホストは 1 か所を 1 KB の中に収める。
-- バスの誤り（SWD の FAULT・JTAG の STICKYERR）: 値は出さず errors に数え、ABORT（STKERRCLR）で消してから CSW・TAR・
-- SELECT を戻して渡す（ホストの次の転送が、サンプラの起こした誤りで FAULT にならない）。誤りが前のか所の値のものなら
-- 今のか所は読み直し、今のか所の値のもの（か、どの値のものでもない）ならそのか所の残りを飛ばす（どちらでも巡回は進む）。
-- WAIT は wait_retry 回（ホストが DAP_TransferConfigure で設定）まで繰り返す。FAULT 以外の異常（応答なし・パリティ）は線の不調なので、SELECT だけ戻して渡す
-- （ponytail: swd_injector と同じ）。誤りはどれも errors に数える。出さなかった読みは誤りの区間（種類 18）の件にする。
-- 前の巡回が終わる前に次の周期が来たら overruns に数え、巡回が終わったら間を空けずに次の巡回を始める（周期に
-- 追いつかないときは「できるだけ速く」になる。周期を丸ごと飛ばすと実際の速さが半分に落ちた。2026-10-18 に実機で）。
-- jitter（k > 0）: 周期に 0〜2^k − 1 の乱数（16 ビットの LFSR）を足し、2^(k−1) を引く（平均の周期は変わらない）。
-- 周期的な処理（割り込み等）と読みの周期が噛み合って割合が偏る（エイリアシング）のを防ぐ（PC サンプリング）。
-- か所の表（番地・語数）は分散 RAM に置き、CSR からは 1 か所ずつ書く（tbl_write）。巡回は か所を順に 1 つずつ
-- 読むので、全か所をフリップフロップで並べる必要は無い（Primer で 8 か所 × 36 ビットの FF と 8:1 の選択が入りきらなかった）。
-- 有効の印だけは次の か所を探すために FF に置く。
--
-- 時刻と窓（R6）: 語の時刻は、その語を読みに行った DRW の転送（WAIT を繰り返したら OK の回）の線上の点（phy_rsp_time。SWD は
-- ACK の後の立ち上がり、JTAG は Update-DR を出る立ち上がり）= 窓の始め。値を返した転送の WAIT の回数を [119:115] に入れる
-- （31 で止まる）。窓の後端は「値を返した転送の線上の点」で、件の並びの上で次のサンプラの件の [47:0] がそれになる: 値を返した
-- 転送が次の語を読みに行った DRW なら、その語の件（種類 7）か、その語が誤りの区間の始めなら種類 18。そうでなければ（JTAG の
-- CTRL/STAT・TAR・RDBUFF、SWD の回収の RDBUFF）その転送の点を種類 17 の件で語の直後に出す（記録が欠けた lost を除く）。
-- 件（rec_*）は 1 周期に 1 件。確認した語を出し終えるまで次の転送を出さず、線も渡さない（イベントの受け口は 1 周期 1 件で、
-- サンプラが線を持つ間は DAP・割り込み書き込みの件が無い）。
-- 種類 7: [47:0] 語の時刻、[79:48] 番地、[111:80] 値、[114:112] ACK（OK）、[119:115] 値を返した転送の WAIT の回数。
-- 種類 17: [47:0] 値を返した転送の点、[79:48] その語の番地、[111:80] 0、[115:112] その転送の要求（CMSIS の並び）、[119:116] 0。
-- 種類 18: [47:0] 値を出さなかった最初の読みの点、[95:48] それが分かった転送の点（線を失ったときは最後の転送の点）、
--   [102:96] 最初の読みの番号（か所 × 16 + 語）、[111:103] 読みの数、[114:112] 分かった転送の ACK（線を失ったときは 000）、
--   [115] CTRL/STAT の STICKYERR で分かった、[116] 線を失った（切断・接続の方式の切り替え）、[119:117] 0。
--
-- 構成: この entity は手順（借りる・読む・確かめる・戻す）だけを持ち、次の部品を使う。
--   dap_sampler_timer : 周期の時計とゆらぎ（巡回を始める due）
--   dap_sampler_out   : 確認を待つ語の溜め（buf）と件・標本の出力（種類 7・17・18、quiet = 標本の一致が届いた後）
--   dap_phy_borrow    : PHY の命令の切り替え・上流の命令の残り・WAIT の再試行の比べ
--   dap_sampler_pkg   : 確認を待つ語の記録（entry_t）
library ieee;
use ieee.std_logic_1164.all;
use ieee.numeric_std.all;
use work.event_pkg.all;
use work.util_pkg.all;
use work.dap_pkg.all;
use work.dap_sampler_pkg.all;

entity dap_sampler is
  generic (
    SLOTS     : positive range 1 to 8 := 8;       -- sample_index = か所 × 16 + 語 を 7 ビットに収める
    CHECK_MAX : positive range 1 to 64 := 1       -- JTAG の確認の間隔 K の上限（溜める語の数。ケイパビリティで申告）
  );
  port (
    clk           : in  std_logic;
    rst           : in  std_logic;
    -- WAIT の再試行の回数（ホストが DAP_TransferConfigure で設定した値。dap_processor の wait_retry_out）
    wait_retry    : in  unsigned(15 downto 0);
    -- 設定（CSR）
    run           : in  std_logic;
    period        : in  unsigned(31 downto 0);           -- 巡回の周期（clk の周期数。0 = 止める）
    jitter        : in  natural range 0 to 15;           -- 周期のゆらぎ（2^jitter 周期の幅。0 = なし）
    ap            : in  std_logic_vector(7 downto 0);    -- 止めてから変える
    tbl_write     : in  std_logic;                       -- 1 周期: か所 tbl_index を書く
    tbl_index     : in  natural range 0 to SLOTS - 1;
    tbl_addr      : in  std_logic_vector(31 downto 0);   -- 語の境界（下位 2 ビットは無視）
    tbl_enable    : in  std_logic;
    tbl_words     : in  std_logic_vector(3 downto 0);    -- 語数 − 1
    check_words   : in  natural range 0 to 127;          -- JTAG の確認の間隔 K（止めてから変える。0 は 1、CHECK_MAX を超えれば CHECK_MAX）
    enable        : in  std_logic;                       -- SWD が接続中
    jtag          : in  std_logic := '0';                -- 接続は JTAG（dap_processor の jtag。PHY の命令の実行中には変わらない）
    up_busy       : in  std_logic;                       -- 上流（dap_processor・swd_injector）が使っている・これから使う（1 なら次のか所を始めない）
    up_injector_held : in std_logic := '0';              -- 上流の swd_injector が PHY を借りている（その held）
    up_injector_ap : in std_logic_vector(7 downto 0) := x"00";   -- その AP
    select_shadow : in  std_logic_vector(31 downto 0);
    csw_shadow    : in  std_logic_vector(31 downto 0);
    tar_shadow    : in  std_logic_vector(31 downto 0);
    shadow_valid  : in  std_logic;
    shadow_ap     : in  std_logic_vector(7 downto 0);    -- 控えを書いた AP。ap と違えば控えを使わず読んで保存する
    -- 読んだ値（1 周期のパルス。誤りが無いと確かめた値だけ。sampler_match へ）
    sample_valid  : out std_logic;
    sample_addr   : out std_logic_vector(31 downto 0);
    sample_data   : out std_logic_vector(31 downto 0);
    sample_ack    : out std_logic_vector(2 downto 0);
    sample_index  : out unsigned(6 downto 0);            -- か所 × 16 + 語（sampler_match が番号ごとに状態を持つ）
    sample_time   : out unsigned(47 downto 0);           -- 読みに行った転送の線上の点（窓の始め）
    -- イベントの件（1 周期のパルス。種類 7・17・18 と [119:0]。event_collector がそのまま出す）
    rec_valid     : out std_logic;
    rec_kind      : out std_logic_vector(7 downto 0);
    rec_word      : out std_logic_vector(119 downto 0);
    -- 状態
    busy          : out std_logic;
    held          : out std_logic;                       -- 線を持っている（ホストの次の転送の前に戻す）
    line_handed   : out std_logic;                       -- 直接渡した線の SELECT・CSW が分かっている（§3.7。swd_injector へ）
    rounds        : out unsigned(31 downto 0);
    errors        : out unsigned(31 downto 0);
    overruns      : out unsigned(31 downto 0);
    restores      : out unsigned(15 downto 0);
    handoffs      : out unsigned(15 downto 0) := (others => '0');   -- 戻さずに swd_injector に渡した回数（試験用。つながなければ合成で消える）
    last_ack      : out std_logic_vector(2 downto 0);
    -- 上流側（swd_phy の命令 I/F と同じ）
    up_cmd_valid  : in  std_logic;
    up_cmd_ready  : out std_logic;
    up_cmd_cond   : in  std_logic := '0';                -- 前の転送が OK のときだけ受ける（PHY へそのまま。持っている間は 0）
    up_cmd_op     : in  std_logic_vector(1 downto 0);
    up_cmd_count  : in  natural range 1 to 64;
    up_cmd_req    : in  std_logic_vector(3 downto 0);
    up_cmd_data   : in  std_logic_vector(63 downto 0);
    up_rsp_valid  : out std_logic;
    -- swd_phy 側（応答の ack / data / parity / time は両者へそのまま配る）
    phy_cmd_valid : out std_logic;
    phy_cmd_ready : in  std_logic;
    phy_cmd_cond  : out std_logic;
    phy_cmd_op    : out std_logic_vector(1 downto 0);
    phy_cmd_count : out natural range 1 to 64;
    phy_cmd_req   : out std_logic_vector(3 downto 0);
    phy_cmd_data  : out std_logic_vector(63 downto 0);
    phy_rsp_valid : in  std_logic;
    phy_rsp_ack   : in  std_logic_vector(2 downto 0);
    phy_rsp_data  : in  std_logic_vector(31 downto 0);
    phy_rsp_par   : in  std_logic;
    phy_rsp_time  : in  unsigned(63 downto 0)             -- 応答の転送の線上の点（debug_port の rsp_time）
  );
end entity;

architecture rtl of dap_sampler is

  -- 手順。P_POSTED_CTRL は JTAG だけ（直前の DRW の値を受け取る CTRL/STAT の読み）、P_RDBUFF・P_CHECK は残りの値の回収
  -- （SWD: RDBUFF で値、CTRL/STAT で確認。JTAG: RDBUFF のスキャンで CTRL/STAT を受け取る）
  type step_t is (P_SELECT, P_READ_CSW, P_READ_TAR, P_RDBUFF_TAR, P_CSW, P_TAR, P_DRW, P_POSTED_CTRL, P_RDBUFF, P_CHECK,
                  P_CLEAR, P_RESTORE_CSW, P_RESTORE_TAR, P_RESTORE_SELECT);
  type state_t is (S_IDLE, S_NEXT_SLOT, S_LOAD, S_WAIT_BOUNDARY, S_SETTLE, S_READ_BOUNDARY, S_ISSUE, S_RESPONSE);
  -- か所の表（分散 RAM。リセットしない）: 番地(31..2) & 語数 − 1
  type table_t is array (0 to SLOTS - 1) of std_logic_vector(33 downto 0);
  signal table       : table_t := (others => (others => '0'));
  signal enables     : std_logic_vector(SLOTS - 1 downto 0) := (others => '0');
  signal state       : state_t := S_IDLE;
  signal step        : step_t := P_SELECT;
  signal mine        : boolean := false;                 -- PHY を借りている（held。ホストの値に戻すまで）
  signal jtag_r      : boolean := false;                 -- 借りたときの接続の方式（手順を選ぶ）
  signal lost        : boolean;                          -- 切断か、借りた後に接続の方式が変わった: 戻さずに放す
  signal mine_sl     : std_logic;
  signal up_pending  : boolean;                          -- 上流の命令が PHY に残っている（dap_phy_borrow）
  signal in_round    : boolean := false;
  signal pending     : boolean := false;                 -- 巡回の最中に周期が来た: 終わったらすぐ次の巡回
  signal slot        : natural range 0 to SLOTS := 0;
  signal word        : natural range 0 to 16 := 0;        -- このか所で受理された DRW の読みの数
  signal words       : natural range 1 to 16 := 1;
  signal base        : unsigned(31 downto 0) := (others => '0');
  signal aborting    : boolean := false;               -- 誤りを数えた後の後始末の最中
  signal skip        : boolean := false;               -- 後始末の後、今のか所の残りを飛ばす（誤りが今のか所の値のもの）
  signal saved       : boolean := false;               -- CSW・TAR の戻す値がある（控え、または読んで保存した）
  -- 値をまだ受け取っていない読み（先読み）: 番地・番号・時刻は読んだときに控える（値が返るのは次のか所の転送のことがある）
  signal pend        : boolean := false;               -- 受理された DRW の読みで、値をまだ受け取っていないものがある
  signal ctrl_pend   : boolean := false;               -- JTAG: CTRL/STAT を読んだ。次のスキャンの取り込みが CTRL/STAT
  signal p_addr      : std_logic_vector(31 downto 0) := (others => '0');
  signal p_index     : unsigned(6 downto 0) := (others => '0');
  signal p_time      : std_logic_vector(47 downto 0) := (others => '0');
  signal to_idle     : boolean := false;               -- 回収の後: 巡回の終わり（でなければ か所の境界に戻る）
  signal collecting  : boolean := false;               -- JTAG の回収: CTRL/STAT を読んだら次は RDBUFF
  -- 確認を待つ語（値を受け取った語の記録。dap_sampler_out の buf に溜め、確認が通ったら順に件にして出す）
  signal buf_we      : std_logic := '0';
  signal buf_wa      : natural range 0 to CHECK_MAX - 1 := 0;
  signal buf_wd      : std_logic_vector(ENTRY_BITS - 1 downto 0) := (others => '0');
  signal nbuf        : natural range 0 to CHECK_MAX := 0;     -- 溜めた（確認を待つ）語の数
  signal kmax        : natural range 1 to CHECK_MAX := 1;     -- 確認の間隔（check_words を範囲に収めて登録）
  signal since_check : natural range 0 to CHECK_MAX := 0;     -- JTAG: 前に CTRL/STAT を読んでから受理された DRW の読みの数
  -- 確認を待つ読み（値を受け取った語 + pend）の数と、その最初の読み（誤りの区間の始め）
  signal u_count     : natural range 0 to CHECK_MAX + 1 := 0;
  signal u_time      : std_logic_vector(47 downto 0) := (others => '0');
  signal u_index     : unsigned(6 downto 0) := (others => '0');
  signal u_here      : boolean := false;               -- 確認を待つ読みに、今のか所の読みがある
  signal last_time   : std_logic_vector(47 downto 0) := (others => '0');   -- 最後の転送の点（線を失ったときの区間の終わり）
  -- 件の出力（dap_sampler_out）への指示（どれも登録して次の周期に届ける）と、その状態
  signal dr_start    : std_logic := '0';               -- 確認が通った: 溜めた dr_n 語を出す
  signal dr_n        : natural range 1 to CHECK_MAX := 1;
  signal gap_go      : std_logic := '0';               -- 誤りの区間の件 gap_word を出す
  signal gap_word    : std_logic_vector(119 downto 0) := (others => '0');
  signal out_draining, out_gap, out_quiet : std_logic;
  signal draining    : boolean;                        -- 確認した語を出している
  signal gap_pend    : boolean;                        -- 誤りの区間の件をまだ出していない
  signal quiet       : boolean;                        -- 最後に出した標本の一致が up_busy に届いた後（か所を始めてよい）
  signal settle      : natural range 0 to 1 := 0;
  signal tries       : unsigned(15 downto 0) := (others => '0');
  signal retry_more  : std_logic;                        -- tries < wait_retry（dap_phy_borrow）
  signal csw, tar    : std_logic_vector(31 downto 0) := (others => '0');
  signal fast        : boolean := false;
  signal same        : boolean := false;               -- 有効なか所が 1 つで 1 語: 自動増加なしで TAR を動かさない
  signal tar_ok      : boolean := false;               -- TAR はか所の番地のまま
  signal stale       : boolean := false;               -- 持っている間にか所の表が変わった: 戻して借り直す
  -- 1 周期前に登録した判定（組み合わせの段を減らす）
  signal give_back   : boolean := false;               -- 渡す: 上流の PHY の命令・割り込み書き込みが借りた・止めた・表が変わった
  signal handoff     : boolean := false;               -- 戻さずに渡せる（上流は同じ AP の swd_injector で、控えがある）
  signal handoffs_r  : unsigned(15 downto 0) := (others => '0');
  signal handed_r    : boolean := false;               -- line_handed
  signal single      : boolean := false;               -- 有効なか所がちょうど 1 つ
  signal due_sl      : std_logic;
  signal due         : boolean;                        -- 周期が満ちた（dap_sampler_timer。次の周期に巡回を始める）
  signal req_r       : std_logic_vector(3 downto 0) := (others => '0');
  signal count_r     : natural range 1 to 64 := 1;
  signal data_r      : std_logic_vector(31 downto 0) := (others => '0');
  signal valid_r     : std_logic := '0';
  signal ack_r       : std_logic_vector(2 downto 0) := (others => '0');
  signal rounds_r, errors_r, overruns_r : unsigned(31 downto 0) := (others => '0');
  signal restores_r  : unsigned(15 downto 0) := (others => '0');
begin

  -- 周期の時計とゆらぎ（巡回の始まりの due だけを本体へ）
  timer_i : entity work.dap_sampler_timer
    port map (clk => clk, rst => rst, run => run, period => period, jitter => jitter, due => due_sl);
  due <= due_sl = '1';

  -- 確認を待つ語の溜めと件の出力
  out_i : entity work.dap_sampler_out
    generic map (CHECK_MAX => CHECK_MAX)
    port map (clk => clk, rst => rst, wr_valid => buf_we, wr_index => buf_wa, wr_data => buf_wd,
              start_valid => dr_start, start_count => dr_n, gap_valid => gap_go, gap_data => gap_word,
              draining => out_draining, gap_pending => out_gap, quiet => out_quiet,
              sample_valid => sample_valid, sample_addr => sample_addr, sample_data => sample_data,
              sample_ack => sample_ack, sample_index => sample_index, sample_time => sample_time,
              rec_valid => rec_valid, rec_kind => rec_kind, rec_word => rec_word);
  draining <= out_draining = '1';
  gap_pend <= out_gap = '1';
  quiet    <= out_quiet = '1';

  -- PHY の命令の切り替え（mine は境界でしか変わらない）・上流の命令の残り・WAIT の再試行の比べ
  mine_sl <= to_sl(mine);
  borrow_i : entity work.dap_phy_borrow
    port map (clk => clk, rst => rst, own => mine_sl, enable => enable, jtag => jtag, own_valid => valid_r, own_count => count_r, own_req => req_r,
              own_data => data_r, wait_retry => wait_retry, tries => tries, retry_more => retry_more,
              up_cmd_valid => up_cmd_valid, up_cmd_ready => up_cmd_ready, up_cmd_cond => up_cmd_cond,
              up_cmd_op => up_cmd_op, up_cmd_count => up_cmd_count, up_cmd_req => up_cmd_req,
              up_cmd_data => up_cmd_data, up_rsp_valid => up_rsp_valid, up_pending => up_pending,
              phy_cmd_valid => phy_cmd_valid, phy_cmd_ready => phy_cmd_ready, phy_cmd_cond => phy_cmd_cond,
              phy_cmd_op => phy_cmd_op, phy_cmd_count => phy_cmd_count, phy_cmd_req => phy_cmd_req,
              phy_cmd_data => phy_cmd_data, phy_rsp_valid => phy_rsp_valid);
  lost <= enable = '0' or (mine and (jtag = '1') /= jtag_r);

  process (clk)
    procedure issue(p : step_t) is
    begin
      valid_r <= '1';
      count_r <= 1;
      data_r  <= (others => '0');
      case p is
        when P_SELECT         => req_r <= DP_W_SELECT; data_r <= ap & x"000000";
        when P_READ_CSW       => req_r <= AP_R_CSW;                            -- 読みは 1 つ後で返る
        when P_READ_TAR       => req_r <= AP_R_TAR;                            -- CSW が返る
        when P_RDBUFF_TAR     => req_r <= DP_R_RDBUFF;                         -- TAR が返る
        when P_CSW            =>                                               -- 32 ビット・増加 single（same: なし）
          req_r <= AP_W_CSW;
          if same then
            data_r <= csw_word32(csw, CSW_ADDRINC_OFF);
          else
            data_r <= csw_word32(csw, CSW_ADDRINC_SINGLE);
          end if;
        when P_TAR            => req_r <= AP_W_TAR; data_r <= std_logic_vector(base);
        when P_DRW            => req_r <= AP_R_DRW;
        when P_POSTED_CTRL    => req_r <= DP_R_CTRL; count_r <= COUNT_POSTED;   -- 直前の DRW の値が返る（CTRL/STAT は次のスキャンで）
        when P_RDBUFF         => req_r <= DP_R_RDBUFF;
        when P_CHECK          => req_r <= DP_R_CTRL;
        when P_CLEAR          => req_r <= DP_W_ABORT; data_r <= STKERRCLR;
        when P_RESTORE_CSW    => req_r <= AP_W_CSW; data_r <= csw;
        when P_RESTORE_TAR    => req_r <= AP_W_TAR; data_r <= tar;
        when P_RESTORE_SELECT => req_r <= DP_W_SELECT; data_r <= select_shadow;
      end case;
    end procedure;

    -- 線を渡すために戻し始める（CSW・TAR の戻す値が無ければ SELECT だけ）
    procedure start_restore is
    begin
      tries    <= (others => '0');
      aborting <= false;
      state    <= S_ISSUE;
      if saved then
        step <= P_RESTORE_CSW;
      else
        step <= P_RESTORE_SELECT;
      end if;
    end procedure;

    -- 戻すか、直接渡すか（handoff）。値をまだ出していない読みは無く、件も出し終えていること（先に回収する）
    procedure give_line is
    begin
      if handoff then
        mine       <= false;
        handed_r   <= fast;                                  -- SELECT・CSW は控えから作った値（§3.7）
        handoffs_r <= handoffs_r + 1;
        if in_round then
          state <= S_NEXT_SLOT;
        else
          state <= S_IDLE;
        end if;
      else
        start_restore;
      end if;
    end procedure;

    -- 値をまだ出していない読みを回収する（その後は、か所の境界か巡回の終わりに戻る）
    procedure start_collect(idle : boolean) is
    begin
      tries    <= (others => '0');
      aborting <= false;
      to_idle  <= idle;
      state    <= S_ISSUE;
      if jtag_r and not ctrl_pend then
        step       <= P_POSTED_CTRL;                         -- JTAG: CTRL/STAT を読み（直前の読みの値を受け取る）、RDBUFF で確かめる
        collecting <= true;
      else
        step <= P_RDBUFF;
      end if;
    end procedure;

    procedure end_collect is
    begin
      if not to_idle then
        state <= S_WAIT_BOUNDARY;
      elsif in_round then                                    -- 回収の間に次の周期が来た
        state <= S_NEXT_SLOT;
      else
        state <= S_IDLE;
      end if;
    end procedure;

    -- 確認を待つ読み・語を捨てる（値は出さない）
    procedure forget is
    begin
      pend        <= false;
      ctrl_pend   <= false;
      nbuf        <= 0;
      since_check <= 0;
      collecting  <= false;
      u_count     <= 0;
      u_here      <= false;
    end procedure;

    -- 線を放す（切断・方式の切り替え）: 戻さない。値を得ていない・確かめていない読みは、誤りの区間（線を失った）にする
    procedure drop is
    begin
      mine <= false;
      forget;
      if u_count /= 0 then
        gap_go   <= '1';
        gap_word <= "000" & '1' & '0' & "000" & std_logic_vector(to_unsigned(u_count, 9)) & std_logic_vector(u_index)
                    & last_time & u_time;
      end if;
    end procedure;

    -- 戻し終えた（か後始末で戻しきれなかった）: 線を渡す。巡回の途中なら残りは次の境界で借り直して読む
    procedure hand_back is
    begin
      mine       <= false;
      restores_r <= restores_r + 1;
      if in_round then
        state <= S_NEXT_SLOT;
      else
        state <= S_IDLE;
      end if;
    end procedure;

    -- 誤り（値は出さない）: 数えて後始末へ。誤りが前のか所の値のものなら今のか所は読み直す。確認を待つ読み（と include =
    -- この転送が受理された DRW の読み）は誤りの区間の件にする。sticky = CTRL/STAT の STICKYERR で分かった
    procedure fail(clear : boolean; include : boolean; sticky : boolean) is
      variable n : natural range 0 to CHECK_MAX + 2;
      variable first_t : std_logic_vector(47 downto 0);
      variable first_i : unsigned(6 downto 0);
    begin
      errors_r <= errors_r + 1;
      aborting <= true;
      skip     <= u_here or include or not (pend or ctrl_pend or nbuf /= 0);
      n       := u_count;
      first_t := u_time;
      first_i := u_index;
      if include then
        n := n + 1;
        if u_count = 0 then
          first_t := std_logic_vector(phy_rsp_time(47 downto 0));
          first_i := to_unsigned(slot * 16 + word, 7);
        end if;
      end if;
      if n /= 0 then
        gap_go   <= '1';
        gap_word <= "000" & '0' & to_sl(sticky) & phy_rsp_ack & std_logic_vector(to_unsigned(n, 9))
                    & std_logic_vector(first_i) & std_logic_vector(phy_rsp_time(47 downto 0)) & first_t;
      end if;
      forget;
      tries    <= (others => '0');
      state    <= S_ISSUE;
      if clear then
        step <= P_CLEAR;                                     -- バスの誤り: 消してから全部戻す
      else
        step <= P_RESTORE_SELECT;                            -- 線の不調: SELECT だけ戻す
      end if;
    end procedure;

    -- か所の読みを終えた（最後の読みの値は、次のか所の転送か回収で受け取る）
    procedure slot_done is
    begin
      slot   <= slot + 1;
      u_here <= false;
      state  <= S_NEXT_SLOT;
    end procedure;

    -- 次の有効なか所（from から）。無ければ SLOTS
    function next_slot(from : natural; en : std_logic_vector) return natural is
    begin
      for k in 0 to SLOTS - 1 loop
        if k >= from and en(k) = '1' then
          return k;
        end if;
      end loop;
      return SLOTS;
    end function;

    -- 控えた読み（p_*）の値を受け取った: 確認を待つ語に積む。by_read = 値を返したのが次の語の DRW の読み（種類 17 は要らない）
    procedure take(value : std_logic_vector(31 downto 0); by_read : boolean) is
      variable w : std_logic_vector(4 downto 0);
    begin
      if tries > 31 then
        w := (others => '1');                                -- WAIT の回数は 31 で止める（窓の後端は次の件の点で分かる）
      else
        w := std_logic_vector(tries(4 downto 0));
      end if;
      buf_we <= '1';
      buf_wa <= nbuf;
      buf_wd <= to_bits((index => std_logic_vector(p_index), addr => p_addr, value => value,
                         stamp => p_time, waits => w, bound => to_sl(not by_read),
                         rsp_time => std_logic_vector(phy_rsp_time(47 downto 0)), req => req_r));
      pend   <= false;
    end procedure;

    -- 確認が通った: 溜めた語（n 個）を順に出す
    procedure confirm(n : natural) is
    begin
      if n /= 0 then
        dr_start <= '1';                                     -- buf の書き込み（buf_we）が終わる次の周期に届く
        dr_n     <= n;
      end if;
      nbuf   <= 0;
      u_here <= false;
    end procedure;

    variable k : natural range 0 to SLOTS;
    variable bad, shown : boolean;
    variable ucount : natural range 0 to CHECK_MAX + 1;
    variable nb : natural range 0 to CHECK_MAX;
  begin
    if rising_edge(clk) then
      buf_we   <= '0';
      dr_start <= '0';
      gap_go   <= '0';
      if phy_cmd_ready = '1' then
        valid_r <= '0';
      end if;
      if check_words = 0 then
        kmax <= 1;
      elsif check_words > CHECK_MAX then
        kmax <= CHECK_MAX;
      else
        kmax <= check_words;
      end if;
      if phy_rsp_valid = '1' then
        last_time <= std_logic_vector(phy_rsp_time(47 downto 0));
      end if;
      if not mine and up_cmd_valid = '1' and phy_cmd_ready = '1' then
        handed_r   <= false;                                 -- 上流が線を使った: 渡した SELECT・CSW はもう分からない
      end if;
      -- 判定の登録（持っている間は上流の命令は PHY に届かないので、1 周期遅れても追い越されない）
      give_back <= up_cmd_valid = '1' or up_injector_held = '1' or run = '0' or stale;
      handoff   <= up_injector_held = '1' and run = '1' and not stale and
                   up_injector_ap = ap and shadow_valid = '1' and shadow_ap = ap;
      single  <= unsigned(enables) /= 0 and (unsigned(enables) and (unsigned(enables) - 1)) = 0;

      -- 止まっている間（周期の時計は period を積み直す。dap_sampler_timer）は、待っている巡回も無い
      if run = '0' or period = 0 then
        pending <= false;
      end if;
      if due and run = '1' then
        if in_round then
          overruns_r <= overruns_r + 1;                          -- 前の巡回が終わっていない: 終わったらすぐ次を始める
          pending    <= true;
        else
          in_round <= true;
          slot     <= 0;
          if state = S_IDLE then
            state <= S_NEXT_SLOT;
          end if;
        end if;
      end if;

      if rst = '1' then
        state      <= S_IDLE;
        mine       <= false;
        in_round   <= false;
        pending    <= false;
        valid_r    <= '0';
        enables    <= (others => '0');
        stale      <= false;
        give_back  <= false;
        forget;
        handed_r   <= false;
        rounds_r   <= (others => '0');
        errors_r   <= (others => '0');
        overruns_r <= (others => '0');
        restores_r <= (others => '0');
        handoffs_r <= (others => '0');
        ack_r      <= (others => '0');
      else
        if tbl_write = '1' then
          enables(tbl_index) <= tbl_enable;
        end if;
        if enable = '0' then
          handed_r <= false;
        end if;
        case state is
          when S_IDLE =>                                         -- 巡回の外（回収は済んでいる）
            if mine and lost then
              drop;                                              -- 切断: 戻さずに放す
            elsif mine and give_back and not draining and not gap_pend then   -- ホストの転送の前（か止めた・表が変わった）: 戻す
              give_line;                                         -- （件を出し終えてから）
            end if;

          when S_NEXT_SLOT =>
            k := next_slot(slot, enables);
            if k = SLOTS or run = '0' or enable = '0' then
              if in_round and k = SLOTS then
                rounds_r <= rounds_r + 1;
              end if;
              if pending and k = SLOTS and run = '1' and enable = '1' then
                pending <= false;                                -- 追いつかない周期の分: 間を空けずに次の巡回（値は次の読みで受け取る）
                slot    <= 0;
              else
                in_round <= false;
                if mine and not lost and (pend or ctrl_pend or nbuf /= 0) then
                  start_collect(true);                           -- 休む前に、最後の読みの値を回収して確かめる
                else
                  state <= S_IDLE;
                end if;
              end if;
            else
              slot  <= k;
              state <= S_LOAD;
            end if;

          when S_LOAD =>                                         -- 表は分散 RAM: か所を決めた次の周期に読む
            base  <= unsigned(table(slot)(33 downto 4)) & "00";
            words <= to_integer(unsigned(table(slot)(3 downto 0))) + 1;
            state <= S_WAIT_BOUNDARY;

          when S_WAIT_BOUNDARY =>
            if lost then
              if mine then
                drop;                                            -- 切断: 戻さずに放す
              end if;
              in_round <= false;
              state    <= S_IDLE;
            elsif mine then                                      -- 持ったまま（SELECT・CSW は効いている）
              if give_back then
                if pend or ctrl_pend or nbuf /= 0 then
                  start_collect(false);                          -- 渡す前に、前のか所の値を回収して確かめる
                elsif not draining and not gap_pend then
                  give_line;
                end if;
              elsif up_busy = '0' and not draining and quiet then   -- 出した標本の一致が届いてから
                tries <= (others => '0');
                word  <= 0;
                state <= S_ISSUE;
                if tar_ok then
                  step <= P_DRW;
                else
                  step <= P_TAR;
                end if;
              end if;
            elsif up_busy = '0' and up_cmd_valid = '0' and not up_pending then
              mine     <= true;                                  -- 借りる
              jtag_r   <= jtag = '1';
              step     <= P_SELECT;
              word     <= 0;
              tries    <= (others => '0');
              aborting <= false;
              forget;
              handed_r <= false;
              saved    <= shadow_valid = '1' and shadow_ap = ap;
              fast     <= shadow_valid = '1' and shadow_ap = ap;
              tar_ok   <= false;
              stale    <= false;
              same     <= words = 1 and single;
              csw      <= csw_shadow;
              tar      <= tar_shadow;
              state    <= S_ISSUE;
            end if;

          when S_SETTLE =>                                       -- 出した値の一致が up_busy に届くまで（最後の標本から 3 周期）
            if draining then
              null;                                              -- 確認した語を出し終えるまで
            elsif settle = 0 then
              state <= S_READ_BOUNDARY;
            else
              settle <= settle - 1;
            end if;

          when S_READ_BOUNDARY =>                                -- JTAG: TAR を書いた後、DRW を読む前（値は全部出してある）
            if lost then
              drop;
              in_round <= false;
              state    <= S_IDLE;
            elsif give_back then
              if not draining and not gap_pend then
                give_line;
              end if;
            elsif up_busy = '0' and not draining and quiet then
              state <= S_ISSUE;                                  -- step = P_DRW、word = 0
            end if;

          when S_ISSUE =>
            if lost then                                         -- 転送の前に切断・方式の切り替え: 戻さずに放す
              drop;
              valid_r  <= '0';
              in_round <= false;
              state    <= S_IDLE;
            else
              issue(step);                                       -- 件は出しながら次の転送を出す（下の assert）
              state <= S_RESPONSE;
            end if;

          when S_RESPONSE =>
            -- 確認した件を出し終える（最大 2 × CHECK_MAX + 1 周期）より前に次の応答は来ない: 1 転送は最短でも SWD 46 ビット・
            -- JTAG 40 TCK × 2 × half_period（half_period = 1 でも）= 80 周期、SWD は 46 ビット × 2 × 1 = 92 周期。したがって
            -- CHECK_MAX ≤ 39（2 × CHECK_MAX + 1 ≤ 79）なら間に合う（sampler_check_max は designs.py で 16）。buf に積む前に出し終えている
            assert not (phy_rsp_valid = '1' and (draining or gap_pend))
              report "dap_sampler: a response arrived before the confirmed records were out" severity failure;
            if lost then                                         -- 転送の途中で切断・方式の切り替え（PHY は応答しない）
              drop;
              valid_r  <= '0';
              in_round <= false;
              state    <= S_IDLE;
            elsif phy_rsp_valid = '1' then
              ack_r <= phy_rsp_ack;
              if phy_rsp_ack = ACK_OK and phy_rsp_par = '0' then
                tries  <= (others => '0');
                state  <= S_ISSUE;
                bad    := false;
                shown  := false;
                ucount := u_count;
                nb     := nbuf;
                -- 1. この転送の取り込み・応答が何を意味するか
                if (jtag_r and ctrl_pend and (step = P_TAR or step = P_DRW or step = P_RDBUFF)) or
                   (not jtag_r and step = P_CHECK) then
                  -- 取り込み（JTAG）・読みの値（SWD）が CTRL/STAT: 前の確認から後に受け取った値の誤りの確認
                  ctrl_pend <= false;
                  if phy_rsp_data(STICKYERR) = '1' then
                    bad := true;
                    fail(true, jtag_r and step = P_DRW, true);   -- 値は出さずに消す（このスキャンが始めた読みも捨てる）
                  else
                    confirm(nb);
                    shown  := nb /= 0;
                    ucount := 0;
                    nb     := 0;
                  end if;
                elsif pend and ((jtag_r and (step = P_DRW or step = P_POSTED_CTRL or step = P_TAR or step = P_RDBUFF)) or
                                (not jtag_r and (step = P_DRW or step = P_RDBUFF))) then
                  -- 取り込み（JTAG）・読みの値（SWD）が、控えた読みの値
                  take(phy_rsp_data, step = P_DRW);
                  nb := nb + 1;
                  if not jtag_r and step = P_DRW then
                    confirm(nb);                                 -- SWD: この AP の読みの ACK が OK = その値は正しい
                    ucount := 0;
                    nb     := 0;
                  end if;
                end if;
                -- 2. この転送そのもの
                if not bad then
                  case step is
                    when P_SELECT =>
                      if fast then
                        step <= P_CSW;                           -- 控えがあるので CSW・TAR を読まない
                      else
                        step <= P_READ_CSW;
                      end if;
                    when P_READ_CSW   => step <= P_READ_TAR;
                    when P_READ_TAR   => csw <= phy_rsp_data; step <= P_RDBUFF_TAR;
                    when P_RDBUFF_TAR => tar <= phy_rsp_data; saved <= true; step <= P_CSW;
                    when P_CSW        => step <= P_TAR;
                    when P_TAR =>
                      tar_ok <= same;
                      step   <= P_DRW;
                      if jtag_r and shown then
                        settle <= 1;
                        state  <= S_SETTLE;                      -- 前のか所の値を出した: 一致を待ってから読む
                      end if;
                    when P_DRW =>                                -- 読みを受理した: 値は次の転送で返る
                      pend      <= true;
                      u_here    <= true;
                      p_time    <= std_logic_vector(phy_rsp_time(47 downto 0));
                      p_addr    <= std_logic_vector(base(31 downto 10)) &
                                   std_logic_vector(base(9 downto 2) + to_unsigned(word, 8)) & "00";   -- 1 KB をまたがない
                      p_index   <= to_unsigned(slot * 16 + word, 7);
                      if ucount = 0 then
                        u_time  <= std_logic_vector(phy_rsp_time(47 downto 0));
                        u_index <= to_unsigned(slot * 16 + word, 7);
                      end if;
                      ucount := ucount + 1;
                      word      <= word + 1;
                      if jtag_r then
                        if since_check + 1 >= kmax then
                          step <= P_POSTED_CTRL;                 -- K 回目の読み: CTRL/STAT で確かめる
                        else
                          since_check <= since_check + 1;
                          if word + 1 = words then
                            slot_done;
                          end if;
                        end if;
                      elsif word + 1 = words then
                        slot_done;
                      end if;
                    when P_POSTED_CTRL =>                        -- JTAG: CTRL/STAT は次のスキャンで受け取る
                      ctrl_pend   <= true;
                      since_check <= 0;
                      if collecting then
                        collecting <= false;
                        step       <= P_RDBUFF;
                      elsif word = words then
                        slot_done;
                      else
                        step <= P_DRW;
                      end if;
                    when P_RDBUFF =>
                      if jtag_r then
                        end_collect;                             -- 取り込んだ CTRL/STAT で確かめて出した
                      else
                        step <= P_CHECK;                         -- SWD: 値を受け取った。CTRL/STAT で確かめる
                      end if;
                    when P_CHECK =>                              -- SWD: 回収した値を確かめて出した
                      end_collect;
                    when P_CLEAR =>
                      if saved then
                        step <= P_RESTORE_CSW;
                      else
                        step <= P_RESTORE_SELECT;
                      end if;
                    when P_RESTORE_CSW => step <= P_RESTORE_TAR;
                    when P_RESTORE_TAR => step <= P_RESTORE_SELECT;
                    when P_RESTORE_SELECT =>                    -- 戻し終えた: 渡す
                      hand_back;
                      if aborting and skip then                  -- 誤りの後始末: そのか所の残りは飛ばす
                        slot <= slot + 1;
                      end if;
                  end case;
                  u_count <= ucount;
                  nbuf    <= nb;
                end if;
              elsif phy_rsp_ack = ACK_WAIT and retry_more = '1' then
                tries <= tries + 1;
                state <= S_ISSUE;
              elsif aborting then                                -- 後始末の最中にまた失敗: SELECT を戻して終える
                tries <= (others => '0');
                if step = P_RESTORE_SELECT then
                  hand_back;
                  if skip then
                    slot <= slot + 1;
                  end if;
                else
                  step  <= P_RESTORE_SELECT;
                  state <= S_ISSUE;
                end if;
              else
                -- WAIT の使い切り・FAULT・応答なし・パリティ: ACK が OK でパリティだけ悪い DRW は読みを受理している
                fail(phy_rsp_ack = ACK_FAULT, step = P_DRW and phy_rsp_ack = ACK_OK, false);
              end if;
            end if;
        end case;
        -- 表が書かれた: 持っていれば戻して借り直す（借りる周期と重なっても印を残す。case の後に置いて優先する）
        if tbl_write = '1' then
          stale <= true;
        end if;
      end if;
    end if;
  end process;

  -- か所の表の書き込み（分散 RAM に推論させるため、リセットの無い別のプロセス）
  process (clk)
  begin
    if rising_edge(clk) then
      if tbl_write = '1' then
        table(tbl_index) <= tbl_addr(31 downto 2) & tbl_words;
      end if;
    end if;
  end process;

  busy         <= '0' when state = S_IDLE and not draining and not gap_pend else '1';
  held         <= '1' when mine else '0';
  line_handed  <= '1' when handed_r else '0';
  handoffs     <= handoffs_r;
  rounds       <= rounds_r;
  errors       <= errors_r;
  overruns     <= overruns_r;
  restores     <= restores_r;
  last_ack     <= ack_r;
end architecture;
