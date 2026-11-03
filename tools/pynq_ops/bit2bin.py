"""Xilinx .bit → Linux zynq-fpga 用の .bin（ヘッダを外し、32 ビットごとにバイトを入れ替える）。"""
import struct, sys

def bit2bin(src, dst):
    d = open(src, "rb").read()
    i = 2 + struct.unpack(">H", d[:2])[0] + 2              # 先頭のフィールド（長さ + 内容）の後
    i = 2 + struct.unpack(">H", d[0:2])[0]                 # 0x0009 + 9 バイト
    i += 2                                                  # 0x0001
    fields = {}
    while True:
        key = chr(d[i]); i += 1
        if key == "e":
            n = struct.unpack(">I", d[i:i + 4])[0]; i += 4
            payload = d[i:i + n]
            break
        n = struct.unpack(">H", d[i:i + 2])[0]; i += 2
        fields[key] = d[i:i + n].rstrip(b"\0").decode(errors="replace"); i += n
    assert len(payload) % 4 == 0 and len(payload) == n, (len(payload), n)
    assert b"\xaa\x99\x55\x66" in payload[:64], "同期語が先頭に無い"
    out = bytearray(len(payload))
    out[0::4], out[1::4], out[2::4], out[3::4] = payload[3::4], payload[2::4], payload[1::4], payload[0::4]
    assert bytes(out).find(b"\x66\x55\x99\xaa") == payload.find(b"\xaa\x99\x55\x66")
    open(dst, "wb").write(out)
    return fields, n

if __name__ == "__main__":
    f, n = bit2bin(sys.argv[1], sys.argv[2])
    print(f, "payload bytes", n)
