"""ビルド可能なデザイン（トップ entity + VHDL ソース + 対象ボード + クロック制約）の登録。"""

from dataclasses import dataclass
from pathlib import Path

from . import build_info, caps
from .boards import Board
from .boards.nano9k import NANO9K
from .boards.primer20k import PRIMER20K
from .boards.pynq_z2 import PYNQ_Z2
from .protocol import PROTOCOL_VERSION
from .csr.fprobe_map import FPROBE_MAP, LA_DEPTH, LA_WIDTH
from .rtl import CORE_GENERATED, RTL_ROOT, rtl, target_ip
from .toolchain.project import ClockConstraint, GowinIp, GowinProject, IoDelay
from .toolchain.vivado import VivadoProject

__all__ = ["DESIGNS", "Design", "FEATURE_DESIGNS", "PROTOCOL_DESIGNS", "caps_package_path", "core_sources"]


def caps_package_path(design: str) -> Path:
    """デザインのケイパビリティ ROM パッケージ（ファイルは構成ごと、パッケージ名は共通の design_caps_pkg。
    トップは work.design_caps_pkg を参照し、ビルドには自分の構成のファイルだけを入れる）。"""
    return RTL_ROOT / "designs" / "generated" / f"{design}_caps_pkg.vhd"


@dataclass(frozen=True)
class Design:
    name: str
    board: Board
    top: str
    sources: tuple[Path, ...]
    pin_groups: tuple[str, ...]
    clocks: tuple[ClockConstraint, ...]
    io_delays: tuple[IoDelay, ...] = ()
    capabilities: caps.Capabilities | None = None   # fprobe_core を含むデザインが申告する内容
    options: tuple[str, ...] = ()                   # ボードの options に足す配置配線の設定
    ips: tuple[GowinIp, ...] = ()                   # ビルド時に生成するベンダー IP
    placements: tuple[str, ...] = ()                # 配置制約（.cst に足す）
    cdc_max_delay_ns: float | None = None           # クロックまたぎの最大遅延（GowinProject の説明を参照）
    block_designs: tuple[Path, ...] = ()            # Vivado のブロック設計を作る TCL（PS7 等）
    ps_axi: bool = False                            # PS が AXI で触る PL の回路（中継は無し）。書いた後にレベルシフタを有効にする

    def project(self) -> GowinProject | VivadoProject:
        stamp = build_info.stamp(RTL_ROOT.parent) if self.capabilities is not None else None
        if self.board.toolchain == "vivado":
            return VivadoProject(name="top", part=self.board.part, top=self.top, sources=self.sources,
                                 pins=self.board.pins_for(self.pin_groups), clocks=self.clocks,
                                 block_designs=self.block_designs, stamp=stamp)
        return GowinProject(
            name="top", part=self.board.part, family=self.board.family, top=self.top,
            sources=self.sources, pins=self.board.pins_for(self.pin_groups),
            clocks=self.clocks, io_delays=self.io_delays, options=self.board.options + self.options,
            ips=self.ips, placements=self.placements, cdc_max_delay_ns=self.cdc_max_delay_ns,
            # fprobe_core を含むデザインはケイパビリティで自分の版を申告する（U49）
            stamp=stamp)


_COMMON = ("common/util_pkg.vhd", "common/sync_bit.vhd", "common/reset_sync.vhd",
           "common/sync_fifo.vhd")
_SERIAL = ("serial/uart_tx.vhd", "serial/uart_rx.vhd")
_REPORT = ("common/text_report_pkg.vhd", "common/text_report.vhd")
_PRIMER20K = ("boards/primer20k/primer20k_pkg.vhd", "boards/primer20k/gowin_rpll.vhd")
_USB = ("usb/usb_types_pkg.vhd", "usb/usb_timing_pkg.vhd", "usb/usb_crc_pkg.vhd",
        "usb/generated/usb_descriptors_pkg.vhd", "usb/ulpi_link.vhd", "usb/usb_reset.vhd",
        "usb/usb_rx_packet.vhd", "usb/usb_tx_packet.vhd", "usb/usb_ep_out.vhd", "usb/usb_ep_in.vhd",
        "usb/usb_protocol.vhd", "usb/usb_control_ep.vhd", "usb/usb_cdc_acm_requests.vhd", "usb/usb_cdc_notify.vhd",
        "usb/usb_device.vhd")

_CORE = ("core/core_pkg.vhd", "common/stream_slice.vhd", "core/csr_decoder.vhd", "core/control_plane.vhd", "core/timebase.vhd", "core/caps_rom.vhd",
         "common/udiv.vhd", "common/udiv_shared.vhd", "common/reset_fanout.vhd", "debug/generated/dap_info_pkg.vhd", "debug/dap_pkg.vhd", "debug/swd_phy.vhd", "debug/jtag_phy.vhd", "debug/obs_pkg.vhd", "debug/obs_cmd_queue.vhd", "debug/obs_match.vhd", "debug/obs_record_rx.vhd", "debug/obs_jtag_engine.vhd", "debug/debug_port.vhd",
         "debug/dap_packet.vhd", "debug/dap_link.vhd", "debug/dap_cmd_config.vhd",
         "debug/dap_cmd_swo.vhd", "debug/dap_cmd_sequence.vhd", "debug/dap_cmd_transfer.vhd", "debug/dap_processor.vhd", "debug/dap_phy_borrow.vhd", "debug/swd_injector.vhd", "debug/dap_sampler.vhd", "debug/sampler_match.vhd", "debug/dap_monitor.vhd",
         "serial/uart_tx.vhd", "serial/uart_rx.vhd", "serial/uart_bridge.vhd", "debug/swo_manchester_rx.vhd", "debug/swo_receiver.vhd",
         "la/la_filter.vhd", "la/la_probe_inputs.vhd", "la/la_pulse.vhd", "la/la_pkg.vhd", "la/la_front.vhd", "la/la_record.vhd", "la/la_capture.vhd", "la/la_lane_front.vhd", "la/la_lane_capture.vhd", "la/la_lane_inputs.vhd", "la/la_cap_domain.vhd", "la/la_mem_csr.vhd", "common/cdc_value.vhd", "mem/mem_test.vhd", "mem/mem_test_sys.vhd", "common/async_fifo.vhd",
         "common/stream_unpack.vhd", "common/sync_fifo.vhd", "la/la_pack.vhd", "la/la_testgen.vhd", "la/la_encoder.vhd", "la/la_lane_raw.vhd", "la/la_lane_rle.vhd", "la/la_lane_encoder.vhd", "mem/mem_ring_pkg.vhd", "mem/la_mem_writer.vhd",
         "mem/mem_reader.vhd", "la/la_stream_gap.vhd", "common/cdc_request.vhd", "common/hold_one.vhd", "la/la_run_cdc.vhd", "mem/la_write_track.vhd", "mem/mem_cmd_arb.vhd", "la/la_stream_sched.vhd", "la/la_deep_front.vhd", "la/la_deep_buffer.vhd", "la/la_ps_port.vhd", "common/sdp_ram.vhd", "la/event_ring.vhd",
         "core/trig_seq.vhd", "core/xtrig_out.vhd", "core/pin_select.vhd", "core/debug_pin_func.vhd", "core/csr_absent.vhd", "core/aux_link.vhd", "core/sync_link.vhd", "core/event_byte_pack.vhd", "core/event_collector.vhd",
         "core/core_debug.vhd", "core/core_xtrig.vhd", "core/core_periph.vhd", "core/core_la.vhd", "core/core_storage.vhd",
         "core/fprobe_core.vhd")


def core_sources(caps_package: Path) -> list[Path]:
    """fprobe_core とその依存（依存順）。caps_package はデザインのケイパビリティ生成パッケージ。"""
    return [*rtl("common/util_pkg.vhd", "common/sync_bit.vhd", "core/csr_bus_pkg.vhd"),
            CORE_GENERATED / "csr_map_pkg.vhd", CORE_GENERATED / "timing_pkg.vhd", CORE_GENERATED / "ps_window_pkg.vhd",
            CORE_GENERATED / "event_pkg.vhd", build_info.DEFAULT_PACKAGE,
            *(CORE_GENERATED / f"csr_{b.name}.vhd" for b in FPROBE_MAP.blocks if not b.external),
            caps_package,
            *rtl(*_CORE)]


def _primer_usb(name: str, *, la_inputs: int = 0, **features) -> Design:
    """Primer 20K の USB の製品デザイン（トップ・ピン・IP は共通。features はケイパビリティの機能の取捨、la_inputs は
    J14 の専用の入力を使う本数（U22。0 なら使わない））。"""
    # R21 のサンプラの値の一致は Primer では外す（入れると配線が混んで 3 版とも 1200 秒で終わらなかった。2026-10-23）
    features.setdefault("sampler_match", False)
    return Design(
        name=name, board=PRIMER20K, top="primer20k_fprobe_top",
        sources=tuple(dict.fromkeys((          # 重複を除く（コアと共通部品が同じファイルを含む）
            *rtl(*_COMMON, *_PRIMER20K, *_USB, "common/async_fifo.vhd", "common/cdc_value.vhd",
                 "boards/primer20k/primer20k_ulpi_reset.vhd"),
            *core_sources(caps_package_path(name)),
            *rtl("boards/primer20k/primer20k_ddr3.vhd", "boards/primer20k/primer20k_fprobe_top.vhd")))),
        pin_groups=("clk27", "led", "ulpi", "pmod1", "ddr3", "aux", "la_in", "sync"),
        clocks=(PRIMER20K.input_clock(), PRIMER20K.sys_clock(), PRIMER20K.ulpi_clock(), *PRIMER20K.mem_clocks()),
        ips=(PRIMER20K.ddr3.controller,),
        io_delays=PRIMER20K.ulpi_io_delays(),
        placements=PRIMER20K.ulpi_placements("device_i/link_i"),
        cdc_max_delay_ns=8.0,                             # 最短の周期（sys 9.97 ns）より短く
        options=("-place_option 1 -route_option 1",),     # sys（100 MHz）: 既定では余裕 0.1% 未満、これで約 3%
        capabilities=caps.Capabilities(
            protocol_version=PROTOCOL_VERSION, board=PRIMER20K.name, design=name,
            sys_clock_hz=PRIMER20K.sync_pll.fout_hz, timebase_hz=PRIMER20K.sync_pll.fout_hz,
            host_link=caps.LINK_USB_HS_ULPI, channels=(caps.CH_CONTROL, caps.CH_DAP, caps.CH_UART0),
            la_channels=LA_WIDTH, la_depth=0, la_sample_hz=PRIMER20K.sync_pll.fout_hz,   # 内蔵バッファ無し（深いバッファ）
            mem_bytes=PRIMER20K.ddr3.size_bytes, aux=True, outputs=PRIMER20K.outputs(),
            uart_flow=PRIMER20K.uart_flow(), la_inputs=la_inputs, **features))


PYNQ_Z2_DEEP_BYTES = 32 * 1024 * 1024        # 深いバッファの領域（DDR の 0x1E000000〜。host/pynq/uEnv.txt の mem=479M。その下の 1 MB 0x1DF00000〜 は Linux のリング）


# 1 周期に複数のサンプルの構成（fprobe_pynq_fast）の内蔵バッファの深さ（語 = sys の周期）。PYNQ の BRAM 140 タイルのうち、
# fprobe_pynq_cap の使用 11 タイル（C:w51 の util.rpt）を除いた空きの半分（約 64 タイル）以内で最大の 2 の累乗:
# 16384 語 × 96 ビット（N = 6）= 48 タイル、× 128 ビット（N = 8）= 64 タイル（32768 語は 96 タイルで超える）
PYNQ_Z2_LANE_WORDS = 16384

# 取り込みのクロック cap_clk / sys（MMCM の 2 本目の出力。VCO 1200 MHz、boards/pynq_z2.py）。
# fprobe_pynq_cap: 300 MHz（内蔵ロジアナ一式を cap_clk の領域で 1 周期 1 サンプル。2026-10-30-la-capture-clock-design.md）。
# fprobe_pynq_fast: 400 MHz = ISERDESE2 の CLK（DDR で 1 周期に N = 2 × 4 = 8 サンプル、800 MS/s。試験用の方形波もこれで刻む。
# docs/superpowers/specs/2026-11-01-la-resolution-design.md の段 2・§11）
PYNQ_Z2_CAP_RATIO = 3
PYNQ_Z2_FAST_CAP_RATIO = 4


def pynq_z2_iserdes_newest_age(lanes: int) -> int:
    """PYNQ-Z2 の端子の ISERDESE2（xilinx_la_iserdes）の、1 語の最も新しいレーンの遅れ（細かいサンプルの数。fprobe_core の
    LA_LANE_IN_AGE）。VHDL の pynq_z2_pkg.iserdes_newest_age と同じ値で、tests/tb/xilinx_la_iserdes_tb.py がこの関数の値を
    ISERDESE2 の模型で確かめ、pynq_z2_fprobe_top がケイパビリティの生成パッケージの値と VHDL の関数の一致を elaboration で確かめる。"""
    return lanes - 3


# PYNQ-Z2 の深いバッファの垂れ流しを持続できると測った速さ（TLV LA_STREAM_BYTES_PER_S）: RAW 16 ch・25 MS/s（50 MB/s）の 60 秒を
# 11 回中 11 回欠けなし、8 ch・50 MS/s・4 ch・100 MS/s（50 MB/s）の 60 秒を各 2 回欠けなし（PS 直読み・TCP、Windows 11 の PC、
# 同じ LAN。docs/superpowers/specs/2026-10-30-la-stream-ps-direct-design.md §10.14・§10.16）。PC・回線・中継の版で変わる測定の値なので、
# ホストは使う側が明示した上限があればそちらを使う
PYNQ_Z2_STREAM_BYTES_PER_S = 50_000_000


def _pynq(name: str, *, mem_bytes: int = 0, cap: bool = False, lanes: bool = False) -> Design:
    """PYNQ-Z2（Zynq-7020）: 同じ fprobe_core を、PS の Linux の中継（UDP、Ethernet 版と同じ線上の形式）越しに使う。
    Primer では構成に分けている機能（JTAG・サンプラ・故障注入・専用の入力・RTS/CTS）を 1 つに入れる
    （docs/superpowers/specs/2026-10-19-pynq-z2-port-design.md）。mem_bytes > 0 なら深いバッファを PS の DDR に置き
    （ブロック設計 ps7_hp_bd）、内蔵ロジアナは持たない。0 なら内蔵 BRAM のロジアナ（ps7_bd）。
    cap なら内蔵ロジアナの取り込みを、sys の整数倍の速いクロック（MMCM の 2 本目の出力）の領域で動かす
    （docs/superpowers/specs/2026-10-30-la-capture-clock-design.md。深いバッファとは組み合わせない）。
    lanes なら拡張ヘッダのピン（PMOD JA の 8 本）と専用の入力（PMOD JB の 6 本）を端子の ISERDESE2 で取り、sys の 1 周期に
    N = 2 × cap_ratio サンプル（CLK = cap_clk の両方の辺、CLKDIV = sys）を内蔵バッファに記録する
    （docs/superpowers/specs/2026-11-01-la-resolution-design.md の段 1・段 2）。cap とは組み合わせない。
    lanes かつ深いバッファ（fprobe_pynq_deep_fast）なら、同じ N サンプル/周期を深いバッファの垂れ流しに N 並列で符号にする
    （RAW の詰め・RLE。内蔵バッファは持たない。docs/superpowers/specs/2026-11-01-la-deep-fast-design.md）。"""
    deep = mem_bytes > 0
    assert not (cap and deep) and not (lanes and cap)
    pll = PYNQ_Z2.sync_pll
    ratio = PYNQ_Z2_FAST_CAP_RATIO if lanes else PYNQ_Z2_CAP_RATIO if cap else 1
    n = 2 * ratio if lanes else 1                         # DDR: cap_clk の 1 周期に 2 サンプル
    return Design(
        name=name, board=PYNQ_Z2, top="pynq_z2_fprobe_top",
        sources=tuple(dict.fromkeys((
            *rtl(*_COMMON, "boards/pynq_z2/pynq_z2_pkg.vhd", "boards/pynq_z2/xilinx_mmcm.vhd",
                 "boards/pynq_z2/xilinx_la_iserdes.vhd", "boards/pynq_z2/xilinx_oddr.vhd",
                 "common/sdp_ram.vhd", "common/stream_slice.vhd", "hostlink/frame_mux.vhd",
                 "mem/axi_mem_port.vhd"),
            *core_sources(caps_package_path(name)),
            # zynq_frame_link は窓の定数（コアの生成物 ps_window_pkg）を使う。依存順に並べる（GHDL は並びの順に解析する）
            *rtl("hostlink/zynq_frame_link.vhd", "boards/pynq_z2/pynq_z2_fprobe_top.vhd")))),
        pin_groups=("clk125", "led", "pmod1", "la_in", "obs_jtag", "sync", "testgen", "ps_pjtag"),
        clocks=(PYNQ_Z2.input_clock(), PYNQ_Z2.sys_clock(), *((PYNQ_Z2.cap_clock(ratio),) if ratio > 1 else ())),
        block_designs=(RTL_ROOT / "boards" / "pynq_z2" / ("ps7_hp_bd.tcl" if deep else "ps7_bd.tcl"),),
        capabilities=caps.Capabilities(
            protocol_version=PROTOCOL_VERSION, board=PYNQ_Z2.name, design=name,
            sys_clock_hz=PYNQ_Z2.sync_pll.fout_hz, timebase_hz=PYNQ_Z2.sync_pll.fout_hz,
            host_link=caps.LINK_ETH, channels=(caps.CH_CONTROL, caps.CH_DAP, caps.CH_UART0),
            la_channels=LA_WIDTH, la_depth=0 if deep else PYNQ_Z2_LANE_WORDS * n if lanes else LA_DEPTH,
            la_sample_hz=pll.cap_hz(ratio) if cap else pll.fout_hz * n, cap_ratio=ratio,
            la_lanes=n, la_fine_pins=lanes, la_filter=not lanes, la_pulse=not lanes,
            la_lane_in_age=pynq_z2_iserdes_newest_age(n) if lanes else 0,
            mem_bytes=mem_bytes, la_stream_events=deep, la_stream_ps=deep, la_stream_gaps=deep, la_pack=deep, event_ring_bits=0 if deep else 10,
            outputs=PYNQ_Z2.outputs(), uart_flow=PYNQ_Z2.uart_flow(), la_inputs=PYNQ_Z2.la_inputs(),
            fpga_obs=True, sync=deep, la_testgen=cap or deep or lanes, event_byte_pack=True, sampler_check_max=16,
            la_stream_bytes_per_s=PYNQ_Z2_STREAM_BYTES_PER_S if deep else 0))


# 線（プロトコル）→ その線に使う Primer の製品デザイン。ホストが書き替えに使う（fprobe gen がホスト定数
# DESIGN_FOR_SWD / DESIGN_FOR_JTAG に出す）。SWD は標準（サンプラ・故障注入がある）を使う
PROTOCOL_DESIGNS = {"swd": "fprobe", "jtag": "fprobe_jtag"}
# 機能 → その機能のために選ぶ Primer の製品デザイン（U22。fprobe gen がホスト定数 DESIGN_FOR_<機能> に出す）。
# 標準に入りきらない機能の構成。ホストは SWD の接続でこの構成から標準に戻さない（機能のために選んだ構成なので）
FEATURE_DESIGNS = {"la_inputs": "fprobe_la", "la_stream_events": "fprobe_la"}

DESIGNS: dict[str, Design] = {
    # PYNQ-Z2（Zynq-7020）: 同じ fprobe_core を、PS の Linux の中継（UDP、Ethernet 版と同じ線上の形式）越しに使う。
    # Primer では構成に分けている機能（JTAG・サンプラ・故障注入・専用の入力・RTS/CTS）を 1 つに入れる。ロジアナは内蔵 BRAM
    # （docs/superpowers/specs/2026-10-19-pynq-z2-port-design.md）
    "fprobe_pynq": _pynq("fprobe_pynq"),
    # P3: 深いバッファを PS の DDR に置く（S_AXI_HP0 と axi_mem_port。領域は Linux から外した上端の 32 MB）
    # （docs/superpowers/plans/2026-10-20-pynq-p3-deep-buffer.md）
    "fprobe_pynq_deep": _pynq("fprobe_pynq_deep", mem_bytes=PYNQ_Z2_DEEP_BYTES),
    # 内蔵ロジアナの取り込みを sys（100 MHz）の 3 倍の 300 MHz で動かす（時間分解能 10 → 3.33 ns。
    # docs/superpowers/specs/2026-10-30-la-capture-clock-design.md）
    "fprobe_pynq_cap": _pynq("fprobe_pynq_cap", cap=True),
    # 拡張ヘッダのピン（PMOD JA、ch 0〜7）と専用の入力（PMOD JB、ch 10〜15）を端子の ISERDESE2 で取り、sys の 1 周期に 8 サンプル
    # （800 MS/s、1.25 ns。案 B: MMCM の VCO 1200 MHz、cap_clk 400 MHz の両方の辺）。内蔵バッファ 16384 語 = 131072 サンプル（164 µs）。
    # グリッチフィルタ・パルス幅は無し（docs/superpowers/specs/2026-11-01-la-resolution-design.md の段 1・段 2）
    "fprobe_pynq_fast": _pynq("fprobe_pynq_fast", lanes=True),
    # fprobe_pynq_fast と同じ端子の ISERDESE2（800 MS/s、1.25 ns、14 本）を、深いバッファの垂れ流しに N = 8 並列で符号にする
    # （RAW: サンプルの間隔と ch の詰め、RLE: 1 周期に最大 8 レコード）。内蔵バッファ・トリガ取り込みは無い
    # （docs/superpowers/specs/2026-11-01-la-deep-fast-design.md）
    "fprobe_pynq_deep_fast": _pynq("fprobe_pynq_deep_fast", mem_bytes=PYNQ_Z2_DEEP_BYTES, lanes=True),
    # PYNQ-Z2 の P0: LED0 を約 1 Hz で点滅させるだけ（PMOD の端子は口に持たない = 引き下げのまま。安全な状態）
    "pynq_blinky": Design(
        name="pynq_blinky", board=PYNQ_Z2, top="pynq_z2_blinky_top",
        sources=tuple(rtl("boards/pynq_z2/pynq_z2_blinky_top.vhd")),
        pin_groups=("clk125", "led"), clocks=(PYNQ_Z2.input_clock(),)),
    # 対象の FPGA の観測の実機の確かめ（O1・O2）: Primer を「観測される対象」にする（target_ip/fprobe_obs の例）。
    # J12 の 5〜8 番が JTAG（obs_soft_tap）、9 番がカウンタの 12 ビット（fprobe のロジアナで同じ辺を見る）
    "obs_target_primer": Design(
        name="obs_target_primer", board=PRIMER20K, top="primer20k_obs_counter_top",
        sources=tuple(target_ip("fprobe_obs/rtl/obs_async_fifo.vhd", "fprobe_obs/rtl/obs_core.vhd",
                                "fprobe_obs/rtl/obs_soft_tap.vhd",
                                "fprobe_obs/examples/primer20k_counter/primer20k_obs_counter_top.vhd")),
        pin_groups=("clk27", "led", "obs_target"),
        clocks=(PRIMER20K.input_clock(), ClockConstraint("obs_tck", "obs_tck", "port", 30_000_000))),
    # Primer の fprobe が外からデバッグする側（Zynq DUT。docs/superpowers/specs/2026-10-24-zynq-dut-environment-design.md）:
    # PS（Linux のアプリ）が AXI-Lite でサンプル生成 + FIFO を読み、注目信号を PMOD JB に出す。PS の DAP の PJTAG は素通し
    "zynq_dut": Design(
        name="zynq_dut", board=PYNQ_Z2, top="pynq_z2_dut_top",
        sources=tuple(dict.fromkeys(rtl(*_COMMON, "boards/pynq_z2/pynq_z2_pkg.vhd", "boards/pynq_z2/xilinx_mmcm.vhd",
                                        "designs/dut_stream_core.vhd", "designs/dut_pins.vhd", "designs/dut_axil_slave.vhd",
                                        "designs/dut_axil_core.vhd", "boards/pynq_z2/pynq_z2_dut_top.vhd"))),
        pin_groups=("clk125", "led", "dut_dbg", "dut_rpi", "ps_pjtag"),
        clocks=(PYNQ_Z2.input_clock(), PYNQ_Z2.sys_clock()),
        block_designs=(RTL_ROOT / "boards" / "pynq_z2" / "ps7_bd.tcl",),
        ps_axi=True),
    "bringup": Design(
        name="bringup", board=PRIMER20K, top="primer20k_bringup_top",
        sources=tuple(rtl(*_COMMON, *_SERIAL, *_PRIMER20K, "designs/bringup_core.vhd",
                          "boards/primer20k/primer20k_bringup_top.vhd")),
        pin_groups=("clk27", "led", "uart"),
        clocks=(PRIMER20K.input_clock(), PRIMER20K.sys_clock())),
    # ULPI PHY との疎通確認（レジスタ R/W を UART で報告）
    "ulpi_probe": Design(
        name="ulpi_probe", board=PRIMER20K, top="primer20k_ulpi_probe_top",
        sources=tuple(rtl(*_COMMON, *_SERIAL, *_REPORT, "usb/ulpi_link.vhd",
                          "designs/ulpi_probe_core.vhd",
                          "boards/primer20k/primer20k_ulpi_reset.vhd",
                          "boards/primer20k/primer20k_ulpi_probe_top.vhd")),
        pin_groups=("clk27", "led", "uart", "ulpi"),
        clocks=(PRIMER20K.input_clock(), PRIMER20K.ulpi_clock()),
        io_delays=PRIMER20K.ulpi_io_delays()),
    # USB HS デバイスの実機試験（M2）: 列挙・ループバック・スループット
    "usbtest": Design(
        name="usbtest", board=PRIMER20K, top="primer20k_usbtest_top",
        sources=tuple(rtl(*_COMMON, *_SERIAL, *_REPORT, *_PRIMER20K, *_USB, "common/async_fifo.vhd",
                          "boards/primer20k/primer20k_ulpi_reset.vhd",
                          "designs/usbtest_responder.vhd",
                          "boards/primer20k/primer20k_usbtest_top.vhd")),
        pin_groups=("clk27", "led", "ulpi", "uart_tx"),
        clocks=(PRIMER20K.input_clock(), PRIMER20K.sys_clock(), PRIMER20K.ulpi_clock()),
        io_delays=PRIMER20K.ulpi_io_delays()),
    # 製品デザイン: USB HS ホストリンク + fprobe_core。Primer に全部は入らないので構成に分け、ホストがターゲット・要る
    # 機能に合わせて書き替える（B22: docs/superpowers/specs/2026-10-18-b22-fpga-area-design.md §4）
    "fprobe": _primer_usb("fprobe", jtag=False),                       # 標準: SWD・サンプラ・故障注入・すべての解析
    "fprobe_jtag": _primer_usb("fprobe_jtag", sampler=False, inject=False),   # JTAG（と SWD）。サンプラ・故障注入なし
    # モジュール化の段階 0（docs/superpowers/specs/2026-10-22-modular-design.md）: メインボードの中心の機能（案 M）だけ。
    # SWD・JTAG・サンプラ・ロジアナ（深いバッファ）・SWO（NRZ）・UART・クロストリガを残し、故障注入・多段トリガ・
    # グリッチフィルタ・パルス幅のトリガ・Manchester の SWO・補助の受け口を外す（サブボードに出せる候補の分の大きさを測る）
    "fprobe_core_m": _primer_usb("fprobe_core_m", inject=False, trig_seq=False, la_filter=False, la_pulse=False,
                                 swo_manchester=False),
    # ロジアナの専用の入力（J14 の 6 本、U22）: 標準に足すと sys のタイミングを満たさない（2026-10-19）ので、サンプラ・
    # 故障注入を外した SWD の構成に入れる（docs/superpowers/specs/2026-10-19-u22-la-inputs-design.md）
    # ストリームにイベントも流す（R1）のも、標準の余裕が無いのでこの構成（docs/superpowers/specs/2026-10-19-r1-unified-stream-design.md）
    "fprobe_la": _primer_usb("fprobe_la", jtag=False, sampler=False, inject=False, la_inputs=PRIMER20K.la_inputs(),
                             la_stream_events=True, sync=True),
    # 基板立ち上げ用: fprobe の深いバッファ・イベント記録の代わりにメモリ試験回路（DDR3 の自己試験・帯域測定）
    "memtest": Design(
        name="memtest", board=PRIMER20K, top="primer20k_fprobe_top",
        sources=tuple(dict.fromkeys((          # 重複を除く（コアと共通部品が同じファイルを含む）
            *rtl(*_COMMON, *_PRIMER20K, *_USB, "common/async_fifo.vhd", "common/cdc_value.vhd",
                 "boards/primer20k/primer20k_ulpi_reset.vhd"),
            *core_sources(caps_package_path("memtest")),
            *rtl("boards/primer20k/primer20k_ddr3.vhd", "boards/primer20k/primer20k_fprobe_top.vhd")))),
        pin_groups=("clk27", "led", "ulpi", "pmod1", "ddr3", "aux", "la_in", "sync"),   # トップが同じ（入力は使わない）
        clocks=(PRIMER20K.input_clock(), PRIMER20K.sys_clock(), PRIMER20K.ulpi_clock(), *PRIMER20K.mem_clocks()),
        ips=(PRIMER20K.ddr3.controller,),
        io_delays=PRIMER20K.ulpi_io_delays(),
        placements=PRIMER20K.ulpi_placements("device_i/link_i"),
        cdc_max_delay_ns=8.0,                             # 最短の周期（sys 9.97 ns）より短く
        options=("-place_option 1 -route_option 1",),     # sys（100 MHz）: 既定では余裕 0.1% 未満、これで約 3%
        capabilities=caps.Capabilities(
            protocol_version=PROTOCOL_VERSION, board=PRIMER20K.name, design="memtest",
            sampler_match=False,                    # R21 は Primer では外す
            sys_clock_hz=PRIMER20K.sync_pll.fout_hz, timebase_hz=PRIMER20K.sync_pll.fout_hz,
            host_link=caps.LINK_USB_HS_ULPI, channels=(caps.CH_CONTROL, caps.CH_DAP, caps.CH_UART0),
            mem_bytes=PRIMER20K.ddr3.size_bytes, mem_test=True, la_filter=False, la_pulse=False)),         # ロジアナ無し（外部メモリは試験に使う）
    # M11 の移植性の確認: 同じ fprobe_core を SerialLink（BL702 の USB-UART）で使う。外部メモリなし、内蔵ロジアナ
    "fprobe_serial": Design(
        name="fprobe_serial", board=PRIMER20K, top="primer20k_fprobe_serial_top",
        sources=tuple(dict.fromkeys((
            *rtl(*_COMMON, *_PRIMER20K, "serial/uart_rx.vhd", "serial/uart_tx.vhd", "common/sdp_ram.vhd", "common/stream_slice.vhd", "hostlink/frame_mux.vhd", "hostlink/serial_link.vhd"),
            *core_sources(caps_package_path("fprobe_serial")),
            *rtl("designs/serial_fprobe.vhd", "boards/primer20k/primer20k_fprobe_serial_top.vhd")))),
        pin_groups=("clk27", "led", "uart", "pmod1", "aux"),
        clocks=(PRIMER20K.input_clock(), PRIMER20K.sys_clock()),
        cdc_max_delay_ns=8.0,
        options=("-place_option 1 -route_option 1",),
        capabilities=caps.Capabilities(
            protocol_version=PROTOCOL_VERSION, board=PRIMER20K.name, design="fprobe_serial",
            sampler_match=False,                    # R21 は Primer では外す（USB の構成と同じく配線が混む）
            sys_clock_hz=PRIMER20K.sync_pll.fout_hz, timebase_hz=PRIMER20K.sync_pll.fout_hz,
            host_link=caps.LINK_SERIAL, channels=(caps.CH_CONTROL, caps.CH_DAP, caps.CH_UART0),
            la_channels=LA_WIDTH, la_depth=LA_DEPTH, la_sample_hz=PRIMER20K.sync_pll.fout_hz, aux=True,
            event_ring_bits=10)),                       # イベント記録を BSRAM に 1024 件（外部メモリ無し）
    # M11: 製品デザインと同じ fprobe_core・DDR3 を、USB の代わりに Ethernet（RTL8201F、UDP）で使う
    "fprobe_eth": Design(
        name="fprobe_eth", board=PRIMER20K, top="primer20k_fprobe_eth_top",
        sources=tuple(dict.fromkeys((
            *rtl(*_COMMON, *_PRIMER20K, *_REPORT, "common/async_fifo.vhd", "common/sdp_ram.vhd",
                 "boards/primer20k/primer20k_ulpi_reset.vhd",
                 "eth/eth_pkg.vhd", "eth/rmii_rx.vhd", "eth/rmii_tx.vhd", "eth/eth_rx_header.vhd", "eth/arp_rx.vhd",
                 "eth/ipv4_rx.vhd", "eth/icmp_rx.vhd", "eth/udp_rx.vhd", "eth/udp_stack.vhd", "common/stream_slice.vhd",
                 "hostlink/frame_mux.vhd",
                 "hostlink/eth_link.vhd"),
            *core_sources(caps_package_path("fprobe_eth")),
            *rtl("boards/primer20k/primer20k_ddr3.vhd", "boards/primer20k/primer20k_eth_diag.vhd", "boards/primer20k/primer20k_fprobe_eth_top.vhd")))),
        pin_groups=("clk27", "led", "eth", "pmod1", "ddr3", "aux", "uart_tx"),
        clocks=(PRIMER20K.input_clock(), PRIMER20K.sys_clock(), PRIMER20K.eth_clock(), *PRIMER20K.mem_clocks()),
        ips=(PRIMER20K.ddr3.controller,),
        io_delays=PRIMER20K.eth_io_delays(),
        cdc_max_delay_ns=8.0,
        options=("-place_option 1 -route_option 1",),
        capabilities=caps.Capabilities(
            protocol_version=PROTOCOL_VERSION, board=PRIMER20K.name, design="fprobe_eth",
            sampler_match=False,                    # R21 は Primer では外す（USB の構成と同じく配線が混む）
            sys_clock_hz=PRIMER20K.sync_pll.fout_hz, timebase_hz=PRIMER20K.sync_pll.fout_hz,
            host_link=caps.LINK_ETH, channels=(caps.CH_CONTROL, caps.CH_DAP, caps.CH_UART0),
            la_channels=LA_WIDTH, la_depth=0, la_sample_hz=PRIMER20K.sync_pll.fout_hz,
            mem_bytes=PRIMER20K.ddr3.size_bytes, aux=True)),
    # M11: Tang Nano 9K（GW1NR-9、LUT 約 8640）。同じ fprobe_core を、クロストリガと多段トリガを外して載せる
    # （ケイパビリティ FEATURES で申告）。ビルドだけ（9K はユーザーの別の検討で使用中で書き込まない）
    "fprobe_nano9k": Design(
        name="fprobe_nano9k", board=NANO9K, top="nano9k_fprobe_top",
        sources=tuple(dict.fromkeys((
            *rtl(*_COMMON, "boards/nano9k/nano9k_pkg.vhd", "boards/primer20k/gowin_rpll.vhd",
                 "serial/uart_rx.vhd", "serial/uart_tx.vhd", "common/sdp_ram.vhd", "common/stream_slice.vhd", "hostlink/frame_mux.vhd", "hostlink/serial_link.vhd"),
            *core_sources(caps_package_path("fprobe_nano9k")),
            *rtl("designs/serial_fprobe.vhd", "boards/nano9k/nano9k_fprobe_top.vhd")))),
        pin_groups=("clk27", "led", "uart", "pmod1"),
        clocks=(NANO9K.input_clock(), NANO9K.sys_clock()),
        cdc_max_delay_ns=8.0,
        capabilities=caps.Capabilities(
            protocol_version=PROTOCOL_VERSION, board=NANO9K.name, design="fprobe_nano9k",
            sys_clock_hz=NANO9K.sync_pll.fout_hz, timebase_hz=NANO9K.sync_pll.fout_hz,
            host_link=caps.LINK_SERIAL, channels=(caps.CH_CONTROL, caps.CH_DAP, caps.CH_UART0),
            la_channels=LA_WIDTH, la_depth=LA_DEPTH, la_sample_hz=NANO9K.sync_pll.fout_hz,
            xtrig=False, trig_seq=False, inject=False, la_filter=False, la_pulse=False)),
}

assert all(DESIGNS[d].capabilities.jtag or p == "swd" for p, d in PROTOCOL_DESIGNS.items())
assert all(getattr(DESIGNS[d].capabilities, f) for f, d in FEATURE_DESIGNS.items())
