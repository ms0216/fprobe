"""プラグイン: SPI NOR Flash を直接読み書きする（SEGGER J-Flash SPI に当たる。台帳 U42）。

本体の汎用の API（spi.transfer・gpio.set・gpio.release）だけを使う。FPGA には何も足さない（SPI は拡張ヘッダの
gpio のビットバング）。仕組みは fprobe.plugins の説明を参照。3.3 V の Flash に限る（Primer の I/O は 3.3 V）。

    from fprobe.plugins import ProbeCaller
    from fprobe.plugins.spiflash import SpiFlash
    with SpiFlash(client, hold={2: 0}) as flash:     # DaemonClient か ProbeCaller(probe)。nRESET（ch2）を Low に保つ
        print(flash.jedec_id(), flash.geometry)
        flash.unprotect()
        flash.write(0, open("image.bin", "rb").read())   # 足りない部分の読み直し → 消去 → 書き込み → ベリファイ
        data = flash.read(0, 4096)

端子（既定）は JTAG の線に合わせる: SCK = ch0（TCK）、MOSI = ch1（TDI）、MISO = ch6（TDO）、CS# = ch3（TMS）。
hold = {ch: 0 / 1} は Flash の間、ほかの端子を決めた値で駆動する（基板のマイコンをリセットに保つ nRESET = ch2 を 0、
WP#・HOLD# を 1 等）。閉じると SPI と hold の端子を離す（普段の機能に戻す）。

手順は JEDEC の標準: 9Fh（JEDEC ID）、5Ah（SFDP、JESD216 の基本表から容量・ページ・消去の種類・QE の規則）、
05h / 35h / 01h（状態）、06h（書き込み許可）、03h（読み出し）、02h（ページの書き込み）、消去は SFDP の種類
（無ければ 20h 4 KB・52h 32 KB・D8h 64 KB）と C7h（全体）。16 MB を超える Flash は B7h で 4 バイトの番地にし、
閉じるとき E9h で戻す。
仕様: docs/superpowers/specs/2026-10-19-u42-spi-flash-design.md
"""

import time
from dataclasses import dataclass

from . import ApiCaller

__all__ = ["EraseType", "FlashGeometry", "JedecId", "SpiFlash", "SpiFlashError"]

READ, PAGE_PROGRAM, WRITE_ENABLE = 0x03, 0x02, 0x06
READ_SR1, READ_SR2, WRITE_SR = 0x05, 0x35, 0x01
JEDEC_ID, SFDP, CHIP_ERASE = 0x9F, 0x5A, 0xC7
RELEASE_POWER_DOWN, ENTER_4BYTE, EXIT_4BYTE = 0xAB, 0xB7, 0xE9
GLOBAL_UNLOCK = 0x98                     # SST26 / Microchip のブロック保護の一括解除

WIP, WEL = 0x01, 0x02
DEFAULT_ERASES = ((65536, 0xD8), (32768, 0x52), (4096, 0x20))
SFDP_SIGNATURE = b"SFDP"
BFPT_ID = 0xFF00                          # 基本表（Basic Flash Parameter Table）

# QE（クアッドの有効）の規則（JESD216 の基本表 DWORD15 bit 22:20）。保護を外すとき QE を壊さないために使う
QER_NONE, QER_SR2_1BYTE_CLEARS, QER_SR1_BIT6, QER_SR2_BIT7, QER_SR2_BIT1, QER_SR2_READ_35 = range(6)
QER_BY_MANUFACTURER = {0xC2: QER_SR1_BIT6, 0xEF: QER_SR2_READ_35, 0xC8: QER_SR2_READ_35}   # SFDP が無いとき


class SpiFlashError(RuntimeError):
    pass


@dataclass(frozen=True)
class JedecId:
    manufacturer: int
    memory_type: int
    capacity: int

    def __str__(self) -> str:
        return f"{self.manufacturer:02X} {self.memory_type:02X} {self.capacity:02X}"


@dataclass(frozen=True)
class EraseType:
    size: int
    opcode: int


@dataclass(frozen=True)
class FlashGeometry:
    size: int                                  # バイト
    page_size: int
    erase_types: tuple[EraseType, ...]         # 大きい順
    address_bytes: int                         # 3 か 4
    quad_enable: int | None                    # QE の規則（QER_*）。分からなければ None
    source: str                                # "sfdp" / "jedec"

    @property
    def min_erase(self) -> int:
        return self.erase_types[-1].size


def _parse_sfdp_header(raw: bytes) -> tuple[int, int] | None:
    """SFDP の頭（8 バイト + 引数の頭 8 バイト × n）→ 基本表の (位置, DWORD の数)。署名が無ければ None。"""
    if raw[:4] != SFDP_SIGNATURE:
        return None
    count = raw[6] + 1
    for i in range(count):
        h = raw[8 + 8 * i:16 + 8 * i]
        if len(h) < 8:
            break
        ident = h[7] << 8 | h[0]
        if ident == BFPT_ID:
            return int.from_bytes(h[4:7], "little"), h[3]
    return None


def _geometry_from_bfpt(dwords: list[int]) -> FlashGeometry:
    d1, d2 = dwords[0], dwords[1]
    size = (1 << (d2 & 0x7FFF_FFFF)) // 8 if d2 >> 31 else (d2 + 1) // 8
    erases = []
    for d in dwords[7:9]:
        for shift in (0, 16):
            n, opcode = d >> shift & 0xFF, d >> (shift + 8) & 0xFF
            if n:
                erases.append(EraseType(1 << n, opcode))
    if not erases and d1 & 0b11 == 0b01:
        erases.append(EraseType(4096, d1 >> 8 & 0xFF))
    page = 1 << (dwords[10] >> 4 & 0xF) if len(dwords) >= 11 and dwords[10] >> 4 & 0xF else 256
    quad = dwords[14] >> 20 & 0b111 if len(dwords) >= 15 else None
    address_bytes = 4 if size > 1 << 24 or (d1 >> 17 & 0b11) == 0b10 else 3
    return FlashGeometry(size=size, page_size=page, erase_types=tuple(sorted(erases, key=lambda e: -e.size)),
                         address_bytes=address_bytes, quad_enable=quad, source="sfdp")


class SpiFlash:
    def __init__(self, api: ApiCaller, sck: int = 0, mosi: int = 1, miso: int = 6, cs: int = 3, mode: int = 0,
                 stretch: int = 0, hold: dict[int, int] | None = None, chunk: int = 32768):
        """mode は 0 か 3（SPI NOR Flash はどちらも受ける）。stretch は長い配線で化けるときに上げる（各状態を
        1 + stretch 回）。chunk は 1 回の spi.transfer で読むバイト数。"""
        if mode not in (0, 3):
            raise ValueError("SPI NOR Flash の mode は 0 か 3")
        self.api = api
        self.pins = {"sck": sck, "mosi": mosi, "miso": miso, "cs": cs}
        self.mode, self.stretch, self.chunk = mode, stretch, chunk
        self.hold = dict(hold or {})
        if set(self.hold) & set(self.pins.values()):
            raise ValueError("hold の端子が SPI の端子と重なる")
        self._ident: JedecId | None = None
        self._geometry: FlashGeometry | None = None
        self._four_byte_mode = False
        self._open = False

    # --- 下の層: 1 回の転送 ---------------------------------------------------------------------

    def _transfer(self, data: bytes, read_length: int = 0) -> bytes:
        out = self.api.call("spi.transfer", **self.pins, mode=self.mode, stretch=self.stretch, data=data.hex(),
                            read_length=read_length)
        return bytes.fromhex(out["received"])

    def _command(self, opcode: int, address: int | None = None, data: bytes = b"", read: int = 0,
                 dummy: int = 0, address_bytes: int | None = None) -> bytes:
        """命令 → 番地 → ダミー → data を送り、read バイト読んで返す。"""
        head = bytes([opcode])
        if address is not None:
            n = address_bytes or self.address_bytes
            head += address.to_bytes(n, "big")
        head += bytes(dummy)
        received = self._transfer(head + data, read)
        return received[len(head) + len(data):]

    @property
    def address_bytes(self) -> int:
        return self._geometry.address_bytes if self._geometry else 3

    # --- 開く・閉じる -------------------------------------------------------------------------

    def _pin_mask(self) -> int:
        return sum(1 << p for p in [*self.pins.values(), *self.hold])

    def open(self) -> "SpiFlash":
        if self._open:
            return self
        try:
            if self.hold:
                mask = sum(1 << p for p in self.hold)
                drive = sum(1 << p for p, v in self.hold.items() if v)
                self.api.call("gpio.set", drive=drive, enable=mask, mask=mask)
            self._command(RELEASE_POWER_DOWN)                 # 深い省電力から起こす（起きていれば何もしない）
            ident = self.jedec_id()
            if ident.manufacturer in (0x00, 0xFF):
                raise SpiFlashError(f"SPI Flash の応答が無い（JEDEC ID {ident}）: 配線（SCK ch{self.pins['sck']}・"
                                    f"MOSI ch{self.pins['mosi']}・MISO ch{self.pins['miso']}・CS# ch{self.pins['cs']}）"
                                    "・電源（3.3 V）・基板のマイコンが線を使っていないか（hold で nRESET を Low に）")
            self._geometry = self._read_geometry(ident)
            if self._geometry.address_bytes == 4 and (self._geometry.size > 1 << 24):
                self._command(WRITE_ENABLE)
                self._command(ENTER_4BYTE)
                self._four_byte_mode = True
            self._open = True
            return self
        except BaseException:
            self._release()
            raise

    def close(self) -> None:
        try:
            if self._open and self._four_byte_mode:
                self._command(EXIT_4BYTE)
        finally:
            self._four_byte_mode = False
            self._open = False
            self._release()

    def _release(self) -> None:
        self.api.call("gpio.release", mask=self._pin_mask())

    def __enter__(self) -> "SpiFlash":
        return self.open()

    def __exit__(self, *exc) -> None:
        self.close()

    # --- 識別 ---------------------------------------------------------------------------------

    def jedec_id(self) -> JedecId:
        raw = self._command(JEDEC_ID, read=3)
        self._ident = JedecId(*raw)
        return self._ident

    def read_sfdp(self, address: int, length: int) -> bytes:
        return self._command(SFDP, address, dummy=1, read=length, address_bytes=3)

    def _read_geometry(self, ident: JedecId) -> FlashGeometry:
        located = _parse_sfdp_header(self.read_sfdp(0, 16 + 8 * 3))
        if located is not None:
            where, count = located
            raw = self.read_sfdp(where, 4 * count)
            dwords = [int.from_bytes(raw[i:i + 4], "little") for i in range(0, len(raw), 4)]
            if len(dwords) >= 9:
                return _geometry_from_bfpt(dwords)
        # SFDP が無い古い Flash: 容量は JEDEC ID の 3 バイト目（多くの会社で 2^n バイト）、消去は定番の 3 種類
        if not 10 <= ident.capacity <= 31:
            raise SpiFlashError(f"容量が分からない（SFDP なし、JEDEC ID {ident}）")
        size = 1 << ident.capacity
        return FlashGeometry(size=size, page_size=256, erase_types=tuple(EraseType(*e) for e in DEFAULT_ERASES),
                             address_bytes=4 if size > 1 << 24 else 3,
                             quad_enable=QER_BY_MANUFACTURER.get(ident.manufacturer), source="jedec")

    @property
    def geometry(self) -> FlashGeometry:
        if self._geometry is None:
            raise SpiFlashError("open（with SpiFlash(...)）してから")
        return self._geometry

    # --- 状態・保護 -----------------------------------------------------------------------------

    def status(self) -> int:
        return self._command(READ_SR1, read=1)[0]

    def wait_ready(self, timeout_s: float, poll_s: float = 0.0) -> None:
        deadline = time.monotonic() + timeout_s
        while self.status() & WIP:
            if time.monotonic() > deadline:
                raise SpiFlashError(f"Flash が {timeout_s} 秒たっても書き込み・消去を終えない")
            if poll_s:
                time.sleep(poll_s)

    def _write_enable(self) -> None:
        self._command(WRITE_ENABLE)
        if not self.status() & WEL:
            raise SpiFlashError("書き込み許可（WEL）が立たない: 配線（MOSI・CS#）か、WP# / 電源を確認")

    def unprotect(self) -> dict:
        """状態レジスタの保護のビット（BP・TB・SEC・SRP）を 0 にする。QE（クアッドの有効）は SFDP の規則に従って
        残す（Macronix は SR1 の bit 6、Winbond 等は SR2 を 2 バイト目に書き戻す）。SST26 は 98h で一括解除も送る。"""
        geo = self.geometry
        quad = geo.quad_enable
        if quad is None:
            quad = QER_BY_MANUFACTURER.get(self._ident.manufacturer if self._ident else -1, QER_NONE)
        keep_bit6 = quad == QER_SR1_BIT6
        clear = 0xBC if keep_bit6 else 0xFC                 # bit 2〜6（BP・TB・SEC）と bit 7（SRP0）
        two_bytes = quad in (QER_SR2_1BYTE_CLEARS, QER_SR2_READ_35)
        sr1 = self.status()
        sr2 = self._command(READ_SR2, read=1)[0] if two_bytes else None
        before = [sr1, sr2]
        if sr1 & clear:
            self._write_enable()
            value = bytes([sr1 & ~clear & 0xFC]) + (bytes([sr2]) if two_bytes else b"")
            self._command(WRITE_SR, data=value)
            self.wait_ready(1.0)
        if self._ident is not None and self._ident.manufacturer == 0xBF:
            self._write_enable()
            self._command(GLOBAL_UNLOCK)
            self.wait_ready(1.0)
        sr1 = self.status()
        sr2 = self._command(READ_SR2, read=1)[0] if two_bytes else None
        result = {"before": before, "after": [sr1, sr2], "unprotected": not sr1 & clear}
        if not result["unprotected"]:
            result["note"] = ("保護のビットが残った: WP# が Low で SRP0 が 1 だと書けない（hold で WP# を High に）、"
                              "または一度だけ書ける（OTP）保護")
        return result

    # --- 読み・消去・書き -----------------------------------------------------------------------

    def _check_range(self, address: int, length: int) -> None:
        if address < 0 or length < 0 or address + length > self.geometry.size:
            raise ValueError(f"範囲 0x{address:X}+0x{length:X} が Flash（0x{self.geometry.size:X} バイト）の外")

    def read(self, address: int, length: int) -> bytes:
        self._check_range(address, length)
        out = bytearray()
        while len(out) < length:
            n = min(self.chunk, length - len(out))
            out += self._command(READ, address + len(out), read=n)
        return bytes(out)

    def verify(self, address: int, data: bytes) -> int | None:
        """一致すれば None、違えば最初に違う番地。"""
        actual = self.read(address, len(data))
        for i, (a, b) in enumerate(zip(actual, data)):
            if a != b:
                return address + i
        return None

    def erase_chip(self, timeout_s: float | None = None) -> None:
        self._write_enable()
        self._command(CHIP_ERASE)
        self.wait_ready(timeout_s or max(60.0, self.geometry.size / 65536 * 2.0), poll_s=0.05)

    def erase(self, address: int, length: int) -> int:
        """範囲を消す（両端は最小の消去の大きさの倍数）。並びに合う一番大きい消去の種類を選ぶ。消したバイト数を返す。"""
        geo = self.geometry
        self._check_range(address, length)
        unit = geo.min_erase
        if address % unit or length % unit:
            raise ValueError(f"消去の範囲は {unit} バイトの倍数（0x{address:X}+0x{length:X}）")
        at, end = address, address + length
        while at < end:
            kind = next(e for e in geo.erase_types if at % e.size == 0 and at + e.size <= end)
            self._write_enable()
            self._command(kind.opcode, at)
            self.wait_ready(max(2.0, kind.size / 4096 * 0.6), poll_s=0.005)
            at += kind.size
        return length

    def program(self, address: int, data: bytes, skip_blank: bool = True) -> int:
        """消してある所にページ単位で書く（ページの境目で分ける）。全部 0xFF のページは飛ばす。書いたバイト数を返す。"""
        self._check_range(address, len(data))
        page = self.geometry.page_size
        written, i = 0, 0
        while i < len(data):
            n = min(page - (address + i) % page, len(data) - i)
            chunk = data[i:i + n]
            if not (skip_blank and chunk.count(0xFF) == n):
                self._write_enable()
                self._command(PAGE_PROGRAM, address + i, data=chunk)
                self.wait_ready(0.5)
                written += n
            i += n
        return written

    def write(self, address: int, data: bytes, verify: bool = True) -> dict:
        """J-Flash の Auto に当たる: 書く範囲を含む消去の単位のうち一部だけ書くものは先に読んで残りを保ち、消して、
        書いて、ベリファイする。結果は段ごとの時間と速さ。"""
        geo = self.geometry
        self._check_range(address, len(data))
        unit = geo.min_erase
        start = address - address % unit
        end = -(-(address + len(data)) // unit) * unit
        t0 = time.perf_counter()
        image = bytearray()
        if start < address:
            image += self.read(start, address - start)
        image += data
        if address + len(data) < end:
            image += self.read(address + len(data), end - address - len(data))
        t1 = time.perf_counter()
        erased = self.erase(start, end - start)
        t2 = time.perf_counter()
        self.program(start, bytes(image))
        t3 = time.perf_counter()
        result = {"address": address, "bytes": len(data), "erased": erased, "verified": False,
                  "read_back_s": t1 - t0, "erase_s": t2 - t1, "program_s": t3 - t2}
        if verify:
            bad = self.verify(start, bytes(image))
            result["verify_s"] = time.perf_counter() - t3
            if bad is not None:
                sr = self.status()
                raise SpiFlashError(f"ベリファイ失敗: 0x{bad:X} が違う（状態 0x{sr:02X}。保護のビットが立っていれば"
                                    " unprotect、配線が長ければ stretch を上げる）")
            result["verified"] = True
        total = time.perf_counter() - t0
        result["total_s"] = total
        result["kib_per_s"] = len(data) / 1024 / total if total > 0 else None
        return result
