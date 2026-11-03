import hashlib
import subprocess

import pytest

from fprobe_gw.pynq_setup import (RINGMAP_CONF, RINGMAP_DIR, RINGMAP_SOURCES, RINGMAP_WORK, install,
                                  query_relay_status, read_ringmap_facts, rebuild_reasons, ringmap,
                                  ringmap_source_digest, status)
from fprobe_gw.toolchain.pynq_loader import PynqError, PynqLoader
from fprobe_gw.toolchain.vivado import VivadoToolchain


def test_install_builds_on_the_board_and_enables_the_service(tmp_path):
    calls = []

    def run(args, **kw):
        calls.append((list(map(str, args)), kw.get("input")))
        return subprocess.CompletedProcess(args, 0, stdout="", stderr="")

    loader = PynqLoader(VivadoToolchain(tmp_path), run=run)
    install(loader, run=run, env={"FPROBE_PYNQ_SUDO_PASSWORD": "pw"})
    (mkdir, _), (scp, _), (make, _), (root, password) = calls
    assert scp[0] == "scp" and any(a.endswith("fprobe-pl") for a in scp)
    assert "make" in make[-1]
    assert password == "pw\n" and "sudo -S" in root[-1]
    assert "systemctl enable fprobe-relay" in root[-1] and "visudo -cf" in root[-1]
    assert "NOPASSWD: /usr/local/bin/fprobe-pl" in root[-1]


def test_install_needs_the_sudo_password(tmp_path):
    with pytest.raises(PynqError, match="FPROBE_PYNQ_SUDO_PASSWORD"):
        install(PynqLoader(VivadoToolchain(tmp_path)), env={})


def test_reserve_puts_uenv_and_the_region_then_reboots(tmp_path):
    # P3: DDR の上端を Linux から外す（/boot/uEnv.txt の mem=479M）と、中継が読む領域（/etc/fprobe/mem）を置いて再起動する
    from fprobe_gw.pynq_setup import reserve
    calls = []

    def run(args, **kw):
        calls.append((list(map(str, args)), kw.get("input")))
        return subprocess.CompletedProcess(args, 0, stdout="", stderr="")

    loader = PynqLoader(VivadoToolchain(tmp_path), run=run)
    reserve(loader, run=run, env={"FPROBE_PYNQ_SUDO_PASSWORD": "pw"})
    scp = [a for a, _ in calls if a[0] == "scp"][0]
    assert any(a.endswith("uEnv.txt") for a in scp) and any(a.endswith("fprobe-mem") for a in scp)
    root = calls[-1][0][-1]
    assert "/boot/uEnv.txt" in root and "/etc/fprobe/mem" in root and "reboot" in root
    assert root.index("fprobe-pl before") < root.index("reboot")        # 再起動の前に中継を止め、レベルシフタを切る


def test_unreserve_removes_uenv_and_the_region(tmp_path):
    from fprobe_gw.pynq_setup import reserve
    calls = []

    def run(args, **kw):
        calls.append(list(map(str, args)))
        return subprocess.CompletedProcess(args, 0, stdout="", stderr="")

    reserve(PynqLoader(VivadoToolchain(tmp_path), run=run), run=run, env={"FPROBE_PYNQ_SUDO_PASSWORD": "pw"},
            undo=True)
    root = calls[-1][-1]
    assert "rm -f /boot/uEnv.txt /etc/fprobe/mem" in root and "reboot" in root


# ---- ゼロコピーの道のモジュールの起動時の読み込み（pynq ringmap / unringmap / status） -------------------------------

RELEASE = "6.6.10-xilinx-v2024.1-g3c0eca68c652"
VERMAGIC = f"{RELEASE} SMP preempt mod_unload modversions ARMv7 p2v8"     # 設計 1.7.8 で読んだ vermagic
KO = f"/lib/modules/{RELEASE}/extra/fprobe_ringmap.ko"
ENV = {"FPROBE_PYNQ_SUDO_PASSWORD": "pw"}
HOST = "192.0.2.7"


class FakePynq:
    """ssh・scp の偽物。事実を読むコマンドには PYNQ の答えの形で返し、送ったコマンドを順に覚える。"""

    def __init__(self, *, ko=None, built=None, loaded=False, relay_after="network.target systemd-modules-load.service",
                 dt_nodes=("fprobe_ring@1e000000",)):
        self.ko = ko                    # 据え付けてある .ko: (vermagic, fprobe_src) または None
        self.built = built              # build.sh が作る .ko: (vermagic, fprobe_src) または None（作れなかった）
        self.loaded = loaded
        self.relay_after = relay_after
        self.dt_nodes = dt_nodes
        self.calls = []                 # (種類 scp / ssh / sudo, 引数かコマンド, 標準入力)

    def run(self, args, **kw):
        args = list(map(str, args))
        out = ""
        if args[0] == "scp":
            self.calls.append(("scp", args, None))
        else:
            command = args[-1]
            self.calls.append(("sudo" if command.startswith("sudo -S") else "ssh", command, kw.get("input")))
            if command.startswith("R=$(uname -r)"):
                out = self._facts()
            elif command.startswith(f"if [ -f {RINGMAP_WORK}/mod/fprobe_ringmap.ko ]") and self.built:
                out = f"present=1\nvermagic={self.built[0]}\nsrc={self.built[1]}\n"
        return subprocess.CompletedProcess(args, 0, stdout=out, stderr="")

    def _facts(self):
        lines = [f"release={RELEASE}"]
        if self.ko:
            lines += ["ko_present=1", f"ko_vermagic={self.ko[0]}", f"ko_src={self.ko[1]}"]
        if self.loaded:
            lines += ["loaded=1", "refcnt=1", "dev=1"]
        lines += [f"dt_node={n}" for n in self.dt_nodes]
        lines += ["modules_load=active", "relay_active=active", f"relay_after={self.relay_after}"]
        return "\n".join(lines) + "\n"

    def commands(self, kind):
        return [c for k, c, _ in self.calls if k == kind]

    def loader(self, tmp_path):
        return PynqLoader(VivadoToolchain(tmp_path), host=HOST, run=self.run)


def plain_steps(root):
    """管理者の手順（sudo -S -p '' sh -c "<手順>"）の手順の中に $ と " が無い（ユーザーのシェルが先に展開しない・閉じない）。"""
    head = "sudo -S -p '' sh -c \""
    assert root.startswith(head) and root.endswith('"')
    steps = root[len(head):-1]
    return "$" not in steps and '"' not in steps


def test_the_source_digest_is_the_same_calculation_as_build_sh(tmp_path):
    # build.sh: sha256sum fprobe_ringmap.c Makefile build.sh cc-compat.sh | cut -c1-64 | sha256sum（モジュールの fprobe_src）
    bodies = (b"a\n", b"b\n", b"c\n", b"d\n")
    for name, body in zip(RINGMAP_SOURCES, bodies):
        (tmp_path / name).write_bytes(body)
    listing = "".join(f"{hashlib.sha256(b).hexdigest()}\n" for b in bodies)
    assert ringmap_source_digest(tmp_path) == hashlib.sha256(listing.encode()).hexdigest()
    build_sh = (RINGMAP_DIR / "build.sh").read_text(encoding="utf-8")
    assert "sha256sum " + " ".join(RINGMAP_SOURCES) + " | cut -c1-64 | sha256sum" in build_sh
    assert b"\r" not in b"".join((RINGMAP_DIR / n).read_bytes() for n in RINGMAP_SOURCES)   # PYNQ でも同じバイト


def test_build_sh_computes_the_same_digest_when_a_shell_is_at_hand():
    # build.sh の SRC_SHA の行そのものを走らせて比べる（sha256sum の名前の欄は環境で違うが、計算はそれに依らない）
    import shutil
    bash = shutil.which("bash")
    if not bash or not shutil.which("sha256sum") or "system32" in bash.lower():     # WSL の bash は Windows の道を読めない
        pytest.skip("bash と sha256sum が要る")
    (line,) = [s for s in (RINGMAP_DIR / "build.sh").read_text(encoding="utf-8").splitlines() if s.startswith("SRC_SHA=")]
    r = subprocess.run([bash, "-c", f'HERE="$1"; {line}; echo "$SRC_SHA"', "sh", RINGMAP_DIR.as_posix()],
                       capture_output=True, text=True, check=True)
    assert r.stdout.strip() == ringmap_source_digest()


def test_a_missing_module_is_built_checked_installed_and_set_to_load_at_boot(tmp_path):
    digest = ringmap_source_digest()
    fake = FakePynq(built=(VERMAGIC, digest))
    shown = []

    def announce(text):
        shown.append((text, len(fake.commands("sudo"))))

    ringmap(fake.loader(tmp_path), run=fake.run, env=ENV, announce=announce)
    scp = [a for k, a, _ in fake.calls if k == "scp"][0]
    sent = {a.replace("\\", "/").rsplit("/", 1)[-1] for a in scp}
    assert {*RINGMAP_SOURCES, "fprobe-ringmap.conf"} <= sent and scp[-1].endswith(":fprobe/ringmap/")
    ssh = fake.commands("ssh")
    (build,) = [c for c in ssh if "build.sh" in c]
    # 前の作業の置き場所（別のカーネルの木の写しかもしれない）を名前で消してから作る。作るのは root でない
    assert build == f"rm -rf {RINGMAP_WORK} && cd fprobe/ringmap && bash build.sh {RINGMAP_WORK}"
    (root,) = fake.commands("sudo")
    assert f"install -D -m 644 {RINGMAP_WORK}/mod/fprobe_ringmap.ko {KO}" in root
    assert root.index("install -D") < root.index(f"depmod {RELEASE}") < root.index(RINGMAP_CONF)
    assert f"install -m 644 fprobe/ringmap/fprobe-ringmap.conf {RINGMAP_CONF}" in root
    for word in ("modprobe", "insmod", "systemctl", "reboot"):         # 今は読み込まない・中継は止めない・再起動しない
        assert word not in root
    assert plain_steps(root)                               # 遠隔のシェルの二重引用符の中で展開・閉じられるものを入れない
    assert ssh[-1] == f"rm -rf {RINGMAP_WORK}"             # 作業の置き場所は据え付けの後に名前で消す
    assert [i for k, _, i in fake.calls if k == "sudo"] == ["pw\n"]
    # 何を変えるかは、管理者の手順の前に 1 度示す
    ((plan, sudo_before),) = shown
    assert sudo_before == 0 and "が無い" in plan and RINGMAP_CONF in plan and "再起動の後に効く" in plan


def test_an_installed_module_of_the_same_kernel_and_source_is_not_rebuilt(tmp_path):
    fake = FakePynq(ko=(VERMAGIC, ringmap_source_digest()))
    shown = []
    ringmap(fake.loader(tmp_path), run=fake.run, env=ENV, announce=shown.append)
    assert not any("build.sh" in c for c in fake.commands("ssh"))
    (root,) = fake.commands("sudo")
    assert "install -D" not in root and f"depmod {RELEASE}" in root and RINGMAP_CONF in root
    assert "作り直さない" in shown[0]


@pytest.mark.parametrize("ko, why", [
    (("6.6.11-other SMP preempt", "SAME"), "uname -r"),           # カーネルを替えた（版の名前が違う）
    ((VERMAGIC, "0" * 64), "sha256"),                             # ソースが変わった
    ((VERMAGIC, ""), "(無い)"),                                   # fprobe_src の印を入れる前に作ったもの
])
def test_a_module_of_another_kernel_or_source_is_rebuilt(tmp_path, ko, why):
    digest = ringmap_source_digest()
    fake = FakePynq(ko=(ko[0], digest if ko[1] == "SAME" else ko[1]), built=(VERMAGIC, digest))
    shown = []
    ringmap(fake.loader(tmp_path), run=fake.run, env=ENV, announce=shown.append)
    assert any("build.sh" in c for c in fake.commands("ssh"))
    assert why in shown[0] and "install -D" in fake.commands("sudo")[0]


def test_rebuild_reasons_read_the_facts(tmp_path):
    digest = "d" * 64
    facts = read_ringmap_facts(FakePynq(ko=(VERMAGIC, digest)).loader(tmp_path))
    assert facts.release == RELEASE and facts.ko_release == RELEASE and facts.ko_path == KO
    assert facts.dt_nodes == ("fprobe_ring@1e000000",) and facts.relay_after_modules_load
    assert rebuild_reasons(facts, digest) == []
    assert len(rebuild_reasons(facts, "e" * 64)) == 1
    assert rebuild_reasons(read_ringmap_facts(FakePynq().loader(tmp_path)), digest) == [f"{KO} が無い"]


def test_an_unusable_kernel_release_is_refused(tmp_path):
    fake = FakePynq()
    fake._facts = lambda: "release=6.6; rm -rf /\n"
    with pytest.raises(PynqError, match="uname -r"):
        read_ringmap_facts(fake.loader(tmp_path))


@pytest.mark.parametrize("built", [("6.6.11-other SMP", "SAME"), (VERMAGIC, "f" * 64), None])
def test_a_built_module_that_does_not_match_is_not_installed(tmp_path, built):
    digest = ringmap_source_digest()
    fake = FakePynq(built=built and (built[0], digest if built[1] == "SAME" else built[1]))
    with pytest.raises(PynqError, match="作ったモジュールが合わない"):
        ringmap(fake.loader(tmp_path), run=fake.run, env=ENV, announce=lambda _: None)
    assert fake.commands("sudo") == []
    assert fake.commands("ssh")[-1] != f"rm -rf {RINGMAP_WORK}"         # 調べるために残す


def test_the_plan_notes_an_old_relay_service_and_a_missing_device_tree_node(tmp_path):
    fake = FakePynq(ko=(VERMAGIC, ringmap_source_digest()), relay_after="network.target", dt_nodes=())
    shown = []
    ringmap(fake.loader(tmp_path), run=fake.run, env=ENV, announce=shown.append)
    assert "After= に systemd-modules-load.service が無い" in shown[0] and "ノードが 0 個" in shown[0]


def test_unringmap_removes_the_boot_setting_and_unloads_after_stopping_the_relay(tmp_path):
    fake = FakePynq(ko=(VERMAGIC, "x"), loaded=True)
    shown = []

    def announce(text):
        shown.append((text, len(fake.commands("sudo"))))

    ringmap(fake.loader(tmp_path), run=fake.run, env=ENV, undo=True, announce=announce)
    assert not [k for k, _, _ in fake.calls if k == "scp"] and not any("build.sh" in c for c in fake.commands("ssh"))
    (root,) = fake.commands("sudo")
    assert root.index(f"rm -f {RINGMAP_CONF} {KO}") < root.index(f"depmod {RELEASE}")
    # 中継が写している間は rmmod できない: 止めてから外し、動いていたら起動し直す。読み込まれていなければ何もしない
    stop, rm, start = (root.index(s) for s in ("systemctl stop fprobe-relay", "rmmod fprobe_ringmap",
                                               "systemctl start fprobe-relay"))
    assert stop < rm < start and "if [ -d /sys/module/fprobe_ringmap ]" in root
    assert plain_steps(root)
    ((plan, sudo_before),) = shown
    assert sudo_before == 0 and "ストリームは切れる" in plan


def test_ringmap_needs_the_sudo_password(tmp_path):
    fake = FakePynq()
    with pytest.raises(PynqError, match="FPROBE_PYNQ_SUDO_PASSWORD"):
        ringmap(fake.loader(tmp_path), run=fake.run, env={})
    assert fake.calls == []


def test_status_shows_the_module_the_boot_setting_and_the_relay_path(tmp_path):
    fake = FakePynq(ko=(VERMAGIC, ringmap_source_digest()), loaded=True)
    asked = []

    def relay_query(host):
        asked.append(host)
        return {"zc_state": "ready", "tcp_path": "zerocopy"}

    text = status(fake.loader(tmp_path), relay_query=relay_query)
    assert asked == [HOST]
    assert f"vermagic の版 {RELEASE}" in text and "リポジトリのソースの sha256 と同じ" in text
    assert "読み込まれている: あり（refcnt 1）" in text and "/dev/fprobe_ring: あり" in text
    assert "zc_state ready、tcp_path zerocopy" in text
    assert fake.commands("sudo") == []                    # 状態は管理者の権限なしで読む
    other = status(FakePynq().loader(tmp_path), relay_query=lambda host: None)
    assert "モジュールのファイル: なし" in other and "返事なし" in other


class FakeUdp:
    def __init__(self, replies):
        self.replies, self.sent, self.closed = list(replies), [], False

    def __call__(self, *_):
        return self

    def bind(self, addr):
        pass

    def settimeout(self, t):
        pass

    def sendto(self, data, addr):
        self.sent.append((data, addr))

    def recvfrom(self, n):
        if not self.replies:
            raise TimeoutError
        return self.replies.pop(0), (HOST, 63920)

    def close(self):
        self.closed = True


def test_query_relay_status_sends_only_the_status_request_and_skips_other_datagrams():
    reply = bytes([0x41, 9, 2, 0, 1, 2, 0, 0])               # 番号 9 を写した版 2、zc_state ready、tcp_path zerocopy、値 0 個
    stale = bytes([0x41, 8, 2, 0, 0, 0, 0, 0])               # 前の問い合わせ（番号 8）の遅れた返事
    udp = FakeUdp([bytes([0x04, 7, 1, 2]), stale, reply])     # 古い中継は宛先を移すので、ほかのフレームも来うる
    got = query_relay_status(HOST, sock_factory=udp, token=9)
    assert udp.sent == [(bytes([0x41, 9]), (HOST, 63920))] and udp.closed
    assert got["zc_state"] == "ready" and got["tcp_path"] == "zerocopy" and got["token"] == 9
    assert query_relay_status(HOST, sock_factory=FakeUdp([])) is None
    assert query_relay_status(HOST, sock_factory=FakeUdp([bytes([0x41, 0, 1, 0, 1, 2, 0, 0])])) is None   # 版 1（番号を写さない）
