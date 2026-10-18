"""通信の解読。ロジアナの捕捉（Transitions）から、どのプロトコルも同じ形の注釈の列を返す。
解読の結果の上に別の解読器を重ねられ（例: SPI の上の SPI Flash）、利用者の Python の解読器も登録できる。

仕様: docs/superpowers/specs/2026-10-17-f7-protocol-decode-design.md、
docs/superpowers/specs/2026-10-18-u20-decoders-design.md
"""

from .can import decode_can
from .core import Annotation, ChannelLevels, Decoding, project
from .eeprom import decode_eeprom24
from .i2c import decode_i2c
from .i2s import decode_i2s
from .jtag import decode_jtag, decode_jtag_arm
from .manchester import decode_manchester
from .modbus import decode_modbus
from .onewire import decode_onewire
from .parallel import decode_parallel
from .registry import (BUILTIN, Decoder, decode, decode_on, decoder, decoders, get_decoder, load_file, register,
                       unregister)
from .spi import decode_spi
from .spiflash import decode_spiflash
from .swd import decode_swd
from .uart import decode_uart, estimate_baud

__all__ = ["Annotation", "ChannelLevels", "Decoder", "Decoding", "decode", "decode_can", "decode_eeprom24",
           "decode_i2c", "decode_i2s", "decode_jtag", "decode_jtag_arm", "decode_manchester", "decode_modbus",
           "decode_on", "decode_onewire", "decode_parallel", "decode_spi", "decode_spiflash", "decode_swd",
           "decode_uart", "decoder", "decoders", "estimate_baud", "get_decoder", "load_file", "project", "register",
           "unregister"]

# 組み込みの解読器: (名前, 関数, 下の解読, 要約, la.decode の既定の種類)
for _name, _function, _lower, _summary, _kinds in (
        ("uart", decode_uart, (), "UART（channel・baud（省略で推定）・data_bits・parity・stop_bits・invert）", None),
        ("spi", decode_spi, (), "SPI（sck・mosi・miso・cs・mode・word_bits・msb_first）", None),
        ("i2c", decode_i2c, (), "I2C（scl・sda）", None),
        ("swd", decode_swd, (), "SWD（swclk・swdio・drive）", ("transfer",)),
        ("onewire", decode_onewire, (), "1-Wire（channel・overdrive）", ("reset", "presence", "rom_command", "rom",
                                                                         "data")),
        ("jtag", decode_jtag, (), "JTAG（tck・tms・tdi・tdo・trst・initial_state・ir_names）", ("ir", "dr", "reset")),
        ("jtag_arm", decode_jtag_arm, ("jtag",), "JTAG の上の ARM JTAG-DP（DPACC・APACC・IDCODE。ir_bits）", None),
        ("i2s", decode_i2s, (), "I2S・左詰め・右詰め（sck・ws・sd・format・word_bits・signed）", None),
        ("parallel", decode_parallel, (), "パラレルバス（data・clock・edge・enable・enable_level・settle）", None),
        ("manchester", decode_manchester, (), "Manchester（channel・bitrate・convention・word_bits・msb_first）",
         ("word", "frame")),
        ("can", decode_can, (), "CAN / CAN FD（channel・bitrate・data_bitrate・sample_point・invert）", None),
        ("spiflash", decode_spiflash, ("spi",), "SPI の上の SPI NOR Flash のコマンド（address_bytes）", None),
        ("eeprom24", decode_eeprom24, ("i2c",), "I2C の上の 24 系 EEPROM（device・address_bytes・block_bits・page_size）",
         None),
        ("modbus", decode_modbus, ("uart",), "UART の上の Modbus RTU（gap_chars）", None),
):
    register(_name, _function, lower=_lower, summary=_summary, default_kinds=_kinds, origin=BUILTIN)
