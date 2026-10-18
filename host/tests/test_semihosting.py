"""fprobe.semihosting: 偽のターゲット（レジスタの辞書とメモリ）で、BKPT 0xAB の各要求を処理する。"""

import struct

import pytest

from fprobe.semihosting import (ADP_STOPPED_APPLICATION_EXIT, TARGET_ERRNO, FileAccess, Op, SemihostingHost)

BASE = 0x20000000
CODE = BASE + 0x100            # BKPT 0xAB を置く番地
ARGS = BASE + 0x200            # 引数のブロック
DATA = BASE + 0x400            # 文字列・バッファ


class FakeTarget:
    def __init__(self):
        self.mem = bytearray(0x2000)
        self.regs = {"r0": 0, "r1": 0, "pc": CODE}
        self.resumed = 0
        self.poke(CODE, struct.pack("<H", 0xBEAB))

    def read_core_register(self, name: str) -> int:
        return self.regs[name]

    def write_core_register(self, name: str, value: int) -> None:
        self.regs[name] = value

    def read_memory(self, address: int, length: int) -> bytes:
        assert BASE <= address and address + length <= BASE + len(self.mem), "範囲外の読み"
        return bytes(self.mem[address - BASE:address - BASE + length])

    def write_memory(self, address: int, data: bytes) -> None:
        self.mem[address - BASE:address - BASE + len(data)] = data

    def resume(self) -> None:
        self.resumed += 1

    def poke(self, address: int, data: bytes) -> None:
        self.write_memory(address, data)

    def peek(self, address: int, length: int) -> bytes:
        return self.read_memory(address, length)


class Console:
    def __init__(self):
        self.out: list[tuple[str, bytes]] = []

    def __call__(self, stream: str, data: bytes) -> None:
        self.out.append((stream, data))

    def text(self, stream: str = "stdout") -> bytes:
        return b"".join(d for s, d in self.out if s == stream)


@pytest.fixture
def env():
    return FakeTarget(), Console()


def call(host: SemihostingHost, target: FakeTarget, op: int, *args: int, r1: int | None = None) -> int:
    """要求を 1 つ出して処理させ、R0（符号付き）を返す。PC が 2 進むことも確かめる。"""
    target.regs.update(r0=op, r1=ARGS if r1 is None else r1, pc=CODE)
    if args:
        target.poke(ARGS, struct.pack(f"<{len(args)}I", *args))
    result = host.handle(target)
    assert result.handled and result.operation == op
    if not result.exited:
        assert target.regs["pc"] == CODE + 2
    r0 = target.regs["r0"]
    return r0 - (1 << 32) if r0 & 0x80000000 else r0


def string(target: FakeTarget, text: bytes, at: int = DATA) -> tuple[int, int]:
    target.poke(at, text + b"\0")
    return at, len(text)


# --- 出力 -------------------------------------------------------------------------------

def test_not_a_semihosting_breakpoint_is_left_alone(env):
    target, console = env
    target.poke(CODE, struct.pack("<H", 0xBE00))                       # BKPT 0
    result = SemihostingHost(console=console).handle(target)
    assert not result.handled and target.regs["pc"] == CODE


def test_writec_and_write0(env):
    target, console = env
    host = SemihostingHost(console=console)
    target.poke(DATA, b"A")
    call(host, target, Op.SYS_WRITEC, r1=DATA)
    long_text = b"hello, semihosting " * 10                            # 64 バイトのかたまりをまたぐ
    string(target, long_text)
    call(host, target, Op.SYS_WRITE0, r1=DATA)
    assert console.text() == b"A" + long_text


def test_service_resumes_after_handling(env):
    target, console = env
    target.poke(DATA, b"x")
    target.regs.update(r0=Op.SYS_WRITEC, r1=DATA)
    host = SemihostingHost(console=console)
    assert host.service(target).handled and target.resumed == 1


def test_open_tt_and_write_read(env):
    target, console = env
    inputs = [b"typed"]
    host = SemihostingHost(console=console, stdin=lambda n: inputs.pop(0)[:n] if inputs else b"")
    name, length = string(target, b":tt", at=DATA + 0x100)
    stdin = call(host, target, Op.SYS_OPEN, name, 0, length)
    stdout = call(host, target, Op.SYS_OPEN, name, 4, length)
    stderr = call(host, target, Op.SYS_OPEN, name, 8, length)
    assert len({stdin, stdout, stderr}) == 3 and min(stdin, stdout, stderr) > 0
    target.poke(DATA, b"out!")
    assert call(host, target, Op.SYS_WRITE, stdout, DATA, 4) == 0                # 書けなかった数 = 0
    assert call(host, target, Op.SYS_WRITE, stderr, DATA, 3) == 0
    assert console.text("stdout") == b"out!" and console.text("stderr") == b"out"
    assert call(host, target, Op.SYS_READ, stdin, DATA, 10) == 10 - 5           # 読めなかった数
    assert target.peek(DATA, 5) == b"typed"
    assert call(host, target, Op.SYS_READ, stdin, DATA, 10) == 10                # 入力なし = EOF
    assert call(host, target, Op.SYS_ISTTY, stdout) == 1
    assert call(host, target, Op.SYS_CLOSE, stdout) == 0
    assert call(host, target, Op.SYS_WRITE, stdout, DATA, 4) == 4                # 閉じた
    assert call(host, target, Op.SYS_ERRNO) == TARGET_ERRNO["EBADF"]


def test_readc(env):
    target, console = env
    host = SemihostingHost(console=console, stdin=lambda n: b"q")
    assert call(host, target, Op.SYS_READC) == ord("q")
    assert call(SemihostingHost(console=console), target, Op.SYS_READC) == -1


def test_features_file(env):
    target, console = env
    host = SemihostingHost(console=console)
    name, length = string(target, b":semihosting-features", at=DATA + 0x100)
    handle = call(host, target, Op.SYS_OPEN, name, 0, length)
    assert call(host, target, Op.SYS_FLEN, handle) == 5
    assert call(host, target, Op.SYS_READ, handle, DATA, 5) == 0
    assert target.peek(DATA, 5) == b"SHFB\x03"


# --- 時刻・その他 ---------------------------------------------------------------------------

def test_clock_time_elapsed_tickfreq(env):
    target, console = env
    now = [100.0]
    host = SemihostingHost(console=console, clock=lambda: now[0], wallclock=lambda: 1_700_000_000.5)
    now[0] = 102.5
    assert call(host, target, Op.SYS_CLOCK) == 250                                # センチ秒
    assert call(host, target, Op.SYS_TIME) == 1_700_000_000
    assert call(host, target, Op.SYS_TICKFREQ) == 1_000_000
    assert call(host, target, Op.SYS_ELAPSED) == 0
    assert struct.unpack("<Q", target.peek(ARGS, 8))[0] == 2_500_000


def test_iserror_cmdline_heapinfo(env):
    target, console = env
    host = SemihostingHost(console=console, cmdline="app --fast", heapinfo=(1, 2, 3, 4))
    assert call(host, target, Op.SYS_ISERROR, 0xFFFFFFFF) == 1
    assert call(host, target, Op.SYS_ISERROR, 5) == 0
    assert call(host, target, Op.SYS_GET_CMDLINE, DATA, 64) == 0
    assert target.peek(DATA, 11) == b"app --fast\0" and struct.unpack("<I", target.peek(ARGS + 4, 4))[0] == 10
    assert call(host, target, Op.SYS_GET_CMDLINE, DATA, 4) == -1                  # 入らない
    target.poke(ARGS, struct.pack("<I", DATA + 0x80))                             # R1 → 4 語のブロックへの番地
    call(host, target, Op.SYS_HEAPINFO, r1=ARGS)
    assert struct.unpack("<4I", target.peek(DATA + 0x80, 16)) == (1, 2, 3, 4)


def test_system_is_always_refused_and_unknown_is_unsupported(env):
    target, console = env
    host = SemihostingHost(console=console)
    name, length = string(target, b"rm -rf /")
    assert call(host, target, Op.SYS_SYSTEM, name, length) == -1
    target.regs.update(r0=0x55, r1=0, pc=CODE)
    result = host.handle(target)
    assert result.unsupported and target.regs["r0"] == 0xFFFFFFFF
    assert call(host, target, Op.SYS_ERRNO) == TARGET_ERRNO["ENOSYS"]


def test_exit(env):
    target, console = env
    host = SemihostingHost(console=console)
    target.regs.update(r0=Op.SYS_EXIT, r1=ADP_STOPPED_APPLICATION_EXIT, pc=CODE)
    result = host.service(target)
    assert result.exited and result.exit_code == 0 and target.resumed == 0
    target.regs.update(r0=Op.SYS_EXIT, r1=0x20023, pc=CODE)                      # 他の理由（RunTimeErrorUnknown）
    assert host.handle(target).exit_code == 1
    target.poke(ARGS, struct.pack("<II", ADP_STOPPED_APPLICATION_EXIT, 42))
    target.regs.update(r0=Op.SYS_EXIT_EXTENDED, r1=ARGS, pc=CODE)
    result = host.handle(target)
    assert result.exited and result.exit_code == 42 and result.exit_reason == ADP_STOPPED_APPLICATION_EXIT


# --- ファイル（既定は無効、許可は root の下だけ） ------------------------------------------------

def test_files_are_refused_by_default(env, tmp_path):
    target, console = env
    (tmp_path / "a.txt").write_bytes(b"data")
    host = SemihostingHost(console=console)
    name, length = string(target, str(tmp_path / "a.txt").encode())
    assert call(host, target, Op.SYS_OPEN, name, 1, length) == -1
    assert call(host, target, Op.SYS_ERRNO) == TARGET_ERRNO["EACCES"]


def test_read_file_under_root(env, tmp_path):
    target, console = env
    (tmp_path / "sub").mkdir()
    (tmp_path / "sub" / "a.bin").write_bytes(b"0123456789")
    host = SemihostingHost(console=console, files=FileAccess(tmp_path))
    name, length = string(target, b"sub/a.bin", at=DATA + 0x100)
    h = call(host, target, Op.SYS_OPEN, name, 1, length)
    assert h > 0 and call(host, target, Op.SYS_ISTTY, h) == 0
    assert call(host, target, Op.SYS_FLEN, h) == 10
    assert call(host, target, Op.SYS_SEEK, h, 4) == 0
    assert call(host, target, Op.SYS_READ, h, DATA, 100) == 100 - 6
    assert target.peek(DATA, 6) == b"456789"
    assert call(host, target, Op.SYS_WRITE, h, DATA, 2) == 2                     # 読み出し専用で開いた
    assert call(host, target, Op.SYS_ERRNO) == TARGET_ERRNO["EBADF"]
    assert call(host, target, Op.SYS_CLOSE, h) == 0
    missing, n = string(target, b"nope.txt", at=DATA + 0x100)
    assert call(host, target, Op.SYS_OPEN, missing, 1, n) == -1
    assert call(host, target, Op.SYS_ERRNO) == TARGET_ERRNO["ENOENT"]


@pytest.mark.parametrize("name", [b"../outside.txt", b"sub/../../outside.txt", b"/etc/passwd", b"C:\\Windows\\win.ini",
                                  b"C:relative.txt", b"\\\\server\\share\\x", b"//server/share/x", b""])
def test_escaping_names_are_refused(env, tmp_path, name):
    target, console = env
    root = tmp_path / "root"
    root.mkdir()
    (tmp_path / "outside.txt").write_bytes(b"secret")
    host = SemihostingHost(console=console, files=FileAccess(root, writable=True))
    at, length = string(target, name, at=DATA + 0x100)
    assert call(host, target, Op.SYS_OPEN, at, 1, length) == -1
    assert call(host, target, Op.SYS_ERRNO) == TARGET_ERRNO["EACCES"]


def test_symlink_escape_is_refused(env, tmp_path):
    target, console = env
    root = tmp_path / "root"
    root.mkdir()
    (tmp_path / "outside.txt").write_bytes(b"secret")
    try:
        (root / "link.txt").symlink_to(tmp_path / "outside.txt")
    except OSError:
        pytest.skip("シンボリックリンクを作れない（Windows の権限）")
    host = SemihostingHost(console=console, files=FileAccess(root))
    at, length = string(target, b"link.txt", at=DATA + 0x100)
    assert call(host, target, Op.SYS_OPEN, at, 1, length) == -1


def test_write_needs_writable(env, tmp_path):
    target, console = env
    at, length = string(target, b"out.txt", at=DATA + 0x100)
    ro = SemihostingHost(console=console, files=FileAccess(tmp_path))
    assert call(ro, target, Op.SYS_OPEN, at, 4, length) == -1 and not (tmp_path / "out.txt").exists()
    rw = SemihostingHost(console=console, files=FileAccess(tmp_path, writable=True))
    h = call(rw, target, Op.SYS_OPEN, at, 4, length)
    target.poke(DATA, b"written")
    assert call(rw, target, Op.SYS_WRITE, h, DATA, 7) == 0
    assert call(rw, target, Op.SYS_CLOSE, h) == 0
    assert (tmp_path / "out.txt").read_bytes() == b"written"
    new_at, new_len = string(target, b"renamed.txt", at=DATA + 0x180)
    assert call(rw, target, Op.SYS_RENAME, at, length, new_at, new_len) == 0
    assert (tmp_path / "renamed.txt").exists()
    assert call(ro, target, Op.SYS_REMOVE, new_at, new_len) == -1                 # 読み出し専用では消せない
    assert call(rw, target, Op.SYS_REMOVE, new_at, new_len) == 0
    assert not (tmp_path / "renamed.txt").exists()


def test_open_file_limit(env, tmp_path):
    target, console = env
    (tmp_path / "a").write_bytes(b"")
    host = SemihostingHost(console=console, files=FileAccess(tmp_path, max_open=2))
    at, length = string(target, b"a", at=DATA + 0x100)
    assert call(host, target, Op.SYS_OPEN, at, 0, length) > 0
    assert call(host, target, Op.SYS_OPEN, at, 0, length) > 0
    assert call(host, target, Op.SYS_OPEN, at, 0, length) == -1
    assert call(host, target, Op.SYS_ERRNO) == TARGET_ERRNO["EMFILE"]
    host.close()
