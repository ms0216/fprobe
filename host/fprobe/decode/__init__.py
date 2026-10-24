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
from .uart import decode_uart

__all__ = ["Annotation", "ChannelLevels", "Decoder", "Decoding", "decode", "decode_can", "decode_eeprom24",
           "decode_i2c", "decode_i2s", "decode_jtag", "decode_jtag_arm", "decode_manchester", "decode_modbus",
           "decode_on", "decode_onewire", "decode_parallel", "decode_spi", "decode_spiflash", "decode_swd",
           "decode_uart", "decoder", "decoders", "get_decoder", "load_file", "project", "register",
           "unregister"]

# 組み込みの解読器: (名前, 関数, 下の解読, 要約, la.decode の既定の種類)
# 通信の設定（枠の値・線の割り当て）は既定を持たない（必須。つながない線は None を明示）。既定を残したのは、規格の値
# （jtag_arm の ir_bits・1-Wire の timing・Modbus の区切り t3.5）、調べない・付けないことを表す None（eeprom24 の
# page_size・jtag の ir_names）、事実で同期する None（jtag の initial_state）、雑音を捨てない 0（parallel の settle）だけ
for _name, _function, _lower, _summary, _kinds in (
        ("uart", decode_uart, (), "UART（channel・baud・data_bits・parity・stop_bits・invert・lsb_first。すべて必須。"
                                  "ボーは推定しない）", None),
        ("spi", decode_spi, (), "SPI（sck・mosi・miso・cs（つながなければ null）・mode・msb_first・word_bits・"
                                "cs_active_low。すべて必須）", None),
        ("i2c", decode_i2c, (), "I2C（scl・sda）", None),
        ("swd", decode_swd, (), "SWD（swclk・swdio・drive（つながなければ null）・turnaround・orundetect（捕捉の先頭の値）。"
                                "すべて必須）", ("transfer",)),
        ("onewire", decode_onewire, (), "1-Wire（channel・overdrive（捕捉の始まりの速度）は必須。timing の既定は規格の値）",
         ("reset", "presence", "rom_command", "rom", "data")),
        ("jtag", decode_jtag, (), "JTAG（tck・tms・tdi・tdo・trst（つながなければ null）は必須。initial_state を省くと "
                                  "TMS = 1 が 5 回続くまで待つ。ir_names）", ("ir", "dr", "reset")),
        ("jtag_arm", decode_jtag_arm, ("jtag",), "JTAG の上の ARM JTAG-DP（DPACC・APACC・IDCODE。ir_offset・dr_offset は"
                                                 "必須。ir_bits の既定 4 は ADIv5 の値）", None),
        ("i2s", decode_i2s, (), "I2S・左詰め・右詰め（sck・ws・sd・format・word_bits（null = スロットの全ビット）・"
                                "signed。すべて必須）", None),
        ("parallel", decode_parallel, (), "パラレルバス（data・clock・enable（つながなければ null）は必須。edge は clock を、"
                                          "enable_level は enable を与えたとき必須。settle）", None),
        ("manchester", decode_manchester, (), "Manchester（channel・bitrate・tolerance（1 ビットに対する許容のずれ "
                                              "0〜0.25）・convention・word_bits・msb_first。すべて必須）",
         ("word", "frame")),
        ("can", decode_can, (), "CAN / CAN FD（channel・bitrate・sample_point（ビットの中の割合）・invert は必須。"
                                "data_bitrate と data_sample_point は一緒に）", None),
        ("spiflash", decode_spiflash, ("spi",), "SPI の上の SPI NOR Flash のコマンド（address_bytes（捕捉の始まりの"
                                                "アドレス長 3 / 4）は必須。既定の命令は JEDEC で値の決まったものだけ。消去等の機器の命令は extra_commands で"
                                                "明示する）", None),
        ("eeprom24", decode_eeprom24, ("i2c",), "I2C の上の 24 系 EEPROM（device・address_bytes・block_bits は必須。"
                                                "page_size を省くとページの回り込みを調べない）", None),
        ("modbus", decode_modbus, ("uart",), "UART の上の Modbus RTU（gap_chars・gap_s。既定は規格の t3.5。要求か応答か"
                                             "決まらないフレームは kind frame・direction null）", None),
):
    register(_name, _function, lower=_lower, summary=_summary, default_kinds=_kinds, origin=BUILTIN)
