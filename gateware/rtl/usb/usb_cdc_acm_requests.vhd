-- CDC-ACM のクラス要求ハンドラ（usb_control_ep の要求ハンドラ I/F に接続）。
-- 根拠: docs/reference/usb2-hs-device-on-ulpi.md §8。
--
--   SET_LINE_CODING (0x21/0x20, 7 バイト)  → line_* に保持（データ段の完了で反映）
--   GET_LINE_CODING (0xA1/0x21, 7 バイト)  → 保持している値を返す（既定 115200 8N1）
--   SET_CONTROL_LINE_STATE (0x21/0x22)    → wValue bit0 = DTR, bit1 = RTS
--   SEND_BREAK (0x21/0x23)                → wValue = ブレークの長さ（ms）。0 で止める、0xFFFF は 0 が来るまで
--                                           （Windows の usbser・Linux の cdc-acm は 0xFFFF と 0 を使う）。brk = 送っている間
-- 宛先インターフェース（wIndex）が COMM_INTERFACE 以外、またはその他の要求は STALL。
-- 応答は要求の次の周期に返す。
library ieee;
use ieee.std_logic_1164.all;
use ieee.numeric_std.all;

entity usb_cdc_acm_requests is
  generic (
    COMM_INTERFACE : natural;                -- 通信クラスインターフェースの番号
    CLK_HZ         : positive := 60_000_000  -- clk の周波数（ブレークの長さの ms を数える）
  );
  port (
    clk            : in  std_logic;
    rst            : in  std_logic;
    -- usb_control_ep の要求ハンドラ I/F
    req_valid      : in  std_logic;
    req_type       : in  std_logic_vector(7 downto 0);
    req_request    : in  std_logic_vector(7 downto 0);
    req_value      : in  std_logic_vector(15 downto 0);
    req_index      : in  std_logic_vector(15 downto 0);
    req_length     : in  std_logic_vector(15 downto 0);
    req_ack        : out std_logic;
    req_stall      : out std_logic;
    req_in_data    : out std_logic_vector(63 downto 0);
    req_in_length  : out unsigned(3 downto 0);
    req_out_valid  : in  std_logic;
    req_out_data   : in  std_logic_vector(7 downto 0);
    req_out_done   : in  std_logic;
    -- 回線設定
    line_rate      : out unsigned(31 downto 0);
    line_stop_bits : out unsigned(7 downto 0);   -- 0 = 1, 1 = 1.5, 2 = 2
    line_parity    : out unsigned(7 downto 0);   -- 0 なし, 1 奇, 2 偶, 3 マーク, 4 スペース
    line_data_bits : out unsigned(7 downto 0);
    line_changed   : out std_logic;              -- SET_LINE_CODING の反映時に 1 周期
    dtr            : out std_logic;
    rts            : out std_logic;
    brk            : out std_logic               -- ブレークを送る（SEND_BREAK）
  );
end entity;

architecture rtl of usb_cdc_acm_requests is
  constant SET_LINE_CODING        : std_logic_vector(7 downto 0) := x"20";
  constant GET_LINE_CODING        : std_logic_vector(7 downto 0) := x"21";
  constant SET_CONTROL_LINE_STATE : std_logic_vector(7 downto 0) := x"22";
  constant SEND_BREAK             : std_logic_vector(7 downto 0) := x"23";
  constant CLASS_OUT              : std_logic_vector(7 downto 0) := x"21";   -- クラス, IF 宛, OUT
  constant CLASS_IN               : std_logic_vector(7 downto 0) := x"A1";   -- クラス, IF 宛, IN

  -- line coding（リトルエンディアン 7 バイト）: 115200, 1 stop, パリティなし, 8 bit
  constant DEFAULT_CODING : std_logic_vector(55 downto 0) := x"08_00_00_0001C200";

  signal coding     : std_logic_vector(55 downto 0) := DEFAULT_CODING;
  signal incoming   : std_logic_vector(55 downto 0) := (others => '0');
  signal in_count   : natural range 0 to 7 := 0;
  signal expecting  : std_logic := '0';        -- SET_LINE_CODING のデータ段を受信中
  signal dtr_r      : std_logic := '0';
  signal rts_r      : std_logic := '0';
  -- ブレーク: 残りの ms（0xFFFF は止めるまで）と、1 ms の刻み
  constant MS_CYCLES : positive := CLK_HZ / 1000;
  signal brk_ms     : unsigned(15 downto 0) := (others => '0');
  signal ms_timer   : natural range 0 to MS_CYCLES - 1 := 0;
begin
  process (clk)
    variable ours : boolean;
  begin
    if rising_edge(clk) then
      req_ack      <= '0';
      req_stall    <= '0';
      line_changed <= '0';
      if rst = '1' then
        coding    <= DEFAULT_CODING;
        dtr_r     <= '0';
        rts_r     <= '0';
        expecting <= '0';
        brk_ms    <= (others => '0');
        ms_timer  <= 0;
      else
        -- ブレークの長さを数える（0xFFFF は数えない）
        if ms_timer = MS_CYCLES - 1 then
          ms_timer <= 0;
          if brk_ms /= 0 and brk_ms /= x"FFFF" then
            brk_ms <= brk_ms - 1;
          end if;
        else
          ms_timer <= ms_timer + 1;
        end if;

        if req_valid = '1' then
          ours := to_integer(unsigned(req_index)) = COMM_INTERFACE;
          if ours and req_type = CLASS_OUT and req_request = SET_LINE_CODING
             and to_integer(unsigned(req_length)) = 7 then
            in_count  <= 0;
            expecting <= '1';
            req_ack   <= '1';
          elsif ours and req_type = CLASS_IN and req_request = GET_LINE_CODING then
            req_in_data   <= x"00" & coding;
            req_in_length <= to_unsigned(7, 4);
            req_ack       <= '1';
          elsif ours and req_type = CLASS_OUT and req_request = SET_CONTROL_LINE_STATE then
            dtr_r   <= req_value(0);
            rts_r   <= req_value(1);
            req_ack <= '1';
          elsif ours and req_type = CLASS_OUT and req_request = SEND_BREAK then
            brk_ms   <= unsigned(req_value);
            ms_timer <= 0;
            req_ack  <= '1';
          else
            req_stall <= '1';
          end if;
        end if;

        if expecting = '1' and req_out_valid = '1' and in_count /= 7 then
          incoming(8 * in_count + 7 downto 8 * in_count) <= req_out_data;
          in_count <= in_count + 1;
        end if;
        if expecting = '1' and req_out_done = '1' then
          expecting <= '0';
          if in_count = 7 then
            coding       <= incoming;
            line_changed <= '1';
          end if;
        end if;
      end if;
    end if;
  end process;

  line_rate      <= unsigned(coding(31 downto 0));
  line_stop_bits <= unsigned(coding(39 downto 32));
  line_parity    <= unsigned(coding(47 downto 40));
  line_data_bits <= unsigned(coding(55 downto 48));
  dtr            <= dtr_r;
  rts            <= rts_r;
  brk            <= '1' when brk_ms /= 0 else '0';
end architecture;
