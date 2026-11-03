"""1 つの Linux プロセスの GDB リモートシリアルプロトコル（RSP）サーバ（all-stop）。

パケットの整形と読み取りは `fprobe.gdb.rsp`（PacketReader・frame）を使う。**fprobe の `GdbSession`（非同期・Probe とデーモンに結びつく）には
載せていない**: それに載せるには Probe 側に Linux のターゲットが要り、API/MCP と時間軸の統合（スペックの C5）の仕事になる。ここは `Engine` の上の
単独のサーバ。
"""
from __future__ import annotations

import select
import socket
import struct

from fprobe.gdb import rsp as framing

from .bp import UNMAPPED_POLICIES, NoFreeSlot
from .engine import EngineError, Stop
from .mmu import Unmapped
from .process import ProcessExited

SIGINT, SIGTRAP = 2, 5
REGNAMES = ["r%d" % i for i in range(13)] + ["sp", "lr", "pc", "cpsr"]
TARGET_XML = (
    '<?xml version="1.0"?><!DOCTYPE target SYSTEM "gdb-target.dtd"><target version="1.0">'
    "<architecture>arm</architecture>"
    '<feature name="org.gnu.gdb.arm.core">'
    + "".join('<reg name="%s" bitsize="32"%s/>' % (r, {"sp": ' type="data_ptr"', "pc": ' type="code_ptr"'}.get(r, ""))
              for r in REGNAMES)
    + "</feature></target>"
)
WATCH_TYPE = {"2": "w", "3": "r", "4": "a"}                  # RSP の Z の種類 → 見張りの種類
WATCH_KEY = {"w": "watch", "r": "rwatch", "a": "awatch"}      # 種類ごとの停止応答のキー


class Link:
    """ソケット上のパケット（ack・再送・^C の検出つき）。"""

    POLL = 0.25            # 秒。終了の要求が気づかれるまでの待ちの上限

    def __init__(self, sock, log=None):
        self.s, self.log = sock, log
        self.reader, self.events = framing.PacketReader(), []
        self.noack, self.last = False, b""

    def _fill(self, timeout: float) -> bool:
        r, _, _ = select.select([self.s], [], [], timeout)
        if not r:
            return False
        d = self.s.recv(65536)
        if not d:
            raise ConnectionError("gdb disconnected")
        self.events.extend(self.reader.feed(d))
        return True

    def interrupted(self) -> bool:
        """実行中に gdb が ^C（0x03）を送っていたら True（1 つ消費）。"""
        self._fill(0)
        if framing.INTERRUPT in self.events:
            self.events.remove(framing.INTERRUPT)
            return True
        return False

    def read_packet(self) -> bytes:
        while True:
            while self.events:
                e = self.events.pop(0)
                if e is framing.NAK and self.last:
                    self.s.sendall(self.last)
                elif e is framing.CORRUPT:
                    if not self.noack:
                        self.s.sendall(b"-")
                elif isinstance(e, framing.Packet):
                    if not self.noack:
                        self.s.sendall(b"+")
                    if self.log:
                        self.log("-> %r" % e.payload[:100])
                    return e.payload
            self._fill(self.POLL)                 # 定期的に起きる: Python はシグナルのハンドラを呼びの間にしか走らせない

    def send(self, payload) -> None:
        if isinstance(payload, str):
            payload = payload.encode()
        pkt = framing.frame(payload)
        self.last = pkt
        self.s.sendall(pkt)
        if self.log:
            self.log("<- %r" % payload[:100])


class Server:
    def __init__(self, engine, link: Link, *, backend_errors: tuple, log=None):
        """backend_errors: コアの操作の失敗の例外の型（OpenOCD なら OcdError、fprobe なら ArmDebugError）。呼び出し側が渡す。"""
        self.e, self.link, self.log = engine, link, log or (lambda m: None)
        self.p, self.bps = engine.p, engine.bps
        self.backend_errors = tuple(backend_errors)
        self.hg = None                 # Hg で選んだスレッド
        self.last = None               # 最後の Stop
        self.done = False
        self._noack_pending = False

    # ------------------------------------------------------------ helpers
    def tid(self) -> int:
        return self.hg if self.hg in self.p.tids() else self.e.default_tid()

    def reply_for(self, st: Stop) -> str:
        if st.kind == "exit":
            self.done = True
            sig = st.code & 0x7F                          # カーネルの終了コード: (status << 8) | シグナル
            return "X%02x" % sig if sig else "W%02x" % ((st.code >> 8) & 0xFF)
        sig = SIGINT if st.kind == "int" else st.signal if st.kind == "signal" else SIGTRAP
        extra = {"swbreak": "swbreak:;", "hwbreak": "hwbreak:;"}.get(st.kind, "")
        if st.kind == "watch" and st.access:
            extra = "%s:%x;" % (WATCH_KEY[st.access], st.addr)
        return "T%02x%sthread:%x;" % (sig, extra, st.tid)

    def run(self, fn) -> str:
        st = fn()
        self.last = st
        if st.tid:
            self.hg = st.tid          # RSP: スレッドを名指しした停止応答は、そのスレッドを現在のスレッドにする
        return self.reply_for(st)

    @staticmethod
    def hexint(s: str) -> int:
        return int(s, 16)

    # ---------------------------------------------------------------- loop
    def serve(self) -> None:
        self.last = Stop("trap", self.e.default_tid())
        while not self.done:
            pk = self.link.read_packet()
            try:
                r = self.handle(pk)
            except Unmapped:
                r = b"E01"
            except ProcessExited:
                self.done = True
                r = b"W00"
            except EngineError as e:
                self.console("%s\n" % e)
                r = b"E03"
            except self.backend_errors as e:
                self.log("core access error: %s" % e)
                r = b"E02"
            self.link.send(r)
            if self._noack_pending:
                self.link.noack, self._noack_pending = True, False

    # ------------------------------------------------------------ dispatch
    def handle(self, pk: bytes) -> bytes:
        if pk.startswith(b"X"):                       # バイナリの書き込み: 文字として解釈してはいけない（逃がしは Link が戻し済み）
            head, data = pk[1:].split(b":", 1)
            a, _ = (self.hexint(x) for x in head.decode().split(","))
            return self.write_mem(a, data).encode()
        t = pk.decode(errors="replace")
        for prefix, fn in self.table:
            if t.startswith(prefix):
                r = fn(self, t)
                return r.encode("latin-1") if isinstance(r, str) else r
        return b""                                    # 未対応 → 空の応答（プロトコルの約束）

    # -------------------------------------------------------------- queries
    def q_supported(self, t):
        return ("PacketSize=4000;qXfer:features:read+;qXfer:auxv:read+;qXfer:threads:read+;"
                "QStartNoAckMode+;swbreak+;hwbreak+;vContSupported+")

    def q_xfer(self, t):
        _, obj, op, annex, rng = t.split(":", 4)
        off, ln = (self.hexint(x) for x in rng.split(","))
        if op != "read":
            return ""
        if obj == "features" and annex == "target.xml":
            data = TARGET_XML.encode()
        elif obj == "auxv":
            data = self.p.auxv()
        elif obj == "threads":
            data = self.threads_xml().encode()
        else:
            return ""
        chunk = data[off:off + ln]
        return (b"m" if off + ln < len(data) else b"l") + framing.escape(chunk)

    def threads_xml(self) -> str:
        rows = []
        for th in self.p.threads:
            core = next((n for n, s in self.e.core.items() if s.tid == th.pid), None)
            rows.append('<thread id="%x"%s name="%s"/>' % (th.pid, ' core="%d"' % core if core is not None else "",
                                                           th.comm.replace('"', "")))
        return "<threads>%s</threads>" % "".join(rows)

    def q_thread_extra(self, t):
        th = self.p.task(self.hexint(t.split(",")[1]))
        return (th.comm if th else "?").encode().hex()

    def q_thread_first(self, t):
        return "m" + ",".join("%x" % th.pid for th in self.p.threads)

    # ------------------------------------------------------------ registers
    def get_regs(self, t):
        return b"".join(struct.pack("<I", v) for v in self.e.get_regs(self.tid())).hex()

    def set_regs(self, t):
        raw = bytes.fromhex(t[1:])
        self.e.set_regs(self.tid(), list(struct.unpack("<17I", raw[:68])))
        return "OK"

    def get_reg(self, t):
        return struct.pack("<I", self.e.get_regs(self.tid())[self.hexint(t[1:])]).hex()

    def set_reg(self, t):
        i, v = t[1:].split("=")
        regs = self.e.get_regs(self.tid())
        regs[self.hexint(i)] = struct.unpack("<I", bytes.fromhex(v))[0]
        self.e.set_regs(self.tid(), regs)
        return "OK"

    # --------------------------------------------------------------- memory
    def read_mem(self, t):
        a, n = (self.hexint(x) for x in t[1:].split(","))
        out = bytearray()
        try:
            while len(out) < n:
                k = min(n - len(out), 4096 - ((a + len(out)) & 0xFFF))
                out += self.p.mem.read(a + len(out), k)
        except Unmapped:
            if not out:
                return "E01"                          # 一部だけ読めるのは正しい。1 バイトも読めないのは誤り
        return self.bps.shadow(self.p.tgid, a, bytes(out)).hex()

    def m_write(self, t):
        head, data = t[1:].split(":", 1)
        a, _ = (self.hexint(x) for x in head.split(","))
        return self.write_mem(a, bytes.fromhex(data))

    def write_mem(self, a: int, data: bytes) -> str:
        self.bps.write(self.p.tgid, a, data)
        return "OK"

    # ---------------------------------------------------------- breakpoints
    def z_insert(self, t):
        typ, addr, kind = t[1:].split(",")[:3]
        if typ in WATCH_TYPE:                         # Z2/Z3/Z4: kind は長さ（バイト）
            try:
                self.e.watches.add(self.hexint(addr), self.hexint(kind), WATCH_TYPE[typ])
            except (NoFreeSlot, ValueError, RuntimeError) as e:
                self.log("watchpoint refused: %s" % e)
                self.console("watchpoint refused: %s\n" % e)
                return "E01"
            return "OK"
        if typ not in "01":
            return ""
        va = self.hexint(addr)
        try:
            self.bps.insert(self.p.tgid, va, kind="hw" if typ == "1" else "sw", width=4 if self.hexint(kind) == 4 else 2)
        except Unmapped:
            self.console("breakpoint at 0x%08x not set: that page is not mapped yet (the code has not run, so the "
                         "kernel has not paged it in), and a software breakpoint needs the page. Use `hbreak` "
                         "(hardware breakpoint), or `monitor bp-unmapped hw` to let `break` use a hardware breakpoint "
                         "for such pages.\n" % va)
            return "E01"
        except NoFreeSlot as e:
            self.console("breakpoint at 0x%08x not set: %s (it is not replaced by a software breakpoint)\n" % (va, e))
            return "E01"
        return "OK"

    def console(self, text: str) -> None:
        """gdb の利用者への文（`O` パケット）: 事実と、どうすればよいか。"""
        self.link.send("O" + text.encode().hex())

    def z_remove(self, t):
        typ, addr, kind = t[1:].split(",")[:3]
        if typ in WATCH_TYPE:
            self.e.watches.remove(self.hexint(addr), self.hexint(kind), WATCH_TYPE[typ])
            return "OK"
        if typ not in "01":
            return ""
        self.bps.remove(self.p.tgid, self.hexint(addr), kind="hw" if typ == "1" else "sw")
        return "OK"

    # ---------------------------------------------------------- run control
    def vcont(self, t):
        acts = [a.split(":") for a in t[6:].split(";") if a]
        step = None
        for a in acts:
            if a[0][0] in "sS":
                step = self.hexint(a[1]) if len(a) > 1 and a[1] not in ("-1", "0") else self.tid()
                break
        return self.run(lambda: self.e.step(step) if step is not None else self.e.cont())

    def resume_c(self, t):
        return self.run(self.e.cont)

    def resume_s(self, t):
        return self.run(lambda: self.e.step(self.tid()))

    def detach(self, t):
        self.done = True
        return "OK"

    def select_thread(self, t):
        v = -1 if t[2:] == "-1" else self.hexint(t[2:])
        if t[1] == "g":
            self.hg = v if v > 0 else None
        return "OK"

    def noack(self, t):
        self._noack_pending = True
        return "OK"

    def monitor(self, t):
        cmd = bytes.fromhex(t.split(",", 1)[1]).decode().split()
        e = self.e
        if cmd[:1] == ["cores"]:
            out = "".join("cpu%d %s pc=%08x cpsr=%08x tid=%d moe=%d\n" % (n, "halted" if s.halted else "running", s.pc,
                                                                         s.cpsr, s.tid, s.moe)
                          for n, s in sorted(e.core.items()))
        elif cmd[:1] == ["threads"]:
            out = "".join("tid=%d task=%08x %s\n" % (th.pid, th.addr, th.comm) for th in self.p.threads)
        elif cmd[:1] == ["bp-unmapped"]:
            if len(cmd) == 2 and cmd[1] in UNMAPPED_POLICIES:
                self.bps.unmapped = cmd[1]
            elif len(cmd) != 1:
                self.link.send("O" + "usage: monitor bp-unmapped [error|hw]\n".encode().hex())
                return "OK"
            out = "bp-unmapped = %s (what `break` does at a page that is not mapped yet)\n" % self.bps.unmapped
        elif cmd[:1] == ["bps"]:
            out = "".join("%08x %s%s%s\n" % (b.va, "hw" if b.hw else "sw", " user" if b.user else "",
                                             " tmp" if b.tmp else "") for b in self.bps.entries(self.p.tgid))
        elif cmd[:1] == ["watches"]:
            out = "".join("%08x len=%d %s\n" % (w.addr, w.length, w.kind) for w in e.watches)
        elif cmd[:1] == ["stats"]:
            out = "".join("%s=%d\n" % kv for kv in sorted(e.stats().counters.items()))
        elif cmd[:1] == ["stops"]:
            out = self.stops_text()
        else:
            out = "monitor commands: cores | threads | bps | watches | stats | stops | bp-unmapped\n"
        self.link.send("O" + out.encode().hex())
        return "OK"

    def stops_text(self) -> str:
        """停止の事実: 方式・コア間の時間差の上限・その根拠・基準との比較。上限が分からないときは「分からない」と言う（0 ではない）。"""
        e = self.e
        r = e.last_stop_result
        out = ""
        if r is None:
            out += "no stop yet\n"
        else:
            rep = r.report
            bound = "unknown (not a measurement of zero)" if rep.skew_upper_bound_ns is None else "%d ns" % rep.skew_upper_bound_ns
            out += "last stop: %s; cores halted %s; skew upper bound %s\n" % (rep.method, list(rep.cores_halted), bound)
            out += "  basis: %s\n" % rep.basis
            out += "  verdict against the limit of %d ns: %s\n" % (e.skew_limit_ns, r.skew_verdict)
        st = e.stats()
        for kind in ("event", "interrupt", "request"):
            k = st.skew[kind]
            mx = "none measured" if k["max_ns"] is None else "%d ns" % k["max_ns"]
            out += "%s stops: %d; max skew bound %s; unknown bound: %d; over the limit: %d\n" % (
                kind, k["stops"], mx, k["unknown"], k["exceeded"])
        out += "resumes cut short (a core stopped before the last was resumed, the rest stayed halted): %d\n" % st.resumes_cut_short
        return out

    table = [
        ("qSupported", q_supported),
        ("qXfer:", q_xfer),
        ("qAttached", lambda s, t: "1"),
        ("qC", lambda s, t: "QC%x" % s.tid()),
        ("qfThreadInfo", q_thread_first),
        ("qsThreadInfo", lambda s, t: "l"),
        ("qThreadExtraInfo", q_thread_extra),
        ("qRcmd", monitor),
        ("qSymbol", lambda s, t: "OK"),
        ("QStartNoAckMode", noack),
        ("?", lambda s, t: s.reply_for(s.last)),
        ("H", select_thread),
        ("T", lambda s, t: "OK" if s.hexint(t[1:]) in s.p.tids() else "E01"),
        ("g", get_regs),
        ("G", set_regs),
        ("p", get_reg),
        ("P", set_reg),
        ("m", read_mem),
        ("M", m_write),
        ("Z", z_insert),
        ("z", z_remove),
        ("vCont?", lambda s, t: "vCont;c;C;s;S"),
        ("vCont;", vcont),
        ("vKill", detach),
        ("c", resume_c),
        ("C", resume_c),
        ("s", resume_s),
        ("S", resume_s),
        ("D", detach),
        ("k", detach),
    ]


def listen(port: int, log=print):
    srv = socket.socket()
    srv.bind(("127.0.0.1", port))       # 外に出さない: このポートはターゲットのメモリを読み書きできる
    srv.listen(1)
    log("gdb: target remote :%d" % port)
    srv.settimeout(Link.POLL)
    while True:
        try:
            conn, _ = srv.accept()
            break
        except socket.timeout:
            continue                     # 終了のシグナルが待ちを割り込めるように
    srv.close()
    conn.settimeout(None)
    conn.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
    return conn
