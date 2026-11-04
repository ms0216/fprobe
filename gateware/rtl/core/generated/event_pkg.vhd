-- 自動生成ファイル。直接編集しないこと（python -m fprobe_gw gen で再生成）。
-- 生成元: gateware/fprobe_gw/events.py（イベント記録の種類と、1 件 128 ビットの並び）
library ieee;
use ieee.std_logic_1164.all;

package event_pkg is
  subtype event_t is std_logic_vector(127 downto 0);
  subtype event_kind_t is std_logic_vector(7 downto 0);

  constant EVENT_KIND_HOST             : event_kind_t := x"01";   -- ホスト（pyOCD 等）の転送
  constant EVENT_KIND_INJECT           : event_kind_t := x"02";   -- クロストリガの割り込み書き込み（旧い回路。転送数なし）
  constant EVENT_KIND_UART_RX          : event_kind_t := x"03";   -- UART の受信バイト（ターゲット → PC）
  constant EVENT_KIND_UART_TX          : event_kind_t := x"04";   -- UART の送信バイト（PC → ターゲット）
  constant EVENT_KIND_SWO              : event_kind_t := x"05";   -- SWO のバイト
  constant EVENT_KIND_AUX              : event_kind_t := x"06";   -- 補助マイコンの計測値（data = ch << 16 | mV）
  constant EVENT_KIND_SAMPLE           : event_kind_t := x"07";   -- DAP サンプラの 1 語
  constant EVENT_KIND_OBS_RECORD       : event_kind_t := x"08";   -- 対象の FPGA の観測の記録
  constant EVENT_KIND_OBS_SYNC         : event_kind_t := x"09";   -- 対象の FPGA の観測の時刻合わせ
  constant EVENT_KIND_SYNC             : event_kind_t := x"0A";   -- ボードの間の時刻の同期の組
  constant EVENT_KIND_INJECT_TRANSFERS : event_kind_t := x"0B";   -- 割り込み書き込み（[119:112] = 停止の書き込みまでの転送数。R20）
  constant EVENT_KIND_HOST_UNTRACKED   : event_kind_t := x"0C";   -- ホストの転送のうち DRW で、メモリの番地を追えなかったもの
  constant EVENT_KIND_UART_RX_PACKED   : event_kind_t := x"0D";   -- 種類 3 を詰めた形
  constant EVENT_KIND_UART_TX_PACKED   : event_kind_t := x"0E";   -- 種類 4 を詰めた形
  constant EVENT_KIND_SWO_PACKED       : event_kind_t := x"0F";   -- 種類 5 を詰めた形
  constant EVENT_KIND_DAP_CONFIG       : event_kind_t := x"10";   -- PHY の設定
  constant EVENT_KIND_SAMPLE_BOUND     : event_kind_t := x"11";   -- サンプラの値を返した転送の点
  constant EVENT_KIND_SAMPLE_GAP       : event_kind_t := x"12";   -- サンプラの誤りの区間
  constant EVENT_KIND_LA_GAP           : event_kind_t := x"13";   -- ロジアナの垂れ流しの欠け（la-deep-fast §12.7）

  -- 1 件: [127:120] 種類、[119:112] 種類ごとの欄、[111:80] データ、[79:48] 番地、[47:0] 時刻
  function make_event(kind : event_kind_t; info : std_logic_vector(7 downto 0); data, addr : std_logic_vector(31 downto 0);
                      stamp : std_logic_vector(47 downto 0)) return event_t;
end package;

package body event_pkg is
  function make_event(kind : event_kind_t; info : std_logic_vector(7 downto 0); data, addr : std_logic_vector(31 downto 0);
                      stamp : std_logic_vector(47 downto 0)) return event_t is
  begin
    return kind & info & data & addr & stamp;
  end function;
end package body;
