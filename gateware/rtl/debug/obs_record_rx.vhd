-- 対象の FPGA の観測（obs_jtag_engine の部品）: 受けた語の解釈。
-- DR の本体で受けた語を 1 つずつもらい、見出し（語 0）・標本型・応答・記録を見分けて、
--   - イベント（記録 = 種類 8、印 = 種類 9）
--   - read_value（応答の結果）・id（ID の照合）・id_ok・標本型への切り替え
--   - 観測した値（obs_match へ）
-- にする。スキャンの組み立て（どの語がいつ来るか）と、止める判定（stuck・id_timeout）は engine の仕事。
-- engine は次の脈だけを渡す（どれも同じ周期に registers から作った組み合わせ回路の脈）:
--   clear      観測の始め: 前の観測の ID・応答・「対象が記録中」・標本型を消す
--   scan_start スキャンの始め: 答えない印の旗（bad）を下ろす
--   word_valid 語が来た（part = 本体の語の番号。0 が見出し）
--   scan_end   スキャンの終わり（Update-DR を出た）: 印のイベントと、ID がそろったときの id_ok
library ieee;
use ieee.std_logic_1164.all;
use ieee.numeric_std.all;

entity obs_record_rx is
  port (
    clk        : in  std_logic;
    rst        : in  std_logic;
    clear      : in  std_logic;
    scan_start : in  std_logic;
    word_valid : in  std_logic;
    word       : in  std_logic_vector(31 downto 0);
    part       : in  unsigned(8 downto 0);
    scan_index : in  unsigned(31 downto 0);                -- 記録のイベントの address（スキャンの番号）
    time_count : in  unsigned(47 downto 0);                -- 記録の時刻（受けた時）
    cap_time   : in  unsigned(47 downto 0);                -- 標本の時刻（Capture-DR を出る TCK の立ち上がり）
    scan_end   : in  std_logic;
    sync_mark  : in  std_logic;                            -- このスキャンは印を載せた
    sync_num   : in  std_logic_vector(7 downto 0);         -- 印の番号
    sync_time  : in  unsigned(47 downto 0);                -- 印の時刻（Update-DR を出る TCK の立ち上がり）
    -- 状態
    id_ok      : out std_logic;
    id_known   : out std_logic;                            -- id_ok、またはこのスキャンで ID の 2 つがそろった
    bad        : out std_logic;                            -- このスキャンで答えない印・ID を見た
    snap_mode  : out std_logic;
    rec_count  : out unsigned(15 downto 0);                -- 最後の見出しの count
    ovf        : out std_logic;
    tgt_run    : out std_logic;
    read_value : out std_logic_vector(31 downto 0);
    id         : out std_logic_vector(31 downto 0);
    -- 観測した値（1 周期遅れで obs_match へ）
    obs_valid  : out std_logic;
    obs_v      : out std_logic_vector(15 downto 0);
    -- イベント
    ev_valid   : out std_logic;
    ev_sync    : out std_logic;
    ev_data    : out std_logic_vector(31 downto 0);
    ev_addr    : out std_logic_vector(31 downto 0);
    ev_time    : out std_logic_vector(47 downto 0)
  );
end entity;

architecture rtl of obs_record_rx is
  constant ID_LO     : std_logic_vector(15 downto 0) := x"4253";       -- "FOBS"
  constant ID_HI     : std_logic_vector(15 downto 0) := x"464F";
  constant MARK      : std_logic_vector(7 downto 0) := x"A6";          -- 見出しの印（v2 の記録型）
  constant SNAP_MARK : std_logic_vector(7 downto 0) := x"A7";          -- 標本型の印
  constant SNAP_ID   : std_logic_vector(31 downto 0) := x"534E4150";   -- 標本型の id の表示（"SNAP"）
  constant RESPONSE  : std_logic_vector(5 downto 0) := "110001";        -- 記録の 31:26（制御・応答）
  constant HEADER_WORDS : natural := 1;

  signal id_ok_r, bad_r, snap_mode_r, ovf_r, tgt_run_r, id_lo_ok, id_hi_ok, snap_seen : std_logic;
  signal rec_count_r  : unsigned(15 downto 0);
  signal read_value_r, id_r, snap_last : std_logic_vector(31 downto 0);
  signal resp_seq     : unsigned(7 downto 0);                -- 応答の番号（read_value の 31:24）
  signal ev_valid_r, ev_sync_r : std_logic;
  signal ev_data_r, ev_addr_r  : std_logic_vector(31 downto 0);
  signal ev_time_r    : std_logic_vector(47 downto 0);
  signal obs_valid_r  : std_logic;
  signal obs_v_r      : std_logic_vector(15 downto 0);
begin
  id_ok      <= id_ok_r;
  id_known   <= '1' when id_ok_r = '1' or (id_lo_ok = '1' and id_hi_ok = '1') else '0';
  bad        <= bad_r;
  snap_mode  <= snap_mode_r;
  rec_count  <= rec_count_r;
  ovf        <= ovf_r;
  tgt_run    <= tgt_run_r;
  read_value <= read_value_r;
  id         <= id_r;
  obs_valid  <= obs_valid_r;
  obs_v      <= obs_v_r;
  ev_valid   <= ev_valid_r;
  ev_sync    <= ev_sync_r;
  ev_data    <= ev_data_r;
  ev_addr    <= ev_addr_r;
  ev_time    <= ev_time_r;

  process (clk)
    procedure emit(sync : std_logic; d, a : std_logic_vector(31 downto 0); t : unsigned(47 downto 0)) is
    begin
      ev_valid_r <= '1';
      ev_sync_r  <= sync;
      ev_data_r  <= d;
      ev_addr_r  <= a;
      ev_time_r  <= std_logic_vector(t);
    end procedure;

    -- 観測した値ごとに obs_match へ渡す（一致の判定と回数は obs_match。無効でも前の値は追う）
    procedure observe(v : std_logic_vector(15 downto 0)) is
    begin
      obs_valid_r <= '1';
      obs_v_r <= v;
    end procedure;
  begin
    if rising_edge(clk) then
      ev_valid_r <= '0';
      obs_valid_r <= '0';

      if clear = '1' then
        id_ok_r <= '0';
        id_r <= (others => '0');                           -- 前の観測の ID・応答・「対象が記録中」を見せない
        tgt_run_r <= '0';
        read_value_r <= (others => '0');
        ovf_r <= '0';
        id_lo_ok <= '0';
        id_hi_ok <= '0';
        snap_mode_r <= '0';
        snap_seen <= '0';
      end if;

      if scan_start = '1' then
        bad_r <= '0';
      end if;

      if word_valid = '1' then
        if part = 0 and word(31 downto 24) = SNAP_MARK and (id_ok_r = '0' or snap_mode_r = '1') then   -- 記録型の稼働中の 1 ビット化けで標本型に切り替えない
          -- 標本型: この語が標本（後ろの語は使わない）
          rec_count_r <= (others => '0');
          tgt_run_r <= '1';
          snap_mode_r <= '1';
          id_ok_r <= '1';
          id_r <= SNAP_ID;
          -- 値が前と同じで、その間の変化も無い標本は何も伝えないので出さない（イベントを変化の数まで減らす）
          snap_last <= word;
          snap_seen <= '1';
          if snap_seen = '0' or word(15 downto 0) /= snap_last(15 downto 0) or word(23 downto 16) /= x"00" then
            emit('0', word, std_logic_vector(scan_index), cap_time);
            observe(word(15 downto 0));
          end if;
        elsif part = 0 then
          rec_count_r <= unsigned(word(15 downto 0));
          ovf_r <= word(16);
          tgt_run_r <= word(17);
          if word(31 downto 24) /= MARK then                 -- 印（旗のほかのビットは見ない）
            bad_r <= '1';
          end if;
        elsif part - HEADER_WORDS < rec_count_r and bad_r = '0' then
          if word(31 downto 26) = RESPONSE then
            -- 応答: read_value に写す。ID の 2 つは照合する
            read_value_r <= std_logic_vector(resp_seq + 1) & "0000" & word(19 downto 0);
            resp_seq <= resp_seq + 1;
            if word(19 downto 16) = x"0" then
              id_r(15 downto 0) <= word(15 downto 0);
              if word(15 downto 0) = ID_LO then
                id_lo_ok <= '1';
              else
                bad_r <= '1';
              end if;
            elsif word(19 downto 16) = x"1" then
              id_r(31 downto 16) <= word(15 downto 0);
              if word(15 downto 0) = ID_HI then
                id_hi_ok <= '1';
              else
                bad_r <= '1';
              end if;
            end if;
          end if;
          if id_ok_r = '1' then
            emit('0', word, std_logic_vector(scan_index), time_count);
            if word(31 downto 30) = "00" then                -- 値の記録（31:30 = 0）
              observe(word(15 downto 0));
            end if;
          end if;
        end if;
      end if;

      if scan_end = '1' then
        -- 印を載せたなら、その Update-DR の時刻を印のイベントにする
        if sync_mark = '1' and id_ok_r = '1' and bad_r = '0' and snap_mode_r = '0' then
          emit('1', x"000000" & sync_num, (others => '0'), sync_time);
        end if;
        if id_ok_r = '0' and id_lo_ok = '1' and id_hi_ok = '1' then
          id_ok_r <= '1';
        end if;
      end if;

      if rst = '1' then
        id_ok_r <= '0';
        bad_r <= '0';
        snap_mode_r <= '0';
        snap_seen <= '0';
        snap_last <= (others => '0');
        ovf_r <= '0';
        tgt_run_r <= '0';
        id_lo_ok <= '0';
        id_hi_ok <= '0';
        rec_count_r <= (others => '0');
        read_value_r <= (others => '0');
        id_r <= (others => '0');
        resp_seq <= (others => '0');
        obs_valid_r <= '0';
        obs_v_r <= (others => '0');
        ev_valid_r <= '0';
        ev_sync_r <= '0';
        ev_data_r <= (others => '0');
        ev_addr_r <= (others => '0');
        ev_time_r <= (others => '0');
      end if;
    end if;
  end process;
end architecture;
