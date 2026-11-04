"""イベント記録の種類（1 件 128 ビットの [127:120]）の定義元。

回路（rtl/core/generated/event_pkg.vhd: event_collector・dap_sampler・la_stream_gap・core_debug）とホスト
（host/fprobe/generated.py の EVENT_KIND_*。fprobe.events の KIND_*）は、ここから生成した値を使う。
1 件の並び: [47:0] 時刻、[79:48] 番地、[111:80] データ、[119:112] 種類ごとの欄（ホストの転送はパリティ異常・要求・ACK）、
[127:120] 種類。種類ごとの意味は host/fprobe/events.py の先頭。
"""

__all__ = ["EVENT_KINDS", "render_vhdl_package"]

# 名前 → (値, 説明)。値は回路とホストの約束なので変えない（古い回路の種類も残す）
EVENT_KINDS: dict[str, tuple[int, str]] = {
    "HOST": (1, "ホスト（pyOCD 等）の転送"),
    "INJECT": (2, "クロストリガの割り込み書き込み（旧い回路。転送数なし）"),
    "UART_RX": (3, "UART の受信バイト（ターゲット → PC）"),
    "UART_TX": (4, "UART の送信バイト（PC → ターゲット）"),
    "SWO": (5, "SWO のバイト"),
    "AUX": (6, "補助マイコンの計測値（data = ch << 16 | mV）"),
    "SAMPLE": (7, "DAP サンプラの 1 語"),
    "OBS_RECORD": (8, "対象の FPGA の観測の記録"),
    "OBS_SYNC": (9, "対象の FPGA の観測の時刻合わせ"),
    "SYNC": (10, "ボードの間の時刻の同期の組"),
    "INJECT_TRANSFERS": (11, "割り込み書き込み（[119:112] = 停止の書き込みまでの転送数。R20）"),
    "HOST_UNTRACKED": (12, "ホストの転送のうち DRW で、メモリの番地を追えなかったもの"),
    "UART_RX_PACKED": (13, "種類 3 を詰めた形"),
    "UART_TX_PACKED": (14, "種類 4 を詰めた形"),
    "SWO_PACKED": (15, "種類 5 を詰めた形"),
    "DAP_CONFIG": (16, "PHY の設定"),
    "SAMPLE_BOUND": (17, "サンプラの値を返した転送の点"),
    "SAMPLE_GAP": (18, "サンプラの誤りの区間"),
    "LA_GAP": (19, "ロジアナの垂れ流しの欠け（la-deep-fast §12.7）"),
}


def render_vhdl_package() -> str:
    """rtl/core/generated/event_pkg.vhd: 種類の定数と、1 件を組む関数。"""
    width = max(len(n) for n in EVENT_KINDS)
    lines = [
        "-- 自動生成ファイル。直接編集しないこと（python -m fprobe_gw gen で再生成）。",
        "-- 生成元: gateware/fprobe_gw/events.py（イベント記録の種類と、1 件 128 ビットの並び）",
        "library ieee;",
        "use ieee.std_logic_1164.all;",
        "",
        "package event_pkg is",
        "  subtype event_t is std_logic_vector(127 downto 0);",
        "  subtype event_kind_t is std_logic_vector(7 downto 0);",
        "",
        *(f"  constant EVENT_KIND_{n:<{width}} : event_kind_t := x\"{v:02X}\";   -- {doc}"
          for n, (v, doc) in EVENT_KINDS.items()),
        "",
        "  -- 1 件: [127:120] 種類、[119:112] 種類ごとの欄、[111:80] データ、[79:48] 番地、[47:0] 時刻",
        "  function make_event(kind : event_kind_t; info : std_logic_vector(7 downto 0); data, addr : std_logic_vector(31 downto 0);",
        "                      stamp : std_logic_vector(47 downto 0)) return event_t;",
        "end package;",
        "",
        "package body event_pkg is",
        "  function make_event(kind : event_kind_t; info : std_logic_vector(7 downto 0); data, addr : std_logic_vector(31 downto 0);",
        "                      stamp : std_logic_vector(47 downto 0)) return event_t is",
        "  begin",
        "    return kind & info & data & addr & stamp;",
        "  end function;",
        "end package body;",
        "",
    ]
    return "\n".join(lines)
