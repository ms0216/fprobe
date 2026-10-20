"""Gowin EDA のプロジェクト記述と、gw_sh 用 TCL / SDC / CST の生成。

Gowin はトップに存在しないポートへの制約をエラーにするため、物理制約（.cst）は
デザインが使うピンだけからビルド時に生成する。
"""

from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

__all__ = ["BuildInfo", "BuildStamp", "ClockConstraint", "GowinIp", "GowinProject", "IoDelay", "Pin", "render_cst",
           "render_sdc", "render_tcl"]


@dataclass(frozen=True)
class Pin:
    port: str                        # トップ entity のポート名（ベクタ要素は "led[0]"）
    location: str                    # パッケージのピン番号
    io_type: str = "LVCMOS33"
    pull: str | None = None          # "UP" / "DOWN" / "NONE"
    drive: int | None = None         # 出力電流（mA）
    bank_vccio: str | None = None    # バンクの I/O 電圧（"1.5" 等。SSTL など 3.3V 以外の規格で指定する）
    hysteresis: str | None = None    # 入力のヒステリシス（"NONE" / "L2H" / "H2L" / "HIGH"）


@dataclass(frozen=True)
class ClockConstraint:
    name: str
    target: str                      # ポート名またはネット名
    kind: Literal["port", "net", "pin"]   # pin: Vivado の自動の生成クロック（MMCM の出力の端子）に名前を付ける
    frequency_hz: int
    source: str = ""                 # 発振源。異なる発振源のクロック間は非同期として解析しない
                                     # （空なら name 自身が発振源）
    internal: bool = False           # IP の中だけで使うクロック（他と常に非同期のグループにする）

    @property
    def group(self) -> str:
        return self.source or self.name

    @property
    def period_ns(self) -> float:
        return 1e9 / self.frequency_hz


@dataclass(frozen=True)
class IoDelay:
    """クロックに同期した外部デバイスとの入出力遅延（SDC の set_input_delay / set_output_delay）。

    input: 外部デバイスがクロック端から出力を確定させるまでの遅延（min / max）。
    output: 外部デバイスのセットアップ時間（max）と、ホールド時間の符号反転（min）。
    """
    clock: str
    direction: Literal["input", "output"]
    ports: tuple[str, ...]           # バスは "ulpi_data[*]"
    min_ns: float
    max_ns: float


@dataclass(frozen=True)
class GowinIp:
    """Gowin の IP コア（ベンダー IP）。ビルド時に gw_sh の create_ipc / set_property / generate_target で生成する。

    config は CONFIG.<名前> に設定する値。値の範囲が他の値に依存する（例: CLK_Ratio で Memory_Clock の上限が
    変わる）ので、この順に設定する。生成物は Verilog（暗号化）で、プロジェクトに追加される。

    prebuilt を与えると生成せず、Gowin のインストール先（home）からの相対パスにある生成済みのネットリストを使う
    （現行版の生成器では動かない構成のため。config はその IP の設定の記録）。
    """
    module: str                      # 生成するモジュール名（インスタンス化する名前）
    kind: str                        # IP の種類（create_ipc -name。例 "ddr3"）
    config: tuple[tuple[str, str], ...]
    prebuilt: str | None = None      # Gowin のインストール先からの相対パス

    def directory(self, build_dir: Path) -> Path:
        return Path(build_dir) / "ip" / self.module

    def source(self, build_dir: Path) -> Path:
        # create_project -name <module> -dir . がディレクトリ <module>/ を作り、その src/<module>/ に生成する
        return self.directory(build_dir) / self.module / "src" / self.module / f"{self.module}.v"

    def render_tcl(self, part: str, device_version: str) -> str:
        lines = [f"create_project -name {self.module} -dir . -pn {part} -device_version {device_version} -force",
                 f"create_ipc -name {self.kind} -module_name {self.module}",
                 f"set ip [get_ips {self.module}]"]
        lines += [f"set_property -name CONFIG.{key} -value {{{value}}} $ip" for key, value in self.config]
        lines.append("generate_target $ip")
        return "\n".join(lines) + "\n"


@dataclass(frozen=True)
class BuildInfo:
    """1 回のビルドの版: 入力の要約（inputs.sha256 と同じ。同じ入力なら同じ）・ビルドした時刻・git のコミット。"""
    digest: str                      # 入力の SHA-256（16 進 64 文字）
    time: int                        # ビルドした時刻（UNIX 秒）
    git: str | None                  # リポジトリの HEAD（16 進。取れなければ None）
    dirty: bool                      # ソースのディレクトリにコミットしていない変更があった


@dataclass(frozen=True)
class BuildStamp:
    """ビルドのたびに変わる値（版）を VHDL に入れる仕組み。

    source はリポジトリの既定の VHDL パッケージ（版なし。シミュレーションはこれを使う）で、プロジェクトの sources に
    含める。ビルドでは、その代わりにビルドディレクトリに render(BuildInfo) で書いた同名のファイルを使う。版は入力の
    要約に含めない（要約そのものが版に入るため。同じ入力なら作り直さず、前の版のビットストリームをそのまま使う）。
    repo は git の状態を調べるディレクトリ。"""
    source: Path
    render: Callable[[BuildInfo], str]
    repo: Path

    def target(self, build_dir: Path) -> Path:
        return Path(build_dir) / self.source.name


@dataclass(frozen=True)
class GowinProject:
    name: str                        # 成果物名（<name>.fs）
    part: str
    family: str
    top: str                         # トップ entity 名
    sources: tuple[Path, ...]        # VHDL ソース（順不同。Gowin が依存関係を解決する）
    pins: tuple[Pin, ...]            # 使うピン（.cst を生成する）
    clocks: tuple[ClockConstraint, ...] = ()
    io_delays: tuple[IoDelay, ...] = ()
    options: tuple[str, ...] = ()    # 追加の set_option 引数
    ips: tuple[GowinIp, ...] = ()    # ビルド時に生成するベンダー IP
    placements: tuple[str, ...] = ()  # .cst に足す配置制約の行（GROUP / GRP_LOC / INS_LOC）
    # クロックまたぎ（発振源の違うクロック間の経路）の最大遅延。None なら従来どおり非同期グループで解析から外す。
    # 指定すると、ファブリックのクロック間の経路を set_max_delay で縛り（同期化回路の多ビットのずれを抑える）、
    # hold だけを解析から外す。internal なクロックは非同期グループのまま
    cdc_max_delay_ns: float | None = None
    stamp: BuildStamp | None = None  # 版を入れるパッケージ（sources の 1 つをビルドごとに書いたものに差し替える）

    def __post_init__(self):
        if self.stamp is not None and self.stamp.source not in self.sources:
            raise ValueError(f"版のパッケージ {self.stamp.source} が sources に無い")

    def build_sources(self, build_dir: Path) -> tuple[Path, ...]:
        """ビルドで使うソース（版のパッケージはビルドディレクトリに書いたもの）。"""
        if self.stamp is None:
            return self.sources
        return tuple(self.stamp.target(build_dir) if s == self.stamp.source else s for s in self.sources)

    @property
    def device_version(self) -> str:
        """デバイスの版（family の末尾。例 GW2A-18C → C）。IP の生成に要る。"""
        return self.family[-1]

    def prepare(self, build_dir: Path) -> Path:
        """ビルドディレクトリに TCL・SDC・CST を書き出し、TCL のパスを返す。"""
        missing = [p for p in self.sources if not Path(p).is_file()]
        if missing:
            raise FileNotFoundError(", ".join(map(str, missing)))
        _check_pins(self.pins)
        build_dir = Path(build_dir)
        build_dir.mkdir(parents=True, exist_ok=True)
        (build_dir / f"{self.name}.sdc").write_text(render_sdc(self), encoding="utf-8")
        (build_dir / f"{self.name}.cst").write_text(render_cst(self), encoding="utf-8")
        tcl = build_dir / f"{self.name}.tcl"
        tcl.write_text(render_tcl(self, build_dir), encoding="utf-8")
        return tcl


def _tcl_path(path: Path) -> str:
    # TCL の波括弧で囲み、空白や日本語を含むパスでも 1 引数として渡す
    return "{" + Path(path).resolve().as_posix() + "}"


def render_tcl(project: GowinProject, build_dir: Path = Path(".")) -> str:
    lines = [f"add_file -type vhdl {_tcl_path(src)}" for src in project.build_sources(build_dir)]
    # IP の生成物（ビルドディレクトリからの相対パス。GowinBuilder が先に生成しておく）
    lines += [f"add_file -type verilog {_tcl_path(ip.source(build_dir))}" for ip in project.ips]
    lines += [
        f"add_file -type cst {project.name}.cst",
        f"add_file -type sdc {project.name}.sdc",
        f"set_device -name {project.family} {project.part}",
        "set_option -vhdl_std vhd2008",
        f"set_option -top_module {project.top}",
        "set_option -print_all_synthesis_warning 1 -show_all_warn 1",
        *[f"set_option {opt}" for opt in project.options],
        "run all",
        f"file copy -force impl/pnr/project.fs {project.name}.fs",
    ]
    return "\n".join(lines)


def render_sdc(project: GowinProject) -> str:
    # Gowin の SDC は Tcl ではなく独自パーサ。入れ子の括弧や引用符を使わない
    getter = {"port": "get_ports", "net": "get_nets"}
    lines = [f"create_clock -name {c.name} -period {c.period_ns:.6f} [{getter[c.kind]} {{{c.target}}}]"
             for c in project.clocks]
    for d in project.io_delays:
        for bound, value in (("max", d.max_ns), ("min", d.min_ns)):
            lines.append(f"set_{d.direction}_delay -clock {d.clock} -{bound} {value:.3f} "
                         f"[get_ports {{{' '.join(d.ports)}}}]")
    if project.cdc_max_delay_ns is None:
        groups: dict[str, list[str]] = {}
        for c in project.clocks:
            groups.setdefault(c.group, []).append(c.name)
        if len(groups) > 1:
            lines.append("set_clock_groups -asynchronous " + " ".join(
                f"-group [get_clocks {{{' '.join(names)}}}]" for names in groups.values()))
        return "\n".join(lines) + "\n"
    # Gowin では set_clock_groups が set_max_delay より優先されるので、縛りたいクロック間には使わない（SUG940 §3.8）
    for c in project.clocks:
        if c.internal:
            lines.append(f"set_clock_groups -asynchronous -group [get_clocks {{{c.name}}}]")
    fabric = [c for c in project.clocks if not c.internal]
    for a in fabric:
        for b in fabric:
            if a.group != b.group:
                lines.append(f"set_max_delay -from [get_clocks {{{a.name}}}] -to [get_clocks {{{b.name}}}] "
                             f"{project.cdc_max_delay_ns:.3f}")
                lines.append(f"set_false_path -hold -from [get_clocks {{{a.name}}}] -to [get_clocks {{{b.name}}}]")
    return "\n".join(lines) + "\n"


def _check_pins(pins: tuple[Pin, ...]) -> None:
    for attr in ("port", "location"):
        seen: dict[str, Pin] = {}
        for pin in pins:
            key = getattr(pin, attr)
            if key in seen:
                raise ValueError(f"ピン割り当てが重複: {seen[key]} と {pin}")
            seen[key] = pin


def render_cst(project: GowinProject) -> str:
    lines = []
    for pin in project.pins:
        attrs = [f"IO_TYPE={pin.io_type}"]
        if pin.pull is not None:
            attrs.append(f"PULL_MODE={pin.pull}")
        if pin.drive is not None:
            attrs.append(f"DRIVE={pin.drive}")
        if pin.bank_vccio is not None:
            attrs.append(f"BANK_VCCIO={pin.bank_vccio}")
        if pin.hysteresis is not None:
            attrs.append(f"HYSTERESIS={pin.hysteresis}")
        lines.append(f'IO_LOC "{pin.port}" {pin.location};')
        lines.append(f'IO_PORT "{pin.port}" {" ".join(attrs)};')
    lines.extend(project.placements)
    return "\n".join(lines) + "\n"
