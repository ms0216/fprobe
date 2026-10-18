-- EP0 制御エンドポイント。
-- 根拠: docs/reference/usb2-hs-device-on-ulpi.md §5-7。
--
-- usb_protocol の EP0 として接続する（OUT は生の書き込み I/F、IN はパケット I/F を自前で提供）。
--
-- 自前で処理する要求:
--   標準: GET_DESCRIPTOR（ROM: usb_descriptors_pkg）, SET_ADDRESS, SET/GET_CONFIGURATION,
--         GET_STATUS（デバイス・IF・EP）, SET/CLEAR_FEATURE(ENDPOINT_HALT), GET/SET_INTERFACE（alt 0 のみ）
--   ベンダー: MS OS 2.0 記述子セット（bRequest = MSOS20_VENDOR_CODE, wIndex = 7）
-- それ以外は要求ハンドラ（req_*）へ委譲する。ハンドラは T_HANDLER_TIMEOUT 以内に req_ack か req_stall を返す。
--   IN 要求: req_ack と同時に req_in_data（リトルエンディアン 8 バイトまで）と req_in_length を渡す。
--   OUT 要求: データ段の各パケット確定後にバイトを req_out_valid/req_out_data で渡し、終わりに req_out_done。
--
-- 制御転送の段:
--   データ段 IN: min(ディスクリプタ長, wLength) を 64 バイトずつ。ACK で位置を進め、ACK が無ければ同じ塊を再送。
--               wLength 未満で 64 の倍数ちょうどに終わる場合は ZLP を付ける。
--   ステータス段: IN データ段の後はホストの OUT ZLP、それ以外はこちらの IN ZLP（ACK 時に SET_ADDRESS 等を反映）。
--   未対応・不正な要求は STALL（ep0_stall）。次の SETUP で解除。SETUP はどの段でも制御転送をやり直す。
--
-- エンドポイントの初期化: SET_CONFIGURATION（全エンドポイント）と SET_INTERFACE（その IF のエンドポイント）は、
-- 同じ値の再設定でも HALT を解除し、トグルを DATA0 に戻し、バッファを空にする（USB 2.0 §9.1.1.5, §9.4.5）。
-- CLEAR_FEATURE(ENDPOINT_HALT) はその方向の HALT とトグルだけを戻す（バッファはそのまま）。
library ieee;
use ieee.std_logic_1164.all;
use ieee.numeric_std.all;
use work.usb_types_pkg.all;
use work.usb_descriptors_pkg.all;

entity usb_control_ep is
  generic (
    T_HANDLER_TIMEOUT : positive := 32
  );
  port (
    clk           : in  std_logic;
    rst           : in  std_logic;
    high_speed    : in  std_logic;
    bus_reset     : in  std_logic;
    -- usb_protocol の EP0 OUT
    wr_valid      : in  std_logic;
    wr_data       : in  std_logic_vector(7 downto 0);
    commit        : in  std_logic;
    discard       : in  std_logic;
    setup         : in  std_logic;
    space_for_packet : out std_logic;   -- OUT データパケットを受け取れる（SETUP は常に受け取られる）
    -- usb_protocol の EP0 IN
    pkt_ready     : out std_logic;
    pkt_empty     : out std_logic;
    pl_valid      : out std_logic;
    pl_ready      : in  std_logic;
    pl_data       : out std_logic_vector(7 downto 0);
    pl_last       : out std_logic;
    ack           : in  std_logic;
    retry         : in  std_logic;
    -- デバイス状態
    address       : out std_logic_vector(6 downto 0);
    config_value  : out std_logic_vector(7 downto 0);
    halt_out      : out ep_mask_t;     -- ビット 0 = EP0 の STALL
    halt_in       : out ep_mask_t;
    toggle_reset_out : out ep_mask_t;   -- 該当エンドポイントの OUT トグルを DATA0 に戻す（1 周期）
    toggle_reset_in  : out ep_mask_t;   -- 同 IN
    ep_flush      : out ep_mask_t;     -- 該当エンドポイントのバッファを空にする（1 周期）
    -- 要求ハンドラ（クラス・ベンダー要求）
    req_valid     : out std_logic;
    req_type      : out std_logic_vector(7 downto 0);
    req_request   : out std_logic_vector(7 downto 0);
    req_value     : out std_logic_vector(15 downto 0);
    req_index     : out std_logic_vector(15 downto 0);
    req_length    : out std_logic_vector(15 downto 0);
    req_ack       : in  std_logic;
    req_stall     : in  std_logic;
    req_in_data   : in  std_logic_vector(63 downto 0);
    req_in_length : in  unsigned(3 downto 0);
    req_out_valid : out std_logic;
    req_out_data  : out std_logic_vector(7 downto 0);
    req_out_done  : out std_logic
  );
end entity;

architecture rtl of usb_control_ep is
  constant MPS : natural := MPS_CONTROL;

  -- 標準要求
  constant GET_STATUS        : natural := 0;
  constant CLEAR_FEATURE     : natural := 1;
  constant SET_FEATURE       : natural := 3;
  constant SET_ADDRESS       : natural := 5;
  constant GET_DESCRIPTOR    : natural := 6;
  constant GET_CONFIGURATION : natural := 8;
  constant SET_CONFIGURATION : natural := 9;
  constant GET_INTERFACE     : natural := 10;
  constant SET_INTERFACE     : natural := 11;
  constant MSOS20_INDEX      : natural := 7;

  constant ALL_ENDPOINTS     : ep_mask_t := (others => '1');

  type bytes_t is array (natural range <>) of std_logic_vector(7 downto 0);
  type state_t is (S_IDLE, S_DECODE, S_LOOKUP, S_HANDLER, S_DATA_IN, S_STATUS_OUT,
                   S_DATA_OUT, S_FORWARD, S_STATUS_IN, S_STALL);
  type action_t is (A_NONE, A_SET_ADDRESS, A_SET_CONFIGURATION, A_CLEAR_HALT, A_SET_HALT,
                    A_SET_INTERFACE);

  signal state       : state_t := S_IDLE;
  signal rx_buf      : bytes_t(0 to MPS - 1) := (others => (others => '0'));
  signal rx_count    : natural range 0 to MPS := 0;
  signal req         : bytes_t(0 to 7) := (others => (others => '0'));

  -- データ段 IN の送り元（ROM か小バッファ）
  signal from_rom    : std_logic := '0';
  signal rom_base    : natural range 0 to DESC_ROM_SIZE - 1 := 0;
  signal small_buf   : bytes_t(0 to 7) := (others => (others => '0'));
  signal total       : natural range 0 to 65535 := 0;   -- 送る総バイト数
  signal position    : natural range 0 to 65535 := 0;   -- ACK 済みのバイト数
  signal need_zlp    : std_logic := '0';
  signal fetched     : natural range 0 to MPS := 0;
  signal consumed    : natural range 0 to MPS := 0;
  signal out_full    : std_logic := '0';
  signal rom_q       : std_logic_vector(7 downto 0) := (others => '0');
  signal small_q     : std_logic_vector(7 downto 0) := (others => '0');
  signal chunk       : natural range 0 to MPS;

  signal lookup_i    : natural range 0 to DESC_ENTRIES'length := 0;
  signal timer       : natural range 0 to T_HANDLER_TIMEOUT := 0;
  signal received    : natural range 0 to 65535 := 0;   -- OUT データ段で受け取った総数
  signal fwd_index   : natural range 0 to MPS := 0;
  signal fwd_count   : natural range 0 to MPS := 0;

  signal action      : action_t := A_NONE;
  signal addr_r      : std_logic_vector(6 downto 0) := (others => '0');
  signal config_r    : std_logic_vector(7 downto 0) := (others => '0');
  signal halts_out   : ep_mask_t := (others => '0');
  signal halts_in    : ep_mask_t := (others => '0');

  signal w_value     : natural range 0 to 65535;
  signal w_index     : natural range 0 to 65535;
  signal w_length    : natural range 0 to 65535;
  signal fetch       : boolean;
  signal pop         : boolean;

  function u16(lo, hi : std_logic_vector(7 downto 0)) return natural is
    variable word : std_logic_vector(15 downto 0);
  begin
    word := hi & lo;
    return to_integer(unsigned(word));
  end function;

  function minimum(a, b : natural) return natural is
  begin
    if a < b then
      return a;
    end if;
    return b;
  end function;
begin
  w_value  <= u16(req(2), req(3));
  w_index  <= u16(req(4), req(5));
  w_length <= u16(req(6), req(7));
  chunk    <= minimum(total - position, MPS);

  ------------------------------------------------------------------------------
  -- IN パケットのバイト供給（ROM は同期読み出し）
  ------------------------------------------------------------------------------
  pop   <= out_full = '1' and pl_ready = '1';
  fetch <= (state = S_DATA_IN) and fetched /= chunk and (out_full = '0' or pop)
           and ack = '0' and retry = '0';

  process (clk)
  begin
    if rising_edge(clk) then
      if fetch then
        rom_q   <= DESC_ROM(rom_base + position + fetched);
        small_q <= small_buf((position + fetched) mod 8);
      end if;
    end if;
  end process;

  -- OUT パケットを処理する段でだけ受け取る。要求の解釈中・受信バッファをハンドラへ渡している間などは
  -- NAK（HS で確定直後なら NYET）でホストに待ってもらう。受け取ると確定が無視され、バッファも上書きされる
  space_for_packet <= '1' when state = S_IDLE or state = S_DATA_IN or state = S_STATUS_OUT
                               or state = S_DATA_OUT else '0';

  pkt_ready <= '1' when state = S_DATA_IN or state = S_STATUS_IN else '0';
  pkt_empty <= '1' when state = S_STATUS_IN or chunk = 0 else '0';
  pl_valid  <= out_full;
  pl_data   <= rom_q when from_rom = '1' else small_q;
  pl_last   <= '1' when consumed + 1 = chunk else '0';

  ------------------------------------------------------------------------------
  -- 制御転送の状態機械
  ------------------------------------------------------------------------------
  process (clk)
    procedure stall is
    begin
      state <= S_STALL;
    end procedure;

    -- 小バッファから IN データ段を始める
    procedure reply(n : natural) is
    begin
      from_rom <= '0';
      total    <= minimum(n, w_length);
      need_zlp <= '0';                 -- 8 バイト以下なので ZLP は不要
      position <= 0;
      state    <= S_DATA_IN;
    end procedure;

    procedure status_in(a : action_t) is
    begin
      action <= a;
      state  <= S_STATUS_IN;
    end procedure;

    -- エンドポイントを初期状態に戻す（HALT 解除・トグル DATA0・バッファを空に）
    procedure reinitialize(mask : ep_mask_t) is
    begin
      halts_out        <= halts_out and not mask;
      halts_in         <= halts_in and not mask;
      toggle_reset_out <= mask;
      toggle_reset_in  <= mask;
      ep_flush         <= mask;
    end procedure;

    variable ep        : natural range 0 to NUM_ENDPOINTS - 1;
    variable entry     : desc_entry_t;
    variable speed_ok  : boolean;
    variable next_pos  : natural range 0 to 65535 + MPS;
  begin
    if rising_edge(clk) then
      req_valid     <= '0';
      req_out_valid <= '0';
      req_out_done  <= '0';
      toggle_reset_out <= (others => '0');
      toggle_reset_in  <= (others => '0');
      ep_flush         <= (others => '0');

      -- EP0 OUT の受信（確定した時点で中身を解釈する）
      if wr_valid = '1' and rx_count /= MPS then
        rx_buf(rx_count) <= wr_data;
        rx_count         <= rx_count + 1;
      end if;
      if commit = '1' or discard = '1' then
        rx_count <= 0;
      end if;

      if rst = '1' or bus_reset = '1' then
        state     <= S_IDLE;
        addr_r    <= (others => '0');
        config_r  <= (others => '0');
        halts_out <= (others => '0');
        halts_in  <= (others => '0');
        out_full  <= '0';
        rx_count  <= 0;
      elsif commit = '1' and setup = '1' then
        -- SETUP はどの段でも新しい制御転送を始める（STALL も解除）
        for i in 0 to 7 loop
          req(i) <= rx_buf(i);
        end loop;
        out_full <= '0';
        fetched  <= 0;
        consumed <= 0;
        state    <= S_DECODE;
      else
        case state is
          when S_IDLE | S_STALL =>
            null;

          when S_DECODE =>
            ep     := to_integer(unsigned(req(4)(3 downto 0)));
            action <= A_NONE;
            case to_integer(unsigned(req(0))) * 256 + to_integer(unsigned(req(1))) is
              when 16#80_00# + GET_DESCRIPTOR =>
                lookup_i <= 0;
                state    <= S_LOOKUP;
              when 16#00_00# + SET_ADDRESS =>
                if w_value < 128 and w_length = 0 then
                  status_in(A_SET_ADDRESS);
                else
                  stall;
                end if;
              when 16#00_00# + SET_CONFIGURATION =>
                if w_value <= 1 and w_length = 0 then
                  status_in(A_SET_CONFIGURATION);
                else
                  stall;
                end if;
              when 16#80_00# + GET_CONFIGURATION =>
                small_buf(0) <= config_r;
                reply(1);
              when 16#80_00# + GET_STATUS | 16#81_00# + GET_STATUS =>
                small_buf(0) <= x"00";        -- バス給電・リモートウェイクアップ無し
                small_buf(1) <= x"00";
                reply(2);
              when 16#82_00# + GET_STATUS =>
                if req(4)(7) = '1' then
                  small_buf(0) <= "0000000" & halts_in(ep);
                else
                  small_buf(0) <= "0000000" & halts_out(ep);
                end if;
                small_buf(1) <= x"00";
                reply(2);
              when 16#02_00# + CLEAR_FEATURE | 16#02_00# + SET_FEATURE =>
                if w_value = 0 and w_length = 0 then        -- ENDPOINT_HALT
                  if req(1) = std_logic_vector(to_unsigned(CLEAR_FEATURE, 8)) then
                    status_in(A_CLEAR_HALT);
                  else
                    status_in(A_SET_HALT);
                  end if;
                else
                  stall;
                end if;
              when 16#81_00# + GET_INTERFACE =>
                if w_index < NUM_INTERFACES and config_r /= x"00" then
                  small_buf(0) <= x"00";
                  reply(1);
                else
                  stall;
                end if;
              when 16#01_00# + SET_INTERFACE =>
                if w_index < NUM_INTERFACES and w_value = 0 and config_r /= x"00" then
                  status_in(A_SET_INTERFACE);
                else
                  stall;
                end if;
              when 16#C0_00# + MSOS20_VENDOR_CODE =>
                if w_index = MSOS20_INDEX then
                  lookup_i <= 0;
                  state    <= S_LOOKUP;
                else
                  stall;
                end if;
              when others =>
                if req(0)(6 downto 5) = "00" then
                  stall;                        -- 未対応の標準要求
                else
                  req_valid <= '1';             -- クラス・ベンダー要求はハンドラへ
                  timer     <= 0;
                  state     <= S_HANDLER;
                end if;
            end case;

          when S_LOOKUP =>
            -- ディスクリプタ表を 1 周期 1 項目ずつ調べる（IN トークンには見つかるまで NAK）
            if lookup_i = DESC_ENTRIES'length then
              stall;
            else
              entry    := DESC_ENTRIES(lookup_i);
              speed_ok := entry.speed = 0 or (entry.speed = 1 and high_speed = '1')
                          or (entry.speed = 2 and high_speed = '0');
              if (req(0) = x"C0" and entry.dtype = DTYPE_MSOS20)
                 or (req(0) = x"80" and entry.dtype = to_integer(unsigned(req(3)))
                     and entry.index = to_integer(unsigned(req(2))) and speed_ok) then
                from_rom <= '1';
                rom_base <= entry.offset;
                total    <= minimum(entry.length, w_length);
                -- 要求より短く、かつ MPS の倍数ちょうどで終わるなら ZLP で終端を知らせる
                if entry.length < w_length and entry.length mod MPS = 0 then
                  need_zlp <= '1';
                else
                  need_zlp <= '0';
                end if;
                position <= 0;
                state    <= S_DATA_IN;
              else
                lookup_i <= lookup_i + 1;
              end if;
            end if;

          when S_HANDLER =>
            if req_ack = '1' then
              if w_length = 0 then
                status_in(A_NONE);
              elsif req(0)(7) = '1' then
                for i in 0 to 7 loop
                  small_buf(i) <= req_in_data(8 * i + 7 downto 8 * i);
                end loop;
                reply(to_integer(req_in_length));
              else
                received <= 0;
                state    <= S_DATA_OUT;
              end if;
            elsif req_stall = '1' or timer = T_HANDLER_TIMEOUT then
              stall;
            else
              timer <= timer + 1;
            end if;

          when S_DATA_IN =>
            if ack = '1' then
              next_pos := position + chunk;
              out_full <= '0';
              fetched  <= 0;
              consumed <= 0;
              if chunk < MPS or (next_pos = total and need_zlp = '0') then
                state <= S_STATUS_OUT;          -- 短パケット（ZLP 含む）か要求長に達した
              else
                position <= next_pos;
              end if;
            elsif retry = '1' then
              out_full <= '0';
              fetched  <= 0;
              consumed <= 0;
            elsif commit = '1' then
              state <= S_IDLE;                  -- ホストが早めにステータス段へ進んだ
            else
              if fetch then
                fetched  <= fetched + 1;
                out_full <= '1';
              elsif pop then
                out_full <= '0';
              end if;
              if pop then
                consumed <= consumed + 1;
              end if;
            end if;

          when S_STATUS_OUT =>
            if commit = '1' then
              state <= S_IDLE;
            end if;

          when S_DATA_OUT =>
            if commit = '1' then
              fwd_index <= 0;
              fwd_count <= rx_count;
              state     <= S_FORWARD;
            end if;

          when S_FORWARD =>
            -- 確定したパケットのバイトをハンドラへ渡す（壊れたパケットは渡さない）
            if fwd_index /= fwd_count then
              req_out_valid <= '1';
              req_out_data  <= rx_buf(fwd_index);
              fwd_index     <= fwd_index + 1;
            elsif received + fwd_count >= w_length or fwd_count < MPS then
              req_out_done <= '1';
              status_in(A_NONE);
            else
              received <= received + fwd_count;
              state    <= S_DATA_OUT;
            end if;

          when S_STATUS_IN =>
            if ack = '1' then
              case action is
                when A_SET_ADDRESS =>
                  addr_r <= req(2)(6 downto 0);
                when A_SET_CONFIGURATION =>
                  config_r <= req(2);
                  reinitialize(ALL_ENDPOINTS);
                when A_CLEAR_HALT =>
                  ep := to_integer(unsigned(req(4)(3 downto 0)));
                  -- HALT の解除はその方向のトグルだけを戻す
                  if req(4)(7) = '1' then
                    halts_in(ep)        <= '0';
                    toggle_reset_in(ep) <= '1';
                  else
                    halts_out(ep)        <= '0';
                    toggle_reset_out(ep) <= '1';
                  end if;
                when A_SET_HALT =>
                  ep := to_integer(unsigned(req(4)(3 downto 0)));
                  if ep /= 0 then
                    if req(4)(7) = '1' then
                      halts_in(ep) <= '1';
                    else
                      halts_out(ep) <= '1';
                    end if;
                  end if;
                when A_SET_INTERFACE =>
                  reinitialize(IF_ENDPOINTS(w_index));
                when A_NONE =>
                  null;
              end case;
              state <= S_IDLE;
            end if;
        end case;
      end if;
    end if;
  end process;

  address       <= addr_r;
  config_value  <= config_r;
  halt_out      <= halts_out(NUM_ENDPOINTS - 1 downto 1) & '1' when state = S_STALL
                   else halts_out(NUM_ENDPOINTS - 1 downto 1) & '0';
  halt_in       <= halts_in(NUM_ENDPOINTS - 1 downto 1) & '1' when state = S_STALL
                   else halts_in(NUM_ENDPOINTS - 1 downto 1) & '0';

  req_type    <= req(0);
  req_request <= req(1);
  req_value   <= req(3) & req(2);
  req_index   <= req(5) & req(4);
  req_length  <= req(7) & req(6);
end architecture;
