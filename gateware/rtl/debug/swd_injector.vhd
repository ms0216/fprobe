-- デバッグ動作の割り込み（DebugActionInjector）: クロストリガの要求で、ターゲットのメモリに語の列を書く
-- （語 0 は addr / value、既定は DHCSR に C_HALT = 停止。語 1〜extra は表。例: CTI の無い Cortex-A の全コアの
-- DBGDRCR に停止要求）。仕様: docs/superpowers/specs/2026-10-16-m9-cross-trigger-design.md §2、
-- 語の列と CTRL/STAT の確認は docs/superpowers/specs/2026-10-29-halt-watch-design.md §3.2
--
-- dap_processor と swd_phy の間に入るアービタ。要求が来たら、DAP コマンドの境界（busy = '0' で、
-- PHY に命令が残っていない）で PHY を借り、次の転送を行ってから返す:
--   SELECT ← ap（bank 0）/ CSW・TAR を読んで保存 / CSW ← 32 ビット・自動増加なし /
--   語ごとに TAR ← 番地 / DRW ← 値（語 0〜extra の順）/
--   CTRL/STAT を読む（書き込みの誤りはポストされる。JTAG-DP には FAULT の応答が無いので、STICKYERR でしか分からない。
--   立っていれば失敗として後始末へ）/ CSW・TAR を戻す / SELECT ← ホストが最後に書いた値（select_shadow。dap_monitor が覚えている）
-- 途中で失敗したら、その語で止め、受理された DRW の数（written_words）を残す（ポストなので、最後に受理された語が誤りの
-- ことがある。ホストは対象の状態を読んで確かめる）。
-- ホスト（pyOCD 等）がキャッシュしている SELECT・CSW・TAR を元に戻すので、ホストからは割り込みが見えない。
-- dap_monitor がホストの書いた CSW・TAR を控えていれば（shadow_valid）、読んで保存する転送（CSW・TAR・RDBUFF）を省き、
-- 控えを戻す（停止の書き込みまで 4 転送。仕様 §5）。戻す値はホストのキャッシュそのもの。
-- 控え（shadow_valid）は、その CSW・TAR を書いた AP（shadow_ap）が注入先 ap と同じときだけ使う。違えば読んで保存する。
-- WAIT は wait_retry 回（ホストが DAP_TransferConfigure で設定）まで繰り返す。失敗（WAIT の使い切り・FAULT・プロトコル異常・
-- パリティ異常）なら中止し、後始末で戻す（戻しの転送の WAIT も同じく再試行する）:
--   FAULT: ABORT（STKERRCLR）→ CSW・TAR（戻す値があるとき）→ SELECT。注入が起こしたスティッキーエラーを消す（dap_sampler と同じ）
--   WAIT の使い切り: CSW・TAR（戻す値があるとき）→ SELECT
--   プロトコル異常・パリティ異常（線の不調）: SELECT だけ
-- 下流の dap_sampler が線を持っていれば、放すのを待ってから最初の転送を出す（line_held）。サンプラが直接渡し、
-- 線の SELECT・CSW が分かっている（line_handed: SELECT は同じ AP の bank 0、CSW は控えの CSW の Size を 32 ビットにした値）
-- なら、控えを使う場合（fast）に SELECT・CSW の書き込みを省いて TAR から書く（stop-precision-design §3.7）。サンプラの CSW の
-- AddrInc は single のことがあるが、語ごとに TAR を書いてから DRW を書くので、書く番地は変わらない。終わりの戻しは同じ
-- （CSW・TAR・SELECT を控えに戻す）。
-- 切断（enable = '0'。DAP_Disconnect）・接続の方式の切り替え（jtag が借りたときと違う。DAP_Connect）は PHY を使わないので、
-- 線を借りている間にも起こる。PHY はそのとき転送を捨てて応答を出さないので、どの状態でも（転送を出す前・応答を待つ間も）
-- 戻さずに線を放して終える（result = 2。後始末の転送は通らないので、ホストのキャッシュした SELECT・CSW・TAR は戻らない。
-- 接続し直すホストは書き直す）。dap_sampler の lost と同じ扱い。
-- written_time: 書いた語の DRW の転送の線上の点の時刻（phy_rsp_time。docs/superpowers/specs/2026-11-01-time-accuracy-design.md R5）。
-- 始動の列（docs/superpowers/specs/2026-11-03-halt-watch-fpga-restart-design.md §3.2）: start_request（1 周期）で、表の語
-- start_first〜start_last を同じ書き方で書く（例 停止の見張りで、全コアの DBGDRCR に再開の要求）。要求は start_pending として
-- 残り、空いたときに始める（書き込みの最中に来ても捨てない）。request（クロストリガ）と同じ周期なら request を先にする。
-- done は要求が終わった周期に 1（どちらの種類でも、どの結果でも。その周期の result・written_words はその要求のもの）、
-- kind_start は今（または最後）の要求が始動の列か。
library ieee;
use ieee.std_logic_1164.all;
use ieee.numeric_std.all;

entity swd_injector is
  generic (
    WORDS         : positive range 1 to 8 := 8           -- 書ける語の数（語 0 + 表の語 1〜WORDS − 1）
  );
  port (
    clk           : in  std_logic;
    rst           : in  std_logic;
    -- WAIT の再試行の回数（ホストが DAP_TransferConfigure で設定した値。dap_processor の wait_retry_out）
    wait_retry    : in  unsigned(15 downto 0);
    -- 要求と設定
    request       : in  std_logic;                       -- 1 周期のパルス（実行中に来た要求は捨てる）
    enable        : in  std_logic;                       -- SWD が接続中（dap_processor の swd_enable）
    jtag          : in  std_logic;                       -- 接続は JTAG（dap_processor の jtag。借りた後に変われば線を放す）
    dap_busy      : in  std_logic;
    select_shadow : in  std_logic_vector(31 downto 0);
    csw_shadow    : in  std_logic_vector(31 downto 0);
    tar_shadow    : in  std_logic_vector(31 downto 0);
    shadow_valid  : in  std_logic;
    shadow_ap     : in  std_logic_vector(7 downto 0);    -- 控えを書いた AP。ap と違えば控えを使わず読んで保存する
    ap            : in  std_logic_vector(7 downto 0);
    addr          : in  std_logic_vector(31 downto 0);
    value         : in  std_logic_vector(31 downto 0);
    extra         : in  natural range 0 to WORDS - 1;    -- 語 0 の後に書く表の語の数（止めてから変える）
    tbl_write     : in  std_logic;                       -- 1 周期: 表の語 tbl_index を書く（止めてから）
    tbl_index     : in  natural range 0 to WORDS - 1;
    tbl_addr      : in  std_logic_vector(31 downto 0);
    tbl_value     : in  std_logic_vector(31 downto 0);
    -- 下流の dap_sampler（無ければ '0'）
    line_held     : in  std_logic := '0';                -- 線を持っている（放すまで最初の転送を出さない）
    line_handed   : in  std_logic := '0';                -- 直接渡した線の SELECT・CSW が分かっている
    -- 始動の列
    start_request : in  std_logic;                       -- 1 周期のパルス（待たされても捨てない）
    start_first   : in  natural range 0 to WORDS - 1;    -- 始動の列の最初の語（0 は addr / value）
    start_last    : in  natural range 0 to WORDS - 1;    -- 始動の列の最後の語（first より小さいと first の 1 語）
    start_pending : out std_logic;                       -- 始動の列を要求してから終わるまで
    kind_start    : out std_logic;                       -- 今（または最後）の要求は始動の列
    done          : out std_logic;                       -- 要求が終わった周期に 1
    -- 状態
    busy          : out std_logic;
    held          : out std_logic;                       -- PHY を借りている（下流の dap_sampler が、戻さずに渡すかの判定に使う）
    written       : out std_logic;                       -- DRW の書き込みが受理された周期に 1（語ごと）
    written_addr  : out std_logic_vector(31 downto 0);   -- written の周期: その語の番地
    written_value : out std_logic_vector(31 downto 0);   -- written の周期: その語の値
    written_time  : out unsigned(63 downto 0);           -- written の周期: その語の DRW の線上の点の時刻
    result        : out std_logic_vector(1 downto 0);    -- 最後の要求: 0 成功 / 1 SWD の異常 / 2 未接続（途中の切断・方式の切り替えを含む）
    last_ack      : out std_logic_vector(2 downto 0);
    count         : out unsigned(15 downto 0);           -- 成功した回数
    transfers     : out unsigned(7 downto 0);            -- 最後の要求で停止の書き込みまでに出した転送の数（WAIT の繰り返しを含む、255 で止まる。R20）
    written_words : out unsigned(3 downto 0);            -- 最後の要求で受理された DRW の書き込みの数
    -- dap_processor 側（swd_phy の命令 I/F と同じ）
    dap_cmd_valid : in  std_logic;
    dap_cmd_ready : out std_logic;
    dap_cmd_cond  : in  std_logic := '0';                -- 前の転送が OK のときだけ受ける（PHY へそのまま。借りている間は 0）
    dap_cmd_op    : in  std_logic_vector(1 downto 0);
    dap_cmd_count : in  natural range 1 to 64;
    dap_cmd_req   : in  std_logic_vector(3 downto 0);
    dap_cmd_data  : in  std_logic_vector(63 downto 0);
    dap_rsp_valid : out std_logic;
    -- swd_phy 側（応答の ack / data / parity は両者へそのまま配る）
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
    phy_rsp_time  : in  unsigned(63 downto 0)
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
  constant DP_R_CTRL   : std_logic_vector(3 downto 0) := "0110";
  constant STEP_CHECK  : natural := 7;                    -- 最後の語の後: CTRL/STAT を読む
  constant STEP_RESTORE : natural := 8;                   -- CSW を戻す（→ TAR → SELECT）
  constant LAST_STEP   : natural := 10;
  constant STEP_ABORT  : natural := 11;                   -- 後始末: ABORT ← STKERRCLR
  constant DP_W_ABORT  : std_logic_vector(3 downto 0) := "0000";
  constant STICKYERR   : natural := 5;
  type words_t is array (0 to 7) of std_logic_vector(31 downto 0);   -- 0 は使わない（語 0 は addr / value）

  type state_t is (S_IDLE, S_WAIT_BOUNDARY, S_WAIT_LINE, S_ISSUE, S_RESPONSE);
  signal state       : state_t := S_IDLE;
  signal mine        : boolean := false;                 -- PHY を借りている
  signal jtag_r      : boolean := false;                 -- 借りたときの接続の方式
  signal lost        : boolean;                          -- 切断・方式の切り替え（PHY は応答しない）
  signal dap_out     : natural range 0 to 2 := 0;        -- DAP の命令で PHY が受けて応答をまだ返していない数（PHY は前の
                                                         -- 応答の前に次を受けることがある: 2 まで）
  signal dap_pending : boolean;                          -- DAP の命令が PHY に残っている
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
  signal word        : natural range 0 to WORDS - 1 := 0;   -- 書いている語
  signal words_r     : unsigned(3 downto 0) := (others => '0');
  signal done_s      : std_logic;
  signal tbl_a, tbl_v : words_t;                          -- 表（分散 RAM。語 0 は使わない）
  signal cur_addr, cur_value : std_logic_vector(31 downto 0);
  signal w_addr, w_value : std_logic_vector(31 downto 0) := (others => '0');
  signal w_time      : unsigned(63 downto 0) := (others => '0');
  signal last_w      : natural range 0 to WORDS - 1 := 0;   -- この要求の最後の語
  signal start_pend  : boolean := false;                 -- 始動の列の要求が待っている
  signal kind_s      : boolean := false;                 -- この要求は始動の列
  signal was_busy    : boolean := false;                 -- 前の周期に要求を処理していた（done の検出）
  signal done_now    : std_logic := '0';                 -- 未接続で、書かずに終えた要求（busy にならない）
begin
  -- 表の書き込み（分散 RAM に推論させるため、リセットの無い別のプロセス）
  process (clk)
  begin
    if rising_edge(clk) then
      if tbl_write = '1' and tbl_index > 0 then
        tbl_a(tbl_index) <= tbl_addr;
        tbl_v(tbl_index) <= tbl_value;
      end if;
    end if;
  end process;
  cur_addr  <= addr when word = 0 else tbl_a(word);
  cur_value <= value when word = 0 else tbl_v(word);

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
  dap_pending   <= dap_out /= 0;
  phy_cmd_valid <= valid_r when mine else dap_cmd_valid;
  phy_cmd_cond  <= '0' when mine else dap_cmd_cond;
  phy_cmd_op    <= OP_TRANSFER when mine else dap_cmd_op;
  phy_cmd_count <= 1 when mine else dap_cmd_count;
  phy_cmd_req   <= req_r when mine else dap_cmd_req;
  phy_cmd_data  <= x"00000000" & data_r when mine else dap_cmd_data;
  dap_cmd_ready <= '0' when mine else phy_cmd_ready;
  dap_rsp_valid <= '0' when mine else phy_rsp_valid;
  lost          <= enable = '0' or (mine and (jtag = '1') /= jtag_r);

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
        when 5 => req_r <= AP_W_TAR;    data_r <= cur_addr;
        when 6 => req_r <= AP_W_DRW;    data_r <= cur_value;
        when STEP_CHECK => req_r <= DP_R_CTRL;
        when STEP_RESTORE => req_r <= AP_W_CSW; data_r <= csw;
        when 9 => req_r <= AP_W_TAR;    data_r <= tar;
        when STEP_ABORT => req_r <= DP_W_ABORT; data_r <= x"00000004";   -- STKERRCLR
        when others => req_r <= DP_W_SELECT; data_r <= select_shadow;
      end case;
    end procedure;
  begin
    if rising_edge(clk) then
      written_r <= '0';
      done_now  <= '0';
      if phy_cmd_ready = '1' then
        valid_r <= '0';
      end if;
      if not mine and dap_cmd_valid = '1' and phy_cmd_ready = '1' then
        if not (phy_rsp_valid = '1' and dap_out /= 0) then
          dap_out <= dap_out + 1;
        end if;
      elsif phy_rsp_valid = '1' and dap_out /= 0 then
        dap_out <= dap_out - 1;
      end if;

      if rst = '1' then
        state       <= S_IDLE;
        mine        <= false;
        jtag_r      <= false;
        dap_out     <= 0;
        valid_r     <= '0';
        result_r    <= "00";
        ack_r       <= "000";
        count_r     <= (others => '0');
        transfers_r <= (others => '0');
        cleanup     <= false;
        saved       <= false;
        fast        <= false;
        start_pend  <= false;
        kind_s      <= false;
      else
        case state is
          when S_IDLE =>
            if request = '1' or start_pend then                -- クロストリガの要求を先に
              kind_s <= request = '0';
              if request = '1' then
                word   <= 0;
                last_w <= extra;
              else
                start_pend <= false;
                word   <= start_first;
                last_w <= start_last;
              end if;
              if enable = '0' then
                result_r <= "10";
                words_r  <= (others => '0');
                done_now <= '1';
              else
                state <= S_WAIT_BOUNDARY;
              end if;
            end if;

          when S_WAIT_BOUNDARY =>
            if enable = '0' then
              result_r <= "10";
              words_r  <= (others => '0');                        -- 何も書いていない（前の要求の語の数を残さない）
              state    <= S_IDLE;
            elsif dap_busy = '0' and dap_cmd_valid = '0' and not dap_pending then
              mine  <= true;
              jtag_r <= jtag = '1';
              issued <= (others => '0');
              words_r <= (others => '0');
              step  <= 0;
              tries <= (others => '0');
              fast  <= shadow_valid = '1' and shadow_ap = ap;
              -- 控えがあれば、戻す値は借りた時点からある（最初の SELECT で失敗しても CSW・TAR を戻す。サンプラから直接
              -- 渡されたときは、線の CSW・TAR がサンプラの値のままなので、戻さないとホストのキャッシュと食い違う）
              saved <= shadow_valid = '1' and shadow_ap = ap;
              cleanup <= false;
              csw   <= csw_shadow;
              tar   <= tar_shadow;
              state <= S_WAIT_LINE;
            end if;

          when S_WAIT_LINE =>                                     -- 下流のサンプラが線を放すまで（直接渡すか、戻してから）
            if lost then
              mine     <= false;
              result_r <= "10";
              state    <= S_IDLE;
            elsif line_held = '0' then
              if fast and line_handed = '1' then
                step <= 5;                                        -- SELECT・CSW は渡された線のまま（§3.7）: TAR から
              end if;
              state <= S_ISSUE;
            end if;

          when S_ISSUE =>
            if lost then                                          -- 転送の前に切断・方式の切り替え: 戻さずに放す
              mine     <= false;
              valid_r  <= '0';
              cleanup  <= false;
              result_r <= "10";
              state    <= S_IDLE;
            else
              issue(step);
              if issued /= 255 then
                issued <= issued + 1;
              end if;
              state <= S_RESPONSE;
            end if;

          when S_RESPONSE =>
            if lost then                                          -- 転送の途中で切断・方式の切り替え（PHY は応答しない）
              mine     <= false;
              valid_r  <= '0';                                    -- 受け取られていない転送は取り下げる
              cleanup  <= false;
              result_r <= "10";
              state    <= S_IDLE;
            elsif phy_rsp_valid = '1' then
              ack_r <= phy_rsp_ack;
              if phy_rsp_ack = ACK_OK and phy_rsp_par = '0' then
                tries <= (others => '0');
                state <= S_ISSUE;
                if cleanup then                                   -- 後始末: ABORT → CSW → TAR → SELECT の順に進む
                  if step = STEP_ABORT then
                    if saved then
                      step <= STEP_RESTORE;
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
                    written_r <= '1';
                    w_addr    <= cur_addr;
                    w_value   <= cur_value;
                    w_time    <= phy_rsp_time;
                    words_r   <= words_r + 1;
                    if words_r = 0 then
                      transfers_r <= issued;                      -- 最初の語の書き込みまで（この転送を含む）
                    end if;
                  end if;
                  if step = 6 and word < last_w then
                    word <= word + 1;
                    step <= 5;                                    -- 次の語: TAR ← 番地
                  elsif step = STEP_CHECK and phy_rsp_data(STICKYERR) = '1' then
                    result_r <= "01";                             -- 書き込みのどれかがバスの誤り: 消してから全部戻す
                    cleanup  <= true;
                    step     <= STEP_ABORT;
                  elsif step = LAST_STEP then
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
                  step <= STEP_RESTORE;                           -- WAIT の使い切り: CSW・TAR（戻す値がある）→ SELECT
                else
                  step <= LAST_STEP;                              -- 線の不調など: SELECT だけ
                end if;
              end if;
            end if;

        end case;
        if start_request = '1' then                               -- 受けた周期の取り消しより後: 新しい要求は残る
          start_pend <= true;
        end if;
      end if;
    end if;
  end process;

  -- done: 要求の処理を終えた周期（busy の立ち下がり）、または未接続で書かずに終えた周期
  done_i : process (clk)
  begin
    if rising_edge(clk) then
      was_busy <= state /= S_IDLE and rst = '0';
    end if;
  end process;

  busy     <= '0' when state = S_IDLE else '1';
  done     <= done_s;
  done_s   <= '1' when (was_busy and state = S_IDLE) or done_now = '1' else '0';
  kind_start    <= '1' when kind_s else '0';
  -- 終わった周期（done）も要求中に数える: 結果を残すレジスタ（fprobe_core の start_status）はその周期の終わりに入るので、
  -- pending が下がった周期には結果がもう新しい（前は 1 周期だけ pending = 0 で前の結果が読めた。レビュー 2026-11-03）
  start_pending <= '1' when start_pend or (kind_s and (state /= S_IDLE or done_s = '1')) else '0';
  held     <= '1' when mine else '0';
  written  <= written_r;
  result   <= result_r;
  last_ack <= ack_r;
  count    <= count_r;
  transfers <= transfers_r;
  written_words <= words_r;
  written_addr  <= w_addr;
  written_value <= w_value;
  written_time  <= w_time;
end architecture;
