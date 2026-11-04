-- fprobe_core とその部分（core_debug・core_la・core_storage・core_xtrig・core_periph）の間で受け渡す信号の組。
-- 部分の entity は fprobe_core の中だけで使う（cocotb の試験は fprobe_core の試験台から通す）ので、ポートに組を使う。
library ieee;
use ieee.std_logic_1164.all;
use ieee.numeric_std.all;
use work.timing_pkg.all;

package core_pkg is

  -- ロジアナ（幅と深さは CSR の生成元 fprobe_map.py の LA_WIDTH / LA_DEPTH と同じ）
  constant LA_WIDTH     : positive := 16;
  constant LA_ADDR_BITS : positive := 13;

  -- ロジアナのトリガの設定（csr_la → 内蔵ロジアナ・深いバッファ）
  type la_trig_cfg_t is record
    rate       : std_logic_vector(15 downto 0);
    pretrigger : std_logic_vector(31 downto 0);
    mask       : std_logic_vector(LA_WIDTH - 1 downto 0);
    value      : std_logic_vector(LA_WIDTH - 1 downto 0);
    rise       : std_logic_vector(LA_WIDTH - 1 downto 0);
    fall       : std_logic_vector(LA_WIDTH - 1 downto 0);
  end record;

  -- 割り込み書き込みの語 0（csr_xtrig → swd_injector）
  type inject_cfg_t is record
    ap    : std_logic_vector(7 downto 0);
    addr  : std_logic_vector(31 downto 0);
    value : std_logic_vector(31 downto 0);
  end record;

  -- 番地一致の条件（csr_xtrig → dap_monitor）
  type match_cfg_t is record
    addr      : std_logic_vector(31 downto 0);
    addr_mask : std_logic_vector(31 downto 0);
    data      : std_logic_vector(31 downto 0);
    data_mask : std_logic_vector(31 downto 0);
    on_write  : std_logic;
    on_read   : std_logic;
  end record;

  -- 割り込み書き込みの状態（swd_injector → csr_xtrig）
  type inject_status_t is record
    busy   : std_logic;
    result : std_logic_vector(1 downto 0);
    ack    : std_logic_vector(2 downto 0);
    count  : unsigned(15 downto 0);
    time   : unsigned(63 downto 0);          -- クロストリガの要求で最後に書いた語の DRW の線上の点（time-accuracy R5）
  end record;

  -- イベント記録の源（event_collector の入力。並びと意味は event_collector.vhd の先頭）
  -- ホストの転送（dap_monitor。種類 1 / 12）
  type xfer_event_t is record
    valid   : std_logic;
    kind    : std_logic_vector(7 downto 0);
    parity  : std_logic;
    request : std_logic_vector(3 downto 0);
    ack     : std_logic_vector(2 downto 0);
    data    : std_logic_vector(31 downto 0);
    addr    : std_logic_vector(31 downto 0);
    time    : std_logic_vector(47 downto 0);
  end record;
  -- 割り込み書き込みの語（swd_injector。種類 11）
  type inject_event_t is record
    written   : std_logic;
    transfers : unsigned(7 downto 0);
    value     : std_logic_vector(31 downto 0);
    addr      : std_logic_vector(31 downto 0);
    time      : std_logic_vector(47 downto 0);
  end record;
  -- サンプラ（dap_sampler。種類 7・17・18）
  type sampler_event_t is record
    valid : std_logic;
    kind  : std_logic_vector(7 downto 0);
    word  : std_logic_vector(119 downto 0);
  end record;
  -- バイト（UART の受信・送信、SWO の受信）
  type byte_event_t is record
    fire : std_logic;
    data : std_logic_vector(7 downto 0);
    time : unsigned(47 downto 0);            -- 開始ビットの辺の時刻（R3。UART の送信は使わない）
  end record;
  -- 補助マイコンの計測値（種類 6）
  type aux_event_t is record
    valid : std_logic;
    ch    : natural range 0 to 3;
    value : unsigned(13 downto 0);
    time  : unsigned(47 downto 0);
  end record;
  -- 対象の FPGA の観測（種類 8・9）
  type obs_event_t is record
    valid : std_logic;
    sync  : std_logic;
    data  : std_logic_vector(31 downto 0);
    addr  : std_logic_vector(31 downto 0);
    time  : std_logic_vector(47 downto 0);
  end record;
  -- ボードの間の時刻の同期の組（種類 10）
  type sync_event_t is record
    valid  : std_logic;
    local  : std_logic_vector(47 downto 0);
    parent : std_logic_vector(63 downto 0);
  end record;

  -- ロジアナの構成（fprobe_core の generic から決まる。core_la と core_storage が同じ式を使う）
  -- 1 周期に LANES サンプルの内蔵ロジアナ（la_lane_capture）
  function la_use_lanes(lanes : positive; internal : boolean) return boolean;
  -- 内蔵ロジアナ一式を cap_clk の領域で動かす（la_cap_domain）
  function la_use_cap(cap_ratio : natural; lanes : positive; internal : boolean) return boolean;
  -- 深いバッファを 1 周期に LANES サンプルで記録する（la_deep_buffer の LANES。docs/superpowers/specs/2026-11-01-la-deep-fast-design.md）
  function la_use_deep_lanes(lanes : positive; internal : boolean; mem_addr_bits : natural; mem_test : boolean) return boolean;
  -- 外部トリガ（la_ext）の揃え（sys の la_capture・深いバッファの la_front）: la_probes はグリッチフィルタの段を通った後なので、
  -- その段数を la_front の揃えに足す（la_front の注。外部トリガが 1 の周期に端子が変わったピンのサンプルにトリガが付く。
  -- 時刻の正しさ S1b の N3）。filter_in_sys = グリッチフィルタが sys の経路にある（LA_FILTER and not la_use_cap）
  function la_ext_delay(filter_in_sys : boolean) return natural;

  -- 塊ごとのリセット（fprobe_core の rst_g の並び）
  constant RST_BUS     : natural := 0;       -- バス・CSR
  constant RST_DEBUG   : natural := 1;       -- DAP・デバッグポート・サンプラ
  constant RST_PERIPH  : natural := 2;       -- SWO・UART・補助・同期・観測
  constant RST_LA      : natural := 3;       -- ロジアナ・トリガ・クロストリガ
  constant RST_STORAGE : natural := 4;       -- イベント記録・外部メモリ
  subtype core_resets_t is std_logic_vector(0 to 4);

end package;

package body core_pkg is

  function la_use_lanes(lanes : positive; internal : boolean) return boolean is
  begin
    return lanes > 1 and internal;
  end function;

  function la_use_cap(cap_ratio : natural; lanes : positive; internal : boolean) return boolean is
  begin
    return cap_ratio > 1 and internal and not la_use_lanes(lanes, internal);
  end function;

  function la_use_deep_lanes(lanes : positive; internal : boolean; mem_addr_bits : natural; mem_test : boolean) return boolean is
  begin
    return lanes > 1 and not internal and mem_addr_bits > 0 and not mem_test;
  end function;

  function la_ext_delay(filter_in_sys : boolean) return natural is
  begin
    return LA_FRONT_EXT_ALIGN + LA_FILTER_DELAY * boolean'pos(filter_in_sys);
  end function;

end package body;
