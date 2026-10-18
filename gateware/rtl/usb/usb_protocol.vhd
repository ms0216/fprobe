-- USB トランザクション層（デバイス側）。
-- 根拠: docs/reference/usb2-hs-device-on-ulpi.md §5。
--
-- usb_rx_packet のイベントと usb_tx_packet の送信を使い、エンドポイント 0..15 の
-- SETUP / OUT / PING / IN に応答する。エンドポイントは配列 I/F（ビット n = エンドポイント n）。
--
--   SETUP: DATA0・8 バイト・CRC 正常なら必ず ACK し EP0 に確定（out_setup 付き）。
--          以後の EP0 データ段は DATA1 から（両方向のトグルを 1 にする）。それ以外は無応答。
--   OUT:   CRC 異常・MPS 超過 → 無応答（破棄）。halt → STALL。トグル不一致 → ACK して破棄
--          （ホストが前回の ACK を取りこぼした）。トークン時点で空きが無い → NAK して破棄。
--          受理 → 確定・トグル反転・ACK（HS で次の MPS 分の空きが無ければ NYET）。
--   PING:  halt → STALL、空きあり → ACK、無し → NAK。
--   IN:    halt → STALL、パケットあり → DATAx（トグル）を送り ACK を待つ。ACK でトグル反転・解放、
--          ACK が来ない（タイムアウト・別のトークン）なら同じパケットを再送できるよう巻き戻す。無し → NAK。
--   自アドレス以外・存在しないエンドポイントのトークンは無視する。
--   OUT/SETUP の後、データパケットが T_DATA_TIMEOUT 以内に始まらなければ諦める（始まれば終わりまで待つ）。
--   トグルは bus_reset と toggle_reset_out / toggle_reset_in（SET_CONFIGURATION・SET_INTERFACE・
--   CLEAR_FEATURE で制御エンドポイントが方向別に出す）で DATA0 に戻る。
library ieee;
use ieee.std_logic_1164.all;
use ieee.numeric_std.all;
use work.usb_types_pkg.all;

entity usb_protocol is
  generic (
    OUT_EPS        : ep_mask_t;           -- 存在する OUT エンドポイント（EP0 を含む）
    IN_EPS         : ep_mask_t;           -- 存在する IN エンドポイント（EP0 を含む）
    T_DATA_TIMEOUT : positive := 128;     -- OUT/SETUP トークン後にデータを待つ周期
    T_ACK_TIMEOUT  : positive := 128      -- IN データ送信後に ACK を待つ周期
  );
  port (
    clk             : in  std_logic;
    rst             : in  std_logic;
    high_speed      : in  std_logic;
    bus_reset       : in  std_logic;
    address         : in  std_logic_vector(6 downto 0);
    -- usb_rx_packet から
    token_valid     : in  std_logic;
    token_pid       : in  pid_t;
    token_addr      : in  std_logic_vector(6 downto 0);
    token_endp      : in  std_logic_vector(3 downto 0);
    data_start      : in  std_logic;
    data_valid      : in  std_logic;
    data_byte       : in  std_logic_vector(7 downto 0);
    data_end        : in  std_logic;
    data_ok         : in  std_logic;
    data_pid        : in  pid_t;
    data_length     : in  unsigned(10 downto 0);
    handshake_valid : in  std_logic;
    handshake_pid   : in  pid_t;
    -- usb_tx_packet へ
    hs_send         : out std_logic;
    hs_pid          : out pid_t;
    data_send       : out std_logic;
    tx_data_pid     : out pid_t;
    data_empty      : out std_logic;
    tx_done         : in  std_logic;
    tx_aborted      : in  std_logic;
    pl_valid        : out std_logic;
    pl_ready        : in  std_logic;
    pl_data         : out std_logic_vector(7 downto 0);
    pl_last         : out std_logic;
    -- OUT エンドポイント
    out_wr_valid    : out ep_mask_t;
    out_wr_data     : out std_logic_vector(7 downto 0);
    out_commit      : out ep_mask_t;
    out_discard     : out ep_mask_t;
    out_setup       : out std_logic;     -- out_commit と同時: 確定したのは SETUP
    out_space       : in  ep_mask_t;
    -- IN エンドポイント
    in_pkt_ready    : in  ep_mask_t;
    in_pkt_empty    : in  ep_mask_t;
    in_pl_valid     : in  ep_mask_t;
    in_pl_data      : in  ep_bytes_t;
    in_pl_last      : in  ep_mask_t;
    in_pl_ready     : out ep_mask_t;
    in_ack          : out ep_mask_t;
    in_retry        : out ep_mask_t;
    -- 制御
    out_halt        : in  ep_mask_t;
    in_halt         : in  ep_mask_t;
    toggle_reset_out : in  ep_mask_t;
    toggle_reset_in  : in  ep_mask_t;
    -- トランザクションの合間（待機中で、この周期にトークンが来ていない）。この周期にエンドポイントのバッファを
    -- 空にしても、送り途中・ACK 待ちのパケットを壊さない（B10: SWO の残りを捨てる）
    idle             : out std_logic
  );
end entity;

architecture rtl of usb_protocol is
  type state_t is (S_IDLE, S_RECEIVE, S_OUT_RESPOND, S_HANDSHAKE, S_IN_DATA, S_IN_WAIT_ACK);
  signal state      : state_t := S_IDLE;
  signal ep         : natural range 0 to NUM_ENDPOINTS - 1 := 0;
  signal is_setup   : std_logic := '0';
  signal receiving  : std_logic := '0';     -- 待っていたデータパケットの受信が始まった
  signal writable   : std_logic := '0';     -- トークン時点で受理できる（空きあり・halt でない）
  signal timer      : natural range 0 to T_DATA_TIMEOUT + T_ACK_TIMEOUT := 0;
  signal out_toggle : ep_mask_t := (others => '0');   -- 次に期待する OUT データが DATA1
  signal in_toggle  : ep_mask_t := (others => '0');   -- 次に送る IN データが DATA1
  signal send       : std_logic := '0';                -- 送信要求を 1 周期出す

  signal hs_pid_r      : pid_t := PID_ACK;
  signal data_pid_r    : pid_t := PID_DATA0;
  signal data_empty_r  : std_logic := '0';
  signal send_handshake : boolean := false;

  function bit_mask(n : natural) return ep_mask_t is
    variable m : ep_mask_t := (others => '0');
  begin
    m(n) := '1';
    return m;
  end function;

  function max_packet(endpoint : natural; hs : std_logic) return natural is
  begin
    if endpoint = 0 then
      return MPS_CONTROL;
    elsif hs = '1' then
      return MPS_BULK_HS;
    end if;
    return MPS_BULK_FS;
  end function;
begin
  ------------------------------------------------------------------------------
  -- 出力（組み合わせ）
  ------------------------------------------------------------------------------
  hs_send     <= send when send_handshake else '0';
  data_send   <= send when not send_handshake else '0';
  hs_pid      <= hs_pid_r;
  tx_data_pid <= data_pid_r;
  data_empty  <= data_empty_r;

  out_wr_data <= data_byte;
  process (all)
  begin
    out_wr_valid <= (others => '0');
    in_pl_ready  <= (others => '0');
    pl_valid     <= '0';
    pl_data      <= in_pl_data(ep);
    pl_last      <= in_pl_last(ep);
    if state = S_RECEIVE and writable = '1' then
      out_wr_valid(ep) <= data_valid;
    end if;
    if state = S_IN_DATA then
      pl_valid        <= in_pl_valid(ep);
      in_pl_ready(ep) <= pl_ready;
    end if;
  end process;

  ------------------------------------------------------------------------------
  -- 状態機械
  ------------------------------------------------------------------------------
  process (clk)
    procedure respond(pid : pid_t) is
    begin
      hs_pid_r       <= pid;
      send_handshake <= true;
      send           <= '1';
      state          <= S_HANDSHAKE;
    end procedure;

    -- 待機中、または ACK/データ待ちを打ち切った直後のトークン処理
    procedure take_token is
      variable e : natural range 0 to NUM_ENDPOINTS - 1;
    begin
      e := to_integer(unsigned(token_endp));
      timer <= 0;
      if token_addr /= address then
        state <= S_IDLE;
      elsif token_pid = PID_SETUP and OUT_EPS(e) = '1' then
        ep       <= e;
        is_setup <= '1';
        writable  <= '1';                      -- SETUP は常に受ける
        receiving <= '0';
        state     <= S_RECEIVE;
      elsif token_pid = PID_OUT and OUT_EPS(e) = '1' then
        ep       <= e;
        is_setup <= '0';
        writable  <= out_space(e) and not out_halt(e);
        receiving <= '0';
        state     <= S_RECEIVE;
      elsif token_pid = PID_PING and OUT_EPS(e) = '1' then
        ep <= e;
        if out_halt(e) = '1' then
          respond(PID_STALL);
        elsif out_space(e) = '1' then
          respond(PID_ACK);
        else
          respond(PID_NAK);
        end if;
      elsif token_pid = PID_IN and IN_EPS(e) = '1' then
        ep <= e;
        if in_halt(e) = '1' then
          respond(PID_STALL);
        elsif in_pkt_ready(e) = '1' then
          data_pid_r     <= PID_DATA1 when in_toggle(e) = '1' else PID_DATA0;
          data_empty_r   <= in_pkt_empty(e);
          send_handshake <= false;
          send           <= '1';
          state          <= S_IN_DATA;
        else
          respond(PID_NAK);
        end if;
      else
        state <= S_IDLE;
      end if;
    end procedure;
  begin
    if rising_edge(clk) then
      send        <= '0';
      out_commit  <= (others => '0');
      out_discard <= (others => '0');
      out_setup   <= '0';
      in_ack      <= (others => '0');
      in_retry    <= (others => '0');
      if timer /= T_DATA_TIMEOUT + T_ACK_TIMEOUT then
        timer <= timer + 1;
      end if;

      if rst = '1' or bus_reset = '1' then
        state      <= S_IDLE;
        out_toggle <= (others => '0');
        in_toggle  <= (others => '0');
        if state = S_IN_DATA or state = S_IN_WAIT_ACK then
          in_retry(ep) <= '1';
        end if;
      else
        out_toggle <= out_toggle and not toggle_reset_out;
        in_toggle  <= in_toggle and not toggle_reset_in;

        case state is
          when S_IDLE =>
            if token_valid = '1' then
              take_token;
            end if;

          when S_RECEIVE =>
            if data_end = '1' then
              if data_ok = '0' or to_integer(data_length) > max_packet(ep, high_speed) then
                out_discard(ep) <= '1';          -- 壊れた・長すぎるパケットには応答しない
                state           <= S_IDLE;
              elsif is_setup = '1' then
                if data_pid = PID_DATA0 and data_length = 8 then
                  out_commit(ep) <= '1';
                  out_setup      <= '1';
                  out_toggle(ep) <= '1';        -- 制御転送のデータ段・ステータス段は DATA1 から
                  in_toggle(ep)  <= '1';
                  respond(PID_ACK);
                else
                  out_discard(ep) <= '1';
                  state           <= S_IDLE;
                end if;
              elsif out_halt(ep) = '1' then
                out_discard(ep) <= '1';
                respond(PID_STALL);
              elsif (data_pid = PID_DATA1) /= (out_toggle(ep) = '1') then
                out_discard(ep) <= '1';          -- 再送されたパケット。受理済みなので ACK
                respond(PID_ACK);
              elsif writable = '0' then
                out_discard(ep) <= '1';
                respond(PID_NAK);
              else
                out_commit(ep) <= '1';
                out_toggle(ep) <= not out_toggle(ep);
                state          <= S_OUT_RESPOND;  -- 確定後の空きを見て ACK / NYET を決める
              end if;
            elsif token_valid = '1' then
              out_discard(ep) <= '1';            -- データが来ないまま次のトークン
              take_token;
            elsif data_start = '1' then
              receiving <= '1';                  -- 以後はパケットの長さに関わらず終わりまで待つ
            elsif receiving = '0' and timer = T_DATA_TIMEOUT then
              out_discard(ep) <= '1';            -- データパケットが始まらなかった
              state           <= S_IDLE;
            end if;

          when S_OUT_RESPOND =>
            if high_speed = '1' and out_space(ep) = '0' then
              respond(PID_NYET);
            else
              respond(PID_ACK);
            end if;

          when S_HANDSHAKE =>
            if tx_done = '1' or tx_aborted = '1' then
              state <= S_IDLE;
            end if;

          when S_IN_DATA =>
            if tx_aborted = '1' then
              in_retry(ep) <= '1';
              state        <= S_IDLE;
            elsif tx_done = '1' then
              timer <= 0;
              state <= S_IN_WAIT_ACK;
            end if;

          when S_IN_WAIT_ACK =>
            if handshake_valid = '1' and handshake_pid = PID_ACK then
              in_ack(ep)    <= '1';
              in_toggle(ep) <= not in_toggle(ep);
              state         <= S_IDLE;
            elsif token_valid = '1' then
              in_retry(ep) <= '1';               -- ACK を落とされた。同じパケットを再送する
              take_token;
            elsif handshake_valid = '1' or timer = T_ACK_TIMEOUT then
              in_retry(ep) <= '1';
              state        <= S_IDLE;
            end if;
        end case;
      end if;
    end if;
  end process;

  idle <= '1' when state = S_IDLE and token_valid = '0' else '0';
end architecture;
