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
from .rtl import CORE_GENERATED, RTL_ROOT, rtl
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

_CORE = ("common/stream_slice.vhd", "core/csr_decoder.vhd", "core/control_plane.vhd", "core/timebase.vhd", "core/caps_rom.vhd",
         "common/udiv.vhd", "common/udiv_shared.vhd", "debug/generated/dap_info_pkg.vhd", "debug/swd_phy.vhd", "debug/jtag_phy.vhd", "debug/debug_port.vhd",
         "debug/dap_pkg.vhd", "debug/dap_packet.vhd", "debug/dap_link.vhd", "debug/dap_cmd_config.vhd",
         "debug/dap_cmd_swo.vhd", "debug/dap_cmd_sequence.vhd", "debug/dap_cmd_transfer.vhd", "debug/dap_processor.vhd", "debug/swd_injector.vhd", "debug/dap_sampler.vhd", "debug/dap_monitor.vhd",
         "serial/uart_tx.vhd", "serial/uart_rx.vhd", "serial/uart_bridge.vhd", "debug/swo_manchester_rx.vhd", "debug/swo_receiver.vhd",
         "la/la_filter.vhd", "la/la_pulse.vhd", "la/la_front.vhd", "la/la_capture.vhd", "la/la_mem_csr.vhd", "common/cdc_value.vhd", "mem/mem_test.vhd", "mem/mem_test_sys.vhd", "common/async_fifo.vhd",
         "common/stream_unpack.vhd", "common/sync_fifo.vhd", "la/la_encoder.vhd", "mem/la_mem_writer.vhd",
         "mem/mem_reader.vhd", "la/la_deep_buffer.vhd", "common/sdp_ram.vhd", "la/event_ring.vhd",
         "core/trig_seq.vhd", "core/xtrig_out.vhd", "core/pin_select.vhd", "core/csr_absent.vhd", "core/aux_link.vhd", "core/fprobe_core.vhd")


def core_sources(caps_package: Path) -> list[Path]:
    """fprobe_core とその依存（依存順）。caps_package はデザインのケイパビリティ生成パッケージ。"""
    return [*rtl("common/util_pkg.vhd", "common/sync_bit.vhd", "core/csr_bus_pkg.vhd"),
            CORE_GENERATED / "csr_map_pkg.vhd", build_info.DEFAULT_PACKAGE,
            *(CORE_GENERATED / f"csr_{b.name}.vhd" for b in FPROBE_MAP.blocks if not b.external),
            caps_package,
            *rtl(*_CORE)]


def _primer_usb(name: str, *, la_inputs: int = 0, **features) -> Design:
    """Primer 20K の USB の製品デザイン（トップ・ピン・IP は共通。features はケイパビリティの機能の取捨、la_inputs は
    J14 の専用の入力を使う本数（U22。0 なら使わない））。"""
    return Design(
        name=name, board=PRIMER20K, top="primer20k_fprobe_top",
        sources=tuple(dict.fromkeys((          # 重複を除く（コアと共通部品が同じファイルを含む）
            *rtl(*_COMMON, *_PRIMER20K, *_USB, "common/async_fifo.vhd", "common/cdc_value.vhd",
                 "boards/primer20k/primer20k_ulpi_reset.vhd"),
            *core_sources(caps_package_path(name)),
            *rtl("boards/primer20k/primer20k_ddr3.vhd", "boards/primer20k/primer20k_fprobe_top.vhd")))),
        pin_groups=("clk27", "led", "ulpi", "pmod1", "ddr3", "aux", "la_in"),
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


PYNQ_Z2_DEEP_BYTES = 32 * 1024 * 1024        # 深いバッファの領域（DDR の 0x1E000000〜。host/pynq/uEnv.txt の mem=480M）


def _pynq(name: str, *, mem_bytes: int = 0) -> Design:
    """PYNQ-Z2（Zynq-7020）: 同じ fprobe_core を、PS の Linux の中継（UDP、Ethernet 版と同じ線上の形式）越しに使う。
    Primer では構成に分けている機能（JTAG・サンプラ・故障注入・専用の入力・RTS/CTS）を 1 つに入れる
    （docs/superpowers/specs/2026-10-19-pynq-z2-port-design.md）。mem_bytes > 0 なら深いバッファを PS の DDR に置き
    （ブロック設計 ps7_hp_bd）、内蔵ロジアナは持たない。0 なら内蔵 BRAM のロジアナ（ps7_bd）。"""
    deep = mem_bytes > 0
    return Design(
        name=name, board=PYNQ_Z2, top="pynq_z2_fprobe_top",
        sources=tuple(dict.fromkeys((
            *rtl(*_COMMON, "boards/pynq_z2/pynq_z2_pkg.vhd", "boards/pynq_z2/xilinx_mmcm.vhd",
                 "common/sdp_ram.vhd", "common/stream_slice.vhd", "hostlink/frame_mux.vhd",
                 "hostlink/zynq_frame_link.vhd", "mem/axi_mem_port.vhd"),
            *core_sources(caps_package_path(name)),
            *rtl("boards/pynq_z2/pynq_z2_fprobe_top.vhd")))),
        pin_groups=("clk125", "led", "pmod1", "la_in"),
        clocks=(PYNQ_Z2.input_clock(), PYNQ_Z2.sys_clock()),
        block_designs=(RTL_ROOT / "boards" / "pynq_z2" / ("ps7_hp_bd.tcl" if deep else "ps7_bd.tcl"),),
        capabilities=caps.Capabilities(
            protocol_version=PROTOCOL_VERSION, board=PYNQ_Z2.name, design=name,
            sys_clock_hz=PYNQ_Z2.sync_pll.fout_hz, timebase_hz=PYNQ_Z2.sync_pll.fout_hz,
            host_link=caps.LINK_ETH, channels=(caps.CH_CONTROL, caps.CH_DAP, caps.CH_UART0),
            la_channels=LA_WIDTH, la_depth=0 if deep else LA_DEPTH, la_sample_hz=PYNQ_Z2.sync_pll.fout_hz,
            mem_bytes=mem_bytes, la_stream_events=deep, event_ring_bits=0 if deep else 10,
            outputs=PYNQ_Z2.outputs(), uart_flow=PYNQ_Z2.uart_flow(), la_inputs=PYNQ_Z2.la_inputs()))


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
    # PYNQ-Z2 の P0: LED0 を約 1 Hz で点滅させるだけ（PMOD の端子は口に持たない = 引き下げのまま。安全な状態）
    "pynq_blinky": Design(
        name="pynq_blinky", board=PYNQ_Z2, top="pynq_z2_blinky_top",
        sources=tuple(rtl("boards/pynq_z2/pynq_z2_blinky_top.vhd")),
        pin_groups=("clk125", "led"), clocks=(PYNQ_Z2.input_clock(),)),
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
    # ロジアナの専用の入力（J14 の 6 本、U22）: 標準に足すと sys のタイミングを満たさない（2026-10-19）ので、サンプラ・
    # 故障注入を外した SWD の構成に入れる（docs/superpowers/specs/2026-10-19-u22-la-inputs-design.md）
    # ストリームにイベントも流す（R1）のも、標準の余裕が無いのでこの構成（docs/superpowers/specs/2026-10-19-r1-unified-stream-design.md）
    "fprobe_la": _primer_usb("fprobe_la", jtag=False, sampler=False, inject=False, la_inputs=PRIMER20K.la_inputs(),
                             la_stream_events=True),
    # 基板立ち上げ用: fprobe の深いバッファ・イベント記録の代わりにメモリ試験回路（DDR3 の自己試験・帯域測定）
    "memtest": Design(
        name="memtest", board=PRIMER20K, top="primer20k_fprobe_top",
        sources=tuple(dict.fromkeys((          # 重複を除く（コアと共通部品が同じファイルを含む）
            *rtl(*_COMMON, *_PRIMER20K, *_USB, "common/async_fifo.vhd", "common/cdc_value.vhd",
                 "boards/primer20k/primer20k_ulpi_reset.vhd"),
            *core_sources(caps_package_path("memtest")),
            *rtl("boards/primer20k/primer20k_ddr3.vhd", "boards/primer20k/primer20k_fprobe_top.vhd")))),
        pin_groups=("clk27", "led", "ulpi", "pmod1", "ddr3", "aux", "la_in"),   # トップが同じ（入力は使わない）
        clocks=(PRIMER20K.input_clock(), PRIMER20K.sys_clock(), PRIMER20K.ulpi_clock(), *PRIMER20K.mem_clocks()),
        ips=(PRIMER20K.ddr3.controller,),
        io_delays=PRIMER20K.ulpi_io_delays(),
        placements=PRIMER20K.ulpi_placements("device_i/link_i"),
        cdc_max_delay_ns=8.0,                             # 最短の周期（sys 9.97 ns）より短く
        options=("-place_option 1 -route_option 1",),     # sys（100 MHz）: 既定では余裕 0.1% 未満、これで約 3%
        capabilities=caps.Capabilities(
            protocol_version=PROTOCOL_VERSION, board=PRIMER20K.name, design="memtest",
            sys_clock_hz=PRIMER20K.sync_pll.fout_hz, timebase_hz=PRIMER20K.sync_pll.fout_hz,
            host_link=caps.LINK_USB_HS_ULPI, channels=(caps.CH_CONTROL, caps.CH_DAP, caps.CH_UART0),
            mem_bytes=PRIMER20K.ddr3.size_bytes, mem_test=True, la_filter=False, la_pulse=False)),         # ロジアナ無し（外部メモリは試験に使う）
    # M11 の移植性の確認: 同じ fprobe_core を SerialLink（BL702 の USB-UART）で使う。外部メモリなし、内蔵ロジアナ
    "fprobe_serial": Design(
        name="fprobe_serial", board=PRIMER20K, top="primer20k_fprobe_serial_top",
        sources=tuple(dict.fromkeys((
            *rtl(*_COMMON, *_PRIMER20K, "serial/uart_rx.vhd", "serial/uart_tx.vhd", "common/sdp_ram.vhd", "common/stream_slice.vhd", "hostlink/frame_mux.vhd", "hostlink/serial_link.vhd"),
            *core_sources(caps_package_path("fprobe_serial")),
            *rtl("boards/primer20k/primer20k_fprobe_serial_top.vhd")))),
        pin_groups=("clk27", "led", "uart", "pmod1", "aux"),
        clocks=(PRIMER20K.input_clock(), PRIMER20K.sys_clock()),
        cdc_max_delay_ns=8.0,
        options=("-place_option 1 -route_option 1",),
        capabilities=caps.Capabilities(
            protocol_version=PROTOCOL_VERSION, board=PRIMER20K.name, design="fprobe_serial",
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
                 "eth/rmii_rx.vhd", "eth/rmii_tx.vhd", "eth/udp_stack.vhd", "common/stream_slice.vhd", "hostlink/frame_mux.vhd",
                 "hostlink/eth_link.vhd"),
            *core_sources(caps_package_path("fprobe_eth")),
            *rtl("boards/primer20k/primer20k_ddr3.vhd", "boards/primer20k/primer20k_fprobe_eth_top.vhd")))),
        pin_groups=("clk27", "led", "eth", "pmod1", "ddr3", "aux", "uart_tx"),
        clocks=(PRIMER20K.input_clock(), PRIMER20K.sys_clock(), PRIMER20K.eth_clock(), *PRIMER20K.mem_clocks()),
        ips=(PRIMER20K.ddr3.controller,),
        io_delays=PRIMER20K.eth_io_delays(),
        cdc_max_delay_ns=8.0,
        options=("-place_option 1 -route_option 1",),
        capabilities=caps.Capabilities(
            protocol_version=PROTOCOL_VERSION, board=PRIMER20K.name, design="fprobe_eth",
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
            *rtl("boards/nano9k/nano9k_fprobe_top.vhd")))),
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
