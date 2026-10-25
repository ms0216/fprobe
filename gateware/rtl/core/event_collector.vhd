-- イベント記録の集約（fprobe_core の一部）。イベント（1 件 128 ビット）を作り、1 周期に 1 件ずつ event_valid / event_data に出す。
-- 形式は fprobe_core.vhd 以前の記述（下のコメント）のとおり。
-- 源: DAP の転送（dap_monitor）・割り込み書き込み（swd_injector）・サンプラの語・UART / SWO のバイト・補助マイコンの計測値・
-- 対象の FPGA の観測・ボードの間の時刻の同期。優先順は処理の順（同じ周期に複数あれば残りは保持して後の周期に出す）。
-- 保持が出ないうちに次が来たら捨てて byte_lost に数える。源の無い構成では、その源の valid / fire は 0 で来る
-- （保持のレジスタは定数伝播で消える）。fprobe_core から切り出しただけで、振る舞いは切り出し前と同じ。
library ieee;
use ieee.std_logic_1164.all;
use ieee.numeric_std.all;

entity event_collector is
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
    -- 割り込み書き込み（swd_injector）
    inj_written   : in  std_logic;
    inj_transfers : in  unsigned(7 downto 0);
    inj_value     : in  std_logic_vector(31 downto 0);
    inj_addr      : in  std_logic_vector(31 downto 0);
    -- DAP サンプラの語
    smp_valid     : in  std_logic;
    smp_addr      : in  std_logic_vector(31 downto 0);
    smp_data      : in  std_logic_vector(31 downto 0);
    smp_ack       : in  std_logic_vector(2 downto 0);
    -- UART / SWO のバイト（fire = そのバイトを受け渡した周期）
    uart_rx_fire  : in  std_logic;
    uart_rx_data  : in  std_logic_vector(7 downto 0);
    uart_tx_fire  : in  std_logic;
    uart_tx_data  : in  std_logic_vector(7 downto 0);
    swo_fire      : in  std_logic;
    swo_data      : in  std_logic_vector(7 downto 0);
    -- 補助マイコンの計測値
    aux_valid     : in  std_logic;
    aux_ch        : in  natural range 0 to 3;
    aux_value     : in  unsigned(13 downto 0);
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
  signal lost_r : unsigned(31 downto 0) := (others => '0');
  signal event_valid_r : std_logic := '0';
  signal event_data_r : std_logic_vector(127 downto 0) := (others => '0');
  signal aux_held, obs_held, obs_held_sync, sync_held, smp_held, events_enable_prev : std_logic := '0';
  signal aux_word, obs_word, obs_word_addr, smp_word, smp_word_data : std_logic_vector(31 downto 0) := (others => '0');
  signal aux_time, obs_word_time, sync_word_time, smp_time : std_logic_vector(47 downto 0) := (others => '0');
  signal sync_word : std_logic_vector(63 downto 0) := (others => '0');
  signal smp_word_ack : std_logic_vector(2 downto 0) := (others => '0');
begin
  event_valid <= event_valid_r;
  event_data  <= event_data_r;
  byte_lost   <= lost_r;

  -- イベント（1 件 128 ビット）: [47:0] 時刻、[79:48] 番地、[111:80] データ、[114:112] ACK、[118:115] 要求、
  -- [119] パリティ異常、[127:120] 種類（1 = ホストの転送、2 = 割り込み書き込み（旧い形。今は 11）、3 = UART 受信（ターゲット → PC）、
  -- 4 = UART 送信（PC → ターゲット）、5 = SWO、6 = 補助マイコンの計測値、7 = DAP サンプラの 1 語（番地・値・ACK）、8・9 = FPGA の観測、10 = 時刻の同期の組、
  -- 11 = 割り込み書き込み（[119:112] = 停止の書き込みまでの転送数。R20）、12 = ホストの転送のうち DRW でメモリの番地を
  -- 追えなかったもの（番地 = レジスタの番地。dap_monitor の ev_addr_known））。3〜5 はデータの下位 8 ビットがバイトで、
  -- 6 はデータ = ch << 16 | 値（mV）。時刻はブリッジ / 受信器との
  -- 受け渡しの周期。バイトは源ごとに 1 件保持し、DAP・割り込みのイベントが無い周期に出す（1 周期に 1 件）。
  -- 保持がまだ出ていないうちに次のバイトが来たら捨てて lost_r に数える（events.lost に足す）
  -- 受け渡しは 1 段レジスタで受ける（時刻は 1 周期遅れる。ブリッジの ready からの経路を短くする）
  process (clk)
  begin
    if rising_edge(clk) then
      -- SWO は受けたバイトをすべて記録する（USB の SWO の口の空きに依らない: 口があふれて捨てたバイトも記録には残す。
      -- 2026-10-18 に実機で、受け始めにホストが口を読む前にあふれ、記録からも欠けた）
      byte_fire <= swo_fire & uart_tx_fire & uart_rx_fire;
      byte_in   <= (0 => uart_rx_data, 1 => uart_tx_data, 2 => swo_data);
    end if;
  end process;
  process (clk)
    variable served : boolean;
    variable taken  : std_logic_vector(2 downto 0);
    variable lost_n : natural range 0 to 7;               -- 同じ周期に落ちうる源の数（サンプラ・補助・観測・同期・バイト 3）
    variable aux_taken : boolean;
    variable obs_taken : boolean;
    variable smp_taken : boolean;
    variable sync_taken : boolean;
  begin
    if rising_edge(clk) then
      event_valid_r <= '0';
      served := false;
      taken  := "000";
      lost_n := 0;
      if ev_valid = '1' then
        event_valid_r <= '1';
        event_data_r  <= host_kind & ev_parity & ev_request & ev_ack & ev_data & ev_addr & std_logic_vector(time_count);
        served := true;
      elsif inj_written = '1' then
        event_valid_r <= '1';
        -- 種類 11（R20）: 要求・ACK の欄の代わりに転送数（書き込みは常に AP の DRW・OK。旧い種類 2 は転送数なし）
        event_data_r  <= x"0B" & std_logic_vector(inj_transfers) & inj_value & inj_addr & std_logic_vector(time_count);
        served := true;
      end if;
      smp_taken := false;
      if not served and smp_held = '1' then
        event_valid_r <= '1';
        event_data_r  <= x"07" & '0' & "1111" & smp_word_ack & smp_word_data & smp_word & smp_time;
        served    := true;
        smp_taken := true;
      end if;
      if smp_valid = '1' and events_enable = '1' then
        if smp_held = '1' and not smp_taken then
          lost_n := lost_n + 1;                                -- 前の語がまだ出ていない（語の間隔は SWD の 1 転送なので起きない想定）
        else
          smp_held      <= '1';
          smp_word      <= smp_addr;
          smp_word_data <= smp_data;
          smp_word_ack  <= smp_ack;
          smp_time      <= std_logic_vector(time_count);
        end if;
      elsif smp_taken then
        smp_held <= '0';
      end if;
      for k in 0 to 2 loop
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
          aux_time <= std_logic_vector(time_count);
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
      for k in 0 to 2 loop
        if byte_fire(k) = '1' and events_enable = '1' then
          if byte_held(k) = '1' and taken(k) = '0' then
            lost_n := lost_n + 1;                               -- 前のバイトがまだ出ていない
          else
            byte_held(k) <= '1';
            byte_data(k) <= byte_in(k);
            byte_time(k) <= std_logic_vector(time_count);
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
      end if;
      if rst = '1' or (events_enable = '1' and events_enable_prev = '0') then
        lost_r <= (others => '0');
      else
        lost_r <= lost_r + lost_n;
      end if;
      events_enable_prev <= events_enable;
    end if;
  end process;

end architecture;
