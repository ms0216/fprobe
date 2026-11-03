"""PYNQ-Z2 への据え付け（`python -m fprobe_gw pynq install|status|reserve|unreserve|ringmap|unringmap`）。

install: 中継（host/pynq/fprobe-relay）と書き込みの補助（host/pynq/fprobe-pl）を送り、PYNQ の上で make して
/usr/local/bin に置き、systemd のサービス fprobe-relay を有効にし、補助だけをパスワード無しの sudo で呼べるようにする
（PynqLoader が書き込みの前後に sudo -n で呼ぶ）。管理者の権限が要るのはここだけで、sudo のパスワードは環境変数
FPROBE_PYNQ_SUDO_PASSWORD で渡す（リポジトリに書かない）。

ringmap / unringmap: ゼロコピーの送信の道のカーネルモジュール fprobe_ringmap（host/pynq/fprobe-ringmap）を起動時に読み込む
設定を置く / 外す（2026-11-02 に利用者が承認。設計 docs/superpowers/specs/2026-11-01-bandwidth-observation-design.md 1.7.8・1.7.9）。
起動の設定を変えるので、何を変えるかを、PYNQ の事実を読んだ後・管理者の手順の前に announce で示す。作り直すかは、据え付けてある
.ko の modinfo（vermagic の版の名前が uname -r と同じか、fprobe_src がリポジトリのソースの sha256 と同じか）で決める。
"""

import hashlib
import os
import re
import socket
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path

from .toolchain.pynq_loader import PL_HELPER, PynqError, PynqLoader, run_checked

__all__ = ["RELAY_SOURCES", "RINGMAP_SOURCES", "RingmapFacts", "install", "query_relay_status", "read_ringmap_facts",
           "rebuild_reasons", "reserve", "ringmap", "ringmap_source_digest", "status"]

HOST_PYNQ = Path(__file__).resolve().parents[2] / "host" / "pynq"
RELAY_SOURCES = ("fprobe-relay/main.c", "fprobe-relay/relay.c", "fprobe-relay/relay.h", "fprobe-relay/window_regs.h",
                 "fprobe-relay/Makefile",
                 "fprobe-relay/fprobe-relay.service", "fprobe-pl")
REMOTE_DIR = "fprobe/relay"
SUDO_ENV = "FPROBE_PYNQ_SUDO_PASSWORD"

# ゼロコピーの道のモジュール。RINGMAP_SOURCES の並びと名前は build.sh の sha256sum の引数と同じ（fprobe_src の計算が一致する）
RINGMAP_SOURCES = ("fprobe_ringmap.c", "Makefile", "build.sh", "cc-compat.sh")
RINGMAP_DIR = HOST_PYNQ / "fprobe-ringmap"
RINGMAP_CONF_SOURCE = "fprobe-ringmap.conf"
RINGMAP_REMOTE_DIR = "fprobe/ringmap"
RINGMAP_WORK = "/tmp/fprobe-ringmap-build"              # build.sh の作業の置き場所（約 110 MB。据え付けの後に名前で消す）
RINGMAP_MODULE = "fprobe_ringmap"
RINGMAP_CONF = "/etc/modules-load.d/fprobe-ringmap.conf"
RINGMAP_BUILD_TIMEOUT_S = 1200                          # 実測は約 3 分（設計 1.7.9）
RELAY_UDP_PORT = 63920
_SAFE_RELEASE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._+-]*")    # uname -r。管理者の手順の文字列に入る

_ROOT_STEPS = " && ".join((
    f"install -m 755 {REMOTE_DIR}/fprobe-relay /usr/local/bin/fprobe-relay",
    f"install -m 755 {REMOTE_DIR}/fprobe-pl {PL_HELPER}",
    f"install -m 644 {REMOTE_DIR}/fprobe-relay.service /etc/systemd/system/fprobe-relay.service",
    f"echo '{{user}} ALL=(root) NOPASSWD: {PL_HELPER}' > /tmp/fprobe.sudoers",
    "visudo -cf /tmp/fprobe.sudoers",
    "install -m 440 /tmp/fprobe.sudoers /etc/sudoers.d/fprobe",
    "systemctl daemon-reload",
    "systemctl enable fprobe-relay",
))


def _password(env, what: str) -> str:
    password = env.get(SUDO_ENV)
    if not password:
        raise PynqError(f"{what}には PYNQ の sudo のパスワードが要る（環境変数 {SUDO_ENV}）")
    return password


def _scp(loader: PynqLoader, files, run, remote_dir: str = REMOTE_DIR) -> None:
    r = run_checked(run, ["scp", "-i", str(loader.key), "-o", "BatchMode=yes", *map(str, files),
                          f"{loader.user}@{loader.host}:{remote_dir}/"],
                    what="PYNQ へのファイルの送り", timeout=60, capture_output=True, text=True, encoding="utf-8",
                    errors="replace")
    if r.returncode != 0:
        raise PynqError(f"PYNQ へのファイルの送りが失敗: {(r.stderr or '').strip()}")


def _sudo(loader: PynqLoader, steps: str, password: str, run, *, what: str, ok=(0,)) -> None:
    """管理者の手順 steps を sudo -S で走らせる（パスワードは標準入力。引数に出さない）。steps は呼び手が作る固定の文字列
    （遠隔のシェルの二重引用符の中に入るので、$ と " を含めない）。"""
    r = run_checked(run, ["ssh", "-i", str(loader.key), "-o", "BatchMode=yes", f"{loader.user}@{loader.host}",
                          f"sudo -S -p '' sh -c \"{steps}\""],
                    what=what, timeout=120, input=password + "\n", capture_output=True, text=True, encoding="utf-8",
                    errors="replace")
    if r.returncode not in ok:
        raise PynqError(f"{what}が失敗: {(r.stderr or r.stdout or '').strip()}")


def install(loader: PynqLoader, *, run=subprocess.run, env=os.environ) -> None:
    password = _password(env, "据え付け")
    loader.ssh(f"mkdir -p {REMOTE_DIR}")
    _scp(loader, [HOST_PYNQ / s for s in RELAY_SOURCES], run)
    loader.ssh(f"cd {REMOTE_DIR} && sed -i 's/\\r$//' fprobe-pl && make", timeout=120)
    _sudo(loader, _ROOT_STEPS.format(user=loader.user), password, run, what="PYNQ の上の据え付け（管理者の手順）")


def reserve(loader: PynqLoader, *, run=subprocess.run, env=os.environ, undo: bool = False) -> None:
    """P3 の深いバッファの領域を予約して再起動する（undo = True なら予約を外して再起動する）。

    /boot/uEnv.txt（host/pynq/uEnv.txt: 今の起動の引数 + mem=479M）で DDR の上端 33 MB（Linux のリングの 1 MB 0x1DF00000〜 + 深いバッファの 32 MB）を Linux から外し、
    /etc/fprobe/mem（host/pynq/fprobe-mem）で中継に領域を知らせる。再起動の前に中継を止め、レベルシフタを切る。
    再起動の後、PL は未構成（boot.py は空）なので、fprobe の回路を書き直す（build fprobe_pynq_deep --program）。
    起動しなくなったら、SD を PC に挿して uEnv.txt を消す。"""
    password = _password(env, "予約")
    if undo:
        steps = f"{PL_HELPER} before; rm -f /boot/uEnv.txt /etc/fprobe/mem; sync; reboot"
    else:
        _scp(loader, [HOST_PYNQ / "uEnv.txt", HOST_PYNQ / "fprobe-mem"], run)
        steps = (f"{PL_HELPER} before; install -m 644 {REMOTE_DIR}/uEnv.txt /boot/uEnv.txt && "
                 f"mkdir -p /etc/fprobe && install -m 644 {REMOTE_DIR}/fprobe-mem /etc/fprobe/mem && sync && reboot")
    # reboot で SSH が切れる（終了コード 255）のは成功のうち
    _sudo(loader, steps, password, run, what="PYNQ の予約の手順", ok=(0, 255))


# ---- 起動時の読み込み（fprobe_ringmap） ---------------------------------------------------------------------------------

def ringmap_source_digest(directory: Path = RINGMAP_DIR) -> str:
    """モジュールのソースの sha256（build.sh の「sha256sum ソース 4 つ | cut -c1-64 | sha256sum」と同じ計算: 各ファイルの
    sha256 の 16 進と改行を RINGMAP_SOURCES の順に並べたものの sha256。モジュールの fprobe_src になる）。"""
    lines = "".join(f"{hashlib.sha256((directory / name).read_bytes()).hexdigest()}\n" for name in RINGMAP_SOURCES)
    return hashlib.sha256(lines.encode("utf-8")).hexdigest()


@dataclass(frozen=True)
class RingmapFacts:
    """PYNQ の上で読んだ事実（推測を含まない）。読めなかった値は空・None。"""
    release: str                    # uname -r
    ko_path: str                    # /lib/modules/<release>/extra/fprobe_ringmap.ko
    ko_present: bool
    ko_vermagic: str                # modinfo -F vermagic（無ければ空）
    ko_src: str                     # modinfo -F fprobe_src（無ければ空。この印を入れる前の build.sh で作ったものも空）
    boot_conf: bool                 # RINGMAP_CONF がある
    loaded: bool                    # /sys/module/fprobe_ringmap がある
    refcnt: int | None              # /sys/module/fprobe_ringmap/refcnt（中継が写している間は 1 以上）
    dev: bool                       # /dev/fprobe_ring がある
    dt_nodes: tuple[str, ...]       # /proc/device-tree/reserved-memory の fprobe_ring* のノードの名前
    modules_load: str               # systemctl is-active systemd-modules-load
    relay_active: str               # systemctl is-active fprobe-relay
    relay_after: str                # 動いている systemd が読んだ fprobe-relay.service の After=

    @property
    def ko_release(self) -> str:
        """vermagic の版の名前（最初の語）。"""
        words = self.ko_vermagic.split()
        return words[0] if words else ""

    @property
    def relay_after_modules_load(self) -> bool:
        return "systemd-modules-load.service" in self.relay_after.split()


def _ko_facts_sh(prefix: str, path: str) -> str:
    return (f"if [ -f {path} ]; then echo {prefix}present=1; echo {prefix}vermagic=$(modinfo -F vermagic {path}); "
            f"echo {prefix}src=$(modinfo -F fprobe_src {path}); fi")


_FACTS_SH = "; ".join((
    "R=$(uname -r)", "echo release=$R",
    _ko_facts_sh("ko_", f"/lib/modules/$R/extra/{RINGMAP_MODULE}.ko"),
    f"[ -f {RINGMAP_CONF} ] && echo boot_conf=1",
    f"[ -d /sys/module/{RINGMAP_MODULE} ] && echo loaded=1 && echo refcnt=$(cat /sys/module/{RINGMAP_MODULE}/refcnt)",
    "[ -e /dev/fprobe_ring ] && echo dev=1",
    "for n in /proc/device-tree/reserved-memory/fprobe_ring*; do [ -e $n ] && echo dt_node=${n##*/}; done",
    "echo modules_load=$(systemctl is-active systemd-modules-load)",
    "echo relay_active=$(systemctl is-active fprobe-relay)",
    "echo relay_after=$(systemctl show -p After --value fprobe-relay)",
    "true",
))


def _parse_kv(text: str) -> dict[str, list[str]]:
    out: dict[str, list[str]] = {}
    for line in text.splitlines():
        key, sep, value = line.partition("=")
        if sep:
            out.setdefault(key.strip(), []).append(value.strip())
    return out


def _one(kv: dict[str, list[str]], key: str) -> str:
    return kv.get(key, [""])[0]


def read_ringmap_facts(loader: PynqLoader) -> RingmapFacts:
    kv = _parse_kv(loader.ssh(_FACTS_SH))
    release = _one(kv, "release")
    if not _SAFE_RELEASE.fullmatch(release):
        raise PynqError(f"PYNQ の uname -r が読めない・使えない文字がある: {release!r}")
    refcnt = _one(kv, "refcnt")
    return RingmapFacts(
        release=release, ko_path=f"/lib/modules/{release}/extra/{RINGMAP_MODULE}.ko",
        ko_present=_one(kv, "ko_present") == "1", ko_vermagic=_one(kv, "ko_vermagic"), ko_src=_one(kv, "ko_src"),
        boot_conf=_one(kv, "boot_conf") == "1", loaded=_one(kv, "loaded") == "1",
        refcnt=int(refcnt) if refcnt.isdigit() else None, dev=_one(kv, "dev") == "1",
        dt_nodes=tuple(kv.get("dt_node", [])), modules_load=_one(kv, "modules_load"),
        relay_active=_one(kv, "relay_active"), relay_after=_one(kv, "relay_after"))


def rebuild_reasons(facts: RingmapFacts, digest: str) -> list[str]:
    """据え付けてある .ko を作り直す理由（事実）。空 = そのまま使える（vermagic の版の名前と、作ったソースが一致）。"""
    if not facts.ko_present:
        return [f"{facts.ko_path} が無い"]
    reasons = []
    if facts.ko_release != facts.release:
        reasons.append(f"vermagic の版 {facts.ko_release or '(読めない)'} が uname -r の {facts.release} と違う")
    if facts.ko_src != digest:
        reasons.append(f"fprobe_src {facts.ko_src or '(無い)'} がリポジトリのソースの sha256 {digest} と違う")
    return reasons


def _ringmap_plan(loader: PynqLoader, facts: RingmapFacts, reasons: list[str], *, undo: bool) -> str:
    head = f"PYNQ（{loader.user}@{loader.host}、カーネル {facts.release}）の起動の設定を変える:"
    if undo:
        lines = [f"{RINGMAP_CONF} を消す（次の起動から {RINGMAP_MODULE} を読み込まない）",
                 f"{facts.ko_path} を消し、depmod {facts.release}"]
        if facts.loaded:
            lines.append(f"今読み込まれている（refcnt {facts.refcnt}）ので外す: 中継を止めて rmmod {RINGMAP_MODULE}、"
                         f"中継が動いていたら（今 {facts.relay_active}）起動し直す。流れているストリームは切れる。"
                         "起動し直した中継は複写の道で送る")
        else:
            lines.append("今は読み込まれていない（中継は止めない）")
    else:
        if reasons:
            lines = [f"モジュールを PYNQ の上で作る（build.sh。約 3 分・{RINGMAP_WORK} に約 110 MB、据え付けの後に消す）。"
                     "理由: " + "・".join(reasons),
                     f"{facts.ko_path} に置き、depmod {facts.release}"]
        else:
            lines = [f"{facts.ko_path} は作り直さない（vermagic の版 = uname -r、fprobe_src = ソースの sha256）",
                     f"depmod {facts.release}"]
        lines += [f"{RINGMAP_CONF} を置く（次の起動から systemd-modules-load が {RINGMAP_MODULE} を読み込む）",
                  "今は読み込まない・中継は止めない。再起動の後に効く"]
        if not facts.relay_after_modules_load:
            lines.append("注意: 動いている fprobe-relay.service の After= に systemd-modules-load.service が無い"
                         "（pynq install で新しいサービスのファイルを置く）")
        if len(facts.dt_nodes) != 1:
            lines.append(f"注意: /proc/device-tree/reserved-memory の fprobe_ring のノードが {len(facts.dt_nodes)} 個"
                         "（モジュールはただ 1 つのときだけ読み込める。この起動の設定のままでは読み込みが失敗し、"
                         "中継は複写の道で動く。docs/boards/pynq-z2.md の reserved-memory の起動の設定）")
    return "\n".join([head, *(f"  - {line}" for line in lines)])


def ringmap(loader: PynqLoader, *, run=subprocess.run, env=os.environ, undo: bool = False, announce=print) -> None:
    """fprobe_ringmap を起動時に読み込む設定を置く（undo = True なら外す）。何を変えるかを、事実を読んだ後・管理者の手順の
    前に announce で示す。

    置く: ソースを送り、作り直しが要れば（rebuild_reasons）PYNQ の上で build.sh、作ったものの vermagic の版と fprobe_src を
    確かめてから /lib/modules/<版>/extra に置いて depmod、/etc/modules-load.d に設定。今は読み込まない（再起動の後に効く）。
    外す: 設定と .ko を消して depmod、読み込まれていれば中継を止めて rmmod（中継が写している間は rmmod できない）、
    中継が動いていたら起動し直す。"""
    password = _password(env, "モジュールの起動時の読み込みの設定")
    digest = ringmap_source_digest()
    if not undo:
        loader.ssh(f"mkdir -p {RINGMAP_REMOTE_DIR}")
        _scp(loader, [*(RINGMAP_DIR / s for s in RINGMAP_SOURCES), RINGMAP_DIR / RINGMAP_CONF_SOURCE], run,
             RINGMAP_REMOTE_DIR)
    facts = read_ringmap_facts(loader)
    reasons = [] if undo else rebuild_reasons(facts, digest)
    announce(_ringmap_plan(loader, facts, reasons, undo=undo))
    rel, ko, mod = facts.release, facts.ko_path, RINGMAP_MODULE
    if undo:
        unload = (f"if [ -d /sys/module/{mod} ]; then if systemctl is-active -q fprobe-relay; then "
                  f"systemctl stop fprobe-relay && rmmod {mod} && systemctl start fprobe-relay; else rmmod {mod}; fi; fi")
        _sudo(loader, f"rm -f {RINGMAP_CONF} {ko} && depmod {rel} && {unload}", password, run,
              what="モジュールの起動時の読み込みを外す手順（管理者の手順）")
        return
    built = f"{RINGMAP_WORK}/mod/{mod}.ko"
    if reasons:
        # 前の作業の置き場所（別のカーネルのビルドの木の写しかもしれない。build.sh はあれば写し直さない）を名前で消してから作る
        loader.ssh(f"rm -rf {RINGMAP_WORK} && cd {RINGMAP_REMOTE_DIR} && bash build.sh {RINGMAP_WORK}",
                   timeout=RINGMAP_BUILD_TIMEOUT_S)
        got = _parse_kv(loader.ssh(_ko_facts_sh("", built)))
        vermagic, src = _one(got, "vermagic"), _one(got, "src")
        if _one(got, "present") != "1" or vermagic.split()[:1] != [rel] or src != digest:
            raise PynqError(f"作ったモジュールが合わない（{built}: vermagic {vermagic!r}、uname -r {rel}、"
                            f"fprobe_src {src!r}、ソースの sha256 {digest}）。据え付けない。"
                            f"作業の置き場所 {RINGMAP_WORK} は調べるために残した")
    steps = [f"install -D -m 644 {built} {ko}"] if reasons else []
    steps += [f"depmod {rel}", f"install -m 644 {RINGMAP_REMOTE_DIR}/{RINGMAP_CONF_SOURCE} {RINGMAP_CONF}"]
    _sudo(loader, " && ".join(steps), password, run, what="モジュールの起動時の読み込みの据え付け（管理者の手順）")
    if reasons:
        loader.ssh(f"rm -rf {RINGMAP_WORK}")


def query_relay_status(host: str, *, port: int = RELAY_UDP_PORT, timeout_s: float = 0.5,
                       sock_factory=socket.socket, token: int = 1) -> dict | None:
    """PYNQ の中継に状態を問い合わせる（UDP の CH_RELAY_STATUS。返り値は fprobe.eth_link.decode_relay_status の辞書）。
    timeout_s のうちに返事が無ければ None（中継が動いていない・PL の印が無い・古い中継）。返事は問い合わせの番号 token を写した
    ものだけを受ける（中継の版 2。版 1 の中継は写さないので返事なし）。この問い合わせは中継の PC の宛先を
    変えない（relay.c の relay_dgram_sets_peer）。それを入れる前の中継は変える（使っている PC の UDP の応答がこのソケットへ行く）。"""
    from fprobe.eth_link import CH_RELAY_STATUS, decode_relay_status, relay_status_request   # ホストのライブラリ（同じ venv）
    sock = sock_factory(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        sock.bind(("", 0))
        sock.sendto(relay_status_request(token), (host, port))
        deadline = time.monotonic() + timeout_s
        while (left := deadline - time.monotonic()) > 0:
            sock.settimeout(left)
            try:
                data, _ = sock.recvfrom(65535)
            except TimeoutError:
                return None
            if data[:2] == bytes([CH_RELAY_STATUS, token]):
                return decode_relay_status(data)
        return None
    finally:
        sock.close()


def _ringmap_status_text(facts: RingmapFacts, digest: str, relay: dict | None, relay_log: str) -> str:
    def yn(v):
        return "あり" if v else "なし"
    if facts.ko_present:
        same = "同じ" if facts.ko_src == digest else "違う"
        ko = (f"あり（{facts.ko_path}、vermagic の版 {facts.ko_release or '(読めない)'}、uname -r {facts.release}、"
              f"fprobe_src {facts.ko_src or '(無い)'}: リポジトリのソースの sha256 と{same}）")
    else:
        ko = f"なし（{facts.ko_path}）"
    lines = ["ゼロコピーの道（fprobe_ringmap）:",
             f"  モジュールのファイル: {ko}",
             f"  起動時の読み込みの設定（{RINGMAP_CONF}）: {yn(facts.boot_conf)}",
             f"  読み込まれている: {yn(facts.loaded)}" + (f"（refcnt {facts.refcnt}）" if facts.loaded else ""),
             f"  /dev/fprobe_ring: {yn(facts.dev)}",
             f"  デバイスツリーの fprobe_ring のノード: {' '.join(facts.dt_nodes) or 'なし'}",
             f"  systemd-modules-load: {facts.modules_load}",
             f"  fprobe-relay の After= に systemd-modules-load.service: {yn(facts.relay_after_modules_load)}",
             f"  中継の journal（この起動の fprobe_ring の最後の行）: {relay_log.strip() or '(無い)'}"]
    if relay is None:
        lines.append("  中継の状態（relay_status）: 返事なし（中継が動いていない・PL の印が無い・古い中継）")
    else:
        lines.append(f"  中継の状態（relay_status）: zc_state {relay['zc_state']}、tcp_path {relay['tcp_path']}"
                     "（tcp_path は TCP のストリームがつながっている間だけ none 以外）")
    return "\n".join(lines)


def status(loader: PynqLoader, *, relay_query=query_relay_status) -> str:
    """中継の状態・PL の印・中継の journal の最後と、ゼロコピーの道（モジュール・起動時の読み込み・中継がどちらの道か）。"""
    base = loader.ssh("systemctl is-active fprobe-relay; cat /run/fprobe/pl-design 2>/dev/null || echo '(印なし)'; "
                      "journalctl -u fprobe-relay -n 5 --no-pager 2>/dev/null || true")
    facts = read_ringmap_facts(loader)
    relay_log = loader.ssh("journalctl -u fprobe-relay -b -o cat --no-pager 2>/dev/null | grep fprobe_ring | tail -n 1"
                           " || true")
    text = _ringmap_status_text(facts, ringmap_source_digest(), relay_query(loader.host), relay_log)
    return base.rstrip("\n") + "\n" + text + "\n"
