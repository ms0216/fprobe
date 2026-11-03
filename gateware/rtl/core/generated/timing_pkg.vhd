-- 自動生成ファイル。直接編集しないこと（python -m fprobe_gw gen で再生成）。
-- 生成元: gateware/fprobe_gw/timing.py（時刻の表。各情報源の時刻が本当の出来事から何刻みずれるか）
-- 仕様: docs/superpowers/specs/2026-11-01-time-accuracy-design.md §4.2
package timing_pkg is
  constant LA_FILTER_DELAY : integer := 3;
  constant LA_INTERNAL_DELAY : integer := 3;
  constant LA_DEEP_DELAY : integer := 4;
  constant LA_CAP_LATENCY_BASE : integer := 5;
  constant LA_LANE_PIPE_CYCLES : integer := 3;
  constant LA_FRONT_EXT_ALIGN : integer := 2;
  constant XTRIG_LA_EXT_DELAY : integer := 1;
  constant UART_RX_START_DELAY : integer := 2;
  constant SWO_MANCHESTER_BIT_DELAY : integer := 2;
  constant UART_TX_START_DELAY : integer := 0;
  constant SYNC_RX_LATENCY_TICKS : integer := 2;
  constant SYNC_TX_LATENCY_TICKS : integer := 1;
end package;
