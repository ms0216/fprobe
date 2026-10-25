from fprobe.obs_value import is_on, number, same


def test_numbers_compare_by_value_and_names_by_text():
    assert same(16, "0x10") and same("16", "0x10") and not same(16, "0x11")
    assert same("BUSY", "BUSY") and not same("BUSY", "IDLE")
    assert not same(None, "0") and not same(0, None)


def test_zero_is_off_in_every_spelling_and_a_name_is_on():
    assert not is_on(0) and not is_on("0") and not is_on("0x0") and not is_on(None)
    assert is_on(1) and is_on("BUSY")


def test_number_reads_prefixes_and_refuses_names():
    assert number("0b101") == 5 and number(True) == 1 and number("BUSY") is None and number(1.5) is None
