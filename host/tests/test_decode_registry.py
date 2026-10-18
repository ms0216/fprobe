"""fprobe.decode.registry: 解読器の登録簿・積み重ね・利用者の Python の解読器の読み込み（ハードウェア不要）。"""

import textwrap

import pytest

from fprobe.decode import (Annotation, Decoding, decode, decode_on, decoder, decoders, get_decoder, load_file,
                           register, unregister)
from fprobe.decode.synth import Wave, uart

BAUD = 115200


def capture(data=b"AB"):
    w = Wave(channels=4, sample_rate_hz=BAUD * 16, idle={1: 1})
    end = uart(w, 1, data, 2 / BAUD, baud=BAUD)
    return w.build(end_s=end + 2 / BAUD)


def test_builtin_decoders_are_listed_with_their_parameters():
    names = {d.name for d in decoders()}
    assert {"uart", "spi", "i2c", "swd"} <= names
    uart_info = get_decoder("uart").describe()
    assert uart_info["lower"] == [] and uart_info["origin"] == "builtin"
    params = {p["name"]: p for p in uart_info["params"]}
    assert params["channel"]["required"] and params["baud"] == {"name": "baud", "required": False, "default": None}
    assert get_decoder("swd").default_kinds == ("transfer",)
    with pytest.raises(ValueError):
        get_decoder("nothing")


def _upper(lower: Decoding, *, prefix: str = ">") -> Decoding:
    """下の UART のバイトを 1 つの文字列の注釈にまとめる（試験用の上位の解読器）。"""
    data = lower.of("data")
    text = prefix + bytes(a.value for a in data).decode()
    return lower.derive("upper", [Annotation(data[0].start, data[-1].end, "line", text)], {"prefix": prefix})


def test_stacked_decoder_reads_the_lower_decoding_named_by_its_settings_key():
    register("upper", _upper, lower=("uart",), summary="試験用")
    try:
        d = decode("upper", capture(), uart={"channel": 1, "baud": BAUD}, prefix="#")
        assert [a.text for a in d] == ["#AB"]
        assert d.params == {"prefix": "#", "uart": get_decoder("uart").function(capture(), 1, baud=BAUD).params}
        assert d.sample_period_s == pytest.approx(1 / (BAUD * 16))
        lower = decode("uart", capture(), channel=1, baud=BAUD)
        assert decode_on("upper", lower).annotations[0].text == ">AB"
        with pytest.raises(ValueError, match="uart"):
            decode("upper", capture(), prefix="#")                    # 下の設定が無い
        with pytest.raises(ValueError):
            decode_on("upper", decode("spi", capture(), sck=0, mosi=1))   # 下の種類が違う
    finally:
        unregister("upper")
    assert "upper" not in {d.name for d in decoders()}


def test_names_cannot_be_taken_over_and_lower_must_exist_at_decode_time():
    with pytest.raises(ValueError):
        register("uart", _upper)                                     # 組み込みは上書きできない
    with pytest.raises(ValueError):
        unregister("uart")
    register("orphan", _upper, lower=("nope",))
    try:
        with pytest.raises(ValueError, match="nope"):
            decode("orphan", capture(), nope={})
    finally:
        unregister("orphan")


def test_decorator_registers():
    @decoder("deco", lower=("uart",), summary="飾り")
    def deco(lower, **_):
        return lower.derive("deco", [], {})

    try:
        assert get_decoder("deco").summary == "飾り" and get_decoder("deco").function is deco
    finally:
        unregister("deco")


def test_user_file_is_loaded_and_reloaded_replacing_its_own_decoders(tmp_path):
    path = tmp_path / "mine.py"
    path.write_text(textwrap.dedent('''
        from fprobe.decode import Annotation, decoder

        @decoder("shout", lower=("uart",), summary="大文字にする")
        def shout(lower):
            return lower.derive("shout", [Annotation(a.start, a.end, "char", chr(a.value).upper(), value=a.value)
                                          for a in lower.of("data")], {})
    '''), encoding="utf-8")
    try:
        assert load_file(path) == ["shout"]
        assert get_decoder("shout").origin == str(path.resolve())
        assert [a.text for a in decode("shout", capture(b"ab"), uart={"channel": 1, "baud": BAUD})] == ["A", "B"]
        path.write_text(path.read_text(encoding="utf-8").replace('"shout"', '"yell"'), encoding="utf-8")
        assert load_file(path) == ["yell"]                          # 同じファイルの前の登録は消える
        assert "shout" not in {d.name for d in decoders()}
    finally:
        for name in ("shout", "yell"):
            if name in {d.name for d in decoders()}:
                unregister(name)


def test_a_failing_user_file_leaves_the_registry_unchanged(tmp_path):
    good = tmp_path / "good.py"
    good.write_text("from fprobe.decode import decoder\n@decoder('keep', lower=('uart',))\n"
                    "def keep(lower):\n    return lower.derive('keep', [], {})\n", encoding="utf-8")
    load_file(good)
    try:
        good.write_text("from fprobe.decode import decoder\n@decoder('other', lower=('uart',))\n"
                        "def other(lower):\n    return lower\nraise RuntimeError('boom')\n", encoding="utf-8")
        with pytest.raises(RuntimeError):
            load_file(good)
        names = {d.name for d in decoders()}
        assert "keep" in names and "other" not in names
        empty = tmp_path / "empty.py"
        empty.write_text("x = 1\n", encoding="utf-8")
        with pytest.raises(ValueError):
            load_file(empty)                                        # 何も登録しないファイル
        clash = tmp_path / "clash.py"
        clash.write_text("from fprobe.decode import decoder\n@decoder('spi')\ndef spi(tr):\n    pass\n",
                         encoding="utf-8")
        with pytest.raises(ValueError):
            load_file(clash)
        assert get_decoder("spi").origin == "builtin"
    finally:
        unregister("keep")


def test_decoder_must_return_a_decoding():
    register("bad", lambda lower: None, lower=("uart",))
    try:
        with pytest.raises(TypeError):
            decode("bad", capture(), uart={"channel": 1, "baud": BAUD})
    finally:
        unregister("bad")
