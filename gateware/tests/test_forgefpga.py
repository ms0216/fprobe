"""fprobe_gw.toolchain.forgefpga: Renesas ForgeFPGA（Shrike-Lite の SLG47910）を GUI なしでビルドする。

ForgeFPGA Workshop が裏で呼ぶ yosys と eda-placer を同じ引数で呼び、RP2040 が FPGA に書く形（MCU 用の .bin）を作る。
形は Workshop v6.55 の出力と照らして決めた（同じネットリストからバイト単位で同じものができることを実機で確かめた）。"""

import base64
import struct
import zlib

import pytest

from fprobe_gw.toolchain import forgefpga as ff


def unpack_blob(blob: str) -> list[str]:
    raw = base64.b64decode(blob)
    data = zlib.decompress(raw[4:])
    assert struct.unpack(">I", raw[:4])[0] == len(data)
    n = struct.unpack(">I", data[:4])[0]
    lengths = struct.unpack(f">{n}I", data[4:4 + 4 * n])
    out, i = [], 4 + 4 * n
    for k in lengths:
        out.append(data[i:i + k].decode())
        i += k
    return out


def test_placer_arguments_are_packed_like_the_workshop():
    # Workshop は eda-placer に「FPGA_DATA <引数の束> <口>」を渡す。束 = base64(長さ + zlib(数 + 長さの表 + 文字列))
    args = ff.placer_arguments("C:/w")
    assert args[:4] == ["-edif", "C:/w/config/netlist.edif", "-fp", "C:/w/config/floorplanspec.fp"]
    assert args[-2:] == ["-io", "C:/w/config/io_spec_in.txt"]
    assert unpack_blob(ff.pack_arguments(args)) == args


def test_the_pin_file_lists_each_used_pin():
    pins = {"tck": "CLK_t[0:0]_E_in0", "tms": "IOB_t[0:0]_xy[0:6]_in0", "tdo": "IOB_t[0:0]_xy[0:24]_out0",
            "tdo_oe": "IOB_t[0:0]_xy[0:24]_out1", "lac0_en": "IOB_t[0:0]_xy[31:12]_out0"}
    assert ff.io_spec(pins) == (
        "EFLX_CLK chip_tile_x=0, chip_tile_y=0, clk_side=E\n"
        "    Input=0, pin=tck\n"
        "EFLX_IOB chip_tile_x=0, chip_tile_y=0, chip_x=0, chip_y=6\n"
        "    Input=0, pin=tms,  input_delay=-1\n"
        "EFLX_IOB chip_tile_x=0, chip_tile_y=0, chip_x=0, chip_y=24\n"
        "   Output=0, pin=tdo, output_delay=-1\n"
        "   Output=1, pin=tdo_oe, output_delay=-1\n"
        "EFLX_IOB chip_tile_x=0, chip_tile_y=0, chip_x=31, chip_y=12\n"
        "   Output=0, pin=lac0_en, output_delay=-1\n")


def test_an_unknown_pin_name_is_refused():
    with pytest.raises(ValueError, match="IOB_t"):
        ff.io_spec({"a": "GPIO3"})


def test_board_pins_name_the_gpio_by_its_number():
    assert ff.SHRIKE_LITE.input("F_GPIO2") == "IOB_t[0:0]_xy[0:8]_in0"
    assert ff.SHRIKE_LITE.output("F_GPIO7") == ("IOB_t[0:0]_xy[0:24]_out0", "IOB_t[0:0]_xy[0:24]_out1")
    with pytest.raises(KeyError):
        ff.SHRIKE_LITE.input("F_GPIO99")


def test_the_mcu_image_wraps_the_axi_words():
    # MCU 用 = 0 の 320 語 + 頭の 10 語 + AXI の語（下位バイトから）+ 0 の 8 語
    image = ff.mcu_image([0x22220020, 0x01020304])
    words = [int.from_bytes(image[i:i + 4], "big") for i in range(0, len(image), 4)]
    assert len(words) == 320 + 10 + 2 + 8
    assert words[:320] == [0] * 320
    assert words[320:330] == [0xAA22FF11, 0, 0x22002222, 0x22222222, 0x22220000, 0x03000000, 0, 0, 0, 0]
    assert words[330:332] == [0x20002222, 0x04030201] and words[332:] == [0] * 8


def test_axi_words_are_read_from_the_placer_log():
    assert ff.parse_axi("# header\n22220020\n\n00000001\n") == [0x22220020, 1]


REPORT = """
     131 Logic 6-LUT RBBs
               516 LUTs total
               380 FFs total
### Total EFLX resources from current floorplan: ###
                       184 IOBs
                       140 Logic 6-LUT RBBs
Usage of Logic RBBs is 93.6%.
           tck   <UDEF_tck>                    19.826
"""


def test_the_resource_report_is_read():
    r = ff.parse_resources(REPORT)
    assert (r.luts, r.ffs, r.rbbs, r.rbbs_total) == (516, 380, 131, 140)
    assert r.fmax_mhz == {"tck": 19.826}


def test_a_design_without_logic_reports_zero():
    # 論理の無い回路（ピンを定数にするだけ）は報告に LUT・FF の行が無い
    r = ff.parse_resources("### Total EFLX resources from current floorplan: ###\n     140 Logic 6-LUT RBBs\n")
    assert (r.luts, r.ffs, r.rbbs, r.rbbs_total) == (0, 0, 0, 140)
