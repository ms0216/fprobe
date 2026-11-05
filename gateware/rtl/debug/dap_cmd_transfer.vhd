-- CMSIS-DAP の転送コマンド: Transfer / TransferBlock / WriteAbort / JTAG_IDCODE。
-- 意味論（WAIT 再試行は dap_link、値一致・AP 読み出しのポスティング・最後の書き込みの確認・タイムスタンプ・未接続の応答・
-- 一致マスクの初期値 0）は ARM の参照実装（DAP.c）と同じ。JTAG では dap_index で装置を選ぶ（範囲外は 0 番）。
-- タイムスタンプ（要求の bit7。DAP_Transfer だけ）: その転送の線上の点の時刻（dap_link の stamp。共通タイムベースの下位
-- 32 ビット、Info 0x08 の周波数）。返す位置は DAP.c と同じ: DP の読みは値の前、AP の読みのポストは単独（続けて AP を読む
-- 要求では、前の値の後）、書きは単独。値一致の読み・一致マスクの書き・回収と確認の RDBUFF には付けない。
-- 未接続（enable = '0'）: Transfer / TransferBlock は線に出さず処理数 0・応答 0（DAP.c の Dummy）、WriteAbort・JTAG_IDCODE は
-- DAP_ERROR。WriteAbort は接続中なら応答によらず DAP_OK（DAP.c と同じ。JTAG で範囲外の装置は DAP_ERROR）。
-- 仕様: docs/superpowers/specs/2026-10-15-m4-swd-cmsis-dap-design.md §4、2026-10-16-dap-processor-split-design.md
library ieee;
use ieee.std_logic_1164.all;
use ieee.numeric_std.all;
use work.dap_pkg.all;

entity dap_cmd_transfer is
  port (
    clk          : in  std_logic;
    rst          : in  std_logic;
    -- dap_packet との I/F
    req          : in  unit_req_t;
    rsp          : out unit_rsp_t;
    header       : out std_logic_vector(23 downto 0);
    header_n     : out natural range 0 to 3;
    -- dap_link（転送のみ）
    link_req     : out std_logic;
    link_request : out std_logic_vector(3 downto 0);
    link_data    : out std_logic_vector(63 downto 0);
    link_ack     : in  std_logic;                         -- いちばん古い転送の応答がある（link_take の周期まで保つ）
    link_take    : out std_logic;                         -- link_ack を取る（S_WAIT_LINK・S_B_RUN の間）
    link_resp    : in  std_logic_vector(7 downto 0);
    link_ok      : in  boolean;
    link_rdata   : in  std_logic_vector(31 downto 0);
    link_stamp   : in  std_logic_vector(31 downto 0);
    -- 設定
    enable       : in  std_logic;                         -- 接続中（DAP_Connect の後）
    match_retry  : in  unsigned(15 downto 0);
    jtag         : in  std_logic;
    jtag_count   : in  natural range 1 to JTAG_MAX_DEVICES;
    jtag_index   : out natural range 0 to JTAG_MAX_DEVICES - 1
  );
end entity;

architecture rtl of dap_cmd_transfer is
  type state_t is (S_IDLE, S_DISPATCH, S_WAIT_GET, S_WAIT_PUT, S_WAIT_LINK, S_DONE,
                   S_WRITE_ABORT, S_WRITE_ABORT_DONE, S_IDCODE, S_IDCODE_DONE,
                   S_T_START, S_P_RUN, S_P_REQ, S_P_WDATA, S_P_DRAIN,
                   S_T_REQUEST, S_T_POSTED_COLLECT, S_T_AFTER_COLLECT,
                   S_T_MATCH_VALUE, S_T_MATCH_POSTED, S_T_MATCH_CHECK,
                   S_T_WRITE_COLLECT, S_T_WRITE_GET, S_T_WRITE_DATA, S_T_COUNTED, S_T_HEADER,
                   S_B_START, S_B_FIRST, S_B_RUN, S_B_WRITE, S_B_HEADER);
  signal state, ret : state_t := S_IDLE;
  signal rsp_r      : unit_rsp_t := UNIT_RSP_IDLE;
  signal link_req_r : std_logic := '0';
  signal request_r  : std_logic_vector(3 downto 0) := (others => '0');
  signal data_r     : std_logic_vector(31 downto 0) := (others => '0');
  signal header_r   : std_logic_vector(23 downto 0) := (others => '0');
  signal header_n_r : natural range 0 to 3 := 0;
  signal index_r    : natural range 0 to JTAG_MAX_DEVICES - 1 := 0;

  signal resp        : std_logic_vector(7 downto 0) := (others => '0');   -- 直近の転送応答
  signal last_ok     : boolean := false;                                   -- resp = RESP_OK（前もって保持）
  signal rdata       : std_logic_vector(31 downto 0) := (others => '0');
  signal t_left      : unsigned(15 downto 0) := (others => '0');          -- 残りの要求数
  signal t_done      : unsigned(15 downto 0) := (others => '0');          -- 完了した要求数
  signal t_request   : std_logic_vector(7 downto 0) := (others => '0');
  signal post_read   : std_logic := '0';
  signal check_write : std_logic := '0';
  signal match_value : std_logic_vector(31 downto 0) := (others => '0');
  signal match_mask  : std_logic_vector(31 downto 0) := (others => '0');
  signal match_left  : unsigned(15 downto 0) := (others => '0');
  signal match_zero  : boolean := true;                                    -- match_left = 0
  signal matched     : boolean := false;                                   -- 直近の読み値が値一致
  -- DAP_Transfer の先出し: 出す前の転送（p_*、要求 1 つ = 転送 1〜2 つ）と、出して応答を待つ転送の扱い（a_*、古い順）。
  -- store = 応答の値を put32 する、count = その要求の最後の転送（成功で処理数を 1 足す）
  type req2_t is array (0 to 1) of std_logic_vector(3 downto 0);
  type data2_t is array (0 to 1) of std_logic_vector(31 downto 0);
  type flag2_t is array (0 to 1) of boolean;
  -- タイムスタンプを返すか、返すなら値（store）の前か後か
  type ts_t is (TS_NONE, TS_BEFORE, TS_AFTER);
  type ts2_t is array (0 to 1) of ts_t;
  signal p_req       : req2_t := (others => (others => '0'));
  signal p_data      : data2_t := (others => (others => '0'));
  signal p_store, p_count : flag2_t := (others => false);
  signal p_ts        : ts2_t := (others => TS_NONE);
  signal p_n         : natural range 0 to 2 := 0;
  signal a_store, a_count : flag2_t := (others => false);
  signal a_ts        : ts2_t := (others => TS_NONE);
  signal t_final     : boolean := false;                                   -- 最後の回収・確認の RDBUFF を決めた
  -- TransferBlock の先出し
  signal b_issue     : unsigned(16 downto 0) := (others => '0');          -- まだ出していない転送の数
  signal b_rsp       : unsigned(16 downto 0) := (others => '0');          -- まだ取っていない応答の数
  signal b_out       : natural range 0 to 2 := 0;                          -- 出して応答を取っていない転送の数
  signal b_tail      : boolean := false;                                   -- 最後に RDBUFF を出す（AP の読み・書き）
  signal b_first     : boolean := false;                                   -- 次の応答は AP の読みの最初（値なし）
begin
  link_take    <= '1' when state = S_WAIT_LINK or state = S_B_RUN or state = S_P_RUN or state = S_P_DRAIN else '0';
  rsp          <= rsp_r;
  link_req     <= link_req_r;
  link_request <= request_r;
  link_data    <= x"00000000" & data_r;
  header       <= header_r;
  header_n     <= header_n_r;
  jtag_index   <= index_r;

  process (clk)
    procedure get(n : natural; back : state_t) is
    begin
      rsp_r.get_req <= '1';
      rsp_r.get_n   <= n;
      ret           <= back;
      state         <= S_WAIT_GET;
    end procedure;

    procedure put(n : natural; v : std_logic_vector(63 downto 0); back : state_t) is
    begin
      rsp_r.put_req <= '1';
      rsp_r.put_n   <= n;
      rsp_r.put_val <= v;
      ret           <= back;
      state         <= S_WAIT_PUT;
    end procedure;

    procedure put32(v : std_logic_vector(31 downto 0); back : state_t) is
    begin
      put(4, x"00000000" & v, back);
    end procedure;

    procedure swd(request : std_logic_vector(3 downto 0); wdata : std_logic_vector(31 downto 0);
                  back : state_t) is
    begin
      link_req_r <= '1';
      request_r  <= request;
      data_r     <= wdata;
      ret        <= back;
      state      <= S_WAIT_LINK;
    end procedure;

    -- 転送を出す（応答を待たない。TransferBlock の先出し）
    procedure swd_issue(request : std_logic_vector(3 downto 0); wdata : std_logic_vector(31 downto 0)) is
    begin
      link_req_r <= '1';
      request_r  <= request;
      data_r     <= wdata;
    end procedure;

    variable out_v  : natural range 0 to 3;
    variable stop_v : boolean;
    variable moved_v : boolean;
    variable pn_v   : natural range 0 to 2;
    variable as_v, ac_v : flag2_t;
    variable at_v   : ts2_t;

    -- 要求のタイムスタンプのビットが立っていれば ts、でなければ無し
    function stamp_if(r : std_logic_vector(7 downto 0); ts : ts_t) return ts_t is
    begin
      if r(REQ_TIMESTAMP) = '1' then
        return ts;
      end if;
      return TS_NONE;
    end function;

    -- DAP_Transfer: 要求 1 つの転送（1 つ・2 つ）を出す前の列に置く
    procedure plan1(r : std_logic_vector(3 downto 0); d : std_logic_vector(31 downto 0); st, ct : boolean; ts : ts_t) is
    begin
      p_req(0) <= r; p_data(0) <= d; p_store(0) <= st; p_count(0) <= ct; p_ts(0) <= ts;
      p_n <= 1;
    end procedure;
    -- 回収の RDBUFF（値を返す）の後に、要求の転送
    procedure plan2(r1 : std_logic_vector(3 downto 0); d1 : std_logic_vector(31 downto 0); st1 : boolean; ts1 : ts_t) is
    begin
      p_req(0) <= DP_R_RDBUFF; p_data(0) <= (others => '0'); p_store(0) <= true; p_count(0) <= false; p_ts(0) <= TS_NONE;
      p_req(1) <= r1; p_data(1) <= d1; p_store(1) <= st1; p_count(1) <= true; p_ts(1) <= ts1;
      p_n <= 2;
    end procedure;

    -- 応答の値とタイムスタンプを返す（DAP.c の並び）
    procedure put_result(d, t : std_logic_vector(31 downto 0); st : boolean; ts : ts_t; back : state_t) is
    begin
      if st and ts = TS_BEFORE then
        put(8, d & t, back);
      elsif st and ts = TS_AFTER then
        put(8, t & d, back);
      elsif st then
        put32(d, back);
      else
        put32(t, back);
      end if;
    end procedure;

    procedure set_resp(v : std_logic_vector(7 downto 0)) is
    begin
      resp    <= v;
      last_ok <= v = RESP_OK;
    end procedure;

    procedure finish(h : std_logic_vector(23 downto 0); n : natural) is
    begin
      header_r   <= h;
      header_n_r <= n;
      state      <= S_DONE;
    end procedure;

    procedure reply_status(s : std_logic_vector(7 downto 0)) is
    begin
      put(1, byte_word(s), S_DONE);
    end procedure;

    procedure select_device(index : std_logic_vector(7 downto 0)) is
    begin
      if unsigned(index) < jtag_count then
        index_r <= to_integer(unsigned(index));
      else
        index_r <= 0;
      end if;
    end procedure;
  begin
    if rising_edge(clk) then
      rsp_r.get_req <= '0';
      rsp_r.put_req <= '0';
      link_req_r <= '0';
      rsp_r.done    <= '0';

      if rst = '1' then
        state      <= S_IDLE;
        match_mask <= (others => '0');
        index_r    <= 0;
      else
        case state is
          when S_IDLE =>
            header_n_r <= 0;
            if req.start = '1' then
              state <= S_DISPATCH;
            end if;

          when S_DISPATCH =>
            case req.command is
              when ID_TRANSFER       => get(2, S_T_START);                    -- dap_index, count
              when ID_TRANSFER_BLOCK => get(4, S_B_START);                    -- dap_index, count(2), request
              when ID_WRITE_ABORT    => get(5, S_WRITE_ABORT);                -- dap_index, value(4)
              when others            => get(1, S_IDCODE);                     -- JTAG_IDCODE: index
            end case;

          when S_WAIT_GET =>
            if req.get_ack = '1' then
              state <= ret;
            end if;

          when S_WAIT_PUT =>
            if req.put_ack = '1' then
              state <= ret;
            end if;

          when S_WAIT_LINK =>
            if link_ack = '1' then
              set_resp(link_resp);
              rdata   <= link_rdata;
              matched <= (link_rdata and match_mask) = match_value;
              state   <= ret;
            end if;

          when S_DONE =>
            rsp_r.done <= '1';
            state  <= S_IDLE;

          ------------------------------------------------------------------------------
          -- WriteAbort / JTAG_IDCODE
          ------------------------------------------------------------------------------
          when S_WRITE_ABORT =>
            if enable = '0' or (jtag = '1' and unsigned(req.arg(7 downto 0)) >= jtag_count) then
              reply_status(DAP_ERROR);
            else
              select_device(req.arg(7 downto 0));                       -- SWD では使わない
              swd(DP_W_ABORT, req.arg(39 downto 8), S_WRITE_ABORT_DONE);
            end if;

          when S_WRITE_ABORT_DONE =>
            reply_status(DAP_OK);                                   -- 応答によらない（DAP.c）

          when S_IDCODE =>
            if enable = '0' or jtag = '0' or unsigned(req.arg(7 downto 0)) >= jtag_count then
              reply_status(DAP_ERROR);
            else
              select_device(req.arg(7 downto 0));
              swd(DP_R_IDCODE, (others => '0'), S_IDCODE_DONE);   -- jtag_phy では IDCODE 命令
            end if;

          when S_IDCODE_DONE =>
            if last_ok then
              put(5, x"000000" & rdata & DAP_OK, S_DONE);
            else
              reply_status(DAP_ERROR);
            end if;

          ------------------------------------------------------------------------------
          -- DAP_Transfer（参照実装 DAP.c の DAP_SWD_Transfer と同じ転送・応答。docs/superpowers/specs/2026-11-03-dap-line-rate-design.md §4）
          --   先出し: 要求を読み、その要求の転送（AP の読みの連鎖・ポストの回収の RDBUFF・読み・書き）を決めて出す前の列（p_*）に
          --   置き、応答を待たずに出す（出して応答を取っていない転送は 2 つまで。2 つ目は dap_link が条件つきで出すので、前が
          --   失敗すれば線に出ない）。応答は順に取り、値を put32 し、要求の最後の転送の成功で処理数を足す。失敗の応答を取ったら
          --   止める。どの転送を出すかは要求のビットとポストの有無（post_read）だけで決まり、前の転送の値によらない。
          --   値一致の読み（繰り返す）と一致マスクの書き（以後の要求に効く設定）は、出した転送の応答を取り終えてから 1 つずつ
          --   （S_T_REQUEST 〜 S_T_COUNTED）。全部の要求の後、ポストした読みがあれば RDBUFF で回収し、書きで終わったら確認の RDBUFF。
          ------------------------------------------------------------------------------
          when S_T_START =>
            select_device(req.arg(7 downto 0));
            t_left      <= resize(unsigned(req.arg(15 downto 8)), 16);
            t_done      <= (others => '0');
            set_resp((others => '0'));
            post_read   <= '0';
            check_write <= '0';
            p_n         <= 0;
            b_out       <= 0;
            t_final     <= false;
            if enable = '1' then
              put(2, (others => '0'), S_P_RUN);                       -- [1] 処理数, [2] 応答は最後に書く
            else
              put(2, (others => '0'), S_T_HEADER);                    -- 未接続: 処理数 0・応答 0（要求は dap_packet が読み捨てる）
            end if;

          when S_P_RUN | S_P_DRAIN =>
            out_v   := b_out;
            stop_v  := false;
            moved_v := false;
            pn_v    := p_n;
            as_v    := a_store;
            ac_v    := a_count;
            at_v    := a_ts;
            -- 応答を取る（この状態では link_take = 1）
            if link_ack = '1' then
              out_v := out_v - 1;
              set_resp(link_resp);
              if not link_ok then
                stop_v := true;
                state  <= S_T_HEADER;                                 -- 失敗: 止める（後の転送は dap_link が捨てる）
              else
                if ac_v(0) then
                  t_done <= t_done + 1;
                end if;
                if as_v(0) or at_v(0) /= TS_NONE then
                  put_result(link_rdata, link_stamp, as_v(0), at_v(0), state);   -- 同じ状態に戻る
                  moved_v := true;
                end if;
              end if;
              as_v(0) := as_v(1);
              ac_v(0) := ac_v(1);
              at_v(0) := at_v(1);
            end if;
            -- 出す前の列の先頭を出す（S_P_RUN だけ）
            if state = S_P_RUN and not stop_v and pn_v /= 0 and out_v < 2 then
              swd_issue(p_req(0), p_data(0));
              as_v(out_v) := p_store(0);
              ac_v(out_v) := p_count(0);
              at_v(out_v) := p_ts(0);
              out_v := out_v + 1;
              pn_v  := pn_v - 1;
              p_req(0) <= p_req(1); p_data(0) <= p_data(1); p_store(0) <= p_store(1); p_count(0) <= p_count(1);
              p_ts(0) <= p_ts(1);
              p_n <= pn_v;
            end if;
            a_store <= as_v;
            a_count <= ac_v;
            a_ts    <= at_v;
            b_out   <= out_v;
            if not stop_v and not moved_v then
              if state = S_P_DRAIN then
                if out_v = 0 then
                  state <= S_T_REQUEST;                               -- 応答を取り終えた: 値一致・一致マスクを 1 つずつ
                end if;
              elsif pn_v = 0 then
                if t_left /= 0 then
                  get(1, S_P_REQ);                                    -- 次の要求（転送が線に出ている間に読む）
                elsif not t_final then
                  t_final <= true;
                  if post_read = '1' then
                    post_read <= '0';
                    plan1(DP_R_RDBUFF, (others => '0'), true, false, TS_NONE);       -- 最後のポスト値を回収
                  elsif check_write = '1' then
                    check_write <= '0';
                    plan1(DP_R_RDBUFF, (others => '0'), false, false, TS_NONE);      -- 最後の書き込みの確認
                  end if;
                elsif out_v = 0 then
                  state <= S_T_HEADER;
                end if;
              end if;
            end if;

          when S_P_REQ =>
            t_request <= req.arg(7 downto 0);
            t_left    <= t_left - 1;
            if (req.arg(REQ_RNW) = '1' and req.arg(REQ_MATCH_VALUE) = '1') or (req.arg(REQ_RNW) = '0' and req.arg(REQ_MATCH_MASK) = '1') then
              state <= S_P_DRAIN;
            elsif req.arg(REQ_RNW) = '1' then
              check_write <= '0';
              if post_read = '1' then
                if req.arg(REQ_APNDP) = '1' then
                  -- 続けて AP を読み、前の値を受け取る（またポスト。タイムスタンプは前の値の後）
                  plan1(req.arg(3 downto 0), (others => '0'), true, true, stamp_if(req.arg(7 downto 0), TS_AFTER));
                else
                  post_read <= '0';
                  plan2(req.arg(3 downto 0), (others => '0'), true, stamp_if(req.arg(7 downto 0), TS_BEFORE));   -- 回収してから DP を読む
                end if;
              elsif req.arg(REQ_APNDP) = '1' then
                post_read <= '1';
                plan1(req.arg(3 downto 0), (others => '0'), false, true, stamp_if(req.arg(7 downto 0), TS_BEFORE));   -- 値は次の読み出しで返る
              else
                plan1(req.arg(3 downto 0), (others => '0'), true, true, stamp_if(req.arg(7 downto 0), TS_BEFORE));
              end if;
              state <= S_P_RUN;
            else
              get(4, S_P_WDATA);
            end if;

          when S_P_WDATA =>
            check_write <= '1';
            if post_read = '1' then
              post_read <= '0';
              plan2(t_request(3 downto 0), req.arg(31 downto 0), false, stamp_if(t_request, TS_BEFORE));   -- 回収してから書く
            else
              plan1(t_request(3 downto 0), req.arg(31 downto 0), false, true, stamp_if(t_request, TS_BEFORE));
            end if;
            state <= S_P_RUN;

          -- 値一致の読み・一致マスクの書き（出した転送の応答を取り終えてから。失敗は S_T_HEADER）
          when S_T_REQUEST =>
            if t_request(REQ_RNW) = '1' then
              if post_read = '1' then
                swd(DP_R_RDBUFF, (others => '0'), S_T_POSTED_COLLECT);
              else
                get(4, S_T_MATCH_VALUE);
              end if;
            elsif post_read = '1' then
              swd(DP_R_RDBUFF, (others => '0'), S_T_WRITE_COLLECT);
            else
              get(4, S_T_WRITE_DATA);
            end if;

          when S_T_POSTED_COLLECT =>
            if not last_ok then
              state <= S_T_HEADER;
            else
              post_read <= '0';
              put32(rdata, S_T_AFTER_COLLECT);
            end if;

          when S_T_AFTER_COLLECT =>
            get(4, S_T_MATCH_VALUE);

          when S_T_MATCH_VALUE =>
            match_value <= req.arg(31 downto 0);
            match_left  <= match_retry;
            match_zero  <= match_retry = 0;
            if t_request(REQ_APNDP) = '1' then
              swd(t_request(3 downto 0), (others => '0'), S_T_MATCH_POSTED);   -- AP はポストしてから
            else
              swd(t_request(3 downto 0), (others => '0'), S_T_MATCH_CHECK);
            end if;

          when S_T_MATCH_POSTED =>
            if not last_ok then
              state <= S_T_HEADER;
            else
              swd(t_request(3 downto 0), (others => '0'), S_T_MATCH_CHECK);
            end if;

          when S_T_MATCH_CHECK =>
            if not last_ok then
              state <= S_T_HEADER;
            elsif not matched then
              if not match_zero then
                match_left <= match_left - 1;
                match_zero <= match_left = 1;
                swd(t_request(3 downto 0), (others => '0'), S_T_MATCH_CHECK);
              else
                set_resp(RESP_OK or RESP_MISMATCH);
                state <= S_T_HEADER;
              end if;
            else
              check_write <= '0';
              state       <= S_T_COUNTED;
            end if;

          when S_T_WRITE_COLLECT =>
            if not last_ok then
              state <= S_T_HEADER;
            else
              post_read <= '0';
              put32(rdata, S_T_WRITE_GET);
            end if;

          when S_T_WRITE_GET =>
            get(4, S_T_WRITE_DATA);

          when S_T_WRITE_DATA =>                                      -- 一致マスク（線には出さない）
            match_mask <= req.arg(31 downto 0);
            set_resp(RESP_OK);
            state      <= S_T_COUNTED;

          when S_T_COUNTED =>
            t_done <= t_done + 1;
            state  <= S_P_RUN;

          when S_T_HEADER =>
            -- 失敗で抜けた場合の残りの要求は dap_packet が読み捨てる
            finish(x"00" & resp & std_logic_vector(t_done(7 downto 0)), 2);

          ------------------------------------------------------------------------------
          -- DAP_TransferBlock（先出し。docs/superpowers/specs/2026-11-03-dap-line-rate-design.md §4）
          --   線に出す転送: AP の読みは count 回の AP の読み（最初はポスト）+ RDBUFF、DP の読みは count 回、書きは count 回 +
          --   最後の確認の RDBUFF。応答を待たずに次の転送を出し（応答を取っていない転送は 2 つまで。2 つ目は dap_link が
          --   条件つきで出すので、前が失敗すれば線に出ない）、応答は順に取る: 読みの値は put32（AP の最初の応答は値なし）、
          --   書きは次の値を get(4) で先に取ってから出す。失敗の応答を取ったらそこで止める（出して応答の無い転送は dap_link が
          --   捨てる）。応答の並び・数・失敗の扱いは参照実装（DAP.c）と同じ。
          ------------------------------------------------------------------------------
          when S_B_START =>
            select_device(req.arg(7 downto 0));
            t_left    <= unsigned(req.arg(23 downto 8));
            t_request <= req.arg(31 downto 24);
            t_done    <= (others => '0');
            set_resp((others => '0'));
            if enable = '1' then
              put(3, (others => '0'), S_B_FIRST);                     -- [1..2] 処理数, [3] 応答は最後に書く
            else
              put(3, (others => '0'), S_B_HEADER);                    -- 未接続: 処理数 0・応答 0
            end if;

          when S_B_FIRST =>
            b_out <= 0;
            if t_left = 0 then
              state <= S_B_HEADER;
            elsif t_request(REQ_RNW) = '1' and t_request(REQ_APNDP) = '0' then
              b_issue <= resize(t_left, 17);                          -- DP の読み: count 回
              b_rsp   <= resize(t_left, 17);
              b_tail  <= false;
              state   <= S_B_RUN;
            else
              b_issue <= resize(t_left, 17) + 1;                      -- AP の読み + RDBUFF / 書き + 確認の RDBUFF
              b_rsp   <= resize(t_left, 17) + 1;
              b_tail  <= true;
              state   <= S_B_RUN;
            end if;
            b_first <= t_request(REQ_RNW) = '1' and t_request(REQ_APNDP) = '1';

          when S_B_RUN =>
            out_v  := b_out;
            stop_v := false;
            -- 応答を取る（この状態では link_take = 1）
            if link_ack = '1' then
              out_v := out_v - 1;
              b_rsp <= b_rsp - 1;
              set_resp(link_resp);
              if not link_ok then
                stop_v := true;
                state  <= S_B_HEADER;                                 -- 失敗: 止める
              else
                b_first <= false;
                if b_rsp = 1 then
                  state <= S_B_HEADER;                                -- 最後の応答（値があれば put の後に S_B_RUN で終える）
                end if;
                if t_request(REQ_RNW) = '1' then
                  if not b_first then
                    t_done <= t_done + 1;
                    put32(link_rdata, S_B_RUN);
                  end if;
                elsif not (b_tail and b_rsp = 1) then
                  t_done <= t_done + 1;                               -- 書き（最後の確認の RDBUFF は数えない）
                end if;
              end if;
            elsif b_rsp = 0 then
              state <= S_B_HEADER;
            end if;
            -- 次の転送を出す（応答を取っていない転送は 2 つまで。失敗を取った周期は出さない）
            if not stop_v and b_issue /= 0 and out_v < 2 then
              if b_tail and b_issue = 1 then
                swd_issue(DP_R_RDBUFF, (others => '0'));          -- 最後の RDBUFF（AP の読みの回収・書きの確認）
                out_v   := out_v + 1;
                b_issue <= b_issue - 1;
              elsif t_request(REQ_RNW) = '1' then
                swd_issue(t_request(3 downto 0), (others => '0'));
                out_v   := out_v + 1;
                b_issue <= b_issue - 1;
              else
                get(4, S_B_WRITE);                                    -- 書きの値を取ってから出す（書きは put しない）
              end if;
            end if;
            b_out <= out_v;

          when S_B_WRITE =>
            swd_issue(t_request(3 downto 0), req.arg(31 downto 0));
            b_out   <= b_out + 1;
            b_issue <= b_issue - 1;
            state   <= S_B_RUN;

          when S_B_HEADER =>
            finish(resp & std_logic_vector(t_done), 3);
        end case;
      end if;
    end if;
  end process;
end architecture;
