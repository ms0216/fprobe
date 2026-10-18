-- CDC-ACM の通知エンドポイント（インタラプト IN）: SERIAL_STATE（PSTN 1.2 §6.5.4）を状態が変わるたびに送る。
-- 仕様: docs/superpowers/specs/2026-10-19-u51-u52-swo-uart-design.md §4.4
--
-- 通知は 10 バイト: bmRequestType 0xA1、bNotification 0x20、wValue 0、wIndex = COMM_INTERFACE、wLength 2、
-- データ 2 バイト（UART の状態のビット）。送るビット:
--   bit0 bRxCarrier（OS では DCD）= dcd、bit1 bTxCarrier（OS では DSR）= dsr。
-- SERIAL_STATE には CTS のビットが無い（PSTN の表 31）。fprobe は CTS を dsr に入れて知らせる（トップの配線。
-- 正確な値は CSR uart.lines の cts）。ほかのビット（ブレーク・フレーム異常・オーバーラン等の一度きりの出来事）は送らない
-- （数は CSR uart の frame_errors・overruns）。
--
-- usb_ep_in のプロトコル側と同じ I/F（usb_protocol が IN トークンで pkt_ready を見て pl_* を読み、ack / retry を返す）。
-- 最後に届いた（ACK された）状態と今の状態が違えば 1 パケットを用意する。送っている間に状態が変わっても、送る中身は
-- 用意した時の値のまま（ACK の後に違いがあればもう一度送る）。rst（バスリセット・構成の変更を含む）で「まだ何も
-- 送っていない（全部 0）」に戻る: 構成の直後に、0 でない状態（DCD 等）を 1 回知らせる。
library ieee;
use ieee.std_logic_1164.all;
use ieee.numeric_std.all;

entity usb_cdc_notify is
  generic (
    COMM_INTERFACE : natural                       -- 通信クラスインターフェースの番号
  );
  port (
    clk       : in  std_logic;
    rst       : in  std_logic;
    dcd       : in  std_logic;                     -- このクロックのドメインで
    dsr       : in  std_logic;
    -- usb_protocol（usb_ep_in と同じ）
    pkt_ready : out std_logic;
    pkt_empty : out std_logic;
    pl_valid  : out std_logic;
    pl_ready  : in  std_logic;
    pl_data   : out std_logic_vector(7 downto 0);
    pl_last   : out std_logic;
    ack       : in  std_logic;
    retry     : in  std_logic
  );
end entity;

architecture rtl of usb_cdc_notify is
  signal sent    : std_logic_vector(1 downto 0) := "00";   -- 最後に ACK された状態（DSR, DCD）
  signal pending : std_logic_vector(1 downto 0) := "00";   -- 送っている状態
  signal busy    : std_logic := '0';                        -- パケットを用意した（ACK を待つ）
  signal index   : unsigned(3 downto 0) := (others => '0'); -- 次に出すバイト
  signal now     : std_logic_vector(1 downto 0);
begin
  now <= dsr & dcd;

  process (clk)
  begin
    if rising_edge(clk) then
      if rst = '1' then
        sent  <= "00";
        busy  <= '0';
        index <= (others => '0');
      elsif busy = '0' then
        if now /= sent then
          pending <= now;
          busy    <= '1';
          index   <= (others => '0');
        end if;
      else
        if ack = '1' then
          sent  <= pending;
          busy  <= '0';
          index <= (others => '0');
        elsif retry = '1' then
          index <= (others => '0');
        elsif pl_ready = '1' and index /= 10 then
          index <= index + 1;
        end if;
      end if;
    end if;
  end process;

  pkt_ready <= busy;
  pkt_empty <= '0';
  pl_valid  <= '1' when busy = '1' and index /= 10 else '0';
  pl_last   <= '1' when index = 9 else '0';

  with to_integer(index) select pl_data <=
    x"A1"                                              when 0,
    x"20"                                              when 1,
    std_logic_vector(to_unsigned(COMM_INTERFACE, 8))   when 4,
    x"02"                                              when 6,
    "000000" & pending                                 when 8,
    x"00"                                              when others;   -- wValue・wIndex の上位・wLength の上位・データの上位
end architecture;
