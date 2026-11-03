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
  constant TOTAL_START : unsigned(31 downto 0) := to_unsigned(0, 32) - to_unsigned(TOTAL_START_BELOW_4G, 32);
  -- 欠けの区間（STREAM_GAPS）: hold を立てる未読の余裕と、下ろすまでに減らす拍の数（先頭の注）
  constant GAP_HOLD : natural := minimum(1024, to_integer(LA_SIZE) / 4);
  constant GAP_ROOM : natural := minimum(4096, to_integer(LA_SIZE) / 4);
  -- 受け渡しの組の幅
  constant CTL_W  : natural := 16 + 4 + ADDR_BITS;                 -- epoch・run・gaps・ps_read・stream・post_beats
  constant RD_W   : natural := 8 + 1 + ADDR_BITS + ADDR_BITS + 1;  -- 通し番号・領域・先頭・拍数
  constant RET_W  : natural := 16 + 32 + EV_BITS;                  -- PS の位置の流れの番号・ロジアナ・イベント
  constant ST_W   : natural := 16 + 6 + 32 + ADDR_BITS + 2 * EV_BITS + 3 + ADDR_BITS + ADDR_BITS + 1 + 16 + 8;
  constant CNT_W  : natural := 32 * 4 + 64;                        -- 総数・未読・最大・stream_beats・イベントの総数
  -- 欠けの区間の RAW の 1 拍のサンプルの数の log2（pack = 0）: LANES = 1 は la_pack の 16 ビットの語 8 つ、LANES > 1 は la_lane_raw
  function spb_log return natural is
  begin
    if LANES = 1 then
      return 3;
    end if;
    return clog2(128 / WIDTH);
  end function;
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
  signal pack_eff : std_logic_vector(1 downto 0);
  signal pk_valid, pk_hit : std_logic;
  signal pk_data : std_logic_vector(15 downto 0);
  signal enc_data : std_logic_vector(127 downto 0);
  signal enc_slot : unsigned(2 downto 0);
  signal run_prev, trig_prev : std_logic := '0';
  signal time_r : unsigned(63 downto 0) := (others => '0');
  signal first_wait, first_seen, started_r : std_logic := '0';                -- LANES = 1 の連続記録の起点（R1）
  signal start_r : unsigned(63 downto 0) := (others => '0');
  -- 開始の設定（sys）: 流れの番号と、run の 0→1 で写した設定。run_q は run を同じ辺で写したもの（組が揃う）
  signal epoch_r : unsigned(15 downto 0) := (others => '0');
  signal run_q, cfg_stream, cfg_ps_read, cfg_gaps : std_logic := '0';
  signal cfg_post : unsigned(ADDR_BITS - 1 downto 0) := (others => '0');
  signal ctl_src, ctl_dst : std_logic_vector(CTL_W - 1 downto 0);
  signal ctl_new : std_logic;
  -- 欠けの区間（sys）
  signal gap_en, gap_hold_s, fifo_room, gap_lost : std_logic;
  signal gap_pack : unsigned(2 downto 0);
  signal g_valid, g_ready, g_kind : std_logic;
  signal g_data : std_logic_vector(127 downto 0);
  signal g_tag : std_logic_vector(3 downto 0);
  signal enc_overrun : std_logic;
  -- 読み出しの要求（sys）: 通し番号と、要求の時点の値
  signal rd_no : unsigned(7 downto 0) := (others => '0');
  signal rdq_region : std_logic := '0';
  signal rdq_first : unsigned(ADDR_BITS - 1 downto 0) := (others => '0');
  signal rdq_count : unsigned(ADDR_BITS downto 0) := (others => '0');
  signal rd_src, rd_dst : std_logic_vector(RD_W - 1 downto 0);
  signal rd_new : std_logic;
  -- イベントの合流
  signal ev_in_valid, evq_in_ready, evq_ready, m_in_valid, m_in_ready : std_logic;
  signal evq_valid : std_logic := '0';
  signal evq_data : std_logic_vector(127 downto 0) := (others => '0');
  signal m_in : std_logic_vector(136 downto 0);                                -- 流れの番号の下位 4 | 種類 | 印 | 拍
  signal lost : unsigned(31 downto 0) := (others => '0');
  signal ev_en_m : std_logic;
  signal m_ev_written : unsigned(63 downto 0);
  signal beat_valid, beat_ready : std_logic;
  signal beat_data : std_logic_vector(127 downto 0);
  signal ev_en_prev : std_logic := '0';
  -- 状態の組（mem → sys）と数え
  signal st_src, st_dst : std_logic_vector(ST_W - 1 downto 0);
  signal cnt_src, cnt_dst : std_logic_vector(CNT_W - 1 downto 0);
  signal s_epoch : unsigned(15 downto 0);                                        -- sys が受けた組の各値
  signal s_active, s_writing, s_running, s_triggered, s_done, s_ovf, s_busy : std_logic;
  signal s_la_safe : unsigned(31 downto 0);
  signal s_la_wptr : unsigned(ADDR_BITS - 1 downto 0);
  signal s_ev_safe, s_ev_start : unsigned(EV_BITS - 1 downto 0);
  signal s_slot : unsigned(2 downto 0);
  signal s_beat : unsigned(ADDR_BITS - 1 downto 0);
  signal s_written : unsigned(ADDR_BITS downto 0);
  signal s_pushed : unsigned(15 downto 0);
  signal s_rd_no : unsigned(7 downto 0);
  signal popped : unsigned(15 downto 0) := (others => '0');                      -- sys: FIFO から取り出した拍の数
  signal ps_last : std_logic := '0';                                            -- sys: 最後に始めた流れは PS が読む流れ
  -- mem
  signal m_run, stop_next : std_logic := '0';                                   -- mem の run・始めた次の周期に止める
  signal m_epoch : unsigned(15 downto 0) := (others => '0');                    -- mem: いま受け持つ流れの番号
  signal m_cfg : std_logic_vector(CTL_W - 1 downto 0) := (others => '0');       -- mem: 始めた流れの設定（ctl_dst の写し）
  signal m_rd_no : unsigned(7 downto 0) := (others => '0');
  signal m_rd_new : std_logic := '0';
  signal m_rd_region : std_logic := '0';
  signal m_rd_first : unsigned(ADDR_BITS - 1 downto 0) := (others => '0');
  signal m_rd_count : unsigned(ADDR_BITS downto 0) := (others => '0');
  signal ret_src, ret_dst : std_logic_vector(RET_W - 1 downto 0);
  signal ret_epoch : unsigned(15 downto 0);
  signal ret_la : unsigned(31 downto 0);
  signal ret_ev : unsigned(EV_BITS - 1 downto 0);
  signal region_base : unsigned(ADDR_BITS - 1 downto 0);
  signal region_size : unsigned(ADDR_BITS downto 0);
  signal m_start, m_stop : std_logic := '0';
  signal f_valid, f_ready : std_logic;
  signal f_data : std_logic_vector(136 downto 0);
  -- FIFO（ブロック RAM の読み出し）と書き込みの間の受け渡しの段（mem の頭打ちの経路: ブロック RAM の出力から書き込みの
  -- 選択を通って DDR3 の受け渡しまでを、レジスタで切る）
  signal q_valid, q_ready : std_logic;
  signal q_data : std_logic_vector(136 downto 0);
  -- 拍の流れの番号の関所: ロジアナの拍は、書き込み器が同じ番号の流れを始めてから渡す（先の流れの拍は待たせ、前の流れの拍は捨てる）
  signal g_gen_d : unsigned(3 downto 0);                                        -- 拍の番号と、mem の流れの番号の差（mod 16）
  signal w_in_valid, w_in_ready, beat_old : std_logic;
  signal w_running, w_triggered, w_done : std_logic;
  signal w_beat : unsigned(ADDR_BITS - 1 downto 0);
  signal w_slot : unsigned(2 downto 0);
  signal w_written : unsigned(ADDR_BITS downto 0);
  signal w_ptr : unsigned(ADDR_BITS - 1 downto 0);
  signal w_valid, w_ready, w_write, r_valid, r_ready, r_write, r_busy, r_rd_valid : std_logic;
  signal w_addr, r_addr : unsigned(ADDR_BITS - 1 downto 0);
  signal w_data, r_data : std_logic_vector(127 downto 0);
  signal ro_valid, ro_ready : std_logic;
  signal ro_data : std_logic_vector(127 downto 0);
  signal m_busy : std_logic;
  signal m_pushed : unsigned(15 downto 0) := (others => '0');                   -- mem: sys への FIFO に入れた拍の数
  -- ストリーミング（mem）
  signal w_total : unsigned(31 downto 0);
  signal r_total : unsigned(31 downto 0) := (others => '0');
  signal s_on, s_start, m_ovf, m_full_stop : std_logic := '0';
  signal gaps_m, hold_m : std_logic := '0';                                     -- 欠けの区間（mem）
  -- T_WAIT: 読み出し器が始めを受けた（busy）のを待つ。その時点で r_total の更新が unread_r に届いている
  type stream_state_t is (T_IDLE, T_SUM, T_HDR, T_GO, T_WAIT);
  signal s_state : stream_state_t := T_IDLE;
  signal unread_r : unsigned(31 downto 0) := (others => '0');
  signal peak_r : unsigned(31 downto 0) := (others => '0');
  signal unread_total : unsigned(31 downto 0) := (others => '0');               -- unread_r を求めた周期の w_total（数えの組が揃う）
  signal peak_now : unsigned(31 downto 0);
  signal next_sum : unsigned(ADDR_BITS downto 0) := (others => '0');
  signal r_ptr : unsigned(ADDR_BITS - 1 downto 0) := (others => '0');
  signal rdr_start : std_logic;
  signal rdr_first : unsigned(ADDR_BITS - 1 downto 0) := (others => '0');
  signal rdr_count : unsigned(ADDR_BITS downto 0) := (others => '0');
  signal rdr_region : std_logic := '0';
  signal sel_r : std_logic := '0';
  signal rdr_first_mux : unsigned(ADDR_BITS - 1 downto 0);
  signal rdr_count_mux : unsigned(ADDR_BITS downto 0);
  -- イベントも流す（STREAM_EVENTS。false なら定数のまま）
  signal ev_wpos : unsigned(EV_BITS - 1 downto 0);                             -- イベントの次の書き位置
  signal ev_rpos, ev_lim : unsigned(EV_BITS - 1 downto 0) := (others => '0');  -- 読み位置・流す終わり
  signal ev_unread_r, ev_back_r : unsigned(EV_BITS - 1 downto 0) := (others => '0');
  signal ev_on, ev_keep_m : std_logic := '0';
  signal seg_kind, last_kind : std_logic := '0';                              -- '0' = ロジアナ、'1' = イベント
  signal hdr_valid : std_logic := '0';
  signal hdr_lo : std_logic_vector(63 downto 0) := (others => '0');
  signal o_total : unsigned(31 downto 0) := (others => '0');                  -- 出すと決めた拍の総数
  signal age : unsigned(AGE_BITS - 1 downto 0) := (others => '0');            -- 前のかたまりからの周期
  signal sb_src : unsigned(31 downto 0);
  signal ts_valid, ts_ready : std_logic;
  signal ts_data : std_logic_vector(127 downto 0);
  -- PS が DDR を直接読むモード（ps_read）
  signal ps_on : std_logic := '0';                                              -- mem: この流れは PS が読む
  signal ev_start : unsigned(EV_BITS - 1 downto 0) := (others => '0');          -- mem: この流れの最初のイベントの位置
  -- 出した書きの種類（新しい順。bit 0 = 最後に出した書き）。AXI の応答は書いた順に返るので、応答の来ていない wr_pending 個は、
  -- この並びの下位 wr_pending 個。wr_kinds = '1' はイベント、wr_own = '1' はこの流れのロジアナの拍（流れの開始の前に出した
  -- ロジアナの書きは、この流れの総数に入っていないので引かない）
  signal wr_kinds, wr_own : std_logic_vector(14 downto 0) := (others => '0');
  signal la_pending, ev_pending : unsigned(3 downto 0);
  signal la_safe : unsigned(31 downto 0);                                       -- 応答の来た書きまでのロジアナの拍の総数
  signal la_wptr_safe : unsigned(ADDR_BITS - 1 downto 0);                      -- 同じ時点のロジアナのリングの書き位置
  -- PS に公開するイベントの書き位置（応答の来た書きまで）: PS が読む流れで書き込みが止まった後は ev_lim（止めの命令が効いた時点の書き位置）で頭打ち
  -- （それより後のイベントは流れに入れず、リングに残す。ps-stream-position-design §2.9）
  signal ev_pub  : unsigned(EV_BITS - 1 downto 0);
  signal stream_epoch : unsigned(15 downto 0) := (others => '0');              -- mem: 流れの状態（総数・s_on 等）の流れの番号
  signal st_comb, st_reg : std_logic_vector(ST_W - 1 downto 0) := (others => '0');   -- 状態の組（同じ周期の値）と、そのレジスタ
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
  overrun <= enc_overrun or s_ovf or gap_lost;

  -- 開始・中止とトリガの時刻。流れの番号と開始の設定は run の 0→1 の辺で写す（run_q も同じ辺: 組が揃う）
  process (clk)
  begin
    if rising_edge(clk) then
      run_prev  <= run;
      run_q     <= run;
      trig_prev <= enc_triggered;
      if rst = '1' then
        epoch_r     <= (others => '0');
        run_q       <= '0';
        cfg_stream  <= '0';
        cfg_ps_read <= '0';
        cfg_gaps    <= '0';
        cfg_post    <= (others => '0');
      elsif run = '1' and run_prev = '0' then
        epoch_r     <= epoch_r + 1;
        cfg_stream  <= stream;
        cfg_ps_read <= ps_read and stream;
        cfg_gaps    <= gap_en;
        cfg_post    <= post_beats;
      end if;
      if run = '1' and run_prev = '0' then
        ps_last <= stream and ps_read;                    -- 終わった後も、次の流れが始まるまで stream_beats はこの値のまま
      end if;
      if run = '1' and enc_triggered = '1' and trig_prev = '0' then
        time_r <= time;
      end if;
    end if;
  end process;
  ps_epoch <= epoch_r;
  -- 開始の設定は流し続ける（mem は番号の変化で始め、run = '0' で止める。間の値を飛ばしてよい）
  ctl_src <= std_logic_vector(epoch_r) & run_q & cfg_gaps & cfg_ps_read & cfg_stream & std_logic_vector(cfg_post);
  ctl_i : entity work.cdc_value
    generic map (WIDTH => CTL_W, SAME_CLOCK => SAME_CLOCK)
    port map (src_clk => clk, src_rst => rst, src_load => '1', src_value => ctl_src, src_ready => open,
              dst_clk => mem_clk, dst_rst => mem_rst, dst_value => ctl_dst, dst_changed => ctl_new);

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
    generic map (WIDTH => 137, DEPTH => 512)
    port map (in_clk => clk, in_rst => rst, in_valid => m_in_valid, in_ready => m_in_ready, in_data => m_in,
              in_below_half => fifo_room,
              out_clk => mem_clk, out_rst => mem_rst, out_valid => f_valid, out_ready => f_ready, out_data => f_data);
  to_mem_slice_i : entity work.stream_slice
    generic map (WIDTH => 137)
    port map (clk => mem_clk, rst => mem_rst, in_valid => f_valid, in_ready => f_ready, in_data => f_data,
              out_valid => q_valid, out_ready => q_ready, out_data => q_data);

  ---------------------------------------------------------------------------------------------
  -- mem: 開始・中止（sys の開始の設定から）と読み出しの要求
  ---------------------------------------------------------------------------------------------
  -- 届いた番号が自分の番号と違えば新しい流れを始める。同じ番号で run = '0' になったら止める（リセットの後はどちらも番号 0。
  -- 受け渡しもリセットで 0 を持つ）
  process (mem_clk)
    variable rx_epoch : unsigned(15 downto 0);
    variable rx_run : std_logic;
  begin
    if rising_edge(mem_clk) then
      rx_epoch := unsigned(ctl_dst(CTL_W - 1 downto CTL_W - 16));
      rx_run   := ctl_dst(ADDR_BITS + 3);
      m_start   <= '0';
      m_stop    <= stop_next;
      stop_next <= '0';
      if mem_rst = '1' then
        m_run    <= '0';
        m_epoch  <= (others => '0');
        m_cfg    <= (others => '0');
        m_stop   <= '0';
      elsif ctl_new = '1' then
        if rx_epoch /= m_epoch then
          -- 新しい流れ: 前の流れ（残っていれば）を終えて始める。mem が見る前に止められていた（run = '0'）なら、始めて次の
          -- 周期に止める（拍の無い流れ。番号と状態が揃う）
          m_epoch   <= rx_epoch;
          m_cfg     <= ctl_dst;
          m_start   <= '1';
          stop_next <= not rx_run;
          m_run     <= rx_run;
        elsif rx_run = '0' and m_run = '1' then
          m_stop <= '1';
          m_run  <= '0';
        end if;
      end if;
    end if;
  end process;

  -- 読み出しの要求（sys）: 要求の時点の領域・先頭・拍数を持ち、通し番号を 1 進める。mem は番号が変わったら読む
  process (clk)
  begin
    if rising_edge(clk) then
      if rst = '1' then
        rd_no <= (others => '0');
      elsif read = '1' then
        rd_no      <= rd_no + 1;
        rdq_region <= read_region;
        rdq_first  <= read_first;
        rdq_count  <= read_count;
      end if;
    end if;
  end process;
  rd_src <= std_logic_vector(rd_no) & rdq_region & std_logic_vector(rdq_first) & std_logic_vector(rdq_count);
  rd_i : entity work.cdc_value
    generic map (WIDTH => RD_W, SAME_CLOCK => SAME_CLOCK)
    port map (src_clk => clk, src_rst => rst, src_load => '1', src_value => rd_src, src_ready => open,
              dst_clk => mem_clk, dst_rst => mem_rst, dst_value => rd_dst, dst_changed => rd_new);
  process (mem_clk)
    variable rx_no : unsigned(7 downto 0);
  begin
    if rising_edge(mem_clk) then
      rx_no := unsigned(rd_dst(RD_W - 1 downto RD_W - 8));
      m_rd_new <= '0';
      if mem_rst = '1' then
        m_rd_no   <= (others => '0');
      elsif rd_new = '1' then
        m_rd_no   <= rx_no;
        if rx_no /= m_rd_no then
          m_rd_new    <= '1';
          m_rd_region <= rd_dst(2 * ADDR_BITS + 1);
          m_rd_first  <= unsigned(rd_dst(2 * ADDR_BITS downto ADDR_BITS + 1));
          m_rd_count  <= unsigned(rd_dst(ADDR_BITS downto 0));
        end if;
      end if;
    end if;
  end process;

  ---------------------------------------------------------------------------------------------
  -- mem: リング書き込みと読み出し
  ---------------------------------------------------------------------------------------------
  writer_i : entity work.la_mem_writer
    generic map (ADDR_BITS => ADDR_BITS, COUNT_START_BELOW_4G => COUNT_START_BELOW_4G,
                 TOTAL_START_BELOW_4G => TOTAL_START_BELOW_4G)
    port map (clk => mem_clk, rst => mem_rst, start => m_start, stop => m_stop or m_full_stop,
              post_beats => unsigned(m_cfg(ADDR_BITS - 1 downto 0)), stream => m_cfg(ADDR_BITS),
              ev_enable => ev_en_m, ev_keep => ev_keep_m,
              in_valid => w_in_valid, in_ready => w_in_ready, in_data => q_data(127 downto 0), in_kind => q_data(132),
              in_trig => q_data(131), in_slot => unsigned(q_data(130 downto 128)),
              running => w_running, triggered => w_triggered, done => w_done, trigger_beat => w_beat,
              trigger_slot => w_slot, written => w_written, total => w_total, la_pos => w_ptr, ev_written => m_ev_written,
              ev_pos => ev_wpos,
              cmd_valid => w_valid, cmd_ready => w_ready, cmd_write => w_write, cmd_addr => w_addr, cmd_data => w_data);

  -- 流れの番号の関所（ロジアナの拍だけ）: 差 0 = いまの流れ（書き込み器へ。止まっていれば書き込み器が捨てる）、1〜7 = 先の流れ
  -- （書き込み器がその流れを始めるまで待たせる）、8〜15 = 前の流れ（捨てる）。mem の番号は書き込み器の開始と同じ辺で変わる
  -- stream_epoch。sys が mem より先に進むのは高々数個（開始の受け渡しの間）
  g_gen_d  <= unsigned(q_data(136 downto 133)) - stream_epoch(3 downto 0);
  beat_old <= '1' when q_data(132) = '0' and g_gen_d(3) = '1' else '0';
  w_in_valid <= q_valid when q_data(132) = '1' or g_gen_d = 0 else '0';
  q_ready    <= w_in_ready when q_data(132) = '1' or g_gen_d = 0 else beat_old;

  ev_en_i : entity work.sync_bit
    generic map (SAME_CLOCK => SAME_CLOCK)
    port map (clk => mem_clk, d => ev_enable, q => ev_en_m);
  -- イベントのリングを読む流れの間は、ev_enable を始め直してもリングを先頭に戻さない（C3）
  ev_keep_m <= s_on and ev_on;

  -- 未読の最大（流れの開始から）
  process (mem_clk)
  begin
    if rising_edge(mem_clk) then
      if m_start = '1' then
        peak_r <= (others => '0');
      elsif s_on = '1' and unread_r > peak_r then
        peak_r <= unread_r;
      end if;
    end if;
  end process;

  -- PS の読み終えた位置（sys → mem。その値の流れの番号つき。流し続ける）
  ret_src <= std_logic_vector(ps_ret_epoch) & std_logic_vector(ps_la_consumed) & std_logic_vector(ps_ev_consumed);
  ret_i : entity work.cdc_value
    generic map (WIDTH => RET_W, SAME_CLOCK => SAME_CLOCK)
    port map (src_clk => clk, src_rst => rst, src_load => '1', src_value => ret_src, src_ready => open,
              dst_clk => mem_clk, dst_rst => mem_rst, dst_value => ret_dst, dst_changed => open);
  ret_epoch <= unsigned(ret_dst(RET_W - 1 downto RET_W - 16));
  ret_la    <= unsigned(ret_dst(31 + EV_BITS downto EV_BITS));
  ret_ev    <= unsigned(ret_dst(EV_BITS - 1 downto 0));

  -- 応答の来ていない書き（wr_pending 個。書いた順に応答が返るので、いちばん新しい側）は読めると教えない。種類ごとに引く
  process (mem_clk)
  begin
    if rising_edge(mem_clk) then
      if mem_rst = '1' then
        wr_kinds <= (others => '0');
        wr_own   <= (others => '0');
      elsif w_valid = '1' and w_ready = '1' then             -- axi_mem_port が wr_pending を増やすのと同じ周期
        wr_kinds <= wr_kinds(13 downto 0) & q_data(132);
        wr_own   <= (wr_own(13 downto 0) & not q_data(132)) and not (14 downto 0 => m_start);
      elsif m_start = '1' then
        wr_own   <= (others => '0');                         -- 前の流れのロジアナの書きは、この流れの総数に入っていない
      end if;
    end if;
  end process;
  process (wr_kinds, wr_own, wr_pending)
    variable nl, ne : unsigned(3 downto 0);
  begin
    nl := (others => '0');
    ne := (others => '0');
    for i in 0 to 14 loop
      if i < to_integer(wr_pending) then
        if wr_kinds(i) = '1' then
          ne := ne + 1;
        elsif wr_own(i) = '1' then
          nl := nl + 1;
        end if;
      end if;
    end loop;
    la_pending <= nl;
    ev_pending <= ne;
  end process;
  -- 応答の来た書きまでの位置（同じ周期の総数・書き位置・応答の来ていない数から）。総数は折り返す（mod_diff）
  process (w_total, w_ptr, ev_wpos, la_pending, ev_pending, ps_on, s_on, w_running, ev_rpos, ev_lim)
    variable p : unsigned(ADDR_BITS downto 0);
    variable safe : unsigned(EV_BITS - 1 downto 0);
  begin
    la_safe <= mod_diff(w_total, resize(la_pending, 32));
    p := resize(w_ptr, ADDR_BITS + 1) + LA_SIZE - resize(la_pending, ADDR_BITS + 1);
    if p >= LA_SIZE then
      p := p - LA_SIZE;
    end if;
    la_wptr_safe <= p(ADDR_BITS - 1 downto 0);
    safe := ev_wpos - resize(ev_pending, EV_BITS);
    -- PS に公開する書き位置（状態の組と同じ時点の値）。止めた後の PS の流れでは、読み位置からの距離が ev_lim より遠ければ
    -- ev_lim にする（PS が ev_lim を越えて読むと ev_unread_r が 0 にならず、流れが終わらなかった。レビュー 2026-11-03 の B1）
    if ps_on = '1' and s_on = '1' and w_running = '0' and safe - ev_rpos > ev_lim - ev_rpos then
      ev_pub <= ev_lim;
    else
      ev_pub <= safe;
    end if;
  end process;

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
  la_beats   <= LA_SIZE;
  ev_beats   <= EV_SIZE;

  -- ストリーム: 未読（w_total - r_total）があり読み出し器が空いていれば、最大 STREAM_CHUNK 拍を読ませる。
  -- 未読・拍数・次の番地は 1 周期ずつに分けて求める（起動は STREAM_CHUNK 拍に 1 回程度なので数周期は問題ない）。
  -- STREAM_EVENTS: イベントのリングの未読（ev_lim - ev_rpos）も同じように読み、どちらのかたまりの前にも見出しを送る
  -- （T_HDR）。ev_lim は書き込み中は書き位置を追い、止まったらそこで止まる（止めた時点までを流す）
  process (mem_clk)
    variable wrapped : unsigned(ADDR_BITS downto 0);
    variable take_ev, take_la, ripe, la_ready, ev_ready, judge : boolean;
  begin
    if rising_edge(mem_clk) then
      s_start     <= '0';
      m_full_stop <= '0';
      unread_r    <= mod_diff(w_total, r_total);
      unread_total <= w_total;
      ev_unread_r <= ev_lim - ev_rpos;
      ev_back_r   <= ev_wpos - ev_rpos;                        -- 読んでいない書き込み（上書きの判定）
      if w_running = '1' then
        ev_lim <= ev_wpos;
      end if;
      if mem_rst = '1' then
        s_on      <= '0';
        gaps_m    <= '0';
        hold_m    <= '0';
        ps_on     <= '0';
        m_ovf     <= '0';
        ev_on     <= '0';
        hdr_valid <= '0';
        ev_start  <= (others => '0');
        stream_epoch <= (others => '0');
        s_state   <= T_IDLE;
      elsif m_start = '1' then
        stream_epoch <= m_epoch;                               -- 書き込み器の総数の 0 戻しと同じ辺（状態の組が揃う）
        s_on     <= m_cfg(ADDR_BITS);
        gaps_m   <= m_cfg(ADDR_BITS + 2) and m_cfg(ADDR_BITS);
        hold_m   <= '0';
        ps_on    <= m_cfg(ADDR_BITS + 1) and m_cfg(ADDR_BITS);   -- PS が読む流れ（チャンクの読み出しはしない）
        ev_start <= unsigned(m_cfg(EV_BITS - 1 downto 0));         -- この流れで最初に読むイベントの位置
        m_ovf    <= '0';
        r_total  <= TOTAL_START;
        r_ptr    <= (others => '0');
        unread_r <= (others => '0');                           -- 前の捕捉の総数の差を見せない（次の周期は 0 − 0）
        o_total  <= (others => '0');
        age      <= (others => '0');
        hdr_valid <= '0';
        last_kind <= '0';
        seg_kind  <= '0';
        if STREAM_EVENTS then
          ev_on   <= m_cfg(ADDR_BITS);
          ev_rpos <= unsigned(m_cfg(EV_BITS - 1 downto 0));     -- post_beats = 最初のイベントの位置
          ev_lim  <= unsigned(m_cfg(EV_BITS - 1 downto 0));
        end if;
        ev_unread_r <= (others => '0');
        ev_back_r   <= (others => '0');
        s_state  <= T_IDLE;
      elsif s_on = '1' then
        -- 判定は、unread_r が読ませたかたまりの後の r_total に追いついてから（T_WAIT の間はしない）
        judge := s_state /= T_WAIT;
        if ps_on = '1' and ret_epoch = m_epoch then
          -- 読み終えた位置は PS が返す。この流れの番号の値だけを使う（窓は流れの番号が変わると 0 に戻し、POS_TAG の違う書きを捨てる）。
          -- 書いた総数より先の値は使わない（PS の約束の外の値で未読を負にしない）
          if mod_le(ret_la, w_total) then
            r_total <= ret_la;
          end if;
          if STREAM_EVENTS then
            ev_rpos <= ev_start + ret_ev;
          end if;
        end if;
        if gaps_m = '1' and judge then
          -- 欠けの区間: 未読がリングに迫ったら拍を捨てさせ（書き込みは止めない）、十分減ったら戻す
          if unread_r > resize(LA_SIZE, 32) - GAP_HOLD then
            hold_m <= '1';
          elsif unread_r <= resize(LA_SIZE, 32) - (GAP_HOLD + GAP_ROOM) then
            hold_m <= '0';
          end if;
        end if;
        if w_running = '1' and judge and unread_r > resize(LA_SIZE, 32) - STREAM_CHUNK then
          m_full_stop <= '1';                                  -- 未読がリングに迫った: 書き込みを止める
          m_ovf       <= '1';
        end if;
        if STREAM_EVENTS and ev_on = '1' and judge and ev_back_r(EV_BITS - 1) = '1' then
          m_full_stop <= '1';                                  -- イベントの未読がリングの半分: 止め、以後流さない
          m_ovf       <= '1';
          ev_on       <= '0';
        end if;
        case s_state is
          when T_IDLE =>                                       -- unread_r は r_total の更新から 2 周期後に正しい
            ripe     := not STREAM_EVENTS or age(AGE_BITS - 1) = '1' or w_running = '0';
            la_ready := ps_on = '0' and unread_r /= 0 and (ripe or unread_r >= LA_MIN);   -- PS が読む流れでは読ませない
            ev_ready := ps_on = '0' and STREAM_EVENTS and ev_on = '1' and ev_unread_r /= 0
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
              elsif w_running = '0' and (ps_on = '0' or (unread_r = 0 and ev_unread_r = 0)) then
                -- （止まっていれば ripe なので、未読は両方 0）。PS が読む流れは、PS が読み終えてから
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
                hdr_lo <= std_logic_vector(r_total) & std_logic_vector(resize(rdr_count, 16))
                          & x"00" & SEG_LA;
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
            s_state <= T_WAIT;
          when T_WAIT =>                                       -- 読み出し器が始めを受けた（busy）: unread_r も新しい r_total のもの
            if r_busy = '1' then
              s_state <= T_IDLE;
            end if;
        end case;
      end if;
    end if;
  end process;

  rdr_start  <= m_rd_new or s_start;
  rdr_first_mux <= rdr_first when s_start = '1' else m_rd_first;
  rdr_count_mux <= rdr_count when s_start = '1' else m_rd_count;
  rdr_region <= m_rd_region when s_on = '0' else seg_kind;
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
  -- 読み出し・流れの最中（読み出し器・流れ・FIFO に入れる拍・始めの周期のどれか）
  m_busy     <= r_busy or s_on or ts_valid or rdr_start;
  mem_active <= m_busy or w_running or ev_en_m;               -- イベントの記録中も使う

  -- 読み出した拍（と STREAM_EVENTS の見出し）→ sys → バイト列。見出しは読み出し器が空いている間にだけ送る
  -- （T_HDR は r_busy = '0' の後）ので、読み出し器の出力とぶつからない
  ts_valid <= ro_valid or hdr_valid;
  ts_data  <= SEG_MAGIC & hdr_lo when hdr_valid = '1' else ro_data;
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
  st_comb <= std_logic_vector(stream_epoch) & (ps_on and s_on) & w_running & w_triggered & w_done & m_ovf & m_busy
             & std_logic_vector(la_safe) & std_logic_vector(la_wptr_safe) & std_logic_vector(ev_pub)
             & std_logic_vector(ev_start) & std_logic_vector(w_slot) & std_logic_vector(w_beat)
             & std_logic_vector(w_written) & std_logic_vector(m_pushed) & std_logic_vector(m_rd_no);
  process (mem_clk)
  begin
    if rising_edge(mem_clk) then
      st_reg <= st_comb;
    end if;
  end process;
  st_src <= st_reg;
  st_i : entity work.cdc_value
    generic map (WIDTH => ST_W, SAME_CLOCK => SAME_CLOCK)
    port map (src_clk => mem_clk, src_rst => mem_rst, src_load => '1', src_value => st_src, src_ready => open,
              dst_clk => clk, dst_rst => rst, dst_value => st_dst, dst_changed => open);
  -- 組を解く（並びは st_src と同じ。下位から）
  st_unpack : block
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
    constant O_EP    : natural := O_FLAGS + 6;
  begin
    assert O_EP + 16 = ST_W report "la_deep_buffer: ST_W does not match the status layout" severity failure;
    s_rd_no     <= unsigned(st_dst(O_PUSH - 1 downto O_RD));
    s_pushed    <= unsigned(st_dst(O_WR - 1 downto O_PUSH));
    s_written   <= unsigned(st_dst(O_BEAT - 1 downto O_WR));
    s_beat      <= unsigned(st_dst(O_SLOT - 1 downto O_BEAT));
    s_slot      <= unsigned(st_dst(O_EVS - 1 downto O_SLOT));
    s_ev_start  <= unsigned(st_dst(O_EVW - 1 downto O_EVS));
    s_ev_safe   <= unsigned(st_dst(O_WPTR - 1 downto O_EVW));
    s_la_wptr   <= unsigned(st_dst(O_LA - 1 downto O_WPTR));
    s_la_safe   <= unsigned(st_dst(O_FLAGS - 1 downto O_LA));
    s_busy      <= st_dst(O_FLAGS);
    s_ovf       <= st_dst(O_FLAGS + 1);
    s_done      <= st_dst(O_FLAGS + 2);
    s_triggered <= st_dst(O_FLAGS + 3);
    s_running   <= st_dst(O_FLAGS + 4);
    s_active    <= st_dst(O_FLAGS + 5);
    s_epoch     <= unsigned(st_dst(O_EP + 15 downto O_EP));
  end block;
  s_writing <= s_running;

  -- stream_beats: ストリームで書いた拍の総数。STREAM_EVENTS では出すと決めた拍の総数（見出し・イベントを含む）
  sb_src <= o_total when STREAM_EVENTS else w_total;
  peak_now <= unread_r when s_on = '1' and unread_r > peak_r else peak_r;     -- 最大は unread_r を含める（同じ時点）
  cnt_src <= std_logic_vector(unread_total) & std_logic_vector(unread_r) & std_logic_vector(peak_now) & std_logic_vector(sb_src)
             & std_logic_vector(m_ev_written);
  cnt_i : entity work.cdc_value
    generic map (WIDTH => CNT_W, SAME_CLOCK => SAME_CLOCK)
    port map (src_clk => mem_clk, src_rst => mem_rst, src_load => '1', src_value => cnt_src, src_ready => open,
              dst_clk => clk, dst_rst => rst, dst_value => cnt_dst, dst_changed => open);
  stat_written <= unsigned(cnt_dst(191 downto 160));
  stat_unread  <= unsigned(cnt_dst(159 downto 128));
  stat_peak    <= unsigned(cnt_dst(127 downto 96));
  stream_beats <= ps_out_beats when ps_last = '1' else unsigned(cnt_dst(95 downto 64));
  ev_written   <= unsigned(cnt_dst(63 downto 0));

  -- PS に公開する組（どれも s_epoch の流れのもの）
  ps_pub_epoch  <= s_epoch;
  ps_active     <= s_active;
  ps_writing    <= s_writing;
  ps_overrun    <= s_ovf;
  ps_la_written <= s_la_safe;
  ps_la_wptr    <= s_la_wptr;
  ps_ev_wpos    <= s_ev_safe;
  ps_ev_start   <= s_ev_start;

  -- sys の状態。開始（run の 0→1）から、mem がこの流れの状態を返すまでは捕捉中（前回の done を見せない）。
  -- 読み出し中: 読み出しの要求を mem が受けていない・mem が busy・入れた拍を取り出し終えていない・分解し終えていない・
  -- PS への読み出しの要求に返事が無い
  process (all)
    variable starting : boolean;
  begin
    starting := run = '1' and (run_q = '0' or s_epoch /= epoch_r);
    capturing <= s_running;
    if starting then
      capturing <= '1';
    end if;
    done <= '0';
    if s_epoch = epoch_r and not starting then
      done <= s_done;
    end if;
    reading <= s_busy or out_valid or rd_ps_pending;
    if rd_no /= s_rd_no or popped /= s_pushed then
      reading <= '1';
    end if;
  end process;
  triggered    <= enc_triggered;
  trigger_slot <= s_slot;
  trigger_beat <= s_beat;
  written      <= s_written;
  trigger_time <= time_r;
end architecture;
