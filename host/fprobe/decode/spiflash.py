"""SPI の解読の上の SPI NOR Flash のコマンド。仕様: docs/superpowers/specs/2026-10-18-u20-decoders-design.md

SPI の転送（CS の 1 回の有効の区間。SPI は cs と word_bits = 8 で解読しておく）を 1 つのコマンドとして読む:
最初の MOSI のバイトが命令、続いてアドレス（3 / 4 バイト。EN4B / EX4B で切り替わるのを追う）・ダミー・データ。
書き込みの許可（WEL）を追い、許可されていない書き込み・消去は error="write_disabled"（取りこぼしの原因の手がかり）。
マルチ I/O（Dual / Quad）の命令は名前だけ出す（1 本の MOSI / MISO ではデータを読めない）。

既定の表（JEDEC_COMMANDS）は、JEDEC の文書が命令の値を決めているものだけ（下の出典）。消去（20h・D8h 等）・マルチ I/O の
読み・第 2 / 第 3 の状態のレジスタ（35h・15h・31h・11h）・一時停止（75h / 7Ah）・固有の ID（4Bh・90h・ABh）等は、機器が
SFDP で申告するか製造元で意味が違うので既定に入れない。使う機器の命令は extra_commands で明示して渡す（データシートか
SFDP の値。よく見る値の表はプラグイン fprobe.plugins.heuristics.spiflash_conventional_commands）。表に無い命令は名前を
付けず、アドレス・データを切り出さず、許可の誤りも付けない。
"""

from collections.abc import Mapping

from .core import Annotation, Decoding

__all__ = ["JEDEC_COMMANDS", "JEDEC_WRITES", "decode_spiflash"]

# 命令: (名前, アドレスのバイト数（None = 今のモード、0 = 無し）, ダミーのバイト数, データの向き "r" / "w" / "multi" / None)
# 出典: JESD216（Serial Flash Discoverable Parameters）— SFDP の読み 5Ah（3 バイトのアドレス・8 クロックのダミー）、
# 状態のレジスタの読み 05h（WIP = bit 0・WEL = bit 1 で終わりを待つ）・書き 01h、書き込みの許可 06h / 50h（揮発の状態の
# レジスタ）、4 バイトのアドレスに入る B7h・出る E9h、ソフトウェアのリセット 66h → 99h（JESD252 も同じ）、4 バイトの命令の
# 表（4BAIT）の READ 13h・FAST_READ 0Ch・PAGE_PROGRAM 12h と、その 3 バイトの命令 03h・0Bh・02h。JEDEC の ID の読み 9Fh
# （製造元の番号は JEP106）。書き込みの許可を落とす 04h。
JEDEC_COMMANDS: dict[int, tuple[str, int | None, int, str | None]] = {
    0x03: ("READ", None, 0, "r"), 0x0B: ("FAST_READ", None, 1, "r"), 0x13: ("READ_4B", 4, 0, "r"),
    0x0C: ("FAST_READ_4B", 4, 1, "r"), 0x02: ("PAGE_PROGRAM", None, 0, "w"), 0x12: ("PAGE_PROGRAM_4B", 4, 0, "w"),
    0x06: ("WRITE_ENABLE", 0, 0, None), 0x04: ("WRITE_DISABLE", 0, 0, None), 0x50: ("VOLATILE_SR_WRITE_ENABLE", 0, 0, None),
    0x05: ("READ_STATUS", 0, 0, "r"), 0x01: ("WRITE_STATUS", 0, 0, "w"),
    0x9F: ("READ_JEDEC_ID", 0, 0, "r"), 0x5A: ("READ_SFDP", 3, 1, "r"),
    0x66: ("RESET_ENABLE", 0, 0, None), 0x99: ("RESET", 0, 0, None),
    0xB7: ("ENTER_4B_ADDRESS", 0, 0, None), 0xE9: ("EXIT_4B_ADDRESS", 0, 0, None),
}
JEDEC_WRITES = frozenset({0x02, 0x12, 0x01})          # WEL が要り、終われば落ちる
_SHOWN = 16                                          # 文字列に出すデータのバイト数
_DIRECTIONS = ("r", "w", "multi", None)


def _extra(commands: Mapping[str, Mapping] | None) -> tuple[dict, set[int]]:
    """extra_commands（{"0x20": {"name", "address_bytes", "dummy", "direction", "write"}}）→ (命令の表, 許可の要る命令)。
    欄はすべて明示する（既定の値で埋めない）。"""
    table, writes = {}, set()
    for key, spec in (commands or {}).items():
        try:
            opcode = int(key, 0) if isinstance(key, str) else int(key)
        except ValueError:
            raise ValueError(f"extra_commands の命令の値が読めない: {key!r}") from None
        need = ("name", "address_bytes", "dummy", "direction", "write")
        missing = [k for k in need if k not in spec]
        if missing or not 0 <= opcode <= 0xFF:
            raise ValueError(f"extra_commands の 0x{opcode:02X}: 欄 {', '.join(need)} をすべて渡す（足りない: "
                             f"{', '.join(missing) or 'なし'}。命令は 0〜0xFF）")
        if spec["address_bytes"] not in (None, 0, 3, 4) or spec["direction"] not in _DIRECTIONS:
            raise ValueError(f"extra_commands の 0x{opcode:02X}: address_bytes は null / 0 / 3 / 4、direction は "
                             f"r / w / multi / null")
        table[opcode] = (str(spec["name"]), spec["address_bytes"], int(spec["dummy"]), spec["direction"])
        if spec["write"]:
            writes.add(opcode)
    return table, writes


def decode_spiflash(lower: Decoding, *, address_bytes: int, extra_commands: dict | None = None) -> Decoding:
    """SPI の解読（cs あり・8 ビットの語）の上で SPI Flash のコマンドを読む。address_bytes は捕捉の始まりのアドレス長
    （3 か 4。機器と、それまでの EN4B / EX4B で決まるので既定を持たない）。extra_commands は JEDEC で決まった命令
    （JEDEC_COMMANDS）に足す機器の命令: {"0x20": {"name": "SECTOR_ERASE_4K", "address_bytes": null（今のモード）/ 0 / 3 / 4,
    "dummy": ダミーのバイト数, "direction": "r" / "w" / "multi" / null, "write": 書き込みの許可が要るか}}（同じ値なら上書き）。"""
    if lower.protocol != "spi":
        raise ValueError("spi の解読の上に重ねる")
    if address_bytes not in (3, 4):
        raise ValueError(f"アドレスは 3 か 4 バイト（{address_bytes}）")
    if lower.params.get("cs") is None or lower.params.get("word_bits") != 8:
        raise ValueError("SPI は cs を与え、word_bits = 8 で解読する（転送の区切りがコマンドの区切り）")
    extra, extra_writes = _extra(extra_commands)
    commands = {**JEDEC_COMMANDS, **extra}
    writes = (JEDEC_WRITES - extra.keys()) | extra_writes
    mode = address_bytes
    wel: bool | None = None                          # 書き込みの許可（分からなければ None）
    out: list[Annotation] = []
    for t in lower.of("transfer"):
        mosi, miso = t.fields["mosi"], t.fields["miso"]
        if not mosi:
            continue
        opcode = mosi[0]
        name, abytes, dummy, direction = commands.get(opcode, (None, 0, 0, None))
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
        if opcode == 0x05 and opcode not in extra and fields["data"]:
            status = fields["data"][-1]                # JESD216: WIP = bit 0・WEL = bit 1（ほかのビットは機器ごと）
            fields["status"] = {"value": status, "wip": bool(status & 1), "wel": bool(status & 2)}
            wel = bool(status & 2)
        if opcode == 0x9F and opcode not in extra and len(fields["data"]) >= 3:
            d = fields["data"]
            fields["jedec"] = {"manufacturer": d[0], "memory_type": d[1], "capacity": d[2]}
        if opcode in writes and error is None:
            if wel is False:
                error = "write_disabled"
            wel = False
        if opcode not in extra:                        # 使う側が上書きした命令の意味は表のとおりにだけ読む
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
    params = {"address_bytes": address_bytes}
    if extra_commands:
        params["extra_commands"] = dict(extra_commands)
    return lower.derive("spiflash", out, params)


def _text(opcode: int, name: str | None, f: dict) -> str:
    text = name or f"0x{opcode:02X}"
    if f["address"] is not None:
        text += f" 0x{f['address']:0{2 * f['address_bytes']}X}"
    data = f["data"]
    if data:
        text += f" {len(data)} B: " + " ".join(f"{b:02X}" for b in data[:_SHOWN]) + (" …" if len(data) > _SHOWN else "")
    return text
