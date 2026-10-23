-- 対象の FPGA の観測（docs/superpowers/specs/2026-10-20-fpga-observation-v2-design.md）: 対象の JTAG の口で
-- 観測の回路 v2（target_ip/fprobe_obs の obs_core。DR の形式はその説明）をスキャンし続け、記録と印をイベントにする。
--
-- TCK のタイミングは jtag_phy に任せる（JTAG_Sequence の命令を並べてスキャンを作る）:
--   始め: TMS 1 × 5 → Run-Test/Idle、IR スキャンで USER（鎖の他の装置は BYPASS）、1・2 回目のスキャンで ID の下位・
--     上位のレジスタを読む（応答の記録が "FOBS" なら id_ok）。
--   繰り返し: DR スキャン = TDO 側の BYPASS（dr_before ビット）→ 対象の 1 + M 語 → TDI 側の BYPASS（dr_after ビット）。
--     命令（16 ビット）は対象が最後に受ける 16 ビット = 対象の最後の語の 31:16 に載せる。
--     命令は待ち行列から、無ければ sync_period ごとに印（op 3、番号つき）、それも無ければ何もしない（op 0）。
--   止める: スキャンの区切りで TMS 1 × 5（Test-Logic-Reset）にしてから線を手放す（free_pins なら戻さずすぐ手放す）。
--   使うのは対象の語の最後のビットまで受けた語だけ（obs_core は送り切った記録だけを取り除く。鎖の BYPASS の分
--   余計に送るビットは 14 ビット以下なので、送り切って失う記録は無い）。
--
-- イベント（1 周期の脈。fprobe_core が種類 8・9 にする）:
--   記録: data = 記録の語、address = スキャンの番号（scans）、時刻 = 受けた時。
--   印: data = 番号、address = 0、時刻 = 印を載せたスキャンの Update-DR を出る TCK の立ち上がり（obs_core はその瞬間に
--   印の記録を書く。対象側の遅れは観測の回路が SYNC_DELAY で申告する）。PC は番号で印の記録と組にする。
-- 応答の記録（読みの結果）は read_value にも写す（番号 | 番地 | 値）。
-- 標本型（obs_snap_core。見出しの印 0xA7）: ID の照合をせず、スキャンごとに語 0（印 | 変化の回数 | 値）を記録の
-- イベントにする（値が前と同じで変化の回数も 0 の標本は出さない）。時刻 = Capture-DR を出る TCK の立ち上がり（値を取り込む瞬間）。
-- 印のイベントは出さない。
-- 観測の回路が答えない（見出しの印 31:24 が 0xA6 でない = TDO が 0 か 1 に張り付いた・IR が USER でない・ID の応答が
-- "FOBS" でない・前の記録を吸い出した後 16 スキャンで ID の応答が来ない）ときは stuck にして
-- TAP を戻し、線を手放して止まる（enable を 0 に戻すまで）。
library ieee;
use ieee.std_logic_1164.all;
use ieee.numeric_std.all;

entity obs_jtag_engine is
  port (
    clk         : in  std_logic;
    rst         : in  std_logic;
    time_count  : in  unsigned(47 downto 0);
    -- 設定（CSR obs）
    enable      : in  std_logic;
    free_pins     : in  std_logic;
    half_period : in  unsigned(15 downto 0);
    sample_delay : in natural range 0 to 7;               -- TDO の取り込みの遅れ（jtag_phy）
    ir_len      : in  unsigned(5 downto 0);
    ir_before   : in  unsigned(5 downto 0);
    ir_after    : in  unsigned(5 downto 0);
    dr_before   : in  unsigned(2 downto 0);
    dr_after    : in  unsigned(2 downto 0);
    user_ir     : in  std_logic_vector(31 downto 0);
    words       : in  unsigned(8 downto 0);
    sync_period : in  unsigned(15 downto 0);
    cmd_push    : in  std_logic;
    cmd_data    : in  std_logic_vector(15 downto 0);
    -- 値の一致（クロストリガの源 bit4。docs/superpowers/specs/2026-10-21-system-analysis-design.md §4.1）:
    -- 前の値から match_change のどれかが変わり、かつ (値 xor match_value) and match_mask = 0 で 1 周期のパルス。
    -- match_change = 0 なら一致に入った瞬間（前は不一致・今は一致）
    match_mask   : in  std_logic_vector(15 downto 0);
    match_value  : in  std_logic_vector(15 downto 0);
    match_change : in  std_logic_vector(15 downto 0);
    match_on     : in  std_logic;
    match        : out std_logic;
    match_count  : out unsigned(31 downto 0);
    -- 状態
    running       : out std_logic;
    driving       : out std_logic;                      -- 対象の線を駆動している（0 になるまで他の道具をつながない）
    target_running : out std_logic;                     -- 最後の見出しの「対象の clk 側が記録中」
    id_ok         : out std_logic;
    stuck         : out std_logic;
    cmd_pending   : out std_logic;
    overflow_seen : out std_logic;
    cmd_dropped   : out std_logic;
    scans         : out unsigned(31 downto 0);
    read_value    : out std_logic_vector(31 downto 0);
    id            : out std_logic_vector(31 downto 0);
    -- イベント
    ev_valid    : out std_logic;
    ev_sync     : out std_logic;                       -- 0 = 記録（種類 8）、1 = 時刻合わせ（種類 9）
    ev_data     : out std_logic_vector(31 downto 0);
    ev_addr     : out std_logic_vector(31 downto 0);
    ev_time     : out std_logic_vector(47 downto 0);
    -- 対象の JTAG の線
    tck         : out std_logic;
    tms         : out std_logic;
    tdi         : out std_logic;
    tdo         : in  std_logic;
    oe          : out std_logic
  );
end entity;

architecture rtl of obs_jtag_engine is
  constant OP_SEQ_OUT : std_logic_vector(1 downto 0) := "00";
  constant OP_SEQ_IN  : std_logic_vector(1 downto 0) := "01";
  constant ID_LO      : std_logic_vector(15 downto 0) := x"4253";       -- "FOBS"
  constant ID_HI      : std_logic_vector(15 downto 0) := x"464F";
  constant MARK       : std_logic_vector(7 downto 0) := x"A6";          -- 見出しの印（v2 の記録型）
  constant SNAP_MARK  : std_logic_vector(7 downto 0) := x"A7";          -- 標本型の印
  constant SNAP_ID    : std_logic_vector(31 downto 0) := x"534E4150";   -- 標本型の id の表示（"SNAP"）
  constant OP_READ    : std_logic_vector(3 downto 0) := x"2";
  constant OP_SYNC    : std_logic_vector(3 downto 0) := x"3";
  constant RESPONSE   : std_logic_vector(5 downto 0) := "110001";        -- 記録の 31:26（制御・応答）
  constant HEADER_WORDS : natural := 1;
  constant ID_SCANS   : natural := 16;                                   -- この数のスキャンで ID が来なければ答えない
  constant CMD_DEPTH  : natural := 4;

  -- 手順。E_IR・E_DR は区間の並び（BEFORE → BODY → AFTER）を 1 片ずつ送る
  type step_t is (E_OFF, E_RESET, E_IR_HEAD, E_IR, E_IR_TAIL, E_DR_HEAD, E_DR, E_DR_TAIL, E_EXIT, E_HALT);
  type seg_t is (SEG_BEFORE, SEG_BODY, SEG_AFTER);
  type cmd_queue_t is array (0 to CMD_DEPTH - 1) of std_logic_vector(15 downto 0);

  signal step     : step_t;
  signal pending  : boolean;                          -- 命令を出した（応答を待つ）
  signal seg      : seg_t;
  signal part     : unsigned(8 downto 0);             -- 本体の区間の番号（DR は対象の語の番号）
  signal split    : std_logic;                        -- 最後の区間の最後の 1 ビット（TMS 1）を送る
  signal partial  : std_logic_vector(30 downto 0);   -- 最後の語の先の 31 ビット

  -- jtag_phy
  signal phy_enable    : std_logic;
  signal phy_cmd_valid : std_logic;
  signal phy_cmd_ready : std_logic;
  signal phy_cmd_op    : std_logic_vector(1 downto 0);
  signal phy_cmd_count : natural range 1 to 64;
  signal phy_cmd_tms   : std_logic;
  signal phy_cmd_data  : std_logic_vector(63 downto 0);
  signal phy_rsp_valid : std_logic;
  signal phy_rsp_data  : std_logic_vector(63 downto 0);
  signal phy_tck       : std_logic;
  signal tck_prev      : std_logic;
  signal tail_rises    : unsigned(1 downto 0);

  -- スキャン
  signal m_words     : unsigned(8 downto 0);          -- このスキャンの記録の語数（始めに cfg_words から決める）
  signal scan_cmd    : std_logic_vector(15 downto 0);
  signal scan_count  : unsigned(31 downto 0);
  signal sync_count  : unsigned(15 downto 0);
  signal id_reads    : unsigned(1 downto 0);          -- 出した ID の読み（下位・上位の 2 つ）
  signal id_wait     : unsigned(4 downto 0);          -- ID がそろうまでのスキャンの数
  signal id_lo_ok    : std_logic;
  signal id_hi_ok    : std_logic;
  signal upd_time    : unsigned(47 downto 0);        -- このスキャンの Update-DR の時刻
  signal cap_time    : unsigned(47 downto 0);        -- このスキャンの Capture-DR を出る時刻
  signal head_rises  : unsigned(1 downto 0);
  signal snap_mode   : std_logic;                     -- 対象は標本型
  signal snap_scan   : std_logic;                     -- このスキャンは標本型の 1 語（スキャンの始めに snap_mode から）
  signal snap_last   : std_logic_vector(31 downto 0); -- 前の標本（同じ標本はイベントにしない）
  signal snap_seen   : std_logic;
  signal sync_seq    : unsigned(7 downto 0);          -- 印の番号
  signal m_last      : std_logic_vector(15 downto 0); -- 値の一致: 前の値
  signal m_hit_last  : std_logic;                     -- 前の値が一致していた
  signal m_seen      : std_logic;                     -- 前の値がある（開き直すと 0）
  signal m_count     : unsigned(31 downto 0);
  signal m_on_prev   : std_logic;
  signal resp_seq    : unsigned(7 downto 0);          -- 応答の番号（read_value の 31:24）
  signal rec_count   : unsigned(15 downto 0);        -- 見出しの count
  signal bad         : std_logic;                     -- このスキャンで答えない印を見た

  signal queue       : cmd_queue_t;
  signal q_head      : natural range 0 to CMD_DEPTH - 1;
  signal q_count     : natural range 0 to CMD_DEPTH;

  signal running_r, id_ok_r, stuck_r, ovf_r, dropped_r, tgt_run_r : std_logic;
  signal stop_req : std_logic;                       -- 止める指示を見た（すぐ戻されても区切りで止めて始め直す）
  -- 設定は開始のときに写す（走っている間に CSR を書き換えても、今のスキャンに効かない。jtag_phy は実行中に変えない）
  signal cfg_half_period, cfg_sync_period : unsigned(15 downto 0);
  signal cfg_ir_len, cfg_ir_before, cfg_ir_after : unsigned(5 downto 0);
  signal cfg_dr_before, cfg_dr_after : unsigned(2 downto 0);
  signal cfg_user_ir : std_logic_vector(31 downto 0);
  signal cfg_words : unsigned(8 downto 0);
  signal read_value_r, id_r : std_logic_vector(31 downto 0);
begin
  phy_i : entity work.jtag_phy
    port map (clk => clk, rst => rst, enable => phy_enable, half_period => cfg_half_period, idle_cycles => 0,
              dr_before => 0, dr_after => 0, ir_before => 0, ir_after => 0, ir_len => 1, sample_delay => sample_delay,
              cmd_valid => phy_cmd_valid, cmd_ready => phy_cmd_ready, cmd_op => phy_cmd_op,
              cmd_count => phy_cmd_count, cmd_request => "000" & phy_cmd_tms, cmd_data => phy_cmd_data,
              rsp_valid => phy_rsp_valid, rsp_ack => open, rsp_data => phy_rsp_data,
              tck => phy_tck, tms => tms, tdi => tdi, tdo => tdo);

  tck <= phy_tck;
  oe  <= '0' when step = E_OFF or step = E_HALT or free_pins = '1' else '1';
  driving <= '0' when step = E_OFF or step = E_HALT or free_pins = '1' else '1';
  phy_enable <= '0' when step = E_OFF or step = E_HALT or free_pins = '1' else '1';

  running       <= running_r;
  target_running <= tgt_run_r;
  id_ok         <= id_ok_r;
  stuck         <= stuck_r;
  cmd_pending   <= '1' when q_count /= 0 else '0';
  overflow_seen <= ovf_r;
  cmd_dropped   <= dropped_r;
  scans         <= scan_count;
  match_count   <= m_count;
  read_value    <= read_value_r;
  id            <= id_r;

  process (clk)
    variable seg_len   : natural range 0 to 63;
    variable last      : boolean;
    variable fill      : std_logic;
    variable data      : std_logic_vector(31 downto 0);
    variable word      : std_logic_vector(31 downto 0);
    variable word_done : boolean;
    variable k         : natural range 0 to 63;
    variable want_stop : boolean;
    variable ir        : boolean;
    variable push_ok   : boolean;
    variable pop       : boolean;
    variable flush     : boolean;                            -- 開き直す: 待ち行列を空にする

    -- 区間の長さ（ir = IR スキャン）
    impure function length_of(s : seg_t; ir_scan : boolean) return natural is
    begin
      if ir_scan then
        case s is
          when SEG_BEFORE => return to_integer(cfg_ir_before);
          when SEG_BODY   =>
            if cfg_ir_len = 0 then
              return 1;
            elsif cfg_ir_len > 32 then
              return 32;
            end if;
            return to_integer(cfg_ir_len);
          when SEG_AFTER  => return to_integer(cfg_ir_after);
        end case;
      else
        case s is
          when SEG_BEFORE => return to_integer(cfg_dr_before);
          when SEG_BODY   => return 32;
          when SEG_AFTER  => return to_integer(cfg_dr_after);
        end case;
      end if;
    end function;

    -- 本体の区間の数
    impure function bodies(ir_scan : boolean) return unsigned is
    begin
      if ir_scan or snap_scan = '1' then
        return to_unsigned(1, 9);                         -- 標本型は語 0 だけ（1 スキャン 32 ビット）
      end if;
      return m_words + HEADER_WORDS;
    end function;

    -- スキャンの最後の区間か（最後のビットは TMS 1）
    impure function is_last(s : seg_t; b : unsigned; ir_scan : boolean) return boolean is
    begin
      case s is
        when SEG_BEFORE => return false;
        when SEG_BODY   => return b = bodies(ir_scan) - 1 and length_of(SEG_AFTER, ir_scan) = 0;
        when SEG_AFTER  => return true;
      end case;
    end function;

    -- 区間 s（本体なら番号 b）に入る。長さ 0 の前の区間は飛ばす（後の区間は呼ぶ側が長さを見る）
    procedure enter(s : seg_t; b : unsigned; ir_scan : boolean) is
    begin
      if s = SEG_BEFORE and length_of(SEG_BEFORE, ir_scan) = 0 then
        seg <= SEG_BODY;
        part <= (others => '0');
        split <= '1' when is_last(SEG_BODY, to_unsigned(0, 9), ir_scan) and length_of(SEG_BODY, ir_scan) = 1
                 else '0';
      else
        seg <= s;
        part <= resize(b, 9);
        split <= '1' when is_last(s, b, ir_scan) and length_of(s, ir_scan) = 1 else '0';
      end if;
    end procedure;

    procedure issue(op : std_logic_vector(1 downto 0); count : natural; tms_value : std_logic;
                    bits : std_logic_vector(63 downto 0)) is
    begin
      phy_cmd_valid <= '1';
      phy_cmd_op    <= op;
      phy_cmd_count <= count;
      phy_cmd_tms   <= tms_value;
      phy_cmd_data  <= bits;
      pending       <= true;
    end procedure;

    -- 観測した値ごとに: 一致を比べてパルス・回数（無効でも前の値は追う）
    procedure observe(v : std_logic_vector(15 downto 0)) is
      variable hit, fire : boolean;
    begin
      hit := ((v xor match_value) and match_mask) = x"0000";
      if match_change = x"0000" then
        fire := hit and m_hit_last = '0';
      else
        fire := hit and ((v xor m_last) and match_change) /= x"0000";
      end if;
      if fire and m_seen = '1' and match_on = '1' then
        match <= '1';
        m_count <= m_count + 1;
      end if;
      m_last <= v;
      m_hit_last <= '1' when hit else '0';
      m_seen <= '1';
    end procedure;

    procedure emit(sync : std_logic; d, a : std_logic_vector(31 downto 0); t : unsigned(47 downto 0)) is
    begin
      ev_valid <= '1';
      ev_sync  <= sync;
      ev_data  <= d;
      ev_addr  <= a;
      ev_time  <= std_logic_vector(t);
    end procedure;
  begin
    if rising_edge(clk) then
      ev_valid <= '0';
      match <= '0';
      m_on_prev <= match_on;
      if match_on = '1' and m_on_prev = '0' then                 -- 一致を有効にしたら数え直す
        m_count <= (others => '0');
      end if;
      tck_prev <= phy_tck;
      if phy_cmd_valid = '1' and phy_cmd_ready = '1' then
        phy_cmd_valid <= '0';
      end if;
      -- Capture-DR を出る TCK の立ち上がり（DR の始めの 3 ビット目）
      if step = E_DR_HEAD and pending and phy_tck = '1' and tck_prev = '0' then
        head_rises <= head_rises + 1;
        if head_rises = 2 then
          cap_time <= time_count;
        end if;
      end if;
      -- Update-DR を出る TCK の立ち上がり（DR の終わりの 2 ビット目）
      if step = E_DR_TAIL and pending and phy_tck = '1' and tck_prev = '0' then
        tail_rises <= tail_rises + 1;
        if tail_rises = 1 then
          upd_time <= time_count;
        end if;
      end if;

      -- 命令の待ち行列（積むのはここ、取り出すのは下の pop。数は最後にまとめて）
      push_ok := cmd_push = '1' and q_count < CMD_DEPTH;
      pop := false;
      flush := false;
      if push_ok then
        queue((q_head + q_count) mod CMD_DEPTH) <= cmd_data;
      elsif cmd_push = '1' then
        dropped_r <= '1';
      end if;

      if enable = '0' and step /= E_OFF and step /= E_HALT then
        stop_req <= '1';
      end if;
      want_stop := enable = '0' or free_pins = '1' or stop_req = '1';

      if free_pins = '1' and step /= E_OFF and step /= E_HALT then
        -- すぐ手放す（jtag_phy も止まる）
        step <= E_OFF;
        pending <= false;
        phy_cmd_valid <= '0';
        running_r <= '0';
      elsif pending then
        if phy_rsp_valid = '1' then
          pending <= false;
          case step is
            when E_RESET =>
              step <= E_IR_HEAD;
            when E_IR_HEAD =>
              step <= E_IR;
              enter(SEG_BEFORE, to_unsigned(0, 9), true);
            when E_IR | E_DR =>
              ir := step = E_IR;
              -- 受けた語（DR の本体だけ）
              word_done := false;
              word := phy_rsp_data(31 downto 0);
              if not ir and seg = SEG_BODY then
                if is_last(SEG_BODY, part, false) and split = '0' then
                  partial <= phy_rsp_data(30 downto 0);
                elsif is_last(SEG_BODY, part, false) then
                  word := phy_rsp_data(0) & partial;
                  word_done := true;
                else
                  word_done := true;
                end if;
              end if;
              if word_done then
                if part = 0 and word(31 downto 24) = SNAP_MARK then
                  -- 標本型: この語が標本（後ろの語は使わない）
                  rec_count <= (others => '0');
                  tgt_run_r <= '1';
                  snap_mode <= '1';
                  id_ok_r <= '1';
                  id_r <= SNAP_ID;
                  -- 値が前と同じで、その間の変化も無い標本は何も伝えないので出さない（イベントを変化の数まで減らす）
                  snap_last <= word;
                  snap_seen <= '1';
                  if snap_seen = '0' or word(15 downto 0) /= snap_last(15 downto 0) or word(23 downto 16) /= x"00" then
                    emit('0', word, std_logic_vector(scan_count), cap_time);
                    observe(word(15 downto 0));
                  end if;
                elsif part = 0 then
                  rec_count <= unsigned(word(15 downto 0));
                  ovf_r <= word(16);
                  tgt_run_r <= word(17);
                  if word(31 downto 24) /= MARK then                 -- 印（旗のほかのビットは見ない）
                    bad <= '1';
                  end if;
                elsif part - HEADER_WORDS < rec_count and bad = '0' then
                  if word(31 downto 26) = RESPONSE then
                    -- 応答: read_value に写す。ID の 2 つは照合する
                    read_value_r <= std_logic_vector(resp_seq + 1) & "0000" & word(19 downto 0);
                    resp_seq <= resp_seq + 1;
                    if word(19 downto 16) = x"0" then
                      id_r(15 downto 0) <= word(15 downto 0);
                      if word(15 downto 0) = ID_LO then
                        id_lo_ok <= '1';
                      else
                        bad <= '1';
                      end if;
                    elsif word(19 downto 16) = x"1" then
                      id_r(31 downto 16) <= word(15 downto 0);
                      if word(15 downto 0) = ID_HI then
                        id_hi_ok <= '1';
                      else
                        bad <= '1';
                      end if;
                    end if;
                  end if;
                  if id_ok_r = '1' then
                    emit('0', word, std_logic_vector(scan_count), time_count);
                    if word(31 downto 30) = "00" then                -- 値の記録（31:30 = 0）
                      observe(word(15 downto 0));
                    end if;
                  end if;
                end if;
              end if;
              -- 次の片
              if is_last(seg, part, ir) and split = '0' then
                split <= '1';
              else
                case seg is
                  when SEG_BEFORE => enter(SEG_BODY, to_unsigned(0, 9), ir);
                  when SEG_BODY =>
                    if part + 1 < bodies(ir) then
                      enter(SEG_BODY, part + 1, ir);
                    elsif length_of(SEG_AFTER, ir) /= 0 then
                      enter(SEG_AFTER, to_unsigned(0, 9), ir);
                    elsif ir then
                      step <= E_IR_TAIL;
                    else
                      step <= E_DR_TAIL;
                    end if;
                  when SEG_AFTER =>
                    if ir then
                      step <= E_IR_TAIL;
                    else
                      step <= E_DR_TAIL;
                    end if;
                end case;
              end if;
            when E_IR_TAIL =>
              step <= E_DR_HEAD;
            when E_DR_HEAD =>
              step <= E_DR;
              enter(SEG_BEFORE, to_unsigned(0, 9), false);
            when E_DR_TAIL =>
              -- スキャンの終わり: 印を載せたなら、その Update-DR の時刻を印のイベントにする
              scan_count <= scan_count + 1;
              if scan_cmd(3 downto 0) = OP_SYNC and id_ok_r = '1' and bad = '0' and snap_mode = '0' then
                emit('1', x"000000" & scan_cmd(15 downto 8), (others => '0'), upd_time);
              end if;
              if id_ok_r = '0' then
                if id_lo_ok = '1' and id_hi_ok = '1' then
                  id_ok_r <= '1';
                elsif id_wait /= ID_SCANS and rec_count <= m_words then
                  -- 前の観測の読まれていない記録が FIFO に残っていると、ID の応答はその後ろに並ぶ。吸い出し終えて
                  -- （見出しの数が 1 スキャンで読む語数以下）から数える
                  id_wait <= id_wait + 1;
                end if;
              end if;
              if bad = '1' or (id_ok_r = '0' and id_wait = ID_SCANS) then
                stuck_r <= '1';
                running_r <= '0';
                step <= E_EXIT;
              elsif want_stop then
                step <= E_EXIT;
              else
                step <= E_DR_HEAD;
              end if;
            when E_EXIT =>
              step <= E_HALT when stuck_r = '1' else E_OFF;
              running_r <= '0';
            when others =>
              null;
          end case;
        end if;
      else
        -- 次の命令を出す
        case step is
          when E_OFF =>
            stop_req <= '0';
            if enable = '1' and free_pins = '0' then
              step <= E_RESET;
              flush := true;                                  -- 前の観測で送っていない命令は捨てる（R17）
              cfg_half_period <= half_period;
              cfg_sync_period <= sync_period;
              cfg_ir_len <= ir_len;
              cfg_ir_before <= ir_before;
              cfg_ir_after <= ir_after;
              cfg_dr_before <= dr_before;
              cfg_dr_after <= dr_after;
              cfg_user_ir <= user_ir;
              cfg_words <= words;
              stuck_r <= '0';
              id_ok_r <= '0';
              dropped_r <= '0';
              ovf_r <= '0';
              m_seen <= '0';                                 -- 開き直した: 前の値を忘れる
              id_reads <= (others => '0');
              id_wait <= (others => '0');
              id_lo_ok <= '0';
              id_hi_ok <= '0';
              snap_mode <= '0';
              snap_seen <= '0';
              scan_count <= (others => '0');
              sync_count <= (others => '0');
            end if;
          when E_HALT =>
            stop_req <= '0';
            if enable = '0' then
              step <= E_OFF;
            end if;
          when E_RESET =>
            issue(OP_SEQ_OUT, 6, '1', x"00000000_0000001F");     -- TMS 1 × 5 → Test-Logic-Reset、0 → Idle
          when E_IR_HEAD =>
            issue(OP_SEQ_OUT, 4, '1', x"00000000_00000003");     -- 1,1,0,0 → Shift-IR
          when E_IR_TAIL | E_DR_TAIL =>
            issue(OP_SEQ_OUT, 2, '1', x"00000000_00000001");     -- 1,0 → Update → Idle
            tail_rises <= (others => '0');
          when E_EXIT =>
            issue(OP_SEQ_OUT, 5, '1', x"00000000_0000001F");     -- TMS 1 × 5 → Test-Logic-Reset
          when E_DR_HEAD =>
            if want_stop then
              step <= E_EXIT;
            else
              -- このスキャンの命令と語数
              if snap_mode = '1' then
                scan_cmd <= (others => '0');                         -- 標本型は命令を受けない
              elsif id_reads /= 2 then
                scan_cmd <= x"00" & "000" & std_logic(id_reads(0)) & OP_READ;   -- ID の下位（番地 0）・上位（1）
                id_reads <= id_reads + 1;
              elsif q_count /= 0 then
                scan_cmd <= queue(q_head);
                q_head <= (q_head + 1) mod CMD_DEPTH;
                pop := true;
              elsif cfg_sync_period /= 0 and sync_count + 1 >= cfg_sync_period then
                scan_cmd <= std_logic_vector(sync_seq) & x"0" & OP_SYNC;
                sync_seq <= sync_seq + 1;
                sync_count <= (others => '0');
              else
                scan_cmd <= (others => '0');
                sync_count <= sync_count + 1;
              end if;
              if cfg_words = 0 then
                m_words <= to_unsigned(1, 9);
              elsif cfg_words > 256 then
                m_words <= to_unsigned(256, 9);
              else
                m_words <= cfg_words;
              end if;
              bad <= '0';
              running_r <= id_ok_r;
              issue(OP_SEQ_OUT, 3, '1', x"00000000_00000001");   -- 1,0,0 → Shift-DR
              head_rises <= (others => '0');
              snap_scan <= snap_mode;
            end if;
          when E_IR | E_DR =>
            ir := step = E_IR;
            seg_len := length_of(seg, ir);
            last := is_last(seg, part, ir);
            if ir then
              fill := '1';                                         -- 他の装置の IR は BYPASS（すべて 1）
              data := cfg_user_ir;
            else
              fill := '0';
              if seg = SEG_BODY and part = m_words + HEADER_WORDS - 1 then
                data := scan_cmd & x"0000";                        -- 最後の 16 ビット
              else
                data := (others => '0');
              end if;
            end if;
            if split = '1' then
              -- 最後の 1 ビット（TMS 1 → Exit1）
              if seg = SEG_BODY then
                issue(OP_SEQ_IN, 1, '1', (63 downto 1 => '0') & data((seg_len - 1) mod 32));
              else
                issue(OP_SEQ_IN, 1, '1', (63 downto 1 => '0') & fill);
              end if;
            else
              if last then
                k := seg_len - 1;
              else
                k := seg_len;
              end if;
              if seg = SEG_BODY then
                issue(OP_SEQ_IN, k, '0', x"00000000" & data);
              else
                issue(OP_SEQ_IN, k, '0', (others => fill));
              end if;
            end if;
        end case;
      end if;

      if flush then                                          -- 開き直す（取り出しは無い）: この周期に積んだ 1 つだけ残す
        q_head  <= (q_head + q_count) mod CMD_DEPTH;
        q_count <= 1 when push_ok else 0;
      elsif push_ok and not pop then
        q_count <= q_count + 1;
      elsif pop and not push_ok then
        q_count <= q_count - 1;
      end if;

      if rst = '1' then
        step <= E_OFF;
        pending <= false;
        stop_req <= '0';
        phy_cmd_valid <= '0';
        q_head <= 0;
        q_count <= 0;
        running_r <= '0';
        id_ok_r <= '0';
        stuck_r <= '0';
        ovf_r <= '0';
        tgt_run_r <= '0';
        dropped_r <= '0';
        scan_count <= (others => '0');
        read_value_r <= (others => '0');
        id_r <= (others => '0');
        resp_seq <= (others => '0');
        m_last <= (others => '0');
        m_hit_last <= '0';
        m_seen <= '0';
        m_count <= (others => '0');
        match <= '0';
        sync_seq <= (others => '0');
        id_reads <= (others => '0');
        id_wait <= (others => '0');
        id_lo_ok <= '0';
        id_hi_ok <= '0';
        snap_mode <= '0';
        snap_scan <= '0';
        snap_seen <= '0';
        snap_last <= (others => '0');
        head_rises <= (others => '0');
        cap_time <= (others => '0');
        ev_valid <= '0';
        ev_sync <= '0';
        ev_data <= (others => '0');
        ev_addr <= (others => '0');
        ev_time <= (others => '0');
      end if;
    end if;
  end process;
end architecture;
