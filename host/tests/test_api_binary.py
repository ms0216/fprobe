"""fprobe.api.binary: バイナリの応答のフレーム（JSON-RPC の応答 + 8 バイト境界の区画）。"""

import struct
from array import array

import pytest

from fprobe.api.binary import MAGIC, decode_frame, encode_frame


def test_frame_carries_the_response_and_aligned_sections():
    sections = {"t": array("d", [1.5, 2.0, 1e12]), "s": bytes([0, 1, 2]), "n": array("I", [7, 8])}
    frame = encode_frame({"jsonrpc": "2.0", "id": 4, "result": {"format": 1, "名前": "波形"}}, sections)
    assert frame[:4] == MAGIC
    (n,) = struct.unpack_from("<I", frame, 4)
    response, data = decode_frame(frame)
    assert response["id"] == 4 and response["result"]["名前"] == "波形"
    layout = response["result"]["sections"]
    assert {k: (v["dtype"], v["length"]) for k, v in layout.items()} == {"t": ("f64", 3), "s": ("u8", 3), "n": ("u32", 2)}
    assert all(v["offset"] % 8 == 0 and v["offset"] >= 8 + n for v in layout.values())
    assert list(data["t"]) == [1.5, 2.0, 1e12] and list(data["s"]) == [0, 1, 2] and list(data["n"]) == [7, 8]
    assert len(frame) % 8 == 0


def test_offsets_settle_when_their_digits_change_the_header_length():
    # 区画が多く大きいと、位置の桁が JSON の長さを変える: それでも位置は実際の位置と一致する
    sections = {f"s{i}": array("d", range(5000)) for i in range(30)}
    frame = encode_frame({"jsonrpc": "2.0", "id": 1, "result": {}}, sections)
    response, data = decode_frame(frame)
    for name, s in response["result"]["sections"].items():
        assert struct.unpack_from("<d", frame, s["offset"] + 8 * 4999)[0] == 4999.0
        assert data[name][-1] == 4999.0


def test_rejects_other_frames_and_unknown_array_types():
    with pytest.raises(ValueError):
        decode_frame(b"XXXX" + bytes(8))
    with pytest.raises(TypeError):
        encode_frame({"jsonrpc": "2.0", "id": 1, "result": {}}, {"x": array("b", [1])})
