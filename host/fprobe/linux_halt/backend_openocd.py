"""OpenOCD の Tcl ポート経由の `CoreAccess`（Cortex-A × n、SMP でない設定: コアごとに OpenOCD のターゲット `<prefix><n>`）。
ボードの名前（ターゲット名の前半・DAP・コアのデバッグレジスタに届く APB-AP の番号・コアの数）は `OcdBoard` で必ず渡す
（値は fprobe.plugins.openocd_boards。この層は持たない）。

Pi 2B（BCM2836）には CTI がない（ROM テーブルが未実装）ので、同時停止はできない。停止の時間差は、停止要求の発行にかかった時間で、
ホストで測った上限として毎回の `StopReport` に返す（実際のコアの停止遅延は含まない）。物理アドレスの読み書きは、カーネルの線形マップの
仮想アドレスを通す（この SoC の DAP には止めずに/物理で読む AP がない）。D キャッシュの PoU へのクリーンは書き込みの時点で行い、
`sync_caches` は命令側（ICIALLU・BPIALL）を受け持つ。
"""
from __future__ import annotations

import re
import socket
import struct
import time
from dataclasses import dataclass
from typing import Iterable

from .bases import SEQ_BASIS
from .core_access import CoreState, OutsideLinearMap
from .skew import NotHaltedBefore, stop_report

LINE = 32                                   # キャッシュ保守の単位（A7 の行は 64B。32 は安全な約数）
REGS = ["r%d" % i for i in range(13)] + ["sp", "lr", "pc", "cpsr"]
_CP15 = {"ttbr0": "2 0 0", "ttbr1": "2 0 1", "ttbcr": "2 0 2", "contextidr": "13 0 1", "sctlr": "1 0 0"}
_WATCH_KIND = {"w": "w", "r": "r", "rw": "a"}


@dataclass(frozen=True)
class OcdBoard:
    name: str
    target_prefix: str                      # OpenOCD のターゲット名の前半（`rpi2.cpu` → rpi2.cpu0, rpi2.cpu1, ...）
    dap: str                                # OpenOCD の DAP の名前
    apb_ap: int                             # コアのデバッグレジスタに届く APB-AP の番号
    n_cores: int


class OcdError(RuntimeError):
    """OpenOCD がエラーを返した、またはリンクが切れた。"""


class Ocd:
    """OpenOCD の Tcl RPC クライアント（`tcl port`。メッセージの終端は 0x1a）。"""
    TERM = b"\x1a"

    def __init__(self, port: int, host: str, timeout: float):
        try:
            self.sock = socket.create_connection((host, port), timeout=timeout)
        except OSError as e:
            raise OcdError("cannot connect to OpenOCD tcl port %s:%d (%s)" % (host, port, e)) from e
        self.sock.settimeout(timeout)
        self.cur = None                      # 選んでいるターゲット（`targets` のキャッシュ）
        self.prefix: str | None = None       # ターゲット名の前半（OpenOcdCores が入れる）

    def close(self) -> None:
        try:
            self.sock.close()
        except OSError:
            pass

    def cmd(self, script: str) -> str:
        wrapped = 'set lxc [catch {%s} lxr]; if {$lxc} {set lxr "!LXERR $lxr"}; set lxr' % script
        try:
            self.sock.sendall(wrapped.encode() + self.TERM)
            buf = bytearray()
            while not buf.endswith(self.TERM):
                chunk = self.sock.recv(65536)
                if not chunk:
                    raise OcdError("OpenOCD closed the connection")
                buf += chunk
        except (OSError, socket.timeout) as e:
            raise OcdError("OpenOCD link error on %r: %s" % (script[:60], e)) from e
        out = buf[:-1].decode(errors="replace").strip()
        if out.startswith("!LXERR"):
            raise OcdError("%s -> %s" % (script[:80], out[7:]))
        return out

    def select(self, n: int) -> None:
        if self.cur != n:
            self.cmd("targets %s%d" % (self.prefix, n))
            self.cur = n


_PROCS = r"""
proc lx_states {} {
    global lx_t lx_cores
    set r {}
    foreach n $lx_cores { lappend r [${lx_t}$n curstate] }
    return $r
}
proc lx_halt {cores} {
    global lx_t
    # request every halt first (`halt 0` returns at once), wait afterwards: the cores stop within the few ms
    # the requests take to issue, not one OpenOCD halt-and-poll (~33 ms) apart. Returns the issue time per core (us).
    set r {}
    set t0 [clock microseconds]
    foreach n $cores {
        targets ${lx_t}$n
        halt 0
        lappend r "$n [expr {[clock microseconds] - $t0}]"
    }
    foreach n $cores {
        targets ${lx_t}$n
        wait_halt 2000
    }
    return [join $r "\n"]
}
proc lx_resume {cores} {
    global lx_t lx_dap lx_ap
    # Resume one core after the other (~9 ms each). After each, look whether a core already resumed has stopped by
    # itself (breakpoint hit): then the rest stays halted, so it does not run past the event. Returns the cores
    # actually resumed.
    # While cores run, OpenOCD's own background poll is off: it would notice a stop between two lx_wait calls and spend
    # ~32 ms per core on debug entry before we could halt the others. lx_halt turns it back on.
    poll off
    set done {}
    foreach n $cores {
        targets ${lx_t}$n
        resume
        lappend done $n
        foreach m $done {
            $lx_dap apreg $lx_ap 0x4 [expr {[${lx_t}$m cget -dbgbase] + 0x88}]
            if {[expr {[$lx_dap apreg $lx_ap 0xc] & 1}]} { return $done }
        }
    }
    return $done
}
proc lx_wait {ms cores} {
    global lx_t lx_dap lx_ap
    # Watch DBGDSCR.HALTED of all the cores from inside OpenOCD and, the moment one halted, request a halt of the
    # others. OpenOCD's own poll (20 ms) plus its debug-entry processing (~32 ms per core) would run first otherwise,
    # and those are the tens of milliseconds the other cores keep running after the event. Returns the cores that
    # halted by themselves (empty on timeout); the requested halts are in flight, lx_halt completes them.
    set t0 [clock milliseconds]
    set hit {}
    while {$hit eq {}} {
        set p0 [clock microseconds]
        foreach n $cores {
            $lx_dap apreg $lx_ap 0x4 [expr {[${lx_t}$n cget -dbgbase] + 0x88}]
            if {[expr {[$lx_dap apreg $lx_ap 0xc] & 1}]} { lappend hit $n }
        }
        if {$hit eq {} && [clock milliseconds] - $t0 >= $ms} { return "" }
    }
    set scan [expr {[clock microseconds] - $p0}]
    set q0 [clock microseconds]
    foreach n $cores {
        if {$n ni $hit} { targets ${lx_t}$n; halt 0 }
    }
    return "$hit | $scan [expr {[clock microseconds] - $q0}]"
}
proc lx_snap {cores} {
    global lx_t
    set r {}
    foreach n $cores {
        set t ${lx_t}$n
        targets $t
        set s [$t curstate]
        if {$s eq "halted"} {
            set pc   [lindex [get_reg pc] 1]
            set cpsr [lindex [get_reg cpsr] 1]
            set ctx  [$t arm mrc 15 0 13 0 1]
            set sp_svc [lindex [get_reg sp_svc] 1]
            if {[catch {$t arm mrc 14 0 0 1 0} dscr]} { set dscr -1 }
            lappend r "$n $s $pc $cpsr $ctx $dscr $sp_svc"
        } else {
            lappend r "$n $s"
        }
    }
    return [join $r "\n"]
}
"""


class OpenOcdCores:
    def __init__(self, ocd, *, board: OcdBoard, jtag_khz: int, page_offset: int, phys_offset: int, linear_end: int, hw_slots: int,
                 watch_slots: int, clock=time.perf_counter_ns):
        """jtag_khz: JTAG クロック（配線の性質なので呼び出し側が決める。既定なし）。page_offset・phys_offset: カーネルの線形マップ
        （物理 → 仮想: va = pa - phys_offset + page_offset）。"""
        if not 1 <= jtag_khz <= 100000:
            raise ValueError("jtag_khz must be 1..100000, got %r" % (jtag_khz,))
        self.o = ocd
        self.board, self.n_cores = board, board.n_cores
        ocd.prefix = board.target_prefix
        self.hw_slots, self.watch_slots = hw_slots, watch_slots     # コアごとの数（DBGDIDR の事実。OpenOCD は自分で割り当てる）
        self.jtag_khz = jtag_khz
        self.page_offset, self.phys_offset, self.linear_end = page_offset, phys_offset, linear_end
        self.actual_jtag_khz: int | None = None
        self._clock = clock
        self._ledger = NotHaltedBefore()
        self.parked: set[int] = set()
        self.last_wait: dict | None = None        # 直前の wait_event の {"scan_us", "issue_us"}（次の halt_all が 1 回だけ使う）
        self._hw_bp: dict[tuple[int, int], int] = {}
        self._hw_wp: dict[tuple[int, int], int] = {}

    @classmethod
    def open(cls, host: str, port: int, *, board: OcdBoard, jtag_khz: int, page_offset: int, phys_offset: int, linear_end: int,
             hw_slots: int, watch_slots: int, timeout: float, clock=time.perf_counter_ns):
        """timeout: OpenOCD の応答を待つ秒数（必須。既定なし。長い Tcl の手続きの時間より長く）。"""
        return cls(Ocd(port, host, timeout), board=board, jtag_khz=jtag_khz, page_offset=page_offset, phys_offset=phys_offset,
                   linear_end=linear_end, hw_slots=hw_slots, watch_slots=watch_slots, clock=clock)

    # ------------------------------------------------------------------ setup
    def setup(self) -> None:
        """JTAG クロックを設定して実際の値を読み戻し、補助の手続きを定義し、ポーリングを戻し、単ステップ中の割り込みを止める。"""
        out = self.o.cmd("adapter speed %d" % self.jtag_khz)
        m = re.search(r"(\d+)\s*kHz", out)
        if not m:
            raise OcdError("cannot tell the JTAG clock from OpenOCD's answer %r" % out)
        self.actual_jtag_khz = int(m.group(1))
        b = self.board                            # 手続きが使うボードの名前（Tcl の大域変数）
        self.o.cmd("set lx_t %s; set lx_dap %s; set lx_ap %d; set lx_cores {%s}"
                   % (b.target_prefix, b.dap, b.apb_ap, " ".join(map(str, range(b.n_cores)))))
        for line in _PROCS.strip().split("\n\nproc "):
            self.o.cmd(line if line.startswith("proc ") else "proc " + line)
        self.o.cmd("poll on")                     # 前のセッションが切ったまま死んだかもしれない（lx_resume が切る）
        for n in range(self.n_cores):
            self.o.select(n)
            self.o.cmd("cortex_a maskisr on")
        self.o.cur = None                         # lx_snap が裏でターゲットを切り替える

    def disable_halting_debug(self) -> None:
        """何もしない（停止デバッグは OpenOCD が持っている。fprobe の経路だけが自分で立てる）。"""

    def enable_halting_debug(self) -> None:
        """何もしない（同上）。"""

    def release(self) -> None:
        """OpenOCD にバックグラウンドのポーリングを戻す（走っているあいだ lx_resume が切る）。"""
        self.o.cmd("poll on")

    # ------------------------------------------------------------------ state
    def _states(self) -> list[str]:
        """各コアの状態。走っているのを読んだら、「読み始めの時刻より前には止まっていない」を記録する。"""
        t = self._clock()
        out = self.o.cmd("lx_states").split()
        for n, s in enumerate(out):
            if s != "halted":
                self._ledger.seen_running(n, t)
        return out

    def halted(self) -> tuple[bool, ...]:
        return tuple(s == "halted" for s in self._states())

    def snapshot(self, cores: Iterable[int] | None = None) -> dict[int, CoreState]:
        cores = list(range(self.n_cores) if cores is None else cores)
        out: dict[int, CoreState] = {}
        for line in self.o.cmd("lx_snap {%s}" % " ".join(map(str, cores))).splitlines():
            f = line.split()
            n = int(f[0])
            if len(f) < 7:
                out[n] = CoreState(False)
                continue
            dscr = int(f[5], 0)
            ctx = int(f[4], 16)
            out[n] = CoreState(True, int(f[2], 16), int(f[3], 16), ctx, (dscr >> 2) & 0xF if dscr >= 0 else -1,
                               int(f[6], 16), ctx >> 8)
        self.o.cur = None
        return out

    # ---------------------------------------------------------- run control
    def halt_all(self):
        """走っているコアを全部止める。BCM2836 に CTI はないので同時停止はできない（順の停止）。時間差の上限は
        `fprobe.linux_halt.skew` の式（各コアが「ここより前には止まっていない」と分かっている最小の時刻から、全コアが止まったのを
        確認した時刻まで。検出が遅れた分も含む）。止まった時刻の分からないコアがあれば None。"""
        wait, self.last_wait = self.last_wait, None
        running = [n for n, s in enumerate(self._states()) if s != "halted"]
        if running:
            self.o.cmd("lx_halt {%s}" % " ".join(map(str, running)))
            self.o.cur = None
        t_end = self._clock()
        halted = [n for n, h in enumerate(self.halted()) if h]
        explained = set(wait["hit"]) | set(wait["requested"]) if wait else set()
        return stop_report(self._ledger, self.n_cores, "sequential", halted, t_end, SEQ_BASIS, running, explained, self.parked)

    def wait_event(self, timeout_s: float) -> tuple[int, ...]:
        """コアが自分で（ブレークポイント・ウォッチポイント等で）止まるのを timeout_s まで待つ。止まったコア（タイムアウトなら空）。
        Tcl の手続きは、止まったコアを見つけた瞬間に他のコアへ停止要求を出す。"""
        watch = [n for n in range(self.n_cores) if n not in self.parked]
        t0 = self._clock()
        out = self.o.cmd("lx_wait %d {%s}" % (int(timeout_s * 1000), " ".join(map(str, watch))))
        self.o.cur = None
        hits, _, timing = out.partition("|")
        hit = tuple(int(x) for x in hits.split())
        if not hit:
            self.last_wait = None
            for n in watch:                            # 1 回分の見張りの間、全コアが走っていた
                self._ledger.seen_running(n, t0)
            return ()
        self.last_wait = {"hit": hit, "requested": tuple(n for n in watch if n not in hit)}
        if timing:
            scan, issue = (int(x) for x in timing.split())
            self.last_wait.update(scan_us=scan, issue_us=issue)     # 参考（上限には使わない）
        return hit

    def resume(self, cores: Iterable[int]) -> None:
        """コアを再開する。最初の方のコアが最後を再開する前に自分で止まったら、残りは走らせず `parked` に残す（次の resume まで）。"""
        ns = list(cores)
        if not ns:
            return
        self._ledger.clear()
        t_b = self._clock()
        done = [int(x) for x in self.o.cmd("lx_resume {%s}" % " ".join(map(str, ns))).split()]
        self.o.cur = None
        for n in done:
            self._ledger.restarted(n, t_b)
        self.parked = set(ns) - set(done)

    def resume_all(self) -> None:
        self.resume(range(self.n_cores))

    def step(self, core: int) -> None:
        self.o.select(core)
        self.o.cmd("step")

    # -------------------------------------------------------------- registers
    def regs(self, core: int) -> list[int]:
        self.o.select(core)
        d = {}
        for line in self.o.cmd("get_reg {%s}" % " ".join(REGS)).splitlines():
            k, v = line.split()
            d[k] = int(v, 16)
        return [d[r] for r in REGS]

    def set_regs(self, core: int, vals: list[int]) -> None:
        self.o.select(core)
        self.o.cmd("set_reg {%s}" % " ".join("%s 0x%x" % kv for kv in zip(REGS, vals)))

    def read_reg(self, core: int, name: str) -> int:
        self.o.select(core)
        return int(self.o.cmd("get_reg {%s}" % name).split()[1], 16)

    def write_reg(self, core: int, name: str, value: int) -> None:
        self.o.select(core)
        self.o.cmd("set_reg {%s 0x%x}" % (name, value))

    def read_cp15(self, core: int, name: str) -> int:
        self.o.select(core)
        return int(self.o.cmd("arm mrc 15 0 %s" % _CP15[name]), 16)

    # ----------------------------------------------------------------- memory
    def _va(self, pa: int, length: int) -> int:
        va = pa - self.phys_offset + self.page_offset
        if va < self.page_offset or va + length > self.linear_end:
            raise OutsideLinearMap("physical 0x%x (+%d) is outside the kernel linear map (virtual 0x%08x..0x%08x)"
                                   % (pa, length, self.page_offset, self.linear_end))
        return va

    def read_kernel(self, va: int, length: int) -> bytes:
        """カーネルの仮想アドレスを、コア 0 の MMU を通して読む（OpenOCD が特権モードに切り替える）。vmalloc も読める。"""
        self.o.select(0)
        a0 = va & ~3
        words = (va + length + 3 - a0) // 4
        out = self.o.cmd("read_memory 0x%x 32 %d" % (a0, words)).split()
        if len(out) != words:
            raise OcdError("short read at 0x%x (+%d)" % (va, length))
        raw = b"".join(struct.pack("<I", int(x, 16)) for x in out)
        return raw[va - a0:va - a0 + length]

    def write_kernel(self, va: int, data: bytes) -> None:
        """書いて、D キャッシュを PoU までクリーンする（DCCMVAU。命令側は `sync_caches`）。"""
        self.o.select(0)
        if va % 4 == 0 and len(data) % 4 == 0:
            vals = " ".join("0x%x" % w for w in struct.unpack("<%dI" % (len(data) // 4), data))
            self.o.cmd("write_memory 0x%x 32 {%s}" % (va, vals))
        else:
            self.o.cmd("write_memory 0x%x 8 {%s}" % (va, " ".join("0x%x" % b for b in data)))
        for line in range(va & ~(LINE - 1), va + len(data), LINE):
            self.o.cmd("arm mcr 15 0 7 11 1 %d" % line)

    def read_phys(self, addr: int, length: int) -> bytes:
        return self.read_kernel(self._va(addr, length), length)

    def write_phys(self, addr: int, data: bytes) -> None:
        self.write_kernel(self._va(addr, len(data)), data)

    def fetch(self, core: int, va: int, width: int) -> int:
        self.o.select(core)
        return int(self.o.cmd("read_memory 0x%x %d 1" % (va, width * 8)), 16)

    def sync_caches(self, cores: Iterable[int] | None = None) -> None:
        for n in (range(self.n_cores) if cores is None else cores):
            self.o.select(n)
            self.o.cmd("arm mcr 15 0 7 5 0 0")    # ICIALLU
            self.o.cmd("arm mcr 15 0 7 5 6 0")    # BPIALL

    # ------------------------------------------------- hardware bp / watchpoints
    def set_hw_bp(self, core: int, slot: int, addr: int, width: int) -> None:
        self.o.select(core)
        self.o.cmd("bp 0x%x %d hw" % (addr, width))
        self._hw_bp[(core, slot)] = addr

    def clear_hw_bp(self, core: int, slot: int) -> None:
        addr = self._hw_bp.pop((core, slot), None)
        if addr is not None:
            self.o.select(core)
            self.o.cmd("rbp 0x%x" % addr)

    def set_hw_watch(self, core: int, slot: int, addr: int, length: int, kind: str) -> None:
        self.o.select(core)
        self.o.cmd("wp 0x%x %d %s" % (addr, length, _WATCH_KIND[kind]))
        self._hw_wp[(core, slot)] = addr

    def clear_hw_watch(self, core: int, slot: int) -> None:
        addr = self._hw_wp.pop((core, slot), None)
        if addr is not None:
            self.o.select(core)
            self.o.cmd("rwp 0x%x" % addr)
