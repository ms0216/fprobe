-- デバッグ動作の割り込み（DebugActionInjector）: クロストリガの要求で、ターゲットのメモリ 1 語に書く
-- （既定は DHCSR に C_HALT = 停止）。仕様: docs/superpowers/specs/2026-10-16-m9-cross-trigger-design.md §2
--
-- dap_processor と swd_phy の間に入るアービタ。要求が来たら、DAP コマンドの境界（busy = '0' で、
-- PHY に命令が残っていない）で PHY を借り、次の SWD 転送を行ってから返す:
--   SELECT ← ap（bank 0）/ CSW・TAR を読んで保存 / CSW ← 32 ビット・自動増加なし / TAR ← addr / DRW ← value /
--   CSW・TAR を戻す / SELECT ← ホストが最後に書いた値（select_shadow。dap_monitor が覚えている）
-- ホスト（pyOCD 等）がキャッシュしている SELECT・CSW・TAR を元に戻すので、ホストからは割り込みが見えない。
-- dap_monitor がホストの書いた CSW・TAR を控えていれば（shadow_valid）、読んで保存する転送（CSW・TAR・RDBUFF）を省き、
-- 控えを戻す（停止の書き込みまで 4 転送。仕様 §5）。戻す値はホストのキャッシュそのもの。
-- 控え（shadow_valid）は、その CSW・TAR を書いた AP（shadow_ap）が注入先 ap と同じときだけ使う。違えば読んで保存する。
-- WAIT は wait_retry 回（ホストが DAP_TransferConfigure で設定）まで繰り返す。失敗（WAIT の使い切り・FAULT・プロトコル異常・
-- パリティ異常）なら中止し、後始末で戻す（戻しの転送の WAIT も同じく再試行する）:
--   FAULT: ABORT（STKERRCLR）→ CSW・TAR（戻す値があるとき）→ SELECT。注入が起こしたスティッキーエラーを消す（dap_sampler と同じ）
--   WAIT の使い切り: CSW・TAR（戻す値があるとき）→ SELECT
--   プロトコル異常・パリティ異常（線の不調）: SELECT だけ
library ieee;
use ieee.std_logic_1164.all;
use ieee.numeric_std.all;

entity swd_injector is
  port (
    clk           : in  std_logic;
    rst           : in  std_logic;
    -- WAIT の再試行の回数（ホストが DAP_TransferConfigure で設定した値。dap_processor の wait_retry_out）
    wait_retry    : in  unsigned(15 downto 0);
    -- 要求と設定
    request       : in  std_logic;                       -- 1 周期のパルス（実行中に来た要求は捨てる）
    enable        : in  std_logic;                       -- SWD が接続中（dap_processor の swd_enable）
    dap_busy      : in  std_logic;
    select_shadow : in  std_logic_vector(31 downto 0);
    csw_shadow    : in  std_logic_vector(31 downto 0);
    tar_shadow    : in  std_logic_vector(31 downto 0);
    shadow_valid  : in  std_logic;
    shadow_ap     : in  std_logic_vector(7 downto 0);    -- 控えを書いた AP。ap と違えば控えを使わず読んで保存する
    ap            : in  std_logic_vector(7 downto 0);
    addr          : in  std_logic_vector(31 downto 0);
    value         : in  std_logic_vector(31 downto 0);
    -- 状態
    busy          : out std_logic;
    written       : out std_logic;                       -- DRW の書き込みが受理された周期に 1
    result        : out std_logic_vector(1 downto 0);    -- 最後の要求: 0 成功 / 1 SWD の異常 / 2 未接続
    last_ack      : out std_logic_vector(2 downto 0);
    count         : out unsigned(15 downto 0);           -- 成功した回数
    transfers     : out unsigned(7 downto 0);            -- 最後の要求で停止の書き込みまでに出した転送の数（WAIT の繰り返しを含む、255 で止まる。R20）
    -- dap_processor 側（swd_phy の命令 I/F と同じ）
    dap_cmd_valid : in  std_logic;
    dap_cmd_ready : out std_logic;
    dap_cmd_op    : in  std_logic_vector(1 downto 0);
    dap_cmd_count : in  natural range 1 to 64;
    dap_cmd_req   : in  std_logic_vector(3 downto 0);
    dap_cmd_data  : in  std_logic_vector(63 downto 0);
    dap_rsp_valid : out std_logic;
    -- swd_phy 側（応答の ack / data / parity は両者へそのまま配る）
    phy_cmd_valid : out std_logic;
    phy_cmd_ready : in  std_logic;
    phy_cmd_op    : out std_logic_vector(1 downto 0);
    phy_cmd_count : out natural range 1 to 64;
    phy_cmd_req   : out std_logic_vector(3 downto 0);
    phy_cmd_data  : out std_logic_vector(63 downto 0);
    phy_rsp_valid : in  std_logic;
    phy_rsp_ack   : in  std_logic_vector(2 downto 0);
    phy_rsp_data  : in  std_logic_vector(31 downto 0);
    phy_rsp_par   : in  std_logic
  );
end entity;

architecture rtl of swd_injector is
  constant OP_TRANSFER : std_logic_vector(1 downto 0) := "10";
  constant ACK_OK      : std_logic_vector(2 downto 0) := "001";
  constant ACK_WAIT    : std_logic_vector(2 downto 0) := "010";
  constant ACK_FAULT   : std_logic_vector(2 downto 0) := "100";
  -- 転送要求（CMSIS-DAP の並び: bit3-2 A[3:2], bit1 RnW, bit0 APnDP）
  constant DP_W_SELECT : std_logic_vector(3 downto 0) := "1000";
  constant DP_R_RDBUFF : std_logic_vector(3 downto 0) := "1110";
  constant AP_R_CSW    : std_logic_vector(3 downto 0) := "0011";
  constant AP_R_TAR    : std_logic_vector(3 downto 0) := "0111";
  constant AP_W_CSW    : std_logic_vector(3 downto 0) := "0001";
  constant AP_W_TAR    : std_logic_vector(3 downto 0) := "0101";
  constant AP_W_DRW    : std_logic_vector(3 downto 0) := "1101";
  constant LAST_STEP   : natural := 9;
  constant STEP_ABORT  : natural := 10;                   -- 後始末: ABORT ← STKERRCLR
  constant DP_W_ABORT  : std_logic_vector(3 downto 0) := "0000";

  type state_t is (S_IDLE, S_WAIT_BOUNDARY, S_ISSUE, S_RESPONSE);
  signal state       : state_t := S_IDLE;
  signal mine        : boolean := false;                 -- PHY を借りている
  signal dap_pending : boolean := false;                 -- DAP の命令が PHY に残っている
  signal step        : natural range 0 to STEP_ABORT := 0;
  signal tries       : unsigned(15 downto 0) := (others => '0');
  -- 再試行の回数はいったん受けて（めったに変わらない）、比べた結果もレジスタに。WAIT の応答は数十周期ごとなので、
  -- 1 周期前の tries との比べで足りる（DAP の処理部からの長い経路と 16 ビットの比べを、応答の判定の経路から外す）
  signal retry_r     : unsigned(15 downto 0) := (others => '0');
  signal retry_more  : std_logic := '0';
  signal csw, tar    : std_logic_vector(31 downto 0) := (others => '0');
  signal fast        : boolean := false;                 -- 控えを使う（CSW・TAR を読まない）
  signal saved       : boolean := false;                 -- CSW・TAR の戻す値がある（控え、または読んで保存した）
  signal cleanup     : boolean := false;                 -- 失敗の後始末の最中
  signal req_r       : std_logic_vector(3 downto 0) := (others => '0');
  signal data_r      : std_logic_vector(31 downto 0) := (others => '0');
  signal valid_r     : std_logic := '0';
  signal written_r   : std_logic := '0';
  signal result_r    : std_logic_vector(1 downto 0) := "00";
  signal ack_r       : std_logic_vector(2 downto 0) := "000";
  signal count_r     : unsigned(15 downto 0) := (others => '0');
  signal issued      : unsigned(7 downto 0) := (others => '0');   -- 借りてから出した転送の数
  signal transfers_r : unsigned(7 downto 0) := (others => '0');
begin
  retry_i : process (clk)
  begin
    if rising_edge(clk) then
      retry_r <= wait_retry;
      if tries < retry_r then
        retry_more <= '1';
      else
        retry_more <= '0';
      end if;
    end if;
  end process;

  -- PHY の命令の切り替え（mine は境界でしか変わらない）
  phy_cmd_valid <= valid_r when mine else dap_cmd_valid;
  phy_cmd_op    <= OP_TRANSFER when mine else dap_cmd_op;
  phy_cmd_count <= 1 when mine else dap_cmd_count;
  phy_cmd_req   <= req_r when mine else dap_cmd_req;
  phy_cmd_data  <= x"00000000" & data_r when mine else dap_cmd_data;
  dap_cmd_ready <= '0' when mine else phy_cmd_ready;
  dap_rsp_valid <= '0' when mine else phy_rsp_valid;

  process (clk)
    -- 手順 n の転送要求と書き込み値
    procedure issue(n : natural) is
    begin
      valid_r <= '1';
      data_r  <= (others => '0');
      case n is
        when 0 => req_r <= DP_W_SELECT; data_r <= ap & x"000000";
        when 1 => req_r <= AP_R_CSW;                                  -- 読みは 1 つ後で返る
        when 2 => req_r <= AP_R_TAR;                                  -- CSW が返る
        when 3 => req_r <= DP_R_RDBUFF;                               -- TAR が返る
        when 4 => req_r <= AP_W_CSW;    data_r <= csw(31 downto 6) & "00" & csw(3) & "010";   -- 32 ビット、増加なし
        when 5 => req_r <= AP_W_TAR;    data_r <= addr;
        when 6 => req_r <= AP_W_DRW;    data_r <= value;
        when 7 => req_r <= AP_W_CSW;    data_r <= csw;
        when 8 => req_r <= AP_W_TAR;    data_r <= tar;
        when STEP_ABORT => req_r <= DP_W_ABORT; data_r <= x"00000004";   -- STKERRCLR
        when others => req_r <= DP_W_SELECT; data_r <= select_shadow;
      end case;
    end procedure;
  begin
    if rising_edge(clk) then
      written_r <= '0';
      if phy_cmd_ready = '1' then
        valid_r <= '0';
      end if;
      if not mine and dap_cmd_valid = '1' and phy_cmd_ready = '1' then
        dap_pending <= true;
      elsif phy_rsp_valid = '1' then
        dap_pending <= false;
      end if;

      if rst = '1' then
        state       <= S_IDLE;
        mine        <= false;
        dap_pending <= false;
        valid_r     <= '0';
        result_r    <= "00";
        ack_r       <= "000";
        count_r     <= (others => '0');
        transfers_r <= (others => '0');
        cleanup     <= false;
        saved       <= false;
        fast        <= false;
      else
        case state is
          when S_IDLE =>
            if request = '1' then
              if enable = '0' then
                result_r <= "10";
              else
                state <= S_WAIT_BOUNDARY;
              end if;
            end if;

          when S_WAIT_BOUNDARY =>
            if enable = '0' then
              result_r <= "10";
              state    <= S_IDLE;
            elsif dap_busy = '0' and dap_cmd_valid = '0' and not dap_pending then
              mine  <= true;
              issued <= (others => '0');
              step  <= 0;
              tries <= (others => '0');
              fast  <= shadow_valid = '1' and shadow_ap = ap;
              saved <= false;
              cleanup <= false;
              csw   <= csw_shadow;
              tar   <= tar_shadow;
              state <= S_ISSUE;
            end if;

          when S_ISSUE =>
            issue(step);
            if issued /= 255 then
              issued <= issued + 1;
            end if;
            state <= S_RESPONSE;

          when S_RESPONSE =>
            if phy_rsp_valid = '1' then
              ack_r <= phy_rsp_ack;
              if phy_rsp_ack = ACK_OK and phy_rsp_par = '0' then
                tries <= (others => '0');
                state <= S_ISSUE;
                if cleanup then                                   -- 後始末: ABORT → CSW → TAR → SELECT の順に進む
                  if step = STEP_ABORT then
                    if saved then
                      step <= 7;
                    else
                      step <= LAST_STEP;
                    end if;
                  elsif step = LAST_STEP then
                    mine  <= false;                               -- result は失敗（1）のまま
                    state <= S_IDLE;
                  else
                    step <= step + 1;
                  end if;
                else
                  if step = 2 then
                    csw <= phy_rsp_data;
                  elsif step = 3 then
                    tar <= phy_rsp_data;
                    saved <= true;
                  elsif step = 6 then
                    written_r   <= '1';
                    transfers_r <= issued;                        -- 停止の書き込みまで（この転送を含む）
                  end if;
                  if step = LAST_STEP then
                    result_r <= "00";
                    count_r  <= count_r + 1;
                    mine     <= false;
                    state    <= S_IDLE;
                  elsif fast and step = 0 then
                    saved <= true;
                    step  <= 4;                                   -- 控えがあるので CSW・TAR を読まない
                  else
                    step  <= step + 1;
                  end if;
                end if;
              elsif phy_rsp_ack = ACK_WAIT and retry_more = '1' then
                tries <= tries + 1;
                state <= S_ISSUE;
              elsif cleanup then                                  -- 後始末の最中にまた失敗: 先の戻しを諦めて SELECT へ（SELECT なら終える）
                tries <= (others => '0');
                if step = LAST_STEP then
                  mine  <= false;
                  state <= S_IDLE;
                else
                  step  <= LAST_STEP;
                  state <= S_ISSUE;
                end if;
              else
                result_r <= "01";
                cleanup  <= true;
                tries    <= (others => '0');
                state    <= S_ISSUE;
                if phy_rsp_ack = ACK_FAULT then
                  step <= STEP_ABORT;                             -- バスの誤り: スティッキーエラーを消してから戻す
                elsif phy_rsp_ack = ACK_WAIT and saved then
                  step <= 7;                                      -- WAIT の使い切り: CSW・TAR（戻す値がある）→ SELECT
                else
                  step <= LAST_STEP;                              -- 線の不調など: SELECT だけ
                end if;
              end if;
            end if;

        end case;
      end if;
    end if;
  end process;

  busy     <= '0' when state = S_IDLE else '1';
  written  <= written_r;
  result   <= result_r;
  last_ack <= ack_r;
  count    <= count_r;
  transfers <= transfers_r;
end architecture;
