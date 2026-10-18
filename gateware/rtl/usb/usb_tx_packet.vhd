-- USB 送信パケット層。ハンドシェイクとデータパケットを ulpi_link の送信ストリームに変換する。
-- 根拠: docs/reference/usb2-hs-device-on-ulpi.md §4。
--
--   * hs_send（1 周期）: hs_pid の 1 バイトパケットを送る。
--   * data_send（1 周期）: data_pid の PID バイト → ペイロード（pl_* ストリーム、pl_last で終端。
--     data_empty なら無し = ZLP）→ CRC16（下位バイトから）を送る。ペイロードは途切れなく供給すること
--     （途切れると link がパケットを中断する）。PID は CRC に含めない。
--   * 完了で done、link から中断（tx_aborted）が来たら aborted を 1 周期出して待機に戻る。
--     中断時のペイロードの巻き戻しは供給側（エンドポイントバッファ）の責任。
library ieee;
use ieee.std_logic_1164.all;
use work.usb_crc_pkg.all;

entity usb_tx_packet is
  port (
    clk        : in  std_logic;
    rst        : in  std_logic;
    -- 要求
    hs_send    : in  std_logic;
    hs_pid     : in  std_logic_vector(3 downto 0);
    data_send  : in  std_logic;
    data_pid   : in  std_logic_vector(3 downto 0);
    data_empty : in  std_logic;
    busy       : out std_logic;
    done       : out std_logic;
    aborted    : out std_logic;
    -- ペイロード供給
    pl_valid   : in  std_logic;
    pl_ready   : out std_logic;
    pl_data    : in  std_logic_vector(7 downto 0);
    pl_last    : in  std_logic;
    -- ulpi_link へ
    tx_valid   : out std_logic;
    tx_ready   : in  std_logic;
    tx_data    : out std_logic_vector(7 downto 0);
    tx_last    : out std_logic;
    tx_aborted : in  std_logic
  );
end entity;

architecture rtl of usb_tx_packet is
  type state_t is (S_IDLE, S_HANDSHAKE, S_PID, S_PAYLOAD, S_CRC_LOW, S_CRC_HIGH);
  signal state   : state_t := S_IDLE;
  signal pid     : std_logic_vector(3 downto 0) := (others => '0');
  signal empty   : std_logic := '0';
  signal crc     : std_logic_vector(15 downto 0) := CRC16_INIT;
  signal crc_out : std_logic_vector(15 downto 0);

  function pid_byte(p : std_logic_vector(3 downto 0)) return std_logic_vector is
  begin
    return (not p) & p;
  end function;
begin
  crc_out <= not crc;

  process (all)
  begin
    tx_valid <= '0';
    tx_data  <= (others => '0');
    tx_last  <= '0';
    pl_ready <= '0';
    case state is
      when S_IDLE =>
        null;
      when S_HANDSHAKE =>
        tx_valid <= '1';
        tx_data  <= pid_byte(pid);
        tx_last  <= '1';
      when S_PID =>
        tx_valid <= '1';
        tx_data  <= pid_byte(pid);
      when S_PAYLOAD =>
        tx_valid <= pl_valid;
        tx_data  <= pl_data;
        pl_ready <= tx_ready;
      when S_CRC_LOW =>
        tx_valid <= '1';
        tx_data  <= crc_out(7 downto 0);
      when S_CRC_HIGH =>
        tx_valid <= '1';
        tx_data  <= crc_out(15 downto 8);
        tx_last  <= '1';
    end case;
  end process;

  process (clk)
  begin
    if rising_edge(clk) then
      done    <= '0';
      aborted <= '0';
      if rst = '1' then
        state <= S_IDLE;
      elsif state /= S_IDLE and tx_aborted = '1' then
        aborted <= '1';
        state   <= S_IDLE;
      else
        case state is
          when S_IDLE =>
            crc <= CRC16_INIT;
            if hs_send = '1' then
              pid   <= hs_pid;
              state <= S_HANDSHAKE;
            elsif data_send = '1' then
              pid   <= data_pid;
              empty <= data_empty;
              state <= S_PID;
            end if;

          when S_HANDSHAKE =>
            if tx_ready = '1' then
              done  <= '1';
              state <= S_IDLE;
            end if;

          when S_PID =>
            if tx_ready = '1' then
              state <= S_CRC_LOW when empty = '1' else S_PAYLOAD;
            end if;

          when S_PAYLOAD =>
            if tx_ready = '1' and pl_valid = '1' then
              crc <= crc16_next(crc, pl_data);
              if pl_last = '1' then
                state <= S_CRC_LOW;
              end if;
            end if;

          when S_CRC_LOW =>
            if tx_ready = '1' then
              state <= S_CRC_HIGH;
            end if;

          when S_CRC_HIGH =>
            if tx_ready = '1' then
              done  <= '1';
              state <= S_IDLE;
            end if;
        end case;
      end if;
    end if;
  end process;

  busy <= '0' when state = S_IDLE else '1';
end architecture;
