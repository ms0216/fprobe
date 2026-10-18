"""ボード定義（デバイス・JTAG 識別・ピン制約・クロック）。

ボード固有の RTL（トップ entity・定数パッケージ・ベンダープリミティブのラッパ）は
gateware/rtl/boards/<board>/ にあり、ここはツール側から見たボードの記述。
クロック制約はボードが部品として提供し、どれを使うかは各デザインが決める。
"""

from dataclasses import dataclass

from ..toolchain.pll import PllSolution
from ..toolchain.programmer import JtagIdentity
from ..toolchain.project import ClockConstraint, GowinIp, IoDelay, Pin

__all__ = ["ETH_REF_CLK_PORT", "MEM_CLK_NET", "MEMORY_CLK_NET", "SYS_CLK_NET", "ULPI_CLK_PORT", "Board", "Ddr3",
           "Ethernet", "UlpiTiming"]

# トップ entity の信号名の規約（SDC がこの名前を参照する）
SYS_CLK_NET = "sys_clk"        # PLL 出力のシステムクロック
ULPI_CLK_PORT = "ulpi_clk"     # ULPI PHY の CLKOUT（入力ポート）。usb ドメインのクロック
MEM_CLK_NET = "mem_clk"        # メモリコントローラのユーザー側クロック（mem ドメイン）
MEMORY_CLK_NET = "ddr3_memory_clk"   # DDR3 のメモリクロック（メモリ用 PLL の出力）
ETH_REF_CLK_PORT = "eth_ref_clk"     # RMII の REF_CLK（PHY が出す 50 MHz、入力ポート）

ULPI_CLK_HZ = 60_000_000
ETH_REF_CLK_HZ = 50_000_000


@dataclass(frozen=True)
class UlpiTiming:
    """ULPI PHY のインターフェースタイミング（PHY が CLKOUT を出す 60 MHz 出力クロックモード）。

    出典は PHY のデータシート。基板配線の遅延は数十 ps なので含めない。
    """
    output_min_ns: float             # CLKOUT 立ち上がり → DIR/NXT/DATA 出力
    output_max_ns: float
    setup_ns: float                  # STP/DATA 入力のセットアップ（CLKOUT 立ち上がりに対して）
    hold_ns: float


@dataclass(frozen=True)
class Ddr3:
    """ボードの DDR3（サンプルバッファ）。コントローラは Gowin の DDR3 IP（ボード固有の RTL ラッパの裏）。"""
    memory_pll: PllSolution          # 入力クロック → メモリクロック（DDR のビットレートの半分）
    clock_ratio: int                 # メモリクロック / ユーザー側クロック（mem ドメイン）
    size_bytes: int
    controller: GowinIp

    @property
    def user_clock_hz(self) -> int:
        return self.memory_pll.fout_hz // self.clock_ratio


@dataclass(frozen=True)
class Ethernet:
    """ボードの Ethernet PHY（RMII、PHY が REF_CLK を出す）と、EthernetLink の既定のアドレス。
    RMII のタイミング（RMII 1.2）: PHY の出力は REF_CLK の立ち上がりから 2〜14 ns、PHY の入力はセットアップ 4 ns・
    ホールド 2 ns。"""
    mac: int                         # 48 ビット（ローカル管理アドレス）
    ip: tuple[int, int, int, int]
    udp_port: int

    @property
    def ip_value(self) -> int:
        return int.from_bytes(bytes(self.ip), "big")


@dataclass(frozen=True)
class Board:
    name: str
    part: str
    family: str
    revision: int
    jtag_identity: JtagIdentity
    pin_groups: dict[str, tuple[Pin, ...]]   # 資源名 → ピン（デザインは使う資源名を選ぶ）
    input_clock_port: str
    input_clock_hz: int
    sync_pll: PllSolution
    ulpi_timing: UlpiTiming | None = None    # ULPI PHY を載せたボードのみ
    debug_connector: dict[str, int] | None = None   # デバッグコネクタの信号 → pmod の番号
    ddr3: Ddr3 | None = None
    options: tuple[str, ...] = ()
    # ULPI の出力レジスタ（ulpi_link の data_q<n> / stp_q）を置く、ピンの隣の領域（R[a:b]C[c:d]）。
    # 出力イネーブルが組み合わせなので I/O ブロックに詰められず、配置次第で出力の余裕が無くなるため
    ulpi_output_regions: dict[str, str] | None = None
    serial_baud: int | None = None           # SerialLink のボーレート（USB-UART ブリッジがある場合）
    eth: Ethernet | None = None              # Ethernet PHY を載せたボードのみ
    # クロストリガの動作「出力の端子」の端子の候補 j に使うデバッグコネクタの信号（debug_connector の名前）。
    # RTL の定数（<board>_pkg の XOUT_PIN_CHOICES）と一致させる
    output_pins: tuple[str, ...] = ()
    # UART の RTS/CTS（U52）の端子の候補 j に使うデバッグコネクタの信号（debug_connector の名前）。RTS・CTS のどちらにも
    # 使える。RTL の定数（<board>_pkg の UART_FLOW_PIN_CHOICES）と一致させる
    uart_flow_pins: tuple[str, ...] = ()

    def uart_flow(self) -> tuple[int, ...]:
        """RTS/CTS の端子の候補 j のロジアナのチャンネル（= pmod1 の番号）。ケイパビリティ UART_FLOW の中身。"""
        return tuple(self.debug_connector[name] for name in self.uart_flow_pins)

    def outputs(self) -> tuple[tuple[int, bool], ...]:
        """端子の候補 j の (ロジアナのチャンネル = pmod1 の番号, プルアップがある)。ケイパビリティ OUTPUTS の中身。"""
        pmod = {pin.port: pin for pin in self.pin_groups.get("pmod1", ())}
        result = []
        for name in self.output_pins:
            index = self.debug_connector[name]
            result.append((index, pmod[f"pmod1[{index}]"].pull == "UP"))
        return tuple(result)

    def la_inputs(self) -> int:
        """ロジアナの専用の入力の数（資源 la_in のピンの数。U22）。ケイパビリティ LA_LAYOUT の入力の数。"""
        return len(self.pin_groups.get("la_in", ()))

    def pins_for(self, groups) -> tuple[Pin, ...]:
        unknown = [g for g in groups if g not in self.pin_groups]
        if unknown:
            raise KeyError(f"{self.name} に無いピン資源: {', '.join(unknown)}")
        return tuple(pin for g in groups for pin in self.pin_groups[g])

    def input_clock(self) -> ClockConstraint:
        return ClockConstraint(self.input_clock_port, self.input_clock_port, "port",
                               self.input_clock_hz)

    def sys_clock(self) -> ClockConstraint:
        # 入力クロックから PLL で作るが、入力クロックのドメインとの間で値を直接受け渡す回路は持たない
        # （受け渡しは同期化回路を通す）ので、非同期のクロックとして扱う
        return ClockConstraint(SYS_CLK_NET, SYS_CLK_NET, "net", self.sync_pll.fout_hz)

    def ulpi_clock(self) -> ClockConstraint:
        return ClockConstraint(ULPI_CLK_PORT, ULPI_CLK_PORT, "port", ULPI_CLK_HZ)

    def mem_clocks(self) -> tuple[ClockConstraint, ...]:
        """DDR3 のメモリクロック（メモリ用 PLL の出力）とユーザー側クロック（mem ドメイン）。互いに、また sys とも
        非同期として扱う（メモリクロック ⇄ ユーザー側クロックの経路は IP の中だけにあり、IP が受け渡しを保証する。
        Gowin の参考デザインもメモリクロックだけを制約している）。"""
        if self.ddr3 is None:
            raise ValueError(f"{self.name} には DDR3 が無い")
        return (ClockConstraint(MEMORY_CLK_NET, MEMORY_CLK_NET, "net", self.ddr3.memory_pll.fout_hz, internal=True),
                ClockConstraint(MEM_CLK_NET, MEM_CLK_NET, "net", self.ddr3.user_clock_hz))

    def eth_clock(self) -> ClockConstraint:
        return ClockConstraint(ETH_REF_CLK_PORT, ETH_REF_CLK_PORT, "port", ETH_REF_CLK_HZ)

    def eth_io_delays(self) -> tuple[IoDelay, ...]:
        if self.eth is None:
            raise ValueError(f"{self.name} には Ethernet PHY が無い")
        return (IoDelay(ETH_REF_CLK_PORT, "input", ("eth_crs_dv", "eth_rxd[*]"), 2.0, 14.0),
                IoDelay(ETH_REF_CLK_PORT, "output", ("eth_tx_en", "eth_txd[*]"), -2.0, 4.0))

    def ulpi_placements(self, link_path: str) -> tuple[str, ...]:
        """ulpi_link（インスタンスの階層名 link_path）の出力レジスタをピンの隣に置く制約。名前の末尾
        （合成が付ける _s<n>）は 1 文字のワイルドカードで受ける。"""
        if not self.ulpi_output_regions:
            return ()
        lines = []
        for k, (register, region) in enumerate(self.ulpi_output_regions.items()):
            lines.append(f'GROUP ulpi_out{k} = {{ "{link_path}/{register}_s?" }};')
            lines.append(f"GRP_LOC ulpi_out{k} {region};")
        return tuple(lines)

    def ulpi_io_delays(self) -> tuple[IoDelay, ...]:
        t = self.ulpi_timing
        if t is None:
            raise ValueError(f"{self.name} には ULPI PHY のタイミングが定義されていない")
        return (IoDelay(ULPI_CLK_PORT, "input", ("ulpi_dir", "ulpi_nxt", "ulpi_data[*]"),
                        t.output_min_ns, t.output_max_ns),
                IoDelay(ULPI_CLK_PORT, "output", ("ulpi_stp", "ulpi_data[*]"),
                        0.0 - t.hold_ns, t.setup_ns))   # -0.0 を書かない
