-- DAP サンプラ（F4: 変数のライブ観測）: ターゲットを止めずに、最大 8 か所（各 1〜16 語の連続）のメモリを周期的に読み、
-- 値が返った周期に 1 語ずつ出す（fprobe_core が時刻を付けてイベント 種類 7 にする）。
-- 仕様: docs/superpowers/specs/2026-10-17-f4-live-sampler-design.md
--       docs/superpowers/specs/2026-10-18-b17-sampler-lazy-restore-design.md（B17: 遅延の戻し）
--
-- swd_injector と同じく、上流（dap_processor と swd_injector）と swd_phy の間に入るアービタ。上流の命令の境界
-- （up_busy = '0' で、PHY に上流の命令が残っていない）で PHY を借り（held）、か所を 1 つずつ読む:
--   借りて最初だけ: SELECT ← ap（bank 0）/（控えが無ければ CSW・TAR を読んで保存）/ CSW ← 32 ビット・自動増加 single
--     （有効なか所が 1 つで 1 語なら自動増加なし = 同じ番地を読み続け、TAR も借りて最初の 1 回だけ）
--   か所ごと: TAR ← 番地 / DRW 読み × n（ポスト: 語 i の値は i+1 回目の DRW 読み、最後の語は RDBUFF で返る）/
--     CTRL/STAT（STICKYERR なら最後の語を出さない）
-- 遅延の戻し: ホストが線を使っていない間は SELECT・CSW・TAR を戻さず、線を持ったまま次のか所・次の巡回を読む。
-- 上流が PHY に命令を出したら（up_cmd_valid）、それを待たせたまま（up_cmd_ready = '0'）次の境界（か所の終わり・
-- 巡回の外）で 1 度だけ CSW・TAR・SELECT をホストの値に戻して渡す（ホストがキャッシュしている値なので、ホストからは
-- 見えない）。止めた（run = '0'）とき・持っている間にか所の表が変わったときも戻して渡す（AP 等の設定は止めてから
-- 変える）。きっかけを PHY の命令にするのは、PHY を使わない DAP の命令（SWJ_Clock・切断等）が PHY の設定を変える
-- 間に戻しの転送を重ねないため。切断（enable = '0'）では戻さずに放す（ホストは線をリセットして書き直す）。
-- 戻すきっかけ（give_back）と「有効なか所が 1 つ」（single）は 1 周期前に登録する。持っている間は上流の命令は PHY に
-- 届かないので、きっかけが 1 周期遅れてもホストの命令は追い越されない（判定の組み合わせの段を sys_clk に収める。
-- B17 の前回の版は標準の構成でタイミングを満たさなかった）。状態は 1 ビットの印（tar_ok 等）だけで持ち、値は比べない。
-- 番地の自動増加は 1 KB の境界をまたげない（ADIv5）。ホストは 1 か所を 1 KB の中に収める。
-- バスの誤りは SWD では「次の」AP アクセスで FAULT になる（ポスト）。サンプラが起こした誤りは ABORT（STKERRCLR）で
-- 消してから CSW・TAR・SELECT を戻して渡す（ホストの次の転送が、サンプラの起こした誤りで FAULT にならない）。
-- WAIT は RETRIES 回まで繰り返す。FAULT 以外の異常（応答なし・パリティ）は線の不調なので、SELECT だけ戻して渡す
-- （ponytail: swd_injector と同じ）。誤りはどれも errors に数える。
-- 前の巡回が終わる前に次の周期が来たら overruns に数え、巡回が終わったら間を空けずに次の巡回を始める（周期に
-- 追いつかないときは「できるだけ速く」になる。周期を丸ごと飛ばすと実際の速さが半分に落ちた。2026-10-18 に実機で）。
-- jitter（k > 0）: 周期に 0〜2^k − 1 の乱数（16 ビットの LFSR）を足し、2^(k−1) を引く（平均の周期は変わらない）。
-- 周期的な処理（割り込み等）と読みの周期が噛み合って割合が偏る（エイリアシング）のを防ぐ（PC サンプリング）。
-- か所の表（番地・語数）は分散 RAM に置き、CSR からは 1 か所ずつ書く（tbl_write）。巡回は か所を順に 1 つずつ
-- 読むので、全か所をフリップフロップで並べる必要は無い（Primer で 8 か所 × 36 ビットの FF と 8:1 の選択が入りきらなかった）。
-- 有効の印だけは次の か所を探すために FF に置く。
library ieee;
use ieee.std_logic_1164.all;
use ieee.numeric_std.all;

entity dap_sampler is
  generic (
    SLOTS   : positive := 8;
    RETRIES : positive := 255
  );
  port (
    clk           : in  std_logic;
    rst           : in  std_logic;
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
    enable        : in  std_logic;                       -- SWD が接続中
    up_busy       : in  std_logic;                       -- 上流（dap_processor・swd_injector）が使っている
    select_shadow : in  std_logic_vector(31 downto 0);
    csw_shadow    : in  std_logic_vector(31 downto 0);
    tar_shadow    : in  std_logic_vector(31 downto 0);
    shadow_valid  : in  std_logic;
    -- 読んだ値（1 周期のパルス）
    sample_valid  : out std_logic;
    sample_addr   : out std_logic_vector(31 downto 0);
    sample_data   : out std_logic_vector(31 downto 0);
    sample_ack    : out std_logic_vector(2 downto 0);
    -- 状態
    busy          : out std_logic;
    held          : out std_logic;                       -- 線を持っている（ホストの次の転送の前に戻す）
    rounds        : out unsigned(31 downto 0);
    errors        : out unsigned(31 downto 0);
    overruns      : out unsigned(31 downto 0);
    restores      : out unsigned(15 downto 0);           -- 線を渡した回数（試験用。つながなければ合成で消える）
    last_ack      : out std_logic_vector(2 downto 0);
    -- 上流側（swd_phy の命令 I/F と同じ）
    up_cmd_valid  : in  std_logic;
    up_cmd_ready  : out std_logic;
    up_cmd_op     : in  std_logic_vector(1 downto 0);
    up_cmd_count  : in  natural range 1 to 64;
    up_cmd_req    : in  std_logic_vector(3 downto 0);
    up_cmd_data   : in  std_logic_vector(63 downto 0);
    up_rsp_valid  : out std_logic;
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

architecture rtl of dap_sampler is
  constant OP_TRANSFER : std_logic_vector(1 downto 0) := "10";
  constant ACK_OK      : std_logic_vector(2 downto 0) := "001";
  constant ACK_WAIT    : std_logic_vector(2 downto 0) := "010";
  constant ACK_FAULT   : std_logic_vector(2 downto 0) := "100";
  constant STICKYERR   : natural := 5;                    -- CTRL/STAT
  -- 転送要求（CMSIS-DAP の並び: bit3-2 A[3:2], bit1 RnW, bit0 APnDP）
  constant DP_W_SELECT : std_logic_vector(3 downto 0) := "1000";
  constant DP_R_RDBUFF : std_logic_vector(3 downto 0) := "1110";
  constant DP_R_CTRL   : std_logic_vector(3 downto 0) := "0110";
  constant DP_W_ABORT  : std_logic_vector(3 downto 0) := "0000";
  constant AP_R_CSW    : std_logic_vector(3 downto 0) := "0011";
  constant AP_R_TAR    : std_logic_vector(3 downto 0) := "0111";
  constant AP_R_DRW    : std_logic_vector(3 downto 0) := "1111";
  constant AP_W_CSW    : std_logic_vector(3 downto 0) := "0001";
  constant AP_W_TAR    : std_logic_vector(3 downto 0) := "0101";

  -- 1 か所の手順
  type step_t is (P_SELECT, P_READ_CSW, P_READ_TAR, P_RDBUFF_TAR, P_CSW, P_TAR, P_DRW, P_RDBUFF, P_CHECK, P_CLEAR,
                  P_RESTORE_CSW, P_RESTORE_TAR, P_RESTORE_SELECT);
  type state_t is (S_IDLE, S_NEXT_SLOT, S_LOAD, S_WAIT_BOUNDARY, S_ISSUE, S_RESPONSE);
  -- か所の表（分散 RAM。リセットしない）: 番地(31..2) & 語数 − 1
  type table_t is array (0 to SLOTS - 1) of std_logic_vector(33 downto 0);
  signal table       : table_t := (others => (others => '0'));
  signal enables     : std_logic_vector(SLOTS - 1 downto 0) := (others => '0');
  signal state       : state_t := S_IDLE;
  signal step        : step_t := P_SELECT;
  signal mine        : boolean := false;                 -- PHY を借りている（held。ホストの値に戻すまで）
  signal up_pending  : boolean := false;                 -- 上流の命令が PHY に残っている
  signal in_round    : boolean := false;
  signal pending     : boolean := false;                 -- 巡回の最中に周期が来た: 終わったらすぐ次の巡回
  signal slot        : natural range 0 to SLOTS := 0;
  signal word        : natural range 0 to 16 := 0;        -- 出した DRW 読みの数
  signal words       : natural range 1 to 16 := 1;
  signal base        : unsigned(31 downto 0) := (others => '0');
  signal aborting    : boolean := false;               -- 誤りを数えた後の後始末の最中
  signal saved       : boolean := false;               -- CSW・TAR の戻す値がある（控え、または読んで保存した）
  signal last        : std_logic_vector(31 downto 0) := (others => '0');   -- 最後の語（誤りの確認まで出さない）
  signal tries       : natural range 0 to RETRIES := 0;
  signal csw, tar    : std_logic_vector(31 downto 0) := (others => '0');
  signal fast        : boolean := false;
  signal same        : boolean := false;               -- 有効なか所が 1 つで 1 語: 自動増加なしで TAR を動かさない
  signal tar_ok      : boolean := false;               -- TAR はか所の番地のまま
  signal stale       : boolean := false;               -- 持っている間にか所の表が変わった: 戻して借り直す
  -- 1 周期前に登録した判定（組み合わせの段を減らす）
  signal give_back   : boolean := false;               -- 戻して渡す: 上流の PHY の命令・止めた・表が変わった
  signal single      : boolean := false;               -- 有効なか所がちょうど 1 つ
  signal timer       : unsigned(31 downto 0) := (others => '0');
  signal due         : boolean := false;               -- 周期が満ちた（次の周期に巡回を始める）
  signal reload      : unsigned(31 downto 0) := (others => '0');   -- 次の周期（ゆらぎを足した period）
  -- ゆらぎの計算は 3 段に分ける（1 段だと シフト → マスク → 32 ビットの加減算 が sys_clk に入らなかった）
  signal jit_mask    : unsigned(31 downto 0) := (others => '0');   -- 2^jitter − 1
  signal jit_base    : unsigned(31 downto 0) := (others => '0');   -- period − 2^(jitter−1)
  signal jit_on      : boolean := false;
  signal jit_masked  : unsigned(31 downto 0) := (others => '0');   -- 乱数 & jit_mask
  signal lfsr        : std_logic_vector(15 downto 0) := x"ACE1";
  signal req_r       : std_logic_vector(3 downto 0) := (others => '0');
  signal data_r      : std_logic_vector(31 downto 0) := (others => '0');
  signal valid_r     : std_logic := '0';
  signal s_valid     : std_logic := '0';
  signal s_addr, s_data : std_logic_vector(31 downto 0) := (others => '0');
  signal s_ack, ack_r : std_logic_vector(2 downto 0) := (others => '0');
  signal rounds_r, errors_r, overruns_r : unsigned(31 downto 0) := (others => '0');
  signal restores_r  : unsigned(15 downto 0) := (others => '0');
begin
  -- PHY の命令の切り替え（mine は境界でしか変わらない）
  phy_cmd_valid <= valid_r when mine else up_cmd_valid;
  phy_cmd_op    <= OP_TRANSFER when mine else up_cmd_op;
  phy_cmd_count <= 1 when mine else up_cmd_count;
  phy_cmd_req   <= req_r when mine else up_cmd_req;
  phy_cmd_data  <= x"00000000" & data_r when mine else up_cmd_data;
  up_cmd_ready  <= '0' when mine else phy_cmd_ready;
  up_rsp_valid  <= '0' when mine else phy_rsp_valid;

  process (clk)
    procedure issue(p : step_t) is
    begin
      valid_r <= '1';
      data_r  <= (others => '0');
      case p is
        when P_SELECT         => req_r <= DP_W_SELECT; data_r <= ap & x"000000";
        when P_READ_CSW       => req_r <= AP_R_CSW;                            -- 読みは 1 つ後で返る
        when P_READ_TAR       => req_r <= AP_R_TAR;                            -- CSW が返る
        when P_RDBUFF_TAR     => req_r <= DP_R_RDBUFF;                         -- TAR が返る
        when P_CSW            =>                                               -- 32 ビット・増加 single（same: なし）
          req_r <= AP_W_CSW;
          if same then
            data_r <= csw(31 downto 6) & "00" & csw(3) & "010";
          else
            data_r <= csw(31 downto 6) & "01" & csw(3) & "010";
          end if;
        when P_TAR            => req_r <= AP_W_TAR; data_r <= std_logic_vector(base);
        when P_DRW            => req_r <= AP_R_DRW;
        when P_RDBUFF         => req_r <= DP_R_RDBUFF;
        when P_CHECK          => req_r <= DP_R_CTRL;
        when P_CLEAR          => req_r <= DP_W_ABORT; data_r <= x"00000004";       -- STKERRCLR
        when P_RESTORE_CSW    => req_r <= AP_W_CSW; data_r <= csw;
        when P_RESTORE_TAR    => req_r <= AP_W_TAR; data_r <= tar;
        when P_RESTORE_SELECT => req_r <= DP_W_SELECT; data_r <= select_shadow;
      end case;
    end procedure;

    -- 線を渡すために戻し始める（CSW・TAR の戻す値が無ければ SELECT だけ）
    procedure start_restore is
    begin
      tries    <= 0;
      aborting <= false;
      state    <= S_ISSUE;
      if saved then
        step <= P_RESTORE_CSW;
      else
        step <= P_RESTORE_SELECT;
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

    procedure emit(index : natural; value : std_logic_vector(31 downto 0); ack : std_logic_vector(2 downto 0)) is
    begin
      s_valid <= '1';
      -- 1 か所は 1 KB をまたがない（ホストが保証）: 番地の下位 10 ビットだけを足す
      s_addr  <= std_logic_vector(base(31 downto 10)) & std_logic_vector(base(9 downto 2) + to_unsigned(index, 8))
                 & "00";
      s_data  <= value;
      s_ack   <= ack;
    end procedure;

    variable k : natural range 0 to SLOTS;
  begin
    if rising_edge(clk) then
      s_valid <= '0';
      if phy_cmd_ready = '1' then
        valid_r <= '0';
      end if;
      if not mine and up_cmd_valid = '1' and phy_cmd_ready = '1' then
        up_pending <= true;
      elsif phy_rsp_valid = '1' then
        up_pending <= false;
      end if;
      -- 判定の登録（持っている間は上流の命令は PHY に届かないので、1 周期遅れても追い越されない）
      give_back <= up_cmd_valid = '1' or run = '0' or stale;
      single  <= unsigned(enables) /= 0 and (unsigned(enables) and (unsigned(enables) - 1)) = 0;

      -- ゆらぎ: LFSR（x^16 + x^14 + x^13 + x^11 + 1）を毎周期進め、次の周期 = period + 乱数 − 2^(jitter−1)
      lfsr <= lfsr(14 downto 0) & (lfsr(15) xor lfsr(13) xor lfsr(12) xor lfsr(10));
      jit_on     <= jitter /= 0 and period > shift_left(to_unsigned(1, 32), jitter);
      jit_mask   <= shift_left(to_unsigned(1, 32), jitter) - 1;
      if jitter = 0 then
        jit_base <= period;
      else
        jit_base <= period - shift_left(to_unsigned(1, 32), jitter - 1);
      end if;
      jit_masked <= resize(unsigned(lfsr), 32) and jit_mask;
      if jit_on then
        reload <= jit_base + jit_masked;
      else
        reload <= period;
      end if;

      -- 周期の時計（下り。period を積んで 1 まで数える = period 周期ごと）。満了は due に登録し、巡回を始めるのは
      -- 次の周期（31 ビットの 0 の検出から slot・state 等への経路を切る。周期の長さは変わらない）
      due <= false;
      if run = '0' or period = 0 then
        timer   <= period;
        pending <= false;
      elsif timer(31 downto 1) = 0 then
        timer <= reload;
        due   <= true;
      else
        timer <= timer - 1;
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
        up_pending <= false;
        in_round   <= false;
        pending    <= false;
        valid_r    <= '0';
        timer      <= (others => '0');
        due        <= false;
        lfsr       <= x"ACE1";
        enables    <= (others => '0');
        stale      <= false;
        give_back  <= false;
        rounds_r   <= (others => '0');
        errors_r   <= (others => '0');
        overruns_r <= (others => '0');
        restores_r <= (others => '0');
        ack_r      <= (others => '0');
      else
        if tbl_write = '1' then
          enables(tbl_index) <= tbl_enable;
        end if;
        case state is
          when S_IDLE =>                                         -- 巡回の外
            if mine and enable = '0' then
              mine <= false;                                     -- 切断: 戻さずに放す
            elsif mine and give_back then                        -- ホストの転送の前（か止めた・表が変わった）: 戻す
              start_restore;
            end if;

          when S_NEXT_SLOT =>
            k := next_slot(slot, enables);
            if k = SLOTS or run = '0' or enable = '0' then
              if in_round and k = SLOTS then
                rounds_r <= rounds_r + 1;
              end if;
              if pending and k = SLOTS and run = '1' and enable = '1' then
                pending <= false;                                -- 追いつかない周期の分: 間を空けずに次の巡回
                slot    <= 0;
              else
                in_round <= false;
                state    <= S_IDLE;
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
            if enable = '0' then
              mine     <= false;                                 -- 切断: 戻さずに放す
              in_round <= false;
              state    <= S_IDLE;
            elsif mine then                                      -- 持ったまま（SELECT・CSW は効いている）
              if give_back then
                start_restore;
              elsif up_busy = '0' then
                tries <= 0;
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
              step     <= P_SELECT;
              word     <= 0;
              tries    <= 0;
              aborting <= false;
              saved    <= shadow_valid = '1';
              fast     <= shadow_valid = '1';
              tar_ok   <= false;
              stale    <= false;
              same     <= words = 1 and single;
              csw      <= csw_shadow;
              tar      <= tar_shadow;
              state    <= S_ISSUE;
            end if;

          when S_ISSUE =>
            issue(step);
            state <= S_RESPONSE;

          when S_RESPONSE =>
            if phy_rsp_valid = '1' then
              ack_r <= phy_rsp_ack;
              if phy_rsp_ack = ACK_OK and phy_rsp_par = '0' then
                tries <= 0;
                state <= S_ISSUE;
                case step is
                  when P_SELECT =>
                    if fast then
                      step <= P_CSW;                             -- 控えがあるので CSW・TAR を読まない
                    else
                      step <= P_READ_CSW;
                    end if;
                  when P_READ_CSW   => step <= P_READ_TAR;
                  when P_READ_TAR   => csw <= phy_rsp_data; step <= P_RDBUFF_TAR;
                  when P_RDBUFF_TAR => tar <= phy_rsp_data; saved <= true; step <= P_CSW;
                  when P_CSW        => step <= P_TAR;
                  when P_TAR        => tar_ok <= same; step <= P_DRW;
                  when P_DRW =>
                    if word > 0 then
                      emit(word - 1, phy_rsp_data, phy_rsp_ack);      -- ポスト: 1 つ前の語の値
                    end if;
                    word <= word + 1;
                    if word + 1 = words then
                      step <= P_RDBUFF;
                    end if;
                  when P_RDBUFF =>
                    last <= phy_rsp_data;
                    step <= P_CHECK;
                  when P_CHECK =>
                    if phy_rsp_data(STICKYERR) = '1' then
                      errors_r <= errors_r + 1;                  -- 最後の語の読みがバスの誤り: 出さずに消す
                      aborting <= true;
                      step     <= P_CLEAR;
                    else
                      emit(words - 1, last, ACK_OK);
                      slot  <= slot + 1;                         -- 線は持ったまま次のか所へ
                      state <= S_NEXT_SLOT;
                    end if;
                  when P_CLEAR =>
                    if saved then
                      step <= P_RESTORE_CSW;
                    else
                      step <= P_RESTORE_SELECT;
                    end if;
                  when P_RESTORE_CSW => step <= P_RESTORE_TAR;
                  when P_RESTORE_TAR => step <= P_RESTORE_SELECT;
                  when P_RESTORE_SELECT =>                      -- 戻し終えた: 渡す
                    hand_back;
                    if aborting then                             -- 誤りの後始末ならそのか所は終わり
                      slot <= slot + 1;
                    end if;
                end case;
              elsif phy_rsp_ack = ACK_WAIT and tries < RETRIES then
                tries <= tries + 1;
                state <= S_ISSUE;
              elsif aborting then                                -- 後始末の最中にまた失敗: SELECT を戻して終える
                tries <= 0;
                if step = P_RESTORE_SELECT then
                  hand_back;
                  slot <= slot + 1;
                else
                  step  <= P_RESTORE_SELECT;
                  state <= S_ISSUE;
                end if;
              else
                errors_r <= errors_r + 1;
                aborting <= true;
                tries    <= 0;
                state    <= S_ISSUE;
                if phy_rsp_ack = ACK_FAULT then
                  step <= P_CLEAR;                               -- バスの誤り: 消してから全部戻す
                else
                  step <= P_RESTORE_SELECT;                      -- 線の不調: SELECT だけ戻す
                end if;
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

  busy         <= '0' when state = S_IDLE else '1';
  held         <= '1' when mine else '0';
  sample_valid <= s_valid;
  sample_addr  <= s_addr;
  sample_data  <= s_data;
  sample_ack   <= s_ack;
  rounds       <= rounds_r;
  errors       <= errors_r;
  overruns     <= overruns_r;
  restores     <= restores_r;
  last_ack     <= ack_r;
end architecture;
