-- イベント記録の集約（fprobe_core の一部）。イベント（1 件 128 ビット）を作り、1 周期に 1 件ずつ event_valid / event_data に出す。
-- 形式は fprobe_core.vhd 以前の記述（下のコメント）のとおり。
-- 源: DAP の転送（dap_monitor）・割り込み書き込み（swd_injector）・サンプラの語・UART / SWO のバイト・補助マイコンの計測値・
-- 対象の FPGA の観測・ボードの間の時刻の同期。優先順は処理の順（同じ周期に複数あれば残りは保持して後の周期に出す）。
-- 保持が出ないうちに次が来たら捨てて byte_lost に数える。源の無い構成では、その源の valid / fire は 0 で来る
-- （保持のレジスタは定数伝播で消える）。fprobe_core から切り出しただけで、振る舞いは切り出し前と同じ。
-- BYTE_PACK = true なら、UART・SWO のバイトは源ごとに event_byte_pack で詰めた形（種類 13・14・15）にして出す
-- （ケイパビリティ FEATURES の EVENT_BYTE_PACK。docs/superpowers/specs/2026-11-01-bandwidth-observation-design.md 3.5）。
-- DAP の時刻（docs/superpowers/specs/2026-11-01-time-accuracy-design.md R5・R6・R7。ケイパビリティ FEATURES の DAP_LINE_TIME）:
--   ホストの転送（1・12）と割り込み書き込み（11）の時刻は、源が運ぶ転送の線上の点（ev_time・inj_time）で、受けた周期ではない。
--   サンプラの件（7・17・18）は dap_sampler が組んだ語（smp_kind・smp_word）をそのまま出す（1 つの保持で、源の順を保つ）。
--   CONFIG_EVENTS = true なら、PHY の設定（cfg_word）が変わった周期と、記録を始めた周期に種類 16 を出す（時刻は受けた周期）。
library ieee;
use ieee.std_logic_1164.all;
use ieee.numeric_std.all;

entity event_collector is
  generic (
    BYTE_PACK : boolean := false;          -- UART・SWO のバイトを詰めた形（種類 13〜15）で出す。false = 古い形（3〜5）
    CONFIG_EVENTS : boolean := false       -- PHY の設定のイベント（種類 16）を出す
  );
  port (
    clk, rst      : in  std_logic;
    time_count    : in  unsigned(47 downto 0);
    events_enable : in  std_logic;
    -- DAP の転送（dap_monitor）と、その種類（1 = 通常、12 = DRW でメモリの番地を追えなかった）
    ev_valid      : in  std_logic;
    host_kind     : in  std_logic_vector(7 downto 0);
    ev_parity     : in  std_logic;
    ev_request    : in  std_logic_vector(3 downto 0);
    ev_ack        : in  std_logic_vector(2 downto 0);
    ev_data       : in  std_logic_vector(31 downto 0);
    ev_addr       : in  std_logic_vector(31 downto 0);
    ev_time       : in  std_logic_vector(47 downto 0);   -- その転送の線上の点（dap_monitor の ev_time）
    -- 割り込み書き込み（swd_injector）
    inj_written   : in  std_logic;
    inj_transfers : in  unsigned(7 downto 0);
    inj_value     : in  std_logic_vector(31 downto 0);
    inj_addr      : in  std_logic_vector(31 downto 0);
    inj_time      : in  std_logic_vector(47 downto 0);   -- その語の DRW の線上の点（swd_injector の written_time）
    -- DAP サンプラの件（種類 7・17・18。dap_sampler の rec_*。語は [119:0]）
    smp_valid     : in  std_logic;
    smp_kind      : in  std_logic_vector(7 downto 0);
    smp_word      : in  std_logic_vector(119 downto 0);
    -- UART / SWO のバイト（fire = そのバイトを受け渡した周期。受信の time = 源が付けた時刻: 開始ビットの辺を見つけた周期等）
    uart_rx_fire  : in  std_logic;
    uart_rx_data  : in  std_logic_vector(7 downto 0);
    uart_rx_time  : in  unsigned(47 downto 0);
    uart_tx_fire  : in  std_logic;
    uart_tx_data  : in  std_logic_vector(7 downto 0);
    swo_fire      : in  std_logic;
    swo_data      : in  std_logic_vector(7 downto 0);
    swo_time      : in  unsigned(47 downto 0);
    -- 補助マイコンの計測値
    aux_valid     : in  std_logic;
    aux_ch        : in  natural range 0 to 3;
    aux_value     : in  unsigned(13 downto 0);
    aux_src_time  : in  unsigned(47 downto 0);          -- その組の時刻（aux_link の sample_time: 組の先頭のバイトの開始ビットの辺）
    -- 対象の FPGA の観測
    obs_valid     : in  std_logic;
    obs_sync      : in  std_logic;
    obs_data      : in  std_logic_vector(31 downto 0);
    obs_addr      : in  std_logic_vector(31 downto 0);
    obs_time      : in  std_logic_vector(47 downto 0);
    -- ボードの間の時刻の同期の組
    sync_valid    : in  std_logic;
    sync_local    : in  std_logic_vector(47 downto 0);
    sync_parent   : in  std_logic_vector(63 downto 0);
    -- PHY の設定（種類 16 の [119:48]。CONFIG_EVENTS のとき）
    cfg_word      : in  std_logic_vector(71 downto 0);
    -- 出力
    event_valid   : out std_logic;
    event_data    : out std_logic_vector(127 downto 0);
    byte_lost     : out unsigned(31 downto 0)
  );
end entity;

architecture rtl of event_collector is
  type byte_array_t is array (0 to 2) of std_logic_vector(7 downto 0);
  type time_array_t is array (0 to 2) of std_logic_vector(47 downto 0);
  signal byte_fire, byte_held : std_logic_vector(2 downto 0) := (others => '0');
  signal byte_in, byte_data : byte_array_t := (others => (others => '0'));
  signal byte_time : time_array_t := (others => (others => '0'));
  signal byte_src_time : time_array_t := (others => (others => '0'));   -- 受けたバイトの時刻（受信 = 源の時刻、送信 = 受けた周期 + 1）
  signal rx_time_r, swo_time_r : unsigned(47 downto 0) := (others => '0');
  signal lost_r : unsigned(31 downto 0) := (others => '0');
  signal lost_add : natural range 0 to 31 := 0;             -- 前の周期に落ちた数
  signal event_valid_r : std_logic := '0';
  signal event_data_r : std_logic_vector(127 downto 0) := (others => '0');
  signal aux_held, obs_held, obs_held_sync, sync_held, smp_held, events_enable_prev : std_logic := '0';
  signal aux_word, obs_word, obs_word_addr : std_logic_vector(31 downto 0) := (others => '0');
  signal aux_time, obs_word_time, sync_word_time : std_logic_vector(47 downto 0) := (others => '0');
  signal sync_word : std_logic_vector(63 downto 0) := (others => '0');
  signal smp_held_kind : std_logic_vector(7 downto 0) := (others => '0');
  signal smp_held_word : std_logic_vector(119 downto 0) := (others => '0');
  -- PHY の設定（種類 16）
  signal cfg_prev : std_logic_vector(71 downto 0) := (others => '0');
  signal cfg_held : std_logic := '0';
  signal cfg_held_word : std_logic_vector(71 downto 0) := (others => '0');
  signal cfg_time : std_logic_vector(47 downto 0) := (others => '0');
  -- 詰めた形（BYTE_PACK）: 源ごとの詰めた件、出す周期（優先順は古い形のバイトと同じ位置: サンプラの後、補助の前）
  type word_array_t is array (0 to 2) of std_logic_vector(119 downto 0);
  type count_array_t is array (0 to 2) of natural range 0 to 8;
  signal pk_valid, pk_take : std_logic_vector(2 downto 0) := (others => '0');
  signal pk_word : word_array_t := (others => (others => '0'));
  signal pk_lost : count_array_t := (others => 0);
begin
  event_valid <= event_valid_r;
  event_data  <= event_data_r;
  byte_lost   <= lost_r;

  -- イベント（1 件 128 ビット）: [47:0] 時刻、[79:48] 番地、[111:80] データ、[114:112] ACK、[118:115] 要求、
  -- [119] パリティ異常、[127:120] 種類（1 = ホストの転送、2 = 割り込み書き込み（旧い形。今は 11）、3 = UART 受信（ターゲット → PC）、
  -- 4 = UART 送信（PC → ターゲット）、5 = SWO、6 = 補助マイコンの計測値、7 = DAP サンプラの 1 語（番地・値・ACK）、8・9 = FPGA の観測、10 = 時刻の同期の組、
  -- 11 = 割り込み書き込み（[119:112] = 停止の書き込みまでの転送数。R20）、12 = ホストの転送のうち DRW でメモリの番地を
  -- 追えなかったもの（番地 = レジスタの番地。dap_monitor の ev_addr_known））。3〜5 はデータの下位 8 ビットがバイトで、
  -- 6 はデータ = ch << 16 | 値（mV）、時刻 = その組の先頭のバイトの開始ビットの辺を見つけた周期（aux_link。TLV TIMING の aux）。バイトの時刻: UART 受信・SWO は源が付けた時刻（UART 方式は開始ビットの辺を見つけた周期、
  -- Manchester 方式は受信器がバイトを出した周期。時刻の正しさの R3。意味は TLV TIMING の uart_rx・swo_uart・swo_manchester が申告）、
  -- UART 送信は受け渡しの周期 + 1（下の 1 段。送信器が開始ビットを出す辺）。バイトは源ごとに 1 件保持し、DAP・割り込みのイベントが
  -- 無い周期に出す（1 周期に 1 件）。
  -- 保持がまだ出ていないうちに次のバイトが来たら捨てて lost_r に数える（events.lost に足す）
  -- 受け渡しは 1 段レジスタで受ける（送信の時刻は 1 周期遅れる。ブリッジの ready からの経路を短くする）
  -- 詰めた形（BYTE_PACK、種類 13 = UART 受信、14 = UART 送信、15 = SWO）: [47:0] 先頭のバイトの時刻、[79:48] バイト
  -- （i 番目が [48+8i+7 : 48+8i]）、[115:80] 2〜4 バイト目の前のバイトからの時刻の差（各 12 ビット）、[117:116] バイトの
  -- 数 − 1、[119:118] 0。各バイトの時刻の意味は古い形と同じ。件の切り方は event_byte_pack
  -- 16 = PHY の設定（R7）: [63:48] half_period、[71:64] idle_cycles、[73:72] turnaround − 1、[74] data_phase、[76:75] SWD の
  -- sample_delay の下位 2 ビット、[79:77] JTAG の sample_delay、[80] jtag、[81] enable、[84:82] dr_before、[87:85] dr_after、
  -- [93:88] ir_before、[99:94] ir_after、[104:100] ir_len − 1、[105] SWD の sample_delay の bit2（0〜7 に広げた分。古い回路は 0）、
  -- [119:106] 0（cfg_word の並び。組むのは fprobe_core）。17・18 は dap_sampler の先頭
  process (clk)
  begin
    if rising_edge(clk) then
      -- SWO は受けたバイトをすべて記録する（USB の SWO の口の空きに依らない: 口があふれて捨てたバイトも記録には残す。
      -- 2026-10-18 に実機で、受け始めにホストが口を読む前にあふれ、記録からも欠けた）
      byte_fire <= swo_fire & uart_tx_fire & uart_rx_fire;
      byte_in   <= (0 => uart_rx_data, 1 => uart_tx_data, 2 => swo_data);
      rx_time_r  <= uart_rx_time;
      swo_time_r <= swo_time;
    end if;
  end process;
  byte_src_time <= (0 => std_logic_vector(rx_time_r), 1 => std_logic_vector(time_count), 2 => std_logic_vector(swo_time_r));
  pack_g : if BYTE_PACK generate
    -- 出す周期: DAP・割り込み・サンプラの語が無く、優先順の前の源の件も無い周期（下の process の served と同じ順）
    pk_take(0) <= pk_valid(0) and not ev_valid and not inj_written and not smp_held;
    pk_take(1) <= pk_valid(1) and not ev_valid and not inj_written and not smp_held and not pk_valid(0);
    pk_take(2) <= pk_valid(2) and not ev_valid and not inj_written and not smp_held and not pk_valid(0)
                  and not pk_valid(1);
    packers : for k in 0 to 2 generate
      pack_i : entity work.event_byte_pack
        port map (clk => clk, rst => rst, enable => events_enable, in_fire => byte_fire(k), in_data => byte_in(k),
                  in_time => unsigned(byte_src_time(k)), out_valid => pk_valid(k), out_taken => pk_take(k), out_word => pk_word(k),
                  lost => pk_lost(k));
    end generate;
  end generate;
  no_pack_g : if not BYTE_PACK generate
    pk_valid <= (others => '0');
    pk_take  <= (others => '0');
    pk_word  <= (others => (others => '0'));
    pk_lost  <= (others => 0);
  end generate;

  process (clk)
    variable served : boolean;
    variable taken  : std_logic_vector(2 downto 0);
    variable lost_n : natural range 0 to 31;              -- 同じ周期に落ちた数（サンプラ・補助・観測・同期・バイト 3。詰めた形は
                                                           -- 源ごとに最大 8 バイト）
    variable aux_taken : boolean;
    variable obs_taken : boolean;
    variable smp_taken : boolean;
    variable sync_taken : boolean;
    variable cfg_taken : boolean;
  begin
    if rising_edge(clk) then
      event_valid_r <= '0';
      served := false;
      taken  := "000";
      lost_n := 0;
      if ev_valid = '1' then
        event_valid_r <= '1';
        event_data_r  <= host_kind & ev_parity & ev_request & ev_ack & ev_data & ev_addr & ev_time;
        served := true;
      elsif inj_written = '1' then
        event_valid_r <= '1';
        -- 種類 11（R20）: 要求・ACK の欄の代わりに転送数（書き込みは常に AP の DRW・OK。旧い種類 2 は転送数なし）
        event_data_r  <= x"0B" & std_logic_vector(inj_transfers) & inj_value & inj_addr & inj_time;
        served := true;
      end if;
      smp_taken := false;
      if not served and smp_held = '1' then
        event_valid_r <= '1';
        event_data_r  <= smp_held_kind & smp_held_word;
        served    := true;
        smp_taken := true;
      end if;
      if smp_valid = '1' and events_enable = '1' then
        if smp_held = '1' and not smp_taken then
          lost_n := lost_n + 1;                                -- 前の件がまだ出ていない（サンプラが線を持つ間は DAP・割り込みの件が
                                                               -- 無いので、1 周期 1 件で出し続けられる）
        else
          smp_held      <= '1';
          smp_held_kind <= smp_kind;
          smp_held_word <= smp_word;
        end if;
      elsif smp_taken then
        smp_held <= '0';
      end if;
      for k in 0 to 2 loop
        if pk_take(k) = '1' then                               -- 詰めた形（pk_take は served と同じ優先順）
          event_valid_r <= '1';
          event_data_r  <= std_logic_vector(to_unsigned(13 + k, 8)) & pk_word(k);
          served := true;
        end if;
        lost_n := lost_n + pk_lost(k);
        if not served and byte_held(k) = '1' then
          event_valid_r <= '1';
          event_data_r  <= std_logic_vector(to_unsigned(3 + k, 8)) & x"00" & x"000000" & byte_data(k)
                         & x"00000000" & byte_time(k);
          taken(k) := '1';
          served   := true;
        end if;
      end loop;
      aux_taken := false;
      if not served and aux_held = '1' then
        event_valid_r <= '1';
        event_data_r  <= x"06" & x"00" & aux_word & x"00000000" & aux_time;
        served    := true;
        aux_taken := true;
      end if;
      if aux_valid = '1' and events_enable = '1' then
        if aux_held = '1' and not aux_taken then
          lost_n := lost_n + 1;                                -- 前の計測値がまだ出ていない（間隔 1 ms なので起きない想定）
        else
          aux_held <= '1';
          aux_word <= std_logic_vector(to_unsigned(aux_ch, 16)) & "00" & std_logic_vector(aux_value);
          aux_time <= std_logic_vector(aux_src_time);      -- 源の時刻（時刻の正しさの B2、段階 S3。前は受けた周期）
        end if;
      elsif aux_taken then
        aux_held <= '0';
      end if;
      -- 対象の FPGA の観測（記録 8・時刻合わせ 9。時刻はエンジンが付けたもの）
      obs_taken := false;
      if not served and obs_held = '1' then
        event_valid_r <= '1';
        if obs_held_sync = '1' then
          event_data_r <= x"09" & x"00" & obs_word & obs_word_addr & obs_word_time;
        else
          event_data_r <= x"08" & x"00" & obs_word & obs_word_addr & obs_word_time;
        end if;
        served    := true;
        obs_taken := true;
      end if;
      if obs_valid = '1' and events_enable = '1' then
        if obs_held = '1' and not obs_taken then
          lost_n := lost_n + 1;                                -- 前の語がまだ出ていない（語の間隔は TCK の 32 周期）
        else
          obs_held      <= '1';
          obs_held_sync <= obs_sync;
          obs_word      <= obs_data;
          obs_word_addr <= obs_addr;
          obs_word_time <= obs_time;
        end if;
      elsif obs_taken then
        obs_held <= '0';
      end if;
      -- ボードの間の時刻の同期の組（10。時刻 = 子の時刻、番地 = 親の時刻の下位 32、データ = 上位 32）
      sync_taken := false;
      if not served and sync_held = '1' then
        event_valid_r <= '1';
        event_data_r  <= x"0A" & x"00" & sync_word(63 downto 32) & sync_word(31 downto 0) & sync_word_time;
        served     := true;
        sync_taken := true;
      end if;
      if sync_valid = '1' and events_enable = '1' then
        if sync_held = '1' and not sync_taken then
          lost_n := lost_n + 1;                                -- 前の組がまだ出ていない（組の間隔は 730 周期以上）
        else
          sync_held      <= '1';
          sync_word      <= sync_parent;
          sync_word_time <= sync_local;
        end if;
      elsif sync_taken then
        sync_held <= '0';
      end if;
      -- PHY の設定（16。変わった周期と記録を始めた周期の値。いちばん後の優先順）
      cfg_taken := false;
      if CONFIG_EVENTS then
        if not served and cfg_held = '1' then
          event_valid_r <= '1';
          event_data_r  <= x"10" & cfg_held_word & cfg_time;
          served    := true;
          cfg_taken := true;
        end if;
        if events_enable = '1' and (cfg_word /= cfg_prev or events_enable_prev = '0') then
          if cfg_held = '1' and not cfg_taken then
            lost_n := lost_n + 1;                              -- 前の設定がまだ出ていない（設定はホストの命令で変わるので起きない想定）
          else
            cfg_held      <= '1';
            cfg_held_word <= cfg_word;
            cfg_time      <= std_logic_vector(time_count);
          end if;
        elsif cfg_taken then
          cfg_held <= '0';
        end if;
        cfg_prev <= cfg_word;
      else
        cfg_held      <= '0';
        cfg_held_word <= (others => '0');
        cfg_time      <= (others => '0');
        cfg_prev      <= (others => '0');
      end if;
      for k in 0 to 2 loop
        if byte_fire(k) = '1' and events_enable = '1' and not BYTE_PACK then
          if byte_held(k) = '1' and taken(k) = '0' then
            lost_n := lost_n + 1;                               -- 前のバイトがまだ出ていない
          else
            byte_held(k) <= '1';
            byte_data(k) <= byte_in(k);
            byte_time(k) <= byte_src_time(k);
          end if;
        elsif taken(k) = '1' then
          byte_held(k) <= '0';
        end if;
      end loop;
      if rst = '1' or events_enable = '0' then
        byte_held <= (others => '0');
        aux_held  <= '0';
        smp_held  <= '0';
        obs_held  <= '0';
        sync_held <= '0';
        cfg_held  <= '0';
      end if;
      -- 同じ周期に落ちた数（lost_n）はいったんレジスタに置き、次の周期に足す（源の数の和と 32 ビットの加算を 1 周期に
      -- 入れると、sys を上げると遅い経路だった。2026-10-29）。数の反映が 1 周期遅れるだけで、数は変わらない
      if rst = '1' or (events_enable = '1' and events_enable_prev = '0') then
        lost_r   <= (others => '0');
        lost_add <= 0;
      else
        lost_r   <= lost_r + lost_add;
        lost_add <= lost_n;
      end if;
      events_enable_prev <= events_enable;
    end if;
  end process;

end architecture;
