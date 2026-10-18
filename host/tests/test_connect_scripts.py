"""fprobe.connect_scripts: 接続の手順のスクリプトの検査・内蔵の手順（実行しない）。"""

import pytest

from fprobe import connect_scripts as cs


def write(tmp_path, text):
    path = tmp_path / "user.py"
    path.write_text(text, encoding="utf-8")
    return str(path)


def test_hooks_come_from_pyocd():
    assert {"will_connect", "did_init_target", "will_reset", "unlock_device", "mass_erase"} <= set(cs.HOOKS)
    assert cs.HOOKS["will_reset"]["params"] == ["core", "reset_type"]


def test_a_good_script_lists_its_hooks_and_commands(tmp_path):
    path = write(tmp_path, '''
def will_connect(board):
    pass

def will_reset(core):
    return True

@command(help="x")
def hello():
    print("hi")

def helper(x):
    return x
''')
    check = cs.check_script(path)
    assert check.ok and [h["name"] for h in check.hooks] == ["will_connect", "will_reset"]
    assert check.commands == ["hello"] and check.hooks[1]["line"] == 5


def test_typos_and_wrong_parameters_are_problems(tmp_path):
    path = write(tmp_path, '''
def will_conect(board):
    pass

def did_something(target):
    pass

def will_reset(core, kind):
    pass
''')
    check = cs.check_script(path)
    assert not check.ok
    text = " ".join(check.problems)
    assert "will_connect のつもり" in text and "did_something" in text and "kind" in text


def test_syntax_errors_are_reported_with_the_line(tmp_path):
    check = cs.check_script(write(tmp_path, "def will_connect(board)\n    pass\n"))
    assert check.error and "1 行" in check.error and not check.ok


def test_builtin_scripts_are_listed_and_resolved():
    names = {s["name"]: s for s in cs.builtin_scripts()}
    assert {"builtin:stm32_debug_low_power", "builtin:reset_with_nreset"} <= set(names)
    assert names["builtin:reset_with_nreset"]["hooks"] == ["will_reset"]
    for name in names:
        assert cs.check_script(name).ok, name
    assert cs.resolve_script("builtin:reset_with_nreset").is_file()
    with pytest.raises(ValueError):
        cs.resolve_script("builtin:nothing")
    with pytest.raises(ValueError):
        cs.resolve_script("builtin:../x")
    with pytest.raises(FileNotFoundError):
        cs.resolve_script("no/such/file.py")


def test_builtin_reset_script_drives_nreset():
    namespace: dict = {}
    exec(compile(cs.resolve_script("builtin:reset_with_nreset").read_text(encoding="utf-8"), "s", "exec"), namespace)
    calls = []
    core = type("C", (), {"session": type("S", (), {"probe": type("P", (), {
        "assert_reset": lambda self, v: calls.append(v)})()})()})()
    namespace["PULSE_S"] = 0
    assert namespace["will_reset"](core, None) is True and calls == [True, False]


def test_builtin_low_power_script_sets_dbgmcu_cr():
    namespace: dict = {"LOG": None}
    exec(compile(cs.resolve_script("builtin:stm32_debug_low_power").read_text(encoding="utf-8"), "s", "exec"),
         namespace)
    mem = {0xE0042000: 0x10006433, 0xE0042004: 0x100}

    class M:
        def read32(self, a):
            return mem.get(a, 0)

        def write32(self, a, v):
            mem[a] = v
    namespace["did_init_target"](M())
    assert mem[0xE0042004] == 0x107
