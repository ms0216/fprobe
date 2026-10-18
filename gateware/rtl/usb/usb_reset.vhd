-- USB デバイスのバスリセット検出・High-Speed ネゴシエーション（チャープ）・サスペンド判定。
-- 根拠: docs/reference/usb2-hs-device-on-ulpi.md §3。
--
-- PHY の Function Control（0x04）と OTG Control（0x0A）は「目標値」を宣言するだけにし、
-- 書き込み担当（下の別プロセス）が現在値との差分を ulpi_link のレジスタポートで書く。
-- 状態機械は目標値が書き終わったこと（fc_synced）を待ってから次へ進む。
--
--   起動: 切り離し（0x49, 非駆動・プルアップ無し）を T_DETACH 保つ。FPGA を書き込み直すと PHY のリセットは
--   数 µs だけなので、High-Speed で動いていたホストが切断に気づかず古いアドレスで要求を続けることがある
--   （列挙がまれに失敗した）。確実に切断を見せてから接続する。
--   FS 接続（0x45, D+ プルアップ）→ SE0 が T_RESET_SE0 → bus_reset、チャープ準備（0x54）
--   → チャープ K を T_CHIRP → ホストの K/J（各 T_LINE_STABLE 連続）を 3 組 → HS（0x40）
--   → 揃わず T_HOST_CHIRP_TIMEOUT → FS に戻る
--   HS 中に squelch が T_HS_IDLE 続く → FS に戻し T_SUSPEND_SAMPLE 後: J ならサスペンド、それ以外はリセット
--   サスペンド中: K でレジューム、SE0 が T_LINE_STABLE でリセット。FS で J が T_FS_IDLE でサスペンド。
--   レジューム: ホストの K（20 ms 以上）の間は FS のまま待ち、K が終わったら（LS の EOP の SE0）
--   サスペンド前の速度に戻る（USB 2.0 §7.1.7.7）。
--   VBUS が無くなれば FS に戻し bus_reset。
library ieee;
use ieee.std_logic_1164.all;
use ieee.numeric_std.all;
use work.usb_timing_pkg.all;

entity usb_reset is
  generic (
    T_DETACH             : natural := work.usb_timing_pkg.T_DETACH;
    T_RESET_SE0          : positive := work.usb_timing_pkg.T_RESET_SE0;
    T_CHIRP              : positive := work.usb_timing_pkg.T_CHIRP;
    T_LINE_STABLE        : positive := work.usb_timing_pkg.T_LINE_STABLE;
    T_HOST_CHIRP_TIMEOUT : positive := work.usb_timing_pkg.T_HOST_CHIRP_TIMEOUT;
    T_HS_IDLE            : positive := work.usb_timing_pkg.T_HS_IDLE;
    T_SUSPEND_SAMPLE     : positive := work.usb_timing_pkg.T_SUSPEND_SAMPLE;
    T_FS_IDLE            : positive := work.usb_timing_pkg.T_FS_IDLE
  );
  port (
    clk        : in  std_logic;
    rst        : in  std_logic;
    link_ready : in  std_logic;
    line_state : in  std_logic_vector(1 downto 0);
    vbus_valid : in  std_logic;
    -- PHY レジスタ書き込み（ulpi_link の 4 フェーズポートへ）
    reg_req    : out std_logic;
    reg_we     : out std_logic;
    reg_addr   : out std_logic_vector(5 downto 0);
    reg_wdata  : out std_logic_vector(7 downto 0);
    reg_done   : in  std_logic;
    -- 状態
    chirp_k    : out std_logic;
    bus_reset  : out std_logic;   -- 1 周期パルス
    high_speed : out std_logic;
    suspended  : out std_logic
  );
end entity;

architecture rtl of usb_reset is
  constant LINE_SE0 : std_logic_vector(1 downto 0) := "00";   -- HS では squelch
  constant LINE_J   : std_logic_vector(1 downto 0) := "01";
  constant LINE_K   : std_logic_vector(1 downto 0) := "10";

  constant REG_FUNC_CTRL : std_logic_vector(5 downto 0) := "000100";
  constant REG_OTG_CTRL  : std_logic_vector(5 downto 0) := "001010";
  constant FC_DETACH     : std_logic_vector(7 downto 0) := x"49";   -- FS, Term=0, OpMode 非駆動（切り離し）
  constant FC_FS         : std_logic_vector(7 downto 0) := x"45";   -- FS, Term=1, OpMode 通常
  constant FC_CHIRP      : std_logic_vector(7 downto 0) := x"54";   -- HS, Term=1, OpMode 10
  constant FC_HS         : std_logic_vector(7 downto 0) := x"40";   -- HS, Term=0, OpMode 通常
  constant OTG_DEVICE    : std_logic_vector(7 downto 0) := x"00";   -- D+/D- プルダウン無し

  type state_t is (S_INIT, S_DETACH, S_FS, S_CHIRP_PREP, S_CHIRP, S_HOST_K, S_HOST_J, S_HS_ENTER, S_HS,
                   S_HS_IDLE_CHECK, S_SUSPEND, S_RESUME, S_FS_FALLBACK);
  signal state      : state_t := S_INIT;
  -- 最長の待ち時間より大きい上限で止まるタイマ（状態に入るたびに 0 から数える）
  constant TIMER_MAX : natural := T_CHIRP + T_HOST_CHIRP_TIMEOUT + T_HS_IDLE + T_FS_IDLE;
  signal timer      : natural range 0 to TIMER_MAX := 0;
  signal detach_time : natural range 0 to T_DETACH := 0;
  signal line_time  : natural range 0 to T_LINE_STABLE := 0;
  signal line_prev  : std_logic_vector(1 downto 0) := "00";
  signal pairs      : natural range 0 to 3 := 0;
  signal was_hs     : std_logic := '0';
  signal hs         : std_logic := '0';
  signal reset_p    : std_logic := '0';

  -- 目標値と書き込み済みの値
  signal fc_target   : std_logic_vector(7 downto 0) := FC_FS;
  signal fc_current  : std_logic_vector(7 downto 0) := x"00";
  signal fc_known    : std_logic := '0';
  signal otg_current : std_logic_vector(7 downto 0) := x"00";
  signal otg_known   : std_logic := '0';
  signal fc_synced   : boolean;

  type writer_t is (W_IDLE, W_FC, W_OTG, W_RELEASE);
  signal writer     : writer_t := W_IDLE;
  signal w_addr     : std_logic_vector(5 downto 0) := (others => '0');
  signal w_data     : std_logic_vector(7 downto 0) := (others => '0');

  signal line_stable : boolean;   -- line_state が T_LINE_STABLE 周期変わっていない
begin
  fc_synced   <= fc_known = '1' and fc_current = fc_target and otg_known = '1' and writer = W_IDLE;
  line_stable <= line_time = T_LINE_STABLE;

  ------------------------------------------------------------------------------
  -- PHY レジスタの書き込み担当（目標値との差分を 1 つずつ書く）
  ------------------------------------------------------------------------------
  process (clk)
  begin
    if rising_edge(clk) then
      if rst = '1' then
        writer    <= W_IDLE;
        fc_known  <= '0';
        otg_known <= '0';
      else
        case writer is
          when W_IDLE =>
            if link_ready = '1' then
              if otg_known = '0' or otg_current /= OTG_DEVICE then
                w_addr <= REG_OTG_CTRL;
                w_data <= OTG_DEVICE;
                writer <= W_OTG;
              elsif fc_known = '0' or fc_current /= fc_target then
                w_addr <= REG_FUNC_CTRL;
                w_data <= fc_target;
                writer <= W_FC;
              end if;
            end if;
          when W_OTG =>
            if reg_done = '1' then
              otg_current <= w_data;
              otg_known   <= '1';
              writer      <= W_RELEASE;
            end if;
          when W_FC =>
            if reg_done = '1' then
              fc_current <= w_data;
              fc_known   <= '1';
              writer     <= W_RELEASE;
            end if;
          when W_RELEASE =>
            writer <= W_IDLE;                 -- 4 フェーズ: reg_req を 1 周期下げる
        end case;
      end if;
    end if;
  end process;

  reg_req   <= '1' when writer = W_FC or writer = W_OTG else '0';
  reg_we    <= '1';
  reg_addr  <= w_addr;
  reg_wdata <= w_data;

  ------------------------------------------------------------------------------
  -- ラインステートの安定時間
  ------------------------------------------------------------------------------
  process (clk)
  begin
    if rising_edge(clk) then
      line_prev <= line_state;
      if line_state /= line_prev then
        line_time <= 0;
      elsif line_time /= T_LINE_STABLE then
        line_time <= line_time + 1;
      end if;
    end if;
  end process;

  ------------------------------------------------------------------------------
  -- リセット・チャープ・サスペンドの状態機械
  ------------------------------------------------------------------------------
  process (clk)
    procedure enter(next_state : state_t) is
    begin
      state <= next_state;
      timer <= 0;
    end procedure;

    procedure start_reset is
    begin
      reset_p   <= '1';
      hs        <= '0';
      fc_target <= FC_CHIRP;
      enter(S_CHIRP_PREP);
    end procedure;
  begin
    if rising_edge(clk) then
      reset_p <= '0';
      if timer /= TIMER_MAX then
        timer <= timer + 1;
      end if;

      if rst = '1' then
        state     <= S_INIT;
        timer     <= 0;
        hs        <= '0';
        was_hs    <= '0';
        fc_target <= FC_DETACH;
      elsif vbus_valid = '0' and state /= S_INIT and state /= S_DETACH and state /= S_FS then
        reset_p   <= '1';
        hs        <= '0';
        fc_target <= FC_FS;
        enter(S_FS);
      else
        case state is
          when S_INIT =>
            fc_target   <= FC_DETACH;
            detach_time <= 0;
            if fc_synced then
              enter(S_DETACH);
            end if;

          when S_DETACH =>
            if detach_time /= T_DETACH then
              detach_time <= detach_time + 1;
            else
              fc_target <= FC_FS;
              if fc_synced and fc_target = FC_FS then
                enter(S_FS);
              end if;
            end if;

          when S_FS =>
            -- timer は「現在のラインステートが続いている時間」
            if vbus_valid = '0' or line_state /= line_prev then
              timer <= 0;
            elsif line_state = LINE_SE0 and timer >= T_RESET_SE0 then
              start_reset;
            elsif line_state = LINE_J and timer >= T_FS_IDLE then
              was_hs <= '0';
              enter(S_SUSPEND);
            end if;

          when S_CHIRP_PREP =>
            if fc_synced then
              enter(S_CHIRP);
            end if;

          when S_CHIRP =>
            if timer >= T_CHIRP then
              pairs <= 0;
              enter(S_HOST_K);
            end if;

          when S_HOST_K | S_HOST_J =>
            if timer >= T_HOST_CHIRP_TIMEOUT then
              fc_target <= FC_FS;
              enter(S_FS_FALLBACK);
            elsif state = S_HOST_K and line_state = LINE_K and line_stable then
              state <= S_HOST_J;
            elsif state = S_HOST_J and line_state = LINE_J and line_stable then
              if pairs = 2 then
                fc_target <= FC_HS;
                hs        <= '1';
                enter(S_HS_ENTER);
              else
                pairs <= pairs + 1;
                state <= S_HOST_K;
              end if;
            end if;

          when S_HS_ENTER =>
            if fc_synced then
              enter(S_HS);
            end if;

          when S_FS_FALLBACK =>
            if fc_synced and line_state /= LINE_SE0 then
              enter(S_FS);
            end if;

          when S_HS =>
            if line_state /= LINE_SE0 then
              timer <= 0;                      -- 活動あり（squelch 解除）
            elsif timer >= T_HS_IDLE then
              hs        <= '0';
              fc_target <= FC_FS;
              enter(S_HS_IDLE_CHECK);
            end if;

          when S_HS_IDLE_CHECK =>
            if not fc_synced then
              timer <= 0;                      -- FS に切り替わってから測る
            elsif timer >= T_SUSPEND_SAMPLE then
              if line_state = LINE_J then
                was_hs <= '1';
                enter(S_SUSPEND);
              else
                start_reset;
              end if;
            end if;

          when S_SUSPEND =>
            if line_state = LINE_K then
              enter(S_RESUME);
            elsif line_state = LINE_SE0 and line_stable then
              start_reset;
            end if;

          when S_RESUME =>
            if line_state /= LINE_K then
              if was_hs = '1' then
                fc_target <= FC_HS;
                hs        <= '1';
                enter(S_HS_ENTER);
              else
                enter(S_FS);
              end if;
            end if;
        end case;
      end if;
    end if;
  end process;

  chirp_k    <= '1' when state = S_CHIRP else '0';
  bus_reset  <= reset_p;
  high_speed <= hs;
  suspended  <= '1' when state = S_SUSPEND else '0';
end architecture;
