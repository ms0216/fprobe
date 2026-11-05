"""`linux.halt.open` / CLI の共通の引数 `HaltOptions` と、その組の検査。

つなぐ設定（バックエンド・線の速さ・カーネルの配置・停止の上限の指定など）を 1 つの凍結データにし、`check()` が組の欠け・
組み合わせの誤りを CLI と API で同じ文で断る。**既定の値は持たない**。選べる値の一覧（バックエンド・MMU の形式など）もここ。
"""
from __future__ import annotations

import dataclasses
from dataclasses import dataclass

UNMEASURED = "unmeasured"                 # 上限を測っていない（既定の値ではなく、使う側の明示の指定）
BACKENDS = ("fprobe", "openocd")
MMU_FORMATS = ("short", "lpae")
CTI_BOARDS = ("zynq7000",)
THREAD_IDS = ("stack", "contextidr")
KERNEL_ISAS = ("arm", "thumb")
ACCEPT_POLL_S = 0.25                      # gdb の接続を待つ間、止める要求に気づくまでの間隔（rsp.Link.POLL と同じ）


class OptionsError(ValueError):
    """設定の組が欠けている・組み合わせが誤り（CLI は argparse の誤りに、API は E_DEVICE の誤りの文にする）。"""


def _ocd_boards() -> dict:
    from fprobe.plugins.openocd_boards import BOARDS
    return BOARDS

@dataclass(frozen=True)
class HaltOptions:
    """つなぐ設定。CLI の共通の引数（`--backend` 等）と API の `linux.halt.open` の引数が同じ名前で一対一。None は「渡していない」。
    `fpga_halt_watch_bound_ns`・`cti_event_bound_ns` は ns の整数か "unmeasured"（測っていない。None = 渡していない とは別）。"""
    backend: str
    jtag_khz: int
    offsets: str
    page_offset: int
    phys_offset: int
    linear_map_end: int
    max_tasks: int
    max_threads: int
    mmu_format: str
    journal: str
    fpga_request_times: bool
    part: str | None = None
    fpga_halt_watch_bound_ns: int | str | None = None
    fpga_halt_watch_poll_s: float | None = None
    cti_board: str | None = None
    cti_request_bound_ns: int | None = None
    cti_event_bound_ns: int | str | None = None
    cti_hw_wait_s: float | None = None
    cti_ack_polls: int | None = None
    ocd_board: str | None = None
    ocd_port: int | None = None
    ocd_timeout_s: float | None = None
    hw_slots: int | None = None
    watch_slots: int | None = None

    @property
    def fpga_halt_watch(self) -> bool:
        return self.fpga_halt_watch_bound_ns is not None and self.fpga_halt_watch_poll_s is not None

    @property
    def cti(self) -> bool:
        return all(getattr(self, n) is not None for n in _CTI_FIELDS)

    def check(self) -> "HaltOptions":
        """組の欠け・組み合わせの誤りを断る（OptionsError）。CLI と API の同じ文。"""
        if self.backend not in BACKENDS:
            raise OptionsError("backend must be one of %s" % (BACKENDS,))
        if not 1 <= self.jtag_khz <= 100000:
            raise OptionsError("--jtag-khz must be 1..100000")
        if self.mmu_format not in MMU_FORMATS:
            raise OptionsError("mmu_format must be one of %s" % (MMU_FORMATS,))
        if self.backend == "openocd":
            for flag in ("ocd_board", "ocd_port", "ocd_timeout_s", "hw_slots", "watch_slots"):
                if getattr(self, flag) is None:
                    raise OptionsError("--backend openocd needs --%s" % flag.replace("_", "-"))
            if self.ocd_board not in _ocd_boards():
                raise OptionsError("--ocd-board must be one of %s" % sorted(_ocd_boards()))
        elif self.part is None:
            raise OptionsError("--backend fprobe needs --part")
        for name in ("fpga_halt_watch_bound_ns", "cti_event_bound_ns"):
            v = getattr(self, name)
            if v is not None and v != UNMEASURED and not (isinstance(v, int) and v >= 0):
                raise OptionsError("--%s is a number of ns or %s" % (name.replace("_", "-"), UNMEASURED))
        watch = (self.fpga_halt_watch_bound_ns is not None, self.fpga_halt_watch_poll_s is not None)
        if any(watch) and not all(watch):
            raise OptionsError("the FPGA halt watch needs both --fpga-halt-watch-bound-ns and --fpga-halt-watch-poll-s")
        if any(watch) and self.backend != "fprobe":
            raise OptionsError("the FPGA halt watch is a feature of the fprobe backend")
        cti = [getattr(self, n) is not None for n in _CTI_FIELDS]
        if any(cti) and not all(cti):
            raise OptionsError("the CTI needs every one of --cti-board --cti-request-bound-ns --cti-event-bound-ns "
                               "--cti-hw-wait-s --cti-ack-polls")
        if any(cti) and self.backend != "fprobe":
            raise OptionsError("--cti-* belong to the fprobe backend")
        if any(cti) and self.cti_board not in CTI_BOARDS:
            raise OptionsError("--cti-board must be one of %s" % (CTI_BOARDS,))
        if any(cti) and self.fpga_halt_watch:
            raise OptionsError("the CTI and the FPGA halt watch are not used together")
        if self.fpga_request_times and self.backend != "fprobe":
            raise OptionsError("--fpga-request-times is a feature of the fprobe backend")
        if self.fpga_request_times and self.cti:
            raise OptionsError("--fpga-request-times is for the stop without a CTI (the CTI halts in hardware)")
        return self

    def as_dict(self) -> dict:
        """渡した設定（渡していないものは除く）。応答に、そのまま返す。"""
        return {k: v for k, v in dataclasses.asdict(self).items() if v is not None}


_CTI_FIELDS = ("cti_board", "cti_request_bound_ns", "cti_event_bound_ns", "cti_hw_wait_s", "cti_ack_polls")
