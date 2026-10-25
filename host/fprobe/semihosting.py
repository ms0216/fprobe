"""セミホスティング（Arm「Semihosting for AArch32 and AArch64」v2.0、M プロファイル = BKPT 0xAB）の PC 側。

ターゲットが BKPT 0xAB で止まったら handle() を呼ぶ: R0（操作）・R1（引数）を読み、処理して結果を R0 に書き、
PC を 2 進める。再開は呼び出し側（service() は再開までまとめて行う。SYS_EXIT では再開しない）。
レジスタ・メモリは抽象（SemihostingTarget）で受け取るので、pyOCD にも fprobe の Session にも依らない。
仕様: docs/superpowers/specs/2026-10-17-f1-rtt-itm-semihosting-design.md の 3 節。

安全: SYS_SYSTEM（PC でコマンドを実行）は常に拒否。ファイル操作は既定で無効（FileAccess(root=None)）で、
有効にしても root の下だけ・既定は読み出しだけ。
"""

import errno
import io
import struct
import sys
import time
from collections.abc import Callable
from dataclasses import dataclass
from enum import IntEnum
from pathlib import Path, PurePosixPath, PureWindowsPath
from typing import BinaryIO, Protocol

from .symbols import is_network_path

__all__ = ["ADP_STOPPED_APPLICATION_EXIT", "FileAccess", "Op", "ResumableTarget", "SemihostingHost",
           "SemihostingResult", "SemihostingTarget", "TARGET_ERRNO"]

BKPT_SEMIHOSTING = 0xBEAB                  # Thumb の BKPT 0xAB
ADP_STOPPED_APPLICATION_EXIT = 0x20026     # SYS_EXIT の「正常に終わった」
FEATURES = b"SHFB\x03"                     # 機能: bit0 = SYS_EXIT_EXTENDED、bit1 = :tt の "a" を標準エラーに分ける
TICK_HZ = 1_000_000                        # SYS_ELAPSED の刻み（µs）
WRITE0_LIMIT = 64 * 1024                   # SYS_WRITE0 の文字列の上限
NAME_LIMIT = 1024                          # ファイル名の長さの上限
TRANSFER_LIMIT = 1 << 20                   # 1 回の SYS_READ / SYS_WRITE の上限

# ターゲット側の errno（arm-none-eabi の newlib の値。ホストの OS の値はターゲットでは意味を持たないので訳す）
TARGET_ERRNO = {"EPERM": 1, "ENOENT": 2, "EIO": 5, "EBADF": 9, "EACCES": 13, "EEXIST": 17, "ENOTDIR": 20,
                "EISDIR": 21, "EINVAL": 22, "EMFILE": 24, "ENOSPC": 28, "ESPIPE": 29, "ENOSYS": 88,
                "ENAMETOOLONG": 91}

_MODES = ("rb", "rb", "r+b", "r+b", "wb", "wb", "w+b", "w+b", "ab", "ab", "a+b", "a+b")   # SYS_OPEN のモード 0〜11


class Op(IntEnum):
    SYS_OPEN = 0x01
    SYS_CLOSE = 0x02
    SYS_WRITEC = 0x03
    SYS_WRITE0 = 0x04
    SYS_WRITE = 0x05
    SYS_READ = 0x06
    SYS_READC = 0x07
    SYS_ISERROR = 0x08
    SYS_ISTTY = 0x09
    SYS_SEEK = 0x0A
    SYS_FLEN = 0x0C
    SYS_TMPNAM = 0x0D
    SYS_REMOVE = 0x0E
    SYS_RENAME = 0x0F
    SYS_CLOCK = 0x10
    SYS_TIME = 0x11
    SYS_SYSTEM = 0x12
    SYS_ERRNO = 0x13
    SYS_GET_CMDLINE = 0x15
    SYS_HEAPINFO = 0x16
    SYS_EXIT = 0x18
    SYS_EXIT_EXTENDED = 0x20
    SYS_ELAPSED = 0x30
    SYS_TICKFREQ = 0x31


class SemihostingTarget(Protocol):
    def read_core_register(self, name: str) -> int: ...          # "r0" / "r1" / "pc"
    def write_core_register(self, name: str, value: int) -> None: ...
    def read_memory(self, address: int, length: int) -> bytes: ...
    def write_memory(self, address: int, data: bytes) -> None: ...


class ResumableTarget(SemihostingTarget, Protocol):
    def resume(self) -> None: ...


@dataclass(frozen=True)
class SemihostingResult:
    handled: bool                       # セミホスティングの要求だった（BKPT 0xAB）
    operation: int | None = None
    exit_code: int | None = None        # SYS_EXIT / SYS_EXIT_EXTENDED のとき
    exit_reason: int | None = None
    unsupported: bool = False           # 知らない操作（-1 を返した）

    @property
    def exited(self) -> bool:
        return self.exit_code is not None


class _Refused(Exception):
    """要求を -1（と errno）で断る。"""

    def __init__(self, name: str):
        super().__init__(name)
        self.errno = TARGET_ERRNO[name]


# --- ファイルの許可 -------------------------------------------------------------------------

class FileAccess:
    """ターゲットに開かせてよいファイルの範囲。root=None なら一切開かせない（既定）。"""

    def __init__(self, root: str | Path | None = None, writable: bool = False, max_open: int = 16):
        if root is not None and is_network_path(root):
            raise ValueError(f"外の共有は root にできない: {root}")
        self.root = None if root is None else Path(root).resolve()
        self.writable, self.max_open = writable, max_open

    def resolve(self, name: str, write: bool) -> Path:
        """ターゲットの渡した名前を root の下の実パスにする。外へ出るもの・書き込みの不許可は _Refused。"""
        if self.root is None or (write and not self.writable):
            raise _Refused("EACCES")
        if (not name or "\0" in name or is_network_path(name) or PureWindowsPath(name).drive
                or PureWindowsPath(name).anchor or PurePosixPath(name).is_absolute()):
            raise _Refused("EACCES")
        path = (self.root / name).resolve()                       # .. とシンボリックリンクを解いてから確かめる
        if not path.is_relative_to(self.root):
            raise _Refused("EACCES")
        return path


@dataclass
class _Handle:
    kind: str                           # "stdin" / "stdout" / "stderr" / "file"
    file: BinaryIO | None = None

    @property
    def is_tty(self) -> bool:
        return self.kind != "file"


# --- 本体 ----------------------------------------------------------------------------------

def _default_console(stream: str, data: bytes) -> None:
    out = sys.stderr if stream == "stderr" else sys.stdout
    out.buffer.write(data)
    out.flush()


class SemihostingHost:
    """セミホスティングの要求を処理する。開いたファイル・errno・時計の基準を、要求をまたいで持つ。

    console(stream, data): ターゲットの出力（stream は "stdout" / "stderr"）。
    stdin(n): ターゲットへの入力（最大 n バイト。空なら入力なし = EOF）。None なら常に入力なし。
    """

    def __init__(self, console: Callable[[str, bytes], None] | None = None,
                 stdin: Callable[[int], bytes] | None = None, files: FileAccess | None = None, cmdline: str = "",
                 heapinfo: tuple[int, int, int, int] = (0, 0, 0, 0), clock: Callable[[], float] = time.monotonic,
                 wallclock: Callable[[], float] = time.time):
        self.console = console or _default_console
        self.stdin = stdin
        self.files = files or FileAccess()
        self.cmdline, self.heapinfo = cmdline, heapinfo
        self._clock, self._wallclock = clock, wallclock
        self._start = clock()
        self._handles: dict[int, _Handle] = {}
        self._next_handle = 1
        self.errno = 0

    def close(self) -> None:
        """開いているファイルをすべて閉じる。"""
        for h in self._handles.values():
            if h.file is not None:
                h.file.close()
        self._handles.clear()

    def service(self, target: ResumableTarget) -> SemihostingResult:
        """handle() して、セミホスティングの要求で終了でなければ再開する。"""
        result = self.handle(target)
        if result.handled and not result.exited:
            target.resume()
        return result

    def handle(self, target: SemihostingTarget) -> SemihostingResult:
        """止まった位置が BKPT 0xAB なら要求を処理し、R0 に結果を書いて PC を 2 進める（終了の要求では進めない）。"""
        pc = target.read_core_register("pc")
        if struct.unpack("<H", target.read_memory(pc, 2))[0] != BKPT_SEMIHOSTING:
            return SemihostingResult(False)
        op, r1 = target.read_core_register("r0"), target.read_core_register("r1")
        handler = _HANDLERS.get(op)
        if handler is None:
            self.errno = TARGET_ERRNO["ENOSYS"]
            self._finish(target, pc, -1)
            return SemihostingResult(True, op, unsupported=True)
        try:
            outcome = handler(self, target, r1)
        except _Refused as e:
            self.errno = e.errno
            outcome = -1
        if isinstance(outcome, SemihostingResult):
            return outcome
        self._finish(target, pc, outcome)
        return SemihostingResult(True, op)

    # --- 内部 ---------------------------------------------------------------------------

    @staticmethod
    def _finish(target: SemihostingTarget, pc: int, r0: int | None) -> None:
        if r0 is not None:
            target.write_core_register("r0", r0 & 0xFFFFFFFF)
        target.write_core_register("pc", pc + 2)

    @staticmethod
    def _args(target: SemihostingTarget, r1: int, count: int) -> tuple[int, ...]:
        return struct.unpack(f"<{count}I", target.read_memory(r1, 4 * count))

    @staticmethod
    def _name(target: SemihostingTarget, address: int, length: int) -> str:
        if length > NAME_LIMIT:
            raise _Refused("ENAMETOOLONG")
        return target.read_memory(address, length).decode("utf-8", errors="replace") if length else ""

    def _handle(self, number: int) -> _Handle:
        h = self._handles.get(number)
        if h is None:
            raise _Refused("EBADF")
        return h

    def _new_handle(self, h: _Handle) -> int:
        number = self._next_handle
        self._next_handle += 1
        self._handles[number] = h
        return number

    def _input(self, n: int) -> bytes:
        return self.stdin(n)[:n] if self.stdin is not None and n > 0 else b""

    # --- 操作 ---------------------------------------------------------------------------

    def _open(self, target, r1):
        name_ptr, mode, length = self._args(target, r1, 3)
        if mode >= len(_MODES):
            raise _Refused("EINVAL")
        name = self._name(target, name_ptr, length)
        if name == ":tt":
            return self._new_handle(_Handle(("stdin", "stdout", "stderr")[mode // 4]))
        if name == ":semihosting-features":
            if mode >= 2:
                raise _Refused("EACCES")
            return self._new_handle(_Handle("file", io.BytesIO(FEATURES)))
        path = self.files.resolve(name, write=mode >= 2)
        if sum(1 for h in self._handles.values() if h.file is not None) >= self.files.max_open:
            raise _Refused("EMFILE")
        try:
            f = open(path, _MODES[mode])
        except OSError as e:
            raise _Refused(_errno_name(e)) from e
        return self._new_handle(_Handle("file", f))

    def _close(self, target, r1):
        (number,) = self._args(target, r1, 1)
        h = self._handle(number)
        if h.file is not None:
            h.file.close()
        del self._handles[number]
        return 0

    def _writec(self, target, r1):
        self.console("stdout", target.read_memory(r1, 1))
        return None

    @staticmethod
    def _read_string_chunk(target, address: int) -> bytes:
        """NUL を探して読む 1 回分。64 バイトを読み、領域の終わりで失敗したら半分ずつ縮めて 1 バイトまで読み直す
        （終端が領域の中にある文字列を、先読みの失敗で落とさない）。1 バイトも読めなければその誤りを出す。"""
        size = 64
        while True:
            try:
                return target.read_memory(address, size)
            except Exception:                                  # noqa: BLE001  転送・領域外のエラーは種類を問わず縮めて読み直す
                if size == 1:
                    raise
                size //= 2

    def _write0(self, target, r1):
        text = bytearray()
        while len(text) < WRITE0_LIMIT:
            chunk = self._read_string_chunk(target, r1 + len(text))
            end = chunk.find(b"\0")
            text += chunk if end < 0 else chunk[:end]
            if end >= 0:
                break
        self.console("stdout", bytes(text[:WRITE0_LIMIT]))
        return None

    def _write(self, target, r1):
        number, buf, length = self._args(target, r1, 3)
        try:
            h = self._handle(number)
            if length > TRANSFER_LIMIT or h.kind == "stdin":
                raise _Refused("EINVAL" if length > TRANSFER_LIMIT else "EBADF")
            data = target.read_memory(buf, length) if length else b""
            if h.is_tty:
                self.console(h.kind, data)
                return 0
            try:
                return length - (h.file.write(data) or 0)
            except (OSError, io.UnsupportedOperation) as e:
                raise _Refused(_errno_name(e)) from e
        except _Refused as e:
            self.errno = e.errno
            return length                              # 書けなかったバイト数

    def _read(self, target, r1):
        number, buf, length = self._args(target, r1, 3)
        try:
            h = self._handle(number)
            if length > TRANSFER_LIMIT or h.kind in ("stdout", "stderr"):
                raise _Refused("EINVAL" if length > TRANSFER_LIMIT else "EBADF")
            try:
                data = self._input(length) if h.is_tty else h.file.read(length)
            except (OSError, io.UnsupportedOperation) as e:
                raise _Refused(_errno_name(e)) from e
        except _Refused as e:
            self.errno = e.errno
            return length                              # 読めなかったバイト数（全部）
        if data:
            target.write_memory(buf, data)
        return length - len(data)

    def _readc(self, target, r1):
        data = self._input(1)
        return data[0] if data else -1

    def _iserror(self, target, r1):
        (status,) = self._args(target, r1, 1)
        return 1 if status & 0x80000000 else 0

    def _istty(self, target, r1):
        (number,) = self._args(target, r1, 1)
        return 1 if self._handle(number).is_tty else 0

    def _seek(self, target, r1):
        number, position = self._args(target, r1, 2)
        h = self._handle(number)
        if h.file is None:
            raise _Refused("ESPIPE")
        h.file.seek(position)
        return 0

    def _flen(self, target, r1):
        (number,) = self._args(target, r1, 1)
        h = self._handle(number)
        if h.file is None:
            raise _Refused("ESPIPE")
        here = h.file.tell()
        size = h.file.seek(0, io.SEEK_END)
        h.file.seek(here)
        return size

    def _remove(self, target, r1):
        name_ptr, length = self._args(target, r1, 2)
        path = self.files.resolve(self._name(target, name_ptr, length), write=True)
        try:
            path.unlink()
        except OSError as e:
            raise _Refused(_errno_name(e)) from e
        return 0

    def _rename(self, target, r1):
        old_ptr, old_len, new_ptr, new_len = self._args(target, r1, 4)
        old = self.files.resolve(self._name(target, old_ptr, old_len), write=True)
        new = self.files.resolve(self._name(target, new_ptr, new_len), write=True)
        try:
            old.rename(new)
        except OSError as e:
            raise _Refused(_errno_name(e)) from e
        return 0

    def _clock_cs(self, target, r1):
        return int((self._clock() - self._start) * 100)

    def _time(self, target, r1):
        return int(self._wallclock())

    def _system(self, target, r1):
        raise _Refused("EACCES")                       # PC でコマンドを実行しない

    def _errno(self, target, r1):
        return self.errno

    def _get_cmdline(self, target, r1):
        buf, length = self._args(target, r1, 2)
        text = self.cmdline.encode()
        if len(text) + 1 > length:
            raise _Refused("EINVAL")
        target.write_memory(buf, text + b"\0")
        target.write_memory(r1 + 4, struct.pack("<I", len(text)))
        return 0

    def _heapinfo(self, target, r1):
        (block,) = self._args(target, r1, 1)
        target.write_memory(block, struct.pack("<4I", *self.heapinfo))
        return None

    def _exit(self, target, r1):
        return SemihostingResult(True, Op.SYS_EXIT, exit_code=0 if r1 == ADP_STOPPED_APPLICATION_EXIT else 1,
                                 exit_reason=r1)

    def _exit_extended(self, target, r1):
        reason, subcode = self._args(target, r1, 2)
        code = subcode if reason == ADP_STOPPED_APPLICATION_EXIT else (subcode or 1)
        return SemihostingResult(True, Op.SYS_EXIT_EXTENDED, exit_code=code, exit_reason=reason)

    def _elapsed(self, target, r1):
        ticks = int((self._clock() - self._start) * TICK_HZ)
        target.write_memory(r1, struct.pack("<Q", ticks))
        return 0

    def _tickfreq(self, target, r1):
        return TICK_HZ

    def _unsupported_known(self, target, r1):
        raise _Refused("ENOSYS")


def _errno_name(e: Exception) -> str:
    name = errno.errorcode.get(getattr(e, "errno", None) or 0, "EIO")
    if isinstance(e, PermissionError):
        name = "EACCES"
    elif isinstance(e, io.UnsupportedOperation):        # 読み出し専用で開いたファイルへの書き込み等
        name = "EBADF"
    return name if name in TARGET_ERRNO else "EIO"


_HANDLERS: dict[int, Callable] = {
    Op.SYS_OPEN: SemihostingHost._open,
    Op.SYS_CLOSE: SemihostingHost._close,
    Op.SYS_WRITEC: SemihostingHost._writec,
    Op.SYS_WRITE0: SemihostingHost._write0,
    Op.SYS_WRITE: SemihostingHost._write,
    Op.SYS_READ: SemihostingHost._read,
    Op.SYS_READC: SemihostingHost._readc,
    Op.SYS_ISERROR: SemihostingHost._iserror,
    Op.SYS_ISTTY: SemihostingHost._istty,
    Op.SYS_SEEK: SemihostingHost._seek,
    Op.SYS_FLEN: SemihostingHost._flen,
    Op.SYS_TMPNAM: SemihostingHost._unsupported_known,
    Op.SYS_REMOVE: SemihostingHost._remove,
    Op.SYS_RENAME: SemihostingHost._rename,
    Op.SYS_CLOCK: SemihostingHost._clock_cs,
    Op.SYS_TIME: SemihostingHost._time,
    Op.SYS_SYSTEM: SemihostingHost._system,
    Op.SYS_ERRNO: SemihostingHost._errno,
    Op.SYS_GET_CMDLINE: SemihostingHost._get_cmdline,
    Op.SYS_HEAPINFO: SemihostingHost._heapinfo,
    Op.SYS_EXIT: SemihostingHost._exit,
    Op.SYS_EXIT_EXTENDED: SemihostingHost._exit_extended,
    Op.SYS_ELAPSED: SemihostingHost._elapsed,
    Op.SYS_TICKFREQ: SemihostingHost._tickfreq,
}
