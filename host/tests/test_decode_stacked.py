"""積み重ねる解読器: SPI の上の SPI Flash、I2C の上の 24 系 EEPROM、UART の上の Modbus RTU（ハードウェア不要）。"""

import pytest

from fprobe.decode import decode, decode_on
from fprobe.decode.modbus import modbus_crc
from fprobe.decode.synth import Wave, i2c, spi, uart

SCK, MOSI, MISO, CS = 0, 1, 2, 3
SPI = {"sck": SCK, "mosi": MOSI, "miso": MISO, "cs": CS}


# ---- SPI Flash ----

def flash_wave(transfers):
    w = Wave(channels=4, sample_rate_hz=20e6, idle={CS: 1})
    t = 1e-6
    for mosi, miso in transfers:
        t = spi(w, t, sck=SCK, mosi=MOSI, miso=MISO, cs=CS, clock_hz=1e6, mosi_words=mosi, miso_words=miso) + 2e-6
    return w.build(end_s=t)


def test_spiflash_commands_addresses_data_and_write_enable():
    tr = flash_wave([
        ([0x9F, 0, 0, 0], [0xFF, 0xEF, 0x40, 0x18]),                 # JEDEC ID
        ([0x03, 0x00, 0x10, 0x00, 0, 0, 0], [0] * 4 + [0xDE, 0xAD, 0xBE]),
        ([0x0B, 0x01, 0x02, 0x03, 0xFF, 0, 0], [0] * 5 + [0x11, 0x22]),   # 1 バイトのダミー
        ([0x02, 0, 0, 0x10, 0xAA], [0] * 5),                        # WREN が無い → 分からない（誤りにしない）
        ([0x04], [0]),                                               # WRDI
        ([0x20, 0, 0x20, 0], [0] * 4),                              # 許可されていない消去
        ([0x06], [0]),
        ([0x05, 0], [0, 0x03]),                                      # WIP・WEL
        ([0xB7], [0]),                                               # 4 バイトのアドレスへ
        ([0x03, 0x01, 0x00, 0x00, 0x00, 0], [0] * 5 + [0x5A]),
    ])
    d = decode("spiflash", tr, spi=SPI)
    c = d.of("command")
    assert [a.fields["name"] for a in c] == ["READ_JEDEC_ID", "READ", "FAST_READ", "PAGE_PROGRAM", "WRITE_DISABLE",
                                             "SECTOR_ERASE_4K", "WRITE_ENABLE", "READ_STATUS", "ENTER_4B_ADDRESS",
                                             "READ"]
    assert c[0].fields["jedec"] == {"manufacturer": 0xEF, "memory_type": 0x40, "capacity": 0x18}
    assert (c[1].fields["address"], c[1].fields["data"]) == (0x001000, [0xDE, 0xAD, 0xBE])
    assert c[1].text == "READ 0x001000 3 B: DE AD BE"
    assert (c[2].fields["address"], c[2].fields["data"]) == (0x010203, [0x11, 0x22])
    assert c[3].error is None and c[3].fields["data"] == [0xAA]
    assert c[5].error == "write_disabled"
    assert c[7].fields["status"]["wip"] and c[7].fields["status"]["wel"]
    assert (c[9].fields["address"], c[9].fields["address_bytes"], c[9].fields["data"]) == (0x01000000, 4, [0x5A])
    assert d.params["spi"]["cs"] == CS


def test_spiflash_needs_cs_and_bytes():
    tr = flash_wave([([0x06], [0])])
    with pytest.raises(ValueError):
        decode("spiflash", tr, spi={"sck": SCK, "mosi": MOSI})
    with pytest.raises(ValueError):
        decode("spiflash", tr, spi={**SPI, "word_bits": 16})


# ---- 24 系 EEPROM ----

def eeprom_wave(steps):
    w = Wave(channels=4, sample_rate_hz=4e6, idle={0: 1, 1: 1})
    t = 5e-6
    for kw in steps:
        t = i2c(w, t, scl=0, sda=1, clock_hz=100e3, **kw) + 20e-6
    return w.build(end_s=t)


def test_eeprom_write_random_read_current_read_and_polling():
    tr = eeprom_wave([
        {"address": 0x50, "data": [0x10, 1, 2, 3]},                  # 0x10 へ 3 バイト
        {"address": 0x50, "data": [], "address_ack": False},        # 書き込みサイクル中
        {"address": 0x50, "data": [0x10], "stop": False},           # ランダム読み
        {"address": 0x50, "read": True, "data": [1, 2], "restart": True},
        {"address": 0x50, "read": True, "data": [3]},                # 現在のアドレス（0x12）
        {"address": 0x68, "data": [0x00, 0x55]},                     # 別のデバイス（RTC）は読まない
    ])
    d = decode("eeprom24", tr, i2c={"scl": 0, "sda": 1})
    kinds = [a.kind for a in d]
    assert kinds == ["write", "poll", "read", "read"]
    write, _, rnd, cur = d.annotations
    assert (write.fields["address"], write.fields["data"]) == (0x10, [1, 2, 3])
    assert (rnd.fields["address"], rnd.fields["data"], rnd.fields["random"]) == (0x10, [1, 2], True)
    transfers = decode("i2c", tr, scl=0, sda=1).of("transfer")
    assert (rnd.start, rnd.end) == (transfers[2].start, transfers[3].end)       # アドレスの書きから読みの終わりまで
    assert (cur.fields["address"], cur.fields["random"]) == (0x12, False)


def test_eeprom_two_byte_addresses_page_wrap_and_blocks():
    tr = eeprom_wave([{"address": 0x50, "data": [0x01, 0xFE, 1, 2, 3, 4]},
                      {"address": 0x53, "data": [0x20, 9]}])
    d = decode("eeprom24", tr, i2c={"scl": 0, "sda": 1}, address_bytes=2, page_size=64)
    assert d.of("write")[0].error == "page_wrap" and d.of("write")[0].fields["address"] == 0x01FE
    d = decode("eeprom24", tr, i2c={"scl": 0, "sda": 1}, block_bits=3)
    first, second = d.of("write")
    assert second.fields["address"] == 0x3 << 8 | 0x20


# ---- Modbus RTU ----

BAUD = 19200


def frame(*body):
    crc = modbus_crc(body)
    return bytes(body) + bytes([crc & 0xFF, crc >> 8])


def modbus_wave(frames, gap_chars=4.0):
    w = Wave(channels=2, sample_rate_hz=BAUD * 16, idle={0: 1})
    t = 1e-3
    for f in frames:
        t = uart(w, 0, f, t, baud=BAUD, parity="even") + gap_chars * 11 / BAUD
    return w.build(end_s=t)


def test_modbus_crc_check_value():
    assert modbus_crc(b"123456789") == 0x4B37


def test_modbus_request_response_pairs_exceptions_and_crc_errors():
    frames = [frame(1, 3, 0x00, 0x10, 0x00, 0x02),                  # 保持レジスタ 0x10 から 2 個
              frame(1, 3, 4, 0x12, 0x34, 0x56, 0x78),
              frame(1, 6, 0x00, 0x05, 0xAB, 0xCD),
              frame(1, 6, 0x00, 0x05, 0xAB, 0xCD),                  # エコー
              frame(2, 16, 0x00, 0x00, 0x00, 0x02, 4, 0, 1, 0, 2),
              frame(2, 16, 0x00, 0x00, 0x00, 0x02),
              frame(1, 0x83, 2),                                     # 例外
              frame(1, 1, 0, 0, 0, 10)[:-1] + b"\x00"]              # CRC の誤り
    d = decode("modbus", modbus_wave(frames), uart={"channel": 0, "baud": BAUD, "parity": "even"})
    a = d.annotations
    assert [x.kind for x in a] == ["request", "response", "request", "response", "request", "response",
                                   "exception", "request"]
    assert (a[0].fields["start"], a[0].fields["count"]) == (0x10, 2)
    assert a[1].fields["values"] == [0x1234, 0x5678] and a[1].fields["latency_s"] > 0
    assert a[1].text == "← 01 READ_HOLDING_REGISTERS [0x1234, 0x5678]"
    assert a[3].fields["values"] == [0xABCD]
    assert a[4].fields["values"] == [1, 2] and (a[5].fields["start"], a[5].fields["count"]) == (0, 2)
    assert (a[6].fields["exception_name"], a[6].error) == ("ILLEGAL_DATA_ADDRESS", "exception")
    assert a[7].error == "crc" and not a[7].fields["crc_ok"]
    assert d.params["uart"]["parity"] == "even"


def test_modbus_frames_split_by_silence_only():
    frames = [frame(1, 3, 0, 0, 0, 1), frame(1, 3, 2, 0, 7)]
    tr = modbus_wave(frames, gap_chars=0.5)                          # 無音が短すぎて区切れない
    lower = decode("uart", tr, channel=0, baud=BAUD, parity="even")
    d = decode_on("modbus", lower)
    assert len(d) == 1 and d.annotations[0].error == "crc"
    assert len(decode_on("modbus", lower, gap_chars=0.3)) == 2
