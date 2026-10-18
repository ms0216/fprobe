-- USB 2.0 デバイスの時間定数（ULPI クロック 60 MHz のサイクル数）。
-- 根拠: docs/reference/usb2-hs-device-on-ulpi.md §3（値は LUNA と USB 2.0 7.1.7.5 / 7.1.7.6 に準拠）。
-- usb_reset 等の generic の既定値として使い、シミュレーションでは短縮値を与える。
package usb_timing_pkg is

  constant USB_CLK_HZ : natural := 60_000_000;

  constant T_DETACH             : natural := 6_000_000; -- 起動時に 100 ms 切り離す（下記）
  constant T_RESET_SE0          : natural := 300;       -- 5 us 連続の SE0 でバスリセット
  constant T_CHIRP              : natural := 120_000;   -- デバイスチャープ K 2 ms
  constant T_LINE_STABLE        : natural := 150;       -- ホストチャープ K/J の判定 2.5 us
  constant T_HOST_CHIRP_TIMEOUT : natural := 150_000;   -- ホストチャープ待ち 2.5 ms
  constant T_HS_IDLE            : natural := 180_000;   -- HS で 3 ms 無活動 → FS に戻して判定
  constant T_SUSPEND_SAMPLE     : natural := 12_000;    -- FS に戻してから 200 us 後に判定
  constant T_FS_IDLE            : natural := 180_000;   -- FS で 3 ms J → サスペンド

end package;
