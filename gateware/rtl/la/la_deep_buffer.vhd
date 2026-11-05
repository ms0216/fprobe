-- ロジアナの深いバッファ: サンプル列を符号化して外部メモリにリングとして書き、指定範囲を読み出してバイト列で出す。
-- 仕様: docs/superpowers/specs/2026-10-16-m8-ddr3-design.md §5
--
--   sys: la_deep_front（la_front → la_encoder）→ 非同期 FIFO ─→ mem: la_mem_writer ─┐
--   sys: バイト列 ← stream_unpack ← 非同期 FIFO ← mem: mem_reader ←─┴ mem_cmd_arb → mem_port
--
-- 部品: la_run_cdc（開始・中止と設定の受け渡し）・cdc_request（読み出しの要求の受け渡し）・hold_one（DAP のイベントの
-- 1 件の保持）・la_stream_gap（欠けの区間）・la_mem_writer（リングの書き込み）・la_write_track（応答の来た書きまでの位置）・
-- la_stream_sched（ストリームの読み出しの段取り・溢れ・hold）・mem_reader（読み出し）・mem_cmd_arb（メモリの口の分け合い）。
-- ここはそれらのつなぎと、mem → sys の状態の組（1 つの時点の値の組）・sys の状態（捕捉中・完了・読み出し中）を持つ。
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
-- （その間 reading = '1'）。stream_beats はストリームで書いた拍の総数。総数（書いた・読んだ）は 32 ビットで折り返す
-- （差は util_pkg の mod_diff。2^32 拍 = 68.7 GB）。
--
-- イベントも流す（STREAM_EVENTS = true、R1。仕様: 2026-10-19-r1-unified-stream-design.md）: ストリームを「見出し 1 拍 +
-- 中身」のかたまりの列にし、ロジアナの拍とイベントのリングの未読を交互に載せる（読み出し器が空くたびに、両方に未読が
-- あれば前回と反対の種類）。見出し = [7:0] 種類（1 = ロジアナ、2 = イベント）、[31:16] 拍数、[63:32] 通し番号
-- （ロジアナ: ストリームの先頭からの拍の番号、イベント: 最初のイベントのリングの位置）、[127:64] 印 "FPRBSEG"。
-- 最初のイベントの位置は run の 0→1 の時点の post_beats（ストリームでは使わない）。イベントの未読（書き位置 − 読み
-- 位置）がリングの半分に達したら、ロジアナの書き込みを止めて overrun を立て、以後イベントは流さない。止めたら、
-- 止めた時点（止めの命令が効いた周期）の書き位置までのイベントとロジアナの未読を出し切って終わる（PS が読む流れでも: 止めた後の
-- イベントは公開する書き位置を頭打ちにして流れに入れず、リングに残す。ps-stream-position-design §2.9）。stream_beats は出すと決めた拍の総数
-- （見出し・イベントを含む）。イベントを読む流れの間は、ev_enable を始め直してもイベントのリングを先頭に戻さない
-- （書き位置と総数が続き、読み位置との関係を壊さない。la_mem_writer の ev_keep。C3）。
--
-- 1 周期に複数のサンプル（LANES > 1。仕様: docs/superpowers/specs/2026-11-01-la-deep-fast-design.md）: probes の代わりに lane_data
-- （LANES レーン × WIDTH。レーン k = その周期の k 番目のサンプル、k = 0 が最も古い）を受け、la_front・la_pack・la_encoder の代わりに
-- la_lane_encoder（RAW: サンプルの間隔 2^step と ch の詰め pack、RLE: N 並列の変化点）で拍にする。垂れ流しだけ（トリガは使わない:
-- triggered = 0。トリガ取り込みはホストが断る）。起点の時刻 start_time・固定の遅れ latency を出す（細かいサンプルの番号の式は
-- la_lane_encoder）。
-- LANES = 1 の起点の時刻（R1。docs/superpowers/specs/2026-11-01-time-accuracy-design.md）: run の後の最初のサンプルの時刻を、trigger_time と
-- 同じ段（符号器が入力を受けた周期の次の周期の time）で start_time に持つ。サンプル i のピンの時刻 = start_time + i × (rate + 1) −
-- LA_DEEP_DELAY（− フィルタの段）。連続記録のサンプル 0 の時刻を回路が記録する（ホストの往復による範囲が 1 点になる）。
-- リングの使用量（mem で数え、受け渡しのたびに sys へ）: stat_written = ロジアナの拍の書いた総数（流れの開始から）、stat_unread = 書いた −
-- 読んだ（PL が読み戻す流れは mem_reader に読ませた分、PS が読む流れは PS が返した分）、stat_peak = 流れの開始からの stat_unread の最大。
--
-- 欠けの区間（STREAM_GAPS = true の構成で gaps = '1'。段 B。仕様: docs/superpowers/specs/2026-11-01-la-deep-fast-design.md §12）:
-- ストリームで溢れても止めない。mem 側はロジアナのリングの未読が LA_SIZE − GAP_HOLD を超えたら hold を立て（書き込みは止めない）、
-- LA_SIZE − GAP_HOLD − GAP_ROOM 以下に減ったら下ろす。sys 側の la_stream_gap が、hold の間と非同期 FIFO が受けられない周期の拍を
-- 捨てて数え、hold が下りて FIFO が半分未満になったら欠けの記録（イベント）を FIFO に入れてから拍を出すのを再開する（欠けの記録は
-- 後の拍より先にイベントのリングに書かれる）。GAP_HOLD は sys 側が hold を見るまでに FIFO と受け渡しの中にある拍（約 520 以下）を
-- 収める余裕、GAP_ROOM は欠けを閉じてから次の欠けまでの最小の拍の数（欠けの記録が拍の流れに比べて増えすぎないための粒度。
-- 捨てるサンプルの数は変えない: リングが満ちている間は PC が読む速さで決まる）。それでも未読が LA_SIZE − STREAM_CHUNK を超えたら
-- 今までと同じく書き込みを止めて overrun（上書きはしない）。欠けの記録を書けない（イベントの記録が切れている）ときも overrun。
--
-- sys と mem の受け渡し（仕様: docs/superpowers/specs/2026-11-03-ps-stream-position-design.md）: どれも cdc_value の握手で、値の組を
-- 1 つの時点のものとして渡す（送りかけの値を変えない。流し続け = 握手の一巡ごとに最新の値）。SAME_CLOCK = true（mem_clk = clk。
-- PYNQ）は同期化を使わず 1 周期で渡す。
--   sys → mem: 開始の設定（run・流れの番号 epoch・stream・ps_read・gaps・post_beats。番号は run の 0→1 ごとに sys で 1 進め、
--     設定は同じ辺で写す）。mem は届いた番号が自分の番号と違い run = '1' なら新しい流れを始め、run = '0' を見たら止める（状態を
--     渡すので、間の値を飛ばしても始めと止めを取り違えない。前は run の辺の 1 周期の読み込みで、近い 2 つの辺で開始が消ええた。C5）。
--     読み出しの要求（通し番号つき。番号が変わったら読む）。PS の読み終えた位置（その値の流れの番号つき）。
--   mem → sys: 状態の組（流れの番号・書き込み中・トリガ・完了・溢れ・PS が読む流れの生死・応答の来た書きまでの位置・
--     読み出し器の busy・出した拍の数・受けた読み出しの番号）と、数え（総数・未読・最大・イベントの総数・stream_beats）。
-- リセットの約束（C8）: rst と mem_rst は同時にかける（電源投入・ボードのリセット）。リセットの後、流れの番号は 0、mem は最初に
-- 届いた番号を受けるだけで流れを始めない。片方だけリセットした後の reading（読み出しの完了）は次の両方のリセットまで定まらない
-- （非同期 FIFO と同じ）。
--
-- PS が DDR のリングを直接読む流れ（ps_read。仕様: 2026-11-03-ps-stream-position-design.md）: 読み終えた位置は PS が返す
-- （窓の la_ps_port が、POS_TAG が今の流れの番号の書きだけを受け、流れの番号が変わったら 0 に戻す。値と一緒にその流れの番号
-- ps_ret_epoch を渡す）。mem は ps_ret_epoch が今の流れの番号の値だけを使う。公開する位置（ps_la_written・ps_la_wptr・
-- ps_ev_wpos）は応答の来た書きまで（AXI は書きと読みの順を保証しない）。応答の来ていない書きの数 wr_pending を、書いた順の
-- 種類の並び（ロジアナの拍はこの流れの書きか）で種類ごとに引く。
library ieee;
use ieee.std_logic_1164.all;
use ieee.numeric_std.all;
use work.timing_pkg.all;
use work.util_pkg.all;
use work.mem_ring_pkg.all;

entity la_deep_buffer is
  generic (
    WIDTH         : positive := 16;
    ADDR_BITS     : positive := 23;
    STREAM_EVENTS : boolean := false;                      -- ストリームにイベントも見出し付きで流す
    STREAM_GAPS   : boolean := false;                      -- 欠けの区間を記録して続けられる（STREAM_EVENTS が要る）
    LANES         : positive := 1;                         -- 1 周期のサンプルの数（> 1 は la_lane_encoder）
    IN_AGE        : natural := 0;                          -- LANES > 1: lane_data の最も新しいレーンの遅れ（la_lane_encoder）
    EXT_DELAY     : natural := LA_FRONT_EXT_ALIGN;         -- LANES = 1: 外部トリガの揃え（la_front の注。時刻の正しさ S1b の N3）
    SAME_CLOCK    : boolean := false;                      -- mem_clk = clk（受け渡しに同期化を使わない）
    COUNT_START_BELOW_4G : natural := 0;                   -- 試験用: イベントの書いた数を 2**32 のこの数だけ手前から数える（既定 0）
    TOTAL_START_BELOW_4G : natural := 0                    -- 試験用: ロジアナの拍の総数（書いた・読んだ）を流れの開始で 2**32 のこの数だけ手前から
  );
  port (
    -- sys ドメイン
    clk          : in  std_logic;
    rst          : in  std_logic;
    probes       : in  std_logic_vector(WIDTH - 1 downto 0);
    lane_data    : in  std_logic_vector(LANES * WIDTH - 1 downto 0) := (others => '0');   -- LANES > 1 のとき probes の代わり
    step         : in  unsigned(1 downto 0) := "00";       -- LANES > 1 の RAW: サンプルの間隔 2^step 細かいサンプル（run の間は変えない）
    first_ch     : in  unsigned(3 downto 0) := "0000";     -- LANES > 1 の RAW: 詰めて記録する最初の ch（同）
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
    -- 使うチャンネルだけ詰める（la_pack。RAW のストリームだけ。0 = 詰めない、1 = ch 0〜7、2 = ch 0〜3、3 = ch 0〜1）。run の間は変えない
    pack         : in  std_logic_vector(2 downto 0) := "000";   -- 4 = ch 0 だけ（LANES > 1 の構成だけ）
    stream       : in  std_logic;                          -- run の 0→1 の時点で取り込む
    gaps         : in  std_logic := '0';                   -- 1 = 溢れても止めず欠けの区間を記録して続ける（stream と同時。run の 0→1 の時点で）
    -- PS が DDR のリングを直接読むモード（stream と同時に 1 のときだけ。run の 0→1 の時点で取り込む）。チャンクを mem_reader に
    -- 読ませず、読み終えた位置は PS が返す（流れの開始からの総数）。書いた位置は ps_* に出す（先頭の注）
    ps_read        : in  std_logic := '0';
    ps_epoch       : out unsigned(15 downto 0);             -- 流れの番号（sys。run の 0→1 ごとに 1 進む）
    ps_ret_epoch   : in  unsigned(15 downto 0) := (others => '0');   -- 下の 3 つの値の流れの番号
    ps_la_consumed : in  unsigned(31 downto 0) := (others => '0');
    ps_ev_consumed : in  unsigned(ADDR_BITS - 5 downto 0) := (others => '0');
    ps_out_beats   : in  unsigned(31 downto 0) := (others => '0');   -- PS が流れに出すと決めた拍の総数（見出し・イベントを含む。
                                                                     -- PS が読む流れでは、これが stream_beats になる）
    -- PL が公開する組（mem の 1 つの受け渡しで揃う。どれも ps_pub_epoch の流れのもの）
    ps_pub_epoch   : out unsigned(15 downto 0);
    ps_active      : out std_logic;                         -- PS が読む流れが生きている（開始から、PS が全部読み終えるまで）
    ps_writing     : out std_logic;                         -- 書き込み中
    ps_overrun     : out std_logic;                         -- 溢れて書き込みを止めた
    ps_la_written  : out unsigned(31 downto 0);             -- ロジアナの拍の書いた総数（流れの開始から。応答の来た書きまで）
    ps_la_wptr     : out unsigned(ADDR_BITS - 1 downto 0);  -- ロジアナのリングの次の書き位置（同じ時点）
    ps_ev_wpos     : out unsigned(ADDR_BITS - 5 downto 0);  -- イベントのリングの次の書き位置（応答の来た書きまで）
    ps_ev_start    : out unsigned(ADDR_BITS - 5 downto 0);  -- この流れで最初に読むイベントの位置
    -- 応答の来ていない書きの数（メモリ側。axi_mem_port の wr_pending）。0 のままなら除かない（PS が読まない構成）
    wr_pending     : in  unsigned(3 downto 0) := (others => '0');
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
    stat_written : out unsigned(31 downto 0);
    stat_unread  : out unsigned(31 downto 0);
    stat_peak    : out unsigned(31 downto 0);
    start_time   : out unsigned(63 downto 0);              -- 起点の時刻（start_valid = 今の流れのもの）。LANES = 1: 最初のサンプルを
                                                           -- trigger_time と同じ段で（R1）、LANES > 1: 最初に符号にした語の時刻
    start_valid  : out std_logic;
    latency      : out unsigned(7 downto 0);               -- LANES > 1: 細かいサンプルの番号の式の固定の遅れ（LANES = 1 は 0）
    gap_count    : out unsigned(31 downto 0);              -- この流れで出した欠けの記録の件数（la_stream_gap の gaps）
    gap_last_beat : out unsigned(31 downto 0);             -- 最後に出した欠けの記録の位置（拍）
    gap_open     : out std_logic;                          -- いま拍を捨てている
    -- DAP のイベント
    ev_enable    : in  std_logic;
    ev_valid     : in  std_logic;
    ev_data      : in  std_logic_vector(127 downto 0);
    ev_written   : out unsigned(63 downto 0);                -- 書いたイベントの総数
    ev_lost      : out unsigned(31 downto 0);                -- 記録できなかったイベントの数
    la_beats     : out unsigned(ADDR_BITS downto 0);         -- ロジアナのリングの拍数
    ev_beats     : out unsigned(ADDR_BITS downto 0);         -- イベントのリングの拍数
    read         : in  std_logic;
    -- トリガ取り込みの読み出しを PS が DDR から直接行う（read の代わりに 1 周期。read_region・read_first・read_count はその時点の値）。
    -- mem_reader は動かさず、要求を通し番号つきで公開する。PS が読み終えて番号を ps_rd_ack に返すまで、reading は 1
    read_ps      : in  std_logic := '0';
    ps_rd_ack    : in  unsigned(7 downto 0) := (others => '0');
    ps_rd_epoch  : out unsigned(7 downto 0);
    ps_rd_region : out std_logic;
    ps_rd_first  : out unsigned(ADDR_BITS - 1 downto 0);
    ps_rd_count  : out unsigned(ADDR_BITS downto 0);
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
  constant EV_BITS : natural := ring_ev_bits(ADDR_BITS);
  constant EV_SIZE : unsigned(ADDR_BITS downto 0) := ring_ev_beats(ADDR_BITS);   -- リングの割り振り（mem_ring_pkg）
  constant LA_SIZE : unsigned(ADDR_BITS downto 0) := ring_la_beats(ADDR_BITS);
  -- 受け渡しの組の幅
  constant RD_W   : natural := 1 + ADDR_BITS + ADDR_BITS + 1;      -- 領域・先頭・拍数
  constant RET_W  : natural := 16 + 32 + EV_BITS;                  -- PS の位置の流れの番号・ロジアナ・イベント
  constant CNT_W  : natural := 32 * 4 + 64;                        -- 総数・未読・最大・stream_beats・イベントの総数
  -- 数えの組 cnt_src / cnt_dst の並び（上位から: 総数・未読・最大・stream_beats・イベントの総数）の各欄の下端
  constant C_EVW  : natural := 0;
  constant C_SB   : natural := C_EVW + 64;
  constant C_PEAK : natural := C_SB + 32;
  constant C_UNR  : natural := C_PEAK + 32;
  constant C_TOT  : natural := C_UNR + 32;
  -- mem への FIFO の語 m_in / q_data の並び: [127:0] 拍またはイベント、[131:128] トリガの印と拍の中の位置（イベントは 0）、
  -- [132] 種類（'1' = イベント）、[136:133] 流れの番号の下位 4 ビット
  constant M_IN_W    : natural := 137;
  constant M_KIND    : natural := 132;
  constant M_TRIG    : natural := 131;
  constant M_EPOCH_L : natural := 133;
  -- 状態の組（mem → sys。同じ周期の値の組）。並びは下の O_*（to_bits と to_status が同じ表を使う）
  type status_t is record
    rd_no    : unsigned(7 downto 0);                               -- 受けた読み出しの要求の通し番号
    pushed   : unsigned(15 downto 0);                              -- sys への FIFO に入れた拍の数
    written  : unsigned(ADDR_BITS downto 0);                       -- 書き込み器の書いた拍の数
    beat     : unsigned(ADDR_BITS - 1 downto 0);                   -- トリガの拍
    slot     : unsigned(2 downto 0);                               -- トリガの拍の中の位置
    ev_start : unsigned(EV_BITS - 1 downto 0);                     -- この流れの最初のイベントの位置
    ev_safe  : unsigned(EV_BITS - 1 downto 0);                     -- 応答の来た書きまでのイベントの書き位置
    la_wptr  : unsigned(ADDR_BITS - 1 downto 0);                   -- 同じ時点のロジアナのリングの書き位置
    la_safe  : unsigned(31 downto 0);                              -- 応答の来た書きまでのロジアナの拍の総数
    busy     : std_logic;                                          -- 読み出しの最中
    ovf      : std_logic;                                          -- ストリームの溢れ
    done     : std_logic;
    running  : std_logic;                                          -- 書き込み中
    active   : std_logic;                                          -- PS が読む流れの最中
    epoch    : unsigned(15 downto 0);                              -- 組の流れの番号
  end record;
  constant O_RD    : natural := 0;
  constant O_PUSH  : natural := O_RD + 8;
  constant O_WR    : natural := O_PUSH + 16;
  constant O_BEAT  : natural := O_WR + ADDR_BITS + 1;
  constant O_SLOT  : natural := O_BEAT + ADDR_BITS;
  constant O_EVS   : natural := O_SLOT + 3;
  constant O_EVW   : natural := O_EVS + EV_BITS;
  constant O_WPTR  : natural := O_EVW + EV_BITS;
  constant O_LA    : natural := O_WPTR + ADDR_BITS;
  constant O_FLAGS : natural := O_LA + 32;
  constant O_EP    : natural := O_FLAGS + 5;
  constant ST_W    : natural := O_EP + 16;

  function to_bits(s : status_t) return std_logic_vector is
    variable v : std_logic_vector(ST_W - 1 downto 0);
  begin
    v(O_PUSH - 1 downto O_RD)    := std_logic_vector(s.rd_no);
    v(O_WR - 1 downto O_PUSH)    := std_logic_vector(s.pushed);
    v(O_BEAT - 1 downto O_WR)    := std_logic_vector(s.written);
    v(O_SLOT - 1 downto O_BEAT)  := std_logic_vector(s.beat);
    v(O_EVS - 1 downto O_SLOT)   := std_logic_vector(s.slot);
    v(O_EVW - 1 downto O_EVS)    := std_logic_vector(s.ev_start);
    v(O_WPTR - 1 downto O_EVW)   := std_logic_vector(s.ev_safe);
    v(O_LA - 1 downto O_WPTR)    := std_logic_vector(s.la_wptr);
    v(O_FLAGS - 1 downto O_LA)   := std_logic_vector(s.la_safe);
    v(O_FLAGS)     := s.busy;
    v(O_FLAGS + 1) := s.ovf;
    v(O_FLAGS + 2) := s.done;
    v(O_FLAGS + 3) := s.running;
    v(O_FLAGS + 4) := s.active;
    v(ST_W - 1 downto O_EP)      := std_logic_vector(s.epoch);
    return v;
  end function;

  function to_status(v : std_logic_vector(ST_W - 1 downto 0)) return status_t is
    variable s : status_t;
  begin
    s.rd_no    := unsigned(v(O_PUSH - 1 downto O_RD));
    s.pushed   := unsigned(v(O_WR - 1 downto O_PUSH));
    s.written  := unsigned(v(O_BEAT - 1 downto O_WR));
    s.beat     := unsigned(v(O_SLOT - 1 downto O_BEAT));
    s.slot     := unsigned(v(O_EVS - 1 downto O_SLOT));
    s.ev_start := unsigned(v(O_EVW - 1 downto O_EVS));
    s.ev_safe  := unsigned(v(O_WPTR - 1 downto O_EVW));
    s.la_wptr  := unsigned(v(O_LA - 1 downto O_WPTR));
    s.la_safe  := unsigned(v(O_FLAGS - 1 downto O_LA));
    s.busy     := v(O_FLAGS);
    s.ovf      := v(O_FLAGS + 1);
    s.done     := v(O_FLAGS + 2);
    s.running  := v(O_FLAGS + 3);
    s.active   := v(O_FLAGS + 4);
    s.epoch    := unsigned(v(ST_W - 1 downto O_EP));
    return s;
  end function;
  -- 欠けの区間の RAW の 1 拍のサンプルの数の log2（pack = 0）: LANES = 1 は la_pack の 16 ビットの語 8 つ、LANES > 1 は la_lane_raw
  function spb_log return natural is
  begin
    if LANES = 1 then
      return 3;
    end if;
    return clog2(128 / WIDTH);
  end function;
  -- sys
  signal enc_triggered, enc_valid, enc_ready, enc_trig, enc_overrun : std_logic;
  signal enc_data : std_logic_vector(127 downto 0);
  signal enc_slot : unsigned(2 downto 0);
  signal trig_prev : std_logic := '0';
  signal time_r : unsigned(63 downto 0) := (others => '0');
  signal epoch_r : unsigned(15 downto 0);                                       -- 流れの番号（la_run_cdc）
  signal run_q, ps_last : std_logic;
  -- 欠けの区間（sys）
  signal gap_en, gap_hold_s, fifo_room, gap_lost : std_logic;
  signal gap_pack : unsigned(2 downto 0);
  signal g_valid, g_ready, g_kind : std_logic;
  signal g_data : std_logic_vector(127 downto 0);
  signal g_tag : std_logic_vector(3 downto 0);
  -- 読み出しの要求（sys → mem）
  signal rd_no, m_rd_no : unsigned(7 downto 0);
  signal m_rd_new : std_logic;
  signal m_rd : std_logic_vector(RD_W - 1 downto 0);
  signal m_rd_region : std_logic;
  signal m_rd_first : unsigned(ADDR_BITS - 1 downto 0);
  signal m_rd_count : unsigned(ADDR_BITS downto 0);
  -- イベントの合流
  signal evq_valid, evq_ready, m_in_valid, m_in_ready : std_logic;
  signal evq_data : std_logic_vector(127 downto 0);
  signal m_in : std_logic_vector(M_IN_W - 1 downto 0);                                -- 流れの番号の下位 4 | 種類 | 印 | 拍
  signal ev_en_m : std_logic;
  signal m_ev_written : unsigned(63 downto 0);
  signal beat_valid, beat_ready : std_logic;
  signal beat_data : std_logic_vector(127 downto 0);
  -- 状態の組（mem → sys）と数え
  signal st_dst : std_logic_vector(ST_W - 1 downto 0);
  signal cnt_src, cnt_dst : std_logic_vector(CNT_W - 1 downto 0);
  signal st : status_t;                                                         -- sys が受けた組
  signal popped : unsigned(15 downto 0) := (others => '0');                      -- sys: FIFO から取り出した拍の数
  -- mem: 流れの開始と設定（la_run_cdc）
  signal m_start, m_stop, m_stream, m_ps_read, m_gaps : std_logic;
  signal m_epoch : unsigned(15 downto 0);
  signal m_post : unsigned(ADDR_BITS - 1 downto 0);
  signal ret_src, ret_dst : std_logic_vector(RET_W - 1 downto 0);
  signal region_base : unsigned(ADDR_BITS - 1 downto 0);
  signal region_size : unsigned(ADDR_BITS downto 0);
  signal f_valid, f_ready : std_logic;
  signal f_data : std_logic_vector(M_IN_W - 1 downto 0);
  -- FIFO（ブロック RAM の読み出し）と書き込みの間の受け渡しの段（mem の頭打ちの経路: ブロック RAM の出力から書き込みの
  -- 選択を通って DDR3 の受け渡しまでを、レジスタで切る）
  signal q_valid, q_ready : std_logic;
  signal q_data : std_logic_vector(M_IN_W - 1 downto 0);
  -- 拍の流れの番号の関所: ロジアナの拍は、書き込み器が同じ番号の流れを始めてから渡す（先の流れの拍は待たせ、前の流れの拍は捨てる）
  signal g_gen_d : unsigned(3 downto 0);                                        -- 拍の番号と、mem の流れの番号の差（mod 16）
  signal w_in_valid, w_in_ready, beat_old : std_logic;
  signal w_running, w_done : std_logic;
  signal w_beat : unsigned(ADDR_BITS - 1 downto 0);
  signal w_slot : unsigned(2 downto 0);
  signal w_written : unsigned(ADDR_BITS downto 0);
  signal w_ptr : unsigned(ADDR_BITS - 1 downto 0);
  signal w_total : unsigned(31 downto 0);
  signal w_valid, w_ready, w_write, r_valid, r_ready, r_write, r_busy : std_logic;
  signal w_addr, r_addr : unsigned(ADDR_BITS - 1 downto 0);
  signal w_data, r_data : std_logic_vector(127 downto 0);
  signal ro_valid, ro_ready : std_logic;
  signal ro_data : std_logic_vector(127 downto 0);
  signal m_busy : std_logic;
  signal m_pushed : unsigned(15 downto 0) := (others => '0');                   -- mem: sys への FIFO に入れた拍の数
  -- 読み出し器の起動（読み出しの要求か、ストリームのかたまり）
  signal rdr_start, rdr_region : std_logic;
  signal rdr_first : unsigned(ADDR_BITS - 1 downto 0);
  signal rdr_count : unsigned(ADDR_BITS downto 0);
  -- ストリーミング（mem。la_stream_sched）
  signal s_on, ps_on, ev_keep_m, hold_m, m_ovf, m_full_stop : std_logic;
  signal s_start, s_region, hdr_valid : std_logic;
  signal s_first : unsigned(ADDR_BITS - 1 downto 0);
  signal s_count : unsigned(ADDR_BITS downto 0);
  signal hdr_data : std_logic_vector(127 downto 0);
  signal stream_epoch : unsigned(15 downto 0);
  signal unread_r, unread_total, peak_now, o_total, sb_src : unsigned(31 downto 0);
  signal ev_wpos, ev_safe, ev_pub, ev_start : unsigned(EV_BITS - 1 downto 0);
  signal ev_unacked : unsigned(3 downto 0);
  signal la_safe : unsigned(31 downto 0);                                       -- 応答の来た書きまでのロジアナの拍の総数
  signal la_wptr_safe : unsigned(ADDR_BITS - 1 downto 0);                      -- 同じ時点のロジアナのリングの書き位置
  signal ts_valid, ts_ready : std_logic;
  signal ts_data : std_logic_vector(127 downto 0);
  signal st_reg : std_logic_vector(ST_W - 1 downto 0) := (others => '0');      -- 状態の組（同じ周期の値）のレジスタ
  -- PS への読み出しの要求（sys）
  signal rd_epoch : unsigned(7 downto 0) := (others => '0');
  signal rd_region : std_logic := '0';
  signal rd_first : unsigned(ADDR_BITS - 1 downto 0) := (others => '0');
  signal rd_count : unsigned(ADDR_BITS downto 0) := (others => '0');
  signal rd_ps_pending : std_logic;                                              -- PS がまだ読み終えていない要求がある
begin
  ---------------------------------------------------------------------------------------------
  -- sys: 前段・符号器
  ---------------------------------------------------------------------------------------------
  front_i : entity work.la_deep_front
    generic map (WIDTH => WIDTH, LANES => LANES, IN_AGE => IN_AGE, EXT_DELAY => EXT_DELAY)
    port map (clk => clk, rst => rst, run => run, rle => rle, pack => pack, step => step, first_ch => first_ch,
              probes => probes, lane_data => lane_data, ext_trigger => ext_trigger, time => time, rate => rate,
              pretrigger => pretrigger, trig_mask => trig_mask, trig_value => trig_value, trig_rise => trig_rise,
              trig_fall => trig_fall, pw_enable => pw_enable, pw_event => pw_event,
              enc_valid => enc_valid, enc_ready => enc_ready, enc_data => enc_data, enc_trig => enc_trig,
              enc_slot => enc_slot, enc_triggered => enc_triggered, enc_overrun => enc_overrun,
              start_time => start_time, start_valid => start_valid, latency => latency, gap_pack => gap_pack);
  overrun <= enc_overrun or st.ovf or gap_lost;

  -- トリガの時刻（符号器がトリガを見た周期）
  process (clk)
  begin
    if rising_edge(clk) then
      trig_prev <= enc_triggered;
      if run = '1' and enc_triggered = '1' and trig_prev = '0' then
        time_r <= time;
      end if;
    end if;
  end process;

  ---------------------------------------------------------------------------------------------
  -- sys → mem: 開始の設定・読み出しの要求・PS の読み終えた位置
  ---------------------------------------------------------------------------------------------
  run_i : entity work.la_run_cdc
    generic map (POST_BITS => ADDR_BITS, SAME_CLOCK => SAME_CLOCK)
    port map (clk => clk, rst => rst, run => run, stream => stream, ps_read => ps_read, gaps => gap_en,
              post_beats => post_beats, epoch => epoch_r, run_q => run_q, ps_last => ps_last,
              mem_clk => mem_clk, mem_rst => mem_rst, m_start => m_start, m_stop => m_stop, m_epoch => m_epoch,
              m_stream => m_stream, m_ps_read => m_ps_read, m_gaps => m_gaps, m_post => m_post);
  ps_epoch <= epoch_r;

  -- 読み出しの要求: 要求の時点の領域・先頭・拍数を通し番号つきで渡す（mem は番号が変わったら読む）
  rd_i : entity work.cdc_request
    generic map (WIDTH => RD_W, SAME_CLOCK => SAME_CLOCK)
    port map (src_clk => clk, src_rst => rst, req => read,
              value => read_region & std_logic_vector(read_first) & std_logic_vector(read_count), src_seq => rd_no,
              dst_clk => mem_clk, dst_rst => mem_rst, dst_seq => m_rd_no, dst_new => m_rd_new, dst_value => m_rd);
  m_rd_region <= m_rd(RD_W - 1);
  m_rd_first  <= unsigned(m_rd(2 * ADDR_BITS downto ADDR_BITS + 1));
  m_rd_count  <= unsigned(m_rd(ADDR_BITS downto 0));

  -- PS の読み終えた位置（その値の流れの番号つき。流し続ける）
  ret_src <= std_logic_vector(ps_ret_epoch) & std_logic_vector(ps_la_consumed) & std_logic_vector(ps_ev_consumed);
  ret_i : entity work.cdc_value
    generic map (WIDTH => RET_W, SAME_CLOCK => SAME_CLOCK)
    port map (src_clk => clk, src_rst => rst, src_load => '1', src_value => ret_src, src_ready => open,
              dst_clk => mem_clk, dst_rst => mem_rst, dst_value => ret_dst, dst_changed => open);

  ---------------------------------------------------------------------------------------------
  -- sys: 拍とイベントの合流 → mem への FIFO
  ---------------------------------------------------------------------------------------------
  -- イベント: 1 件の保持レジスタで待たせ、ロジアナの拍が無い周期に合流する（イベントは連続する周期に来ることがある:
  -- DAP の転送の直後のバイト等）。空く前に来たものは ev_lost に数える
  evq_i : entity work.hold_one
    generic map (WIDTH => 128)
    port map (clk => clk, rst => rst, enable => ev_enable, in_valid => ev_valid, in_data => ev_data,
              out_valid => evq_valid, out_ready => evq_ready, out_data => evq_data, lost => ev_lost);

  -- 符号器の拍 → 欠けの区間（gaps でない流れは素通し）→ FIFO。欠けの記録はイベント（種類 '1'）として DAP のイベントより先に
  gap_g : if STREAM_GAPS generate
    assert STREAM_EVENTS report "la_deep_buffer: STREAM_GAPS needs STREAM_EVENTS" severity failure;
    gap_en <= gaps and stream;
    hold_i : entity work.sync_bit
      generic map (SAME_CLOCK => SAME_CLOCK)
      port map (clk => clk, d => hold_m, q => gap_hold_s);
    gap_i : entity work.la_stream_gap
      generic map (SPB_LOG => spb_log)
      port map (clk => clk, rst => rst, run => run, enable => gap_en, rle => rle, pack => gap_pack, time => time,
                hold => gap_hold_s, room => fifo_room, ev_enable => ev_enable,
                in_valid => enc_valid, in_ready => enc_ready, in_data => enc_data, in_tag => enc_trig & std_logic_vector(enc_slot),
                out_valid => g_valid, out_ready => g_ready, out_kind => g_kind, out_data => g_data, out_tag => g_tag,
                gaps => gap_count, last_beat => gap_last_beat, gap_open => gap_open, lost => gap_lost);
  end generate;
  no_gap_g : if not STREAM_GAPS generate
    gap_en <= '0'; gap_hold_s <= '0'; gap_lost <= '0'; gap_open <= '0';
    gap_count <= (others => '0'); gap_last_beat <= (others => '0');
    g_valid <= enc_valid; g_kind <= '0'; g_data <= enc_data; g_tag <= enc_trig & std_logic_vector(enc_slot);
    enc_ready <= g_ready;
  end generate;

  m_in_valid <= g_valid or evq_valid;
  -- 拍にはその流れの番号の下位 4 ビットを付ける（mem は書き込み器がその流れを始めるまで待たせる。開始の受け渡しと拍の
  -- FIFO の遅れの競争にしない）。イベント（欠けの記録を含む）は流れによらずリングに書くので、番号を見ない
  m_in       <= std_logic_vector(epoch_r(3 downto 0)) & g_kind & g_tag & g_data when g_valid = '1' else
                std_logic_vector(epoch_r(3 downto 0)) & '1' & "0000" & evq_data;
  g_ready    <= m_in_ready;
  evq_ready  <= m_in_ready and not g_valid;

  to_mem_i : entity work.async_fifo
    generic map (WIDTH => M_IN_W, DEPTH => 512)
    port map (in_clk => clk, in_rst => rst, in_valid => m_in_valid, in_ready => m_in_ready, in_data => m_in,
              in_below_half => fifo_room,
              out_clk => mem_clk, out_rst => mem_rst, out_valid => f_valid, out_ready => f_ready, out_data => f_data);
  to_mem_slice_i : entity work.stream_slice
    generic map (WIDTH => M_IN_W)
    port map (clk => mem_clk, rst => mem_rst, in_valid => f_valid, in_ready => f_ready, in_data => f_data,
              out_valid => q_valid, out_ready => q_ready, out_data => q_data);

  ---------------------------------------------------------------------------------------------
  -- mem: リング書き込み
  ---------------------------------------------------------------------------------------------
  writer_i : entity work.la_mem_writer
    generic map (ADDR_BITS => ADDR_BITS, COUNT_START_BELOW_4G => COUNT_START_BELOW_4G,
                 TOTAL_START_BELOW_4G => TOTAL_START_BELOW_4G)
    port map (clk => mem_clk, rst => mem_rst, start => m_start, stop => m_stop or m_full_stop,
              post_beats => m_post, stream => m_stream, ev_enable => ev_en_m, ev_keep => ev_keep_m,
              in_valid => w_in_valid, in_ready => w_in_ready, in_data => q_data(127 downto 0), in_kind => q_data(M_KIND),
              in_trig => q_data(M_TRIG), in_slot => unsigned(q_data(M_TRIG - 1 downto 128)),
              running => w_running, triggered => open, done => w_done, trigger_beat => w_beat,   -- triggered は sys の符号器の値を使う
              trigger_slot => w_slot, written => w_written, total => w_total, la_pos => w_ptr, ev_written => m_ev_written,
              ev_pos => ev_wpos,
              cmd_valid => w_valid, cmd_ready => w_ready, cmd_write => w_write, cmd_addr => w_addr, cmd_data => w_data);

  -- 流れの番号の関所（ロジアナの拍だけ）: 差 0 = いまの流れ（書き込み器へ。止まっていれば書き込み器が捨てる）、1〜7 = 先の流れ
  -- （書き込み器がその流れを始めるまで待たせる）、8〜15 = 前の流れ（捨てる）。mem の番号は書き込み器の開始と同じ辺で変わる
  -- stream_epoch。sys が mem より先に進むのは高々数個（開始の受け渡しの間）
  g_gen_d  <= unsigned(q_data(M_IN_W - 1 downto M_EPOCH_L)) - stream_epoch(3 downto 0);
  beat_old <= '1' when q_data(M_KIND) = '0' and g_gen_d(3) = '1' else '0';
  w_in_valid <= q_valid when q_data(M_KIND) = '1' or g_gen_d = 0 else '0';
  q_ready    <= w_in_ready when q_data(M_KIND) = '1' or g_gen_d = 0 else beat_old;

  ev_en_i : entity work.sync_bit
    generic map (SAME_CLOCK => SAME_CLOCK)
    port map (clk => mem_clk, d => ev_enable, q => ev_en_m);

  -- 応答の来た書きまでの位置（PS に公開する）
  track_i : entity work.la_write_track
    generic map (ADDR_BITS => ADDR_BITS)
    port map (clk => mem_clk, rst => mem_rst, start => m_start, wr_fire => w_valid and w_ready, wr_event => q_data(M_KIND),
              wr_pending => wr_pending, la_total => w_total, la_ptr => w_ptr, ev_pos => ev_wpos,
              la_safe => la_safe, la_ptr_safe => la_wptr_safe, ev_safe => ev_safe, ev_unacked => ev_unacked);

  ---------------------------------------------------------------------------------------------
  -- mem: ストリームの段取りと読み出し
  ---------------------------------------------------------------------------------------------
  sched_i : entity work.la_stream_sched
    generic map (ADDR_BITS => ADDR_BITS, STREAM_EVENTS => STREAM_EVENTS, TOTAL_START_BELOW_4G => TOTAL_START_BELOW_4G)
    port map (clk => mem_clk, rst => mem_rst, start => m_start, epoch => m_epoch, stream => m_stream, gaps => m_gaps,
              ps_read => m_ps_read, ev_first => m_post(EV_BITS - 1 downto 0),
              w_total => w_total, w_running => w_running, ev_wpos => ev_wpos, ev_unacked => ev_unacked, ev_safe => ev_safe, full_stop => m_full_stop,
              ret_epoch => unsigned(ret_dst(RET_W - 1 downto RET_W - 16)),
              ret_la => unsigned(ret_dst(31 + EV_BITS downto EV_BITS)), ret_ev => unsigned(ret_dst(EV_BITS - 1 downto 0)),
              rd_busy => r_busy, rd_start => s_start, rd_region => s_region, rd_first => s_first, rd_count => s_count,
              hdr_valid => hdr_valid, hdr_ready => ts_ready, hdr_data => hdr_data,
              active => s_on, ps_on => ps_on, ev_keep => ev_keep_m, hold => hold_m, ovf => m_ovf,
              stream_epoch => stream_epoch, ev_start => ev_start, ev_pub => ev_pub,
              unread => unread_r, unread_total => unread_total, peak => peak_now, out_total => o_total);

  rdr_start  <= m_rd_new or s_start;
  rdr_first  <= s_first when s_start = '1' else m_rd_first;
  rdr_count  <= s_count when s_start = '1' else m_rd_count;
  rdr_region <= m_rd_region when s_on = '0' else s_region;
  region_base <= resize(LA_SIZE, ADDR_BITS) when rdr_region = '1' else (others => '0');
  region_size <= EV_SIZE when rdr_region = '1' else LA_SIZE;

  reader_i : entity work.mem_reader
    generic map (ADDR_BITS => ADDR_BITS)
    port map (clk => mem_clk, rst => mem_rst, start => rdr_start,
              region_base => region_base, region_size => region_size,
              first => rdr_first, count => rdr_count, busy => r_busy,
              out_valid => ro_valid, out_ready => ro_ready, out_data => ro_data,
              cmd_valid => r_valid, cmd_ready => r_ready, cmd_write => r_write, cmd_addr => r_addr, cmd_data => r_data,
              rd_valid => rd_valid, rd_data => rd_data);

  -- mem_port: 書き込みと読み出しが両方求めていれば交互に通す（ストリーミング中は同時に動く）。読み出しの結果は読み出し器だけが受ける
  arb_i : entity work.mem_cmd_arb
    generic map (ADDR_BITS => ADDR_BITS)
    port map (clk => mem_clk, rst => mem_rst,
              a_valid => r_valid, a_ready => r_ready, a_write => r_write, a_addr => r_addr, a_data => r_data,
              b_valid => w_valid, b_ready => w_ready, b_write => w_write, b_addr => w_addr, b_data => w_data,
              cmd_valid => cmd_valid, cmd_ready => cmd_ready, cmd_write => cmd_write, cmd_addr => cmd_addr,
              cmd_data => cmd_data);
  -- 読み出し・流れの最中（読み出し器・流れ・FIFO に入れる拍・始めの周期のどれか）
  m_busy     <= r_busy or s_on or ts_valid or rdr_start;
  mem_active <= m_busy or w_running or ev_en_m;               -- イベントの記録中も使う

  -- 読み出した拍（と STREAM_EVENTS の見出し）→ sys → バイト列。見出しは読み出し器が空いている間にだけ出る
  ts_valid <= ro_valid or hdr_valid;
  ts_data  <= hdr_data when hdr_valid = '1' else ro_data;
  ro_ready <= ts_ready and not hdr_valid;
  to_sys_i : entity work.async_fifo
    generic map (WIDTH => 128, DEPTH => 64)
    port map (in_clk => mem_clk, in_rst => mem_rst, in_valid => ts_valid, in_ready => ts_ready, in_data => ts_data,
              out_clk => clk, out_rst => rst, out_valid => beat_valid, out_ready => beat_ready, out_data => beat_data);
  -- 入れた拍・取り出した拍の数（読み出しの完了: mem が busy でなく、入れた数を sys が全部取り出し、分解し終えた）
  process (mem_clk)
  begin
    if rising_edge(mem_clk) then
      if mem_rst = '1' then
        m_pushed <= (others => '0');
      elsif ts_valid = '1' and ts_ready = '1' then
        m_pushed <= m_pushed + 1;
      end if;
    end if;
  end process;
  process (clk)
  begin
    if rising_edge(clk) then
      if rst = '1' then
        popped <= (others => '0');
      elsif beat_valid = '1' and beat_ready = '1' then
        popped <= popped + 1;
      end if;
    end if;
  end process;

  unpack_i : entity work.stream_unpack
    generic map (BYTES => 16)
    port map (clk => clk, rst => rst, in_valid => beat_valid, in_ready => beat_ready, in_data => beat_data,
              out_valid => out_valid, out_ready => out_ready, out_data => out_data);

  ---------------------------------------------------------------------------------------------
  -- mem → sys: 状態の組と数え（流し続ける。状態の組は 1 つの時点のもの）
  ---------------------------------------------------------------------------------------------
  -- 同じ周期の値を 1 つのレジスタに写してから送る（流れの番号・総数・状態は m_start の辺で一緒に変わる）
  process (mem_clk)
  begin
    if rising_edge(mem_clk) then
      st_reg <= to_bits((rd_no => m_rd_no, pushed => m_pushed, written => w_written, beat => w_beat, slot => w_slot,
                      ev_start => ev_start, ev_safe => ev_pub, la_wptr => la_wptr_safe, la_safe => la_safe,
                      busy => m_busy, ovf => m_ovf, done => w_done, running => w_running, active => ps_on and s_on,
                      epoch => stream_epoch));
    end if;
  end process;
  st_i : entity work.cdc_value
    generic map (WIDTH => ST_W, SAME_CLOCK => SAME_CLOCK)
    port map (src_clk => mem_clk, src_rst => mem_rst, src_load => '1', src_value => st_reg, src_ready => open,
              dst_clk => clk, dst_rst => rst, dst_value => st_dst, dst_changed => open);
  st <= to_status(st_dst);

  -- stream_beats: ストリームで書いた拍の総数。STREAM_EVENTS では出すと決めた拍の総数（見出し・イベントを含む）
  sb_src  <= o_total when STREAM_EVENTS else w_total;
  cnt_src <= std_logic_vector(unread_total) & std_logic_vector(unread_r) & std_logic_vector(peak_now) & std_logic_vector(sb_src)
             & std_logic_vector(m_ev_written);
  cnt_i : entity work.cdc_value
    generic map (WIDTH => CNT_W, SAME_CLOCK => SAME_CLOCK)
    port map (src_clk => mem_clk, src_rst => mem_rst, src_load => '1', src_value => cnt_src, src_ready => open,
              dst_clk => clk, dst_rst => rst, dst_value => cnt_dst, dst_changed => open);
  stat_written <= unsigned(cnt_dst(C_TOT + 31 downto C_TOT));
  stat_unread  <= unsigned(cnt_dst(C_UNR + 31 downto C_UNR));
  stat_peak    <= unsigned(cnt_dst(C_PEAK + 31 downto C_PEAK));
  stream_beats <= ps_out_beats when ps_last = '1' else unsigned(cnt_dst(C_SB + 31 downto C_SB));
  ev_written   <= unsigned(cnt_dst(C_EVW + 63 downto C_EVW));

  -- PS に公開する組（どれも st.epoch の流れのもの）
  ps_pub_epoch  <= st.epoch;
  ps_active     <= st.active;
  ps_writing    <= st.running;
  ps_overrun    <= st.ovf;
  ps_la_written <= st.la_safe;
  ps_la_wptr    <= st.la_wptr;
  ps_ev_wpos    <= st.ev_safe;
  ps_ev_start   <= st.ev_start;
  la_beats      <= LA_SIZE;
  ev_beats      <= EV_SIZE;

  -- PS への読み出しの要求（sys）: 要求の時点の領域・先頭・拍数を持ち、通し番号を 1 進める。PS が同じ番号を返すまで「読み出し中」。
  -- 4 つの値は同じ周期に変わる（窓の la_ps_port は RD_EPOCH の読みで写す）
  process (clk)
  begin
    if rising_edge(clk) then
      if rst = '1' then
        rd_epoch <= (others => '0');
      elsif read_ps = '1' then
        rd_epoch  <= rd_epoch + 1;
        rd_region <= read_region;
        rd_first  <= read_first;
        rd_count  <= read_count;
      end if;
    end if;
  end process;
  rd_ps_pending <= '1' when rd_epoch /= ps_rd_ack else '0';
  ps_rd_epoch   <= rd_epoch;
  ps_rd_region  <= rd_region;
  ps_rd_first   <= rd_first;
  ps_rd_count   <= rd_count;

  -- sys の状態。開始（run の 0→1）から、mem がこの流れの状態を返すまでは捕捉中（前回の done を見せない）。
  -- 読み出し中: 読み出しの要求を mem が受けていない・mem が busy・入れた拍を取り出し終えていない・分解し終えていない・
  -- PS への読み出しの要求に返事が無い
  process (all)
    variable starting : boolean;
  begin
    starting := run = '1' and (run_q = '0' or st.epoch /= epoch_r);
    capturing <= st.running;
    if starting then
      capturing <= '1';
    end if;
    done <= '0';
    if st.epoch = epoch_r and not starting then
      done <= st.done;
    end if;
    reading <= st.busy or out_valid or rd_ps_pending;
    if rd_no /= st.rd_no or popped /= st.pushed then
      reading <= '1';
    end if;
  end process;
  triggered    <= enc_triggered;
  trigger_slot <= st.slot;
  trigger_beat <= st.beat;
  written      <= st.written;
  trigger_time <= time_r;
end architecture;
