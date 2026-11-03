-- CMSIS-DAP の転送コマンド: Transfer / TransferBlock / WriteAbort / JTAG_IDCODE。
-- 意味論（WAIT 再試行は dap_link、値一致・AP 読み出しのポスティング・最後の書き込みの確認）は ARM の参照実装（DAP.c）
-- と同じ。JTAG では dap_index で装置を選ぶ（範囲外は 0 番）。
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
    command      : in  natural range 0 to 255;
    start        : in  std_logic;
    done         : out std_logic;
    header       : out std_logic_vector(23 downto 0);
    header_n     : out natural range 0 to 3;
    get_req      : out std_logic;
    get_n        : out natural range 1 to 8;
    get_ack      : in  std_logic;
    arg          : in  std_logic_vector(63 downto 0);
    put_req      : out std_logic;
    put_n        : out natural range 1 to 8;
    put_val      : out std_logic_vector(63 downto 0);
    put_ack      : in  std_logic;
    -- dap_link（転送のみ）
    link_req     : out std_logic;
    link_request : out std_logic_vector(3 downto 0);
    link_data    : out std_logic_vector(63 downto 0);
    link_ack     : in  std_logic;                         -- いちばん古い転送の応答がある（link_take の周期まで保つ）
    link_take    : out std_logic;                         -- link_ack を取る（S_WAIT_LINK・S_B_RUN の間）
    link_resp    : in  std_logic_vector(7 downto 0);
    link_ok      : in  boolean;
    link_rdata   : in  std_logic_vector(31 downto 0);
    -- 設定
    match_retry  : in  unsigned(15 downto 0);
    jtag         : in  std_logic;
    jtag_count   : in  natural range 1 to JTAG_MAX_DEVICES;
    jtag_index   : out natural range 0 to JTAG_MAX_DEVICES - 1
  );
end entity;

architecture rtl of dap_cmd_transfer is
  type state_t is (S_IDLE, S_DISPATCH, S_WAIT_GET, S_WAIT_PUT, S_WAIT_LINK, S_DONE,
                   S_WRITE_ABORT, S_WRITE_ABORT_DONE, S_IDCODE, S_IDCODE_DONE,
                   S_T_START, S_T_NEXT, S_T_REQUEST, S_T_AP_CHAIN, S_T_POSTED_COLLECT, S_T_AFTER_COLLECT,
                   S_T_READ, S_T_READ_DONE, S_T_MATCH_VALUE, S_T_MATCH_POSTED, S_T_MATCH_CHECK,
                   S_T_WRITE_COLLECT, S_T_WRITE_GET, S_T_WRITE_DATA, S_T_WRITE_DONE, S_T_COUNTED,
                   S_T_FINISH, S_T_LAST_READ, S_T_HEADER,
                   S_B_START, S_B_FIRST, S_B_RUN, S_B_WRITE, S_B_HEADER);
  signal state, ret : state_t := S_IDLE;
  signal get_req_r, put_req_r, link_req_r, done_r : std_logic := '0';
  signal get_n_r, put_n_r : natural range 1 to 8 := 1;
  signal put_val_r  : std_logic_vector(63 downto 0) := (others => '0');
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
  signal match_mask  : std_logic_vector(31 downto 0) := (others => '1');
  signal match_left  : unsigned(15 downto 0) := (others => '0');
  signal match_zero  : boolean := true;                                    -- match_left = 0
  signal matched     : boolean := false;                                   -- 直近の読み値が値一致
  -- TransferBlock の先出し
  signal b_issue     : unsigned(16 downto 0) := (others => '0');          -- まだ出していない転送の数
  signal b_rsp       : unsigned(16 downto 0) := (others => '0');          -- まだ取っていない応答の数
  signal b_out       : natural range 0 to 2 := 0;                          -- 出して応答を取っていない転送の数
  signal b_tail      : boolean := false;                                   -- 最後に RDBUFF を出す（AP の読み・書き）
  signal b_first     : boolean := false;                                   -- 次の応答は AP の読みの最初（値なし）
begin
  link_take    <= '1' when state = S_WAIT_LINK or state = S_B_RUN else '0';
  get_req      <= get_req_r;
  get_n        <= get_n_r;
  put_req      <= put_req_r;
  put_n        <= put_n_r;
  put_val      <= put_val_r;
  link_req     <= link_req_r;
  link_request <= request_r;
  link_data    <= x"00000000" & data_r;
  done         <= done_r;
  header       <= header_r;
  header_n     <= header_n_r;
  jtag_index   <= index_r;

  process (clk)
    procedure get(n : natural; back : state_t) is
    begin
      get_req_r <= '1';
      get_n_r   <= n;
      ret       <= back;
      state     <= S_WAIT_GET;
    end procedure;

    procedure put(n : natural; v : std_logic_vector(63 downto 0); back : state_t) is
    begin
      put_req_r <= '1';
      put_n_r   <= n;
      put_val_r <= v;
      ret       <= back;
      state     <= S_WAIT_PUT;
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
      put(1, x"00000000000000" & s, S_DONE);
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
      get_req_r  <= '0';
      put_req_r  <= '0';
      link_req_r <= '0';
      done_r     <= '0';

      if rst = '1' then
        state      <= S_IDLE;
        match_mask <= (others => '1');
        index_r    <= 0;
      else
        case state is
          when S_IDLE =>
            header_n_r <= 0;
            if start = '1' then
              state <= S_DISPATCH;
            end if;

          when S_DISPATCH =>
            case command is
              when ID_TRANSFER       => get(2, S_T_START);                    -- dap_index, count
              when ID_TRANSFER_BLOCK => get(4, S_B_START);                    -- dap_index, count(2), request
              when ID_WRITE_ABORT    => get(5, S_WRITE_ABORT);                -- dap_index, value(4)
              when others            => get(1, S_IDCODE);                     -- JTAG_IDCODE: index
            end case;

          when S_WAIT_GET =>
            if get_ack = '1' then
              state <= ret;
            end if;

          when S_WAIT_PUT =>
            if put_ack = '1' then
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
            done_r <= '1';
            state  <= S_IDLE;

          ------------------------------------------------------------------------------
          -- WriteAbort / JTAG_IDCODE
          ------------------------------------------------------------------------------
          when S_WRITE_ABORT =>
            swd(DP_ABORT_WRITE, arg(39 downto 8), S_WRITE_ABORT_DONE);

          when S_WRITE_ABORT_DONE =>
            if last_ok then
              reply_status(DAP_OK);
            else
              reply_status(DAP_ERROR);
            end if;

          when S_IDCODE =>
            if jtag = '0' or unsigned(arg(7 downto 0)) >= jtag_count then
              reply_status(DAP_ERROR);
            else
              select_device(arg(7 downto 0));
              swd(DP_IDCODE_READ, (others => '0'), S_IDCODE_DONE);   -- jtag_phy では IDCODE 命令
            end if;

          when S_IDCODE_DONE =>
            if last_ok then
              put(5, x"000000" & rdata & DAP_OK, S_DONE);
            else
              reply_status(DAP_ERROR);
            end if;

          ------------------------------------------------------------------------------
          -- DAP_Transfer（参照実装 DAP.c の DAP_SWD_Transfer と同じ流れ）
          ------------------------------------------------------------------------------
          when S_T_START =>
            select_device(arg(7 downto 0));
            t_left      <= resize(unsigned(arg(15 downto 8)), 16);
            t_done      <= (others => '0');
            set_resp((others => '0'));
            post_read   <= '0';
            check_write <= '0';
            put(2, (others => '0'), S_T_NEXT);                        -- [1] 処理数, [2] 応答は最後に書く

          when S_T_NEXT =>
            if t_left = 0 then
              state <= S_T_FINISH;
            else
              t_left <= t_left - 1;
              get(1, S_T_REQUEST);
            end if;

          when S_T_REQUEST =>
            t_request <= arg(7 downto 0);
            if arg(REQ_RNW) = '1' then
              if post_read = '1' then
                if arg(REQ_APNDP) = '1' and arg(REQ_MATCH_VALUE) = '0' then
                  swd(arg(3 downto 0), (others => '0'), S_T_AP_CHAIN);    -- 続けて AP を読み、前の値を受け取る
                else
                  swd(DP_RDBUFF_READ, (others => '0'), S_T_POSTED_COLLECT);
                end if;
              elsif arg(REQ_MATCH_VALUE) = '1' then
                get(4, S_T_MATCH_VALUE);
              else
                state <= S_T_READ;
              end if;
            elsif post_read = '1' then
              swd(DP_RDBUFF_READ, (others => '0'), S_T_WRITE_COLLECT);
            else
              get(4, S_T_WRITE_DATA);
            end if;

          when S_T_AP_CHAIN =>
            if not last_ok then
              state <= S_T_FINISH;
            else
              check_write <= '0';
              put32(rdata, S_T_COUNTED);                            -- この要求の値はまたポストされている
            end if;

          when S_T_POSTED_COLLECT =>
            if not last_ok then
              state <= S_T_FINISH;
            else
              post_read <= '0';
              put32(rdata, S_T_AFTER_COLLECT);
            end if;

          when S_T_AFTER_COLLECT =>
            if t_request(REQ_MATCH_VALUE) = '1' then
              get(4, S_T_MATCH_VALUE);
            else
              state <= S_T_READ;
            end if;

          when S_T_READ =>
            swd(t_request(3 downto 0), (others => '0'), S_T_READ_DONE);

          when S_T_READ_DONE =>
            if not last_ok then
              state <= S_T_FINISH;
            else
              check_write <= '0';
              if t_request(REQ_APNDP) = '1' then
                post_read <= '1';                                   -- 値は次の読み出しで返る
                state     <= S_T_COUNTED;
              else
                put32(rdata, S_T_COUNTED);
              end if;
            end if;

          when S_T_MATCH_VALUE =>
            match_value <= arg(31 downto 0);
            match_left  <= match_retry;
            match_zero  <= match_retry = 0;
            if t_request(REQ_APNDP) = '1' then
              swd(t_request(3 downto 0), (others => '0'), S_T_MATCH_POSTED);   -- AP はポストしてから
            else
              swd(t_request(3 downto 0), (others => '0'), S_T_MATCH_CHECK);
            end if;

          when S_T_MATCH_POSTED =>
            if not last_ok then
              state <= S_T_FINISH;
            else
              swd(t_request(3 downto 0), (others => '0'), S_T_MATCH_CHECK);
            end if;

          when S_T_MATCH_CHECK =>
            if not last_ok then
              state <= S_T_FINISH;
            elsif not matched then
              if not match_zero then
                match_left <= match_left - 1;
                match_zero <= match_left = 1;
                swd(t_request(3 downto 0), (others => '0'), S_T_MATCH_CHECK);
              else
                set_resp(RESP_OK or RESP_MISMATCH);
                state <= S_T_FINISH;
              end if;
            else
              check_write <= '0';
              state       <= S_T_COUNTED;
            end if;

          when S_T_WRITE_COLLECT =>
            if not last_ok then
              state <= S_T_FINISH;
            else
              post_read <= '0';
              put32(rdata, S_T_WRITE_GET);
            end if;

          when S_T_WRITE_GET =>
            get(4, S_T_WRITE_DATA);

          when S_T_WRITE_DATA =>
            if t_request(REQ_MATCH_MASK) = '1' then
              match_mask <= arg(31 downto 0);
              set_resp(RESP_OK);
              state      <= S_T_COUNTED;
            else
              swd(t_request(3 downto 0), arg(31 downto 0), S_T_WRITE_DONE);
            end if;

          when S_T_WRITE_DONE =>
            if not last_ok then
              state <= S_T_FINISH;
            else
              check_write <= '1';
              state       <= S_T_COUNTED;
            end if;

          when S_T_COUNTED =>
            t_done <= t_done + 1;
            state  <= S_T_NEXT;

          when S_T_FINISH =>
            -- 失敗で抜けた場合の残りの要求は dap_packet が読み捨てる
            if last_ok and post_read = '1' then
              post_read <= '0';
              swd(DP_RDBUFF_READ, (others => '0'), S_T_LAST_READ);     -- 最後のポスト値を回収
            elsif last_ok and check_write = '1' then
              check_write <= '0';
              swd(DP_RDBUFF_READ, (others => '0'), S_T_HEADER);        -- 最後の書き込みの確認
            else
              state <= S_T_HEADER;
            end if;

          when S_T_LAST_READ =>
            if last_ok then
              put32(rdata, S_T_HEADER);
            else
              state <= S_T_HEADER;
            end if;

          when S_T_HEADER =>
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
            select_device(arg(7 downto 0));
            t_left    <= unsigned(arg(23 downto 8));
            t_request <= arg(31 downto 24);
            t_done    <= (others => '0');
            set_resp((others => '0'));
            put(3, (others => '0'), S_B_FIRST);                       -- [1..2] 処理数, [3] 応答は最後に書く

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
                swd_issue(DP_RDBUFF_READ, (others => '0'));          -- 最後の RDBUFF（AP の読みの回収・書きの確認）
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
            swd_issue(t_request(3 downto 0), arg(31 downto 0));
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
