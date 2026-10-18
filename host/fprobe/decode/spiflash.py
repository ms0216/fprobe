"""SPI の解読の上の SPI NOR Flash のコマンド（JEDEC の一般的な命令）。仕様: docs/superpowers/specs/2026-10-18-u20-decoders-design.md

SPI の転送（CS の 1 回の有効の区間。SPI は cs と word_bits = 8 で解読しておく）を 1 つのコマンドとして読む:
最初の MOSI のバイトが命令、続いてアドレス（3 / 4 バイト。EN4B / EX4B で切り替わるのを追う）・ダミー・データ。
書き込みの許可（WEL）を追い、許可されていない書き込み・消去は error="write_disabled"（取りこぼしの原因の手がかり）。
マルチ I/O（Dual / Quad）の命令は名前だけ出す（1 本の MOSI / MISO ではデータを読めない）。
"""

from .core import Annotation, Decoding

__all__ = ["COMMANDS", "decode_spiflash"]

# 命令: (名前, アドレスのバイト数（None = 今のモード、0 = 無し）, ダミーのバイト数, データの向き "r" / "w" / None)
COMMANDS: dict[int, tuple[str, int | None, int, str | None]] = {
    0x03: ("READ", None, 0, "r"), 0x0B: ("FAST_READ", None, 1, "r"), 0x13: ("READ_4B", 4, 0, "r"),
    0x0C: ("FAST_READ_4B", 4, 1, "r"), 0x02: ("PAGE_PROGRAM", None, 0, "w"), 0x12: ("PAGE_PROGRAM_4B", 4, 0, "w"),
    0x06: ("WRITE_ENABLE", 0, 0, None), 0x04: ("WRITE_DISABLE", 0, 0, None), 0x50: ("VOLATILE_SR_WRITE_ENABLE", 0, 0, None),
    0x05: ("READ_STATUS", 0, 0, "r"), 0x35: ("READ_STATUS2", 0, 0, "r"), 0x15: ("READ_STATUS3", 0, 0, "r"),
    0x01: ("WRITE_STATUS", 0, 0, "w"), 0x31: ("WRITE_STATUS2", 0, 0, "w"), 0x11: ("WRITE_STATUS3", 0, 0, "w"),
    0x9F: ("READ_JEDEC_ID", 0, 0, "r"), 0x90: ("READ_MANUFACTURER_ID", 3, 0, "r"), 0xAB: ("RELEASE_POWER_DOWN", 0, 3, "r"),
    0x4B: ("READ_UNIQUE_ID", 0, 4, "r"), 0x5A: ("READ_SFDP", 3, 1, "r"),
    0x20: ("SECTOR_ERASE_4K", None, 0, None), 0x21: ("SECTOR_ERASE_4K_4B", 4, 0, None),
    0x52: ("BLOCK_ERASE_32K", None, 0, None), 0xD8: ("BLOCK_ERASE_64K", None, 0, None),
    0xDC: ("BLOCK_ERASE_64K_4B", 4, 0, None), 0xC7: ("CHIP_ERASE", 0, 0, None), 0x60: ("CHIP_ERASE", 0, 0, None),
    0xB9: ("POWER_DOWN", 0, 0, None), 0x66: ("RESET_ENABLE", 0, 0, None), 0x99: ("RESET", 0, 0, None),
    0xB7: ("ENTER_4B_ADDRESS", 0, 0, None), 0xE9: ("EXIT_4B_ADDRESS", 0, 0, None),
    0x75: ("SUSPEND", 0, 0, None), 0x7A: ("RESUME", 0, 0, None),
    0x3B: ("DUAL_OUTPUT_READ", None, 1, "multi"), 0x6B: ("QUAD_OUTPUT_READ", None, 1, "multi"),
    0xBB: ("DUAL_IO_READ", None, 0, "multi"), 0xEB: ("QUAD_IO_READ", None, 0, "multi"),
    0x32: ("QUAD_PAGE_PROGRAM", None, 0, "multi"),
}
_WRITES = {0x02, 0x12, 0x32, 0x20, 0x21, 0x52, 0xD8, 0xDC, 0xC7, 0x60, 0x01, 0x31, 0x11}   # WEL が要り、終われば落ちる
_SHOWN = 16                                          # 文字列に出すデータのバイト数


def decode_spiflash(lower: Decoding, *, address_bytes: int = 3) -> Decoding:
    """SPI の解読（cs あり・8 ビットの語）の上で SPI Flash のコマンドを読む。address_bytes は始まりのアドレス長。"""
    if lower.protocol != "spi":
        raise ValueError("spi の解読の上に重ねる")
    if address_bytes not in (3, 4):
        raise ValueError(f"アドレスは 3 か 4 バイト（{address_bytes}）")
    if lower.params.get("cs") is None or lower.params.get("word_bits", 8) != 8:
        raise ValueError("SPI は cs を与え、word_bits = 8 で解読する（転送の区切りがコマンドの区切り）")
    mode = address_bytes
    wel: bool | None = None                          # 書き込みの許可（分からなければ None）
    out: list[Annotation] = []
    for t in lower.of("transfer"):
        mosi, miso = t.fields["mosi"], t.fields["miso"]
        if not mosi:
            continue
        opcode = mosi[0]
        name, abytes, dummy, direction = COMMANDS.get(opcode, (None, 0, 0, None))
        abytes = mode if abytes is None else abytes
        fields: dict = {"opcode": opcode, "name": name, "address": None, "address_bytes": abytes or None,
                        "dummy": dummy, "direction": direction, "data": []}
        error = t.error
        header = 1 + abytes + dummy
        if len(mosi) < header:
            error = error or "short"
        if abytes and len(mosi) >= 1 + abytes:
            address = 0
            for b in mosi[1:1 + abytes]:
                address = address << 8 | b
            fields["address"] = address
        if direction == "r":
            fields["data"] = list(miso[header:])
        elif direction == "w":
            fields["data"] = list(mosi[header:])
        if direction == "multi":
            fields["multi_io"] = True
        if opcode in (0x05,) and fields["data"]:
            status = fields["data"][-1]
            fields["status"] = {"wip": bool(status & 1), "wel": bool(status & 2), "bp": status >> 2 & 0xF,
                                "srp": bool(status & 0x80)}
            wel = bool(status & 2)
        if opcode == 0x9F and len(fields["data"]) >= 3:
            d = fields["data"]
            fields["jedec"] = {"manufacturer": d[0], "memory_type": d[1], "capacity": d[2]}
        if opcode in _WRITES and error is None:
            if wel is False:
                error = "write_disabled"
            wel = False
        if opcode == 0x06:
            wel = True
        elif opcode == 0x04:
            wel = False
        elif opcode == 0xB7:
            mode = 4
        elif opcode == 0xE9:
            mode = 3
        fields["write_enabled"] = wel
        out.append(Annotation(t.start, t.end, "command", _text(opcode, name, fields), value=opcode, error=error,
                              fields=fields))
    return lower.derive("spiflash", out, {"address_bytes": address_bytes})


def _text(opcode: int, name: str | None, f: dict) -> str:
    text = name or f"0x{opcode:02X}"
    if f["address"] is not None:
        text += f" 0x{f['address']:0{2 * f['address_bytes']}X}"
    data = f["data"]
    if data:
        text += f" {len(data)} B: " + " ".join(f"{b:02X}" for b in data[:_SHOWN]) + (" …" if len(data) > _SHOWN else "")
    return text
