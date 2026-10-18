-- USB 受信パケット層。ulpi_link の受信バイトを解釈してイベントにする。
-- 根拠: docs/reference/usb2-hs-device-on-ulpi.md §4。
--
--   * PID は上位ニブル = 下位ニブルの反転を検査。不正なら（データでも）壊れたパケットとして扱う。
--   * トークン（OUT/IN/SETUP/PING）と SOF: ちょうど 3 バイトかつ CRC5 正常のときだけ、
--     パケット終了時に token_valid / sof_valid を出す。
--   * データ: PID 受信時に data_start（data_pid）。ペイロードは 2 バイト遅らせて data_valid/data_byte で流し、
--     末尾 2 バイト（CRC16）は流さない。パケット終了時に data_end と data_ok（PID・長さ・CRC16 正常、
--     RxError なし）、data_length（ペイロード長）を出す。受け手は data_ok で確定/破棄すること。
--   * ハンドシェイク: ちょうど 1 バイトのときだけ handshake_valid。
--   * packet_end: 何か受信したパケットの終了ごとに 1 周期（応答タイミングの基準）。
library ieee;
use ieee.std_logic_1164.all;
use ieee.numeric_std.all;
use work.usb_crc_pkg.all;
use work.usb_types_pkg.all;

entity usb_rx_packet is
  port (
    clk             : in  std_logic;
    rst             : in  std_logic;
    -- ulpi_link から
    rx_active       : in  std_logic;
    rx_valid        : in  std_logic;
    rx_data         : in  std_logic_vector(7 downto 0);
    rx_error        : in  std_logic;
    -- トークン
    token_valid     : out std_logic;
    token_pid       : out std_logic_vector(3 downto 0);
    token_addr      : out std_logic_vector(6 downto 0);
    token_endp      : out std_logic_vector(3 downto 0);
    sof_valid       : out std_logic;
    sof_frame       : out std_logic_vector(10 downto 0);
    -- データ
    data_start      : out std_logic;
    data_pid        : out std_logic_vector(3 downto 0);
    data_valid      : out std_logic;
    data_byte       : out std_logic_vector(7 downto 0);
    data_end        : out std_logic;
    data_ok         : out std_logic;
    data_length     : out unsigned(10 downto 0);
    -- ハンドシェイク
    handshake_valid : out std_logic;
    handshake_pid   : out std_logic_vector(3 downto 0);
    -- 任意のパケットの終了
    packet_end      : out std_logic
  );
end entity;

architecture rtl of usb_rx_packet is
  -- 受信バイト数の上限（HS の最大データパケット 1024 + PID + CRC を超えたら飽和）
  constant MAX_COUNT : natural := 1027;

  type kind_t is (K_TOKEN, K_DATA, K_HANDSHAKE, K_INVALID);

  signal active_d : std_logic := '0';
  signal count    : natural range 0 to MAX_COUNT := 0;
  signal pid      : std_logic_vector(3 downto 0) := (others => '0');
  signal kind     : kind_t := K_INVALID;
  signal bad      : std_logic := '0';
  signal byte1    : std_logic_vector(7 downto 0) := (others => '0');
  signal byte2    : std_logic_vector(7 downto 0) := (others => '0');
  signal crc      : std_logic_vector(15 downto 0) := CRC16_INIT;
  signal delay1   : std_logic_vector(7 downto 0) := (others => '0');
  signal delay2   : std_logic_vector(7 downto 0) := (others => '0');

  function classify(p : std_logic_vector(3 downto 0)) return kind_t is
  begin
    if p = PID_OUT or p = PID_IN or p = PID_SOF or p = PID_SETUP or p = PID_PING then
      return K_TOKEN;
    elsif p(1 downto 0) = "11" then
      return K_DATA;            -- DATA0/1/2, MDATA
    elsif p(1 downto 0) = "10" then
      return K_HANDSHAKE;       -- ACK/NAK/STALL/NYET
    end if;
    return K_INVALID;
  end function;
begin
  process (clk)
    variable field : std_logic_vector(15 downto 0);
  begin
    if rising_edge(clk) then
      token_valid     <= '0';
      sof_valid       <= '0';
      data_start      <= '0';
      data_valid      <= '0';
      data_end        <= '0';
      handshake_valid <= '0';
      packet_end      <= '0';
      active_d        <= rx_active;

      if rst = '1' then
        count    <= 0;
        active_d <= '0';
      elsif rx_active = '1' and active_d = '0' then
        -- 新しいパケット
        count <= 0;
        bad   <= rx_error;
        kind  <= K_INVALID;
        crc   <= CRC16_INIT;
      elsif rx_active = '1' then
        if rx_error = '1' then
          bad <= '1';
        end if;
        if rx_valid = '1' then
          if count /= MAX_COUNT then
            count <= count + 1;
          end if;
          if count = 0 then
            pid <= rx_data(3 downto 0);
            if rx_data(7 downto 4) /= not rx_data(3 downto 0) then
              kind <= K_INVALID;
            else
              kind <= classify(rx_data(3 downto 0));
              if classify(rx_data(3 downto 0)) = K_DATA then
                data_start <= '1';
                data_pid   <= rx_data(3 downto 0);
              end if;
            end if;
          else
            if count = 1 then
              byte1 <= rx_data;
            elsif count = 2 then
              byte2 <= rx_data;
            end if;
            if kind = K_DATA then
              crc    <= crc16_next(crc, rx_data);
              delay2 <= delay1;
              delay1 <= rx_data;
              if count >= 3 then
                data_valid <= '1';             -- 2 バイト前の値はペイロード（CRC ではない）
                data_byte  <= delay2;
              end if;
            end if;
          end if;
        end if;
      elsif active_d = '1' then
        -- パケット終了
        if count /= 0 then
          packet_end <= '1';
        end if;
        field := byte2 & byte1;
        case kind is
          when K_TOKEN =>
            if count = 3 and bad = '0' and crc5_token_ok(field) then
              if pid = PID_SOF then
                sof_valid <= '1';
                sof_frame <= field(10 downto 0);
              else
                token_valid <= '1';
                token_pid   <= pid;
                token_addr  <= field(6 downto 0);
                token_endp  <= field(10 downto 7);
              end if;
            end if;
          when K_DATA =>
            data_end <= '1';
            if count >= 3 and bad = '0' and crc = CRC16_RESIDUAL then
              data_ok <= '1';
            else
              data_ok <= '0';
            end if;
            if count >= 3 then
              data_length <= to_unsigned(count - 3, data_length'length);
            else
              data_length <= (others => '0');
            end if;
          when K_HANDSHAKE =>
            if count = 1 and bad = '0' then
              handshake_valid <= '1';
              handshake_pid   <= pid;
            end if;
          when K_INVALID =>
            null;
        end case;
      end if;
    end if;
  end process;
end architecture;
