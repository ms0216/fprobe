"""fprobe-daemon: fprobe を 1 つ握り、JSON-RPC 2.0 over WebSocket で全機能を出す。--gdb で同じセッションの上に
GDB サーバ（fprobe.gdb）も開く（IDE とロジアナ・記録・AI を同時に使う）。
仕様: docs/superpowers/specs/2026-10-17-host-daemon-design.md、docs/superpowers/specs/2026-10-18-u04-gdb-server-design.md

    fprobe-daemon [--serial 0001 | --name NAME | --port COM3 | --eth 192.168.77.2] [--lan [--token T]]
                  [--gdb 3333 [--gdb-target stm32f401xe] [--gdb-role human]]
                  [--stream rtt0=19021:telnet --stream swo=3443 …] [--stream-log rtt0=~/rtt0.log:time …]

--stream は生の流れ（rtt0〜 / swo / uart / semihosting）を TCP で公開する（fprobe.probe_streams。127.0.0.1 だけ）。
"""

import argparse
from pathlib import Path

from . import DEFAULT_PORT, device_of
from .auth import TOKEN_FILE, daemon_token
from .core import NOTIFY_LIMIT, Daemon, StreamHost, _Connection, _Rpc  # noqa: F401（利用者・試験向けの再公開）
from .jsonable import jsonable  # noqa: F401
from .permissions import FILE_NAME as PERMISSIONS_FILE, prepare_permissions
from .webapp import UI_DIR, create_app  # noqa: F401
from ..home import fprobe_home
from ..probe import Probe
from ..gdb.session import Options as GdbOptions

__all__ = ["DEFAULT_PORT", "GdbOptions", "StreamHost", "create_app", "device_of", "main", "parse_stream",
           "parse_stream_log"]


def parse_stream(text: str) -> dict:
    """--stream の値 "名前=ポート[:raw|telnet]" → stream_serve の引数。"""
    name, sep, rest = text.partition("=")
    port, _, protocol = rest.partition(":")
    if not sep or not name or not port.isdigit():
        raise ValueError(f"--stream は 名前=ポート[:raw|telnet]（{text!r}）")
    return {"stream": name, "port": int(port), "protocol": protocol or "raw"}


def parse_stream_log(text: str) -> dict:
    """--stream-log の値 "名前=ファイル[:time]" → stream_log_start の引数。:time なら行の頭に時刻を付ける（文字の流れ
    用。流れが文字かバイナリかは名前から決めないので、使う側が明示する）。"""
    name, sep, path = text.partition("=")
    timestamps = path.endswith(":time")
    if timestamps:
        path = path[:-len(":time")]
    if not sep or not name or not path:
        raise ValueError(f"--stream-log は 名前=ファイル[:time]（{text!r}）")
    return {"stream": name, "path": path, "timestamps": timestamps}



# Ethernet で開くとき、リンクの確立（ボードの書き込み直後の自動ネゴシエーション等）を待つ秒数（この入口の値。識別の READ を
# この間だけ送り直し、送り直した回数は probe.info の link_open で返す。--eth-link-up-wait で変えられる）
ETH_LINK_UP_WAIT_S = 15.0


def main() -> None:
    import uvicorn
    from ..serial_link import DEFAULT_BAUD
    parser = argparse.ArgumentParser(description="fprobe-daemon: fprobe を握って API を出す")
    parser.add_argument("--serial", help="fprobe の USB シリアル番号")
    parser.add_argument("--name", help="個体名（fprobe name set で付けた名前）。--serial / --port / --eth の代わり")
    parser.add_argument("--port", help="SerialLink の COM ポート")
    parser.add_argument("--baud", type=int, default=DEFAULT_BAUD)
    parser.add_argument("--eth", help="EthLink のボードの IP アドレス")
    parser.add_argument("--eth-link-up-wait", type=float, default=ETH_LINK_UP_WAIT_S, metavar="SECONDS",
                        help=f"--eth で開くとき、リンクの確立を待つ秒数（識別の READ をこの間だけ送り直す。既定 {ETH_LINK_UP_WAIT_S:g}）")
    parser.add_argument("--listen", type=int, default=DEFAULT_PORT, help="待ち受けるポート")
    parser.add_argument("--bitstreams", help="Primer の構成のビットストリームの置き場所（<dir>/<design>/top.fs。"
                                             "既定は FPROBE_BITSTREAMS か gateware/build）")
    parser.add_argument("--lan", action="store_true", help="LAN にも開く（平文。暗号化するなら docs/guide/remote.md の SSH の転送）")
    parser.add_argument("--token", help="合言葉（省略時は ~/.fprobe/daemon_token を使い回す。無ければ作る）")
    parser.add_argument("--permissions", type=Path, help="許可の表（PC で実行してよいファイル・書いてよい場所。"
                                                         f"既定 ~/.fprobe/{PERMISSIONS_FILE}。無ければ ~/.fprobe/out "
                                                         "だけに書いてよい表を作る）")
    parser.add_argument("--gdb", type=int, metavar="PORT", help="GDB サーバを開くポート（例 3333。127.0.0.1 だけ）")
    parser.add_argument("--gdb-target", default="cortex_m",
                        help="GDB がつながったときにターゲットが未接続なら接続する pyOCD のターゲット名")
    parser.add_argument("--gdb-frequency", type=int, default=4_000_000, help="そのときの SWD / JTAG の周波数")
    parser.add_argument("--gdb-role", choices=["human", "ai", "script"], default="human",
                        help="GDB の利用者の役割（human は人の一時停止でも保留されない）")
    parser.add_argument("--stream", action="append", default=[], type=parse_stream, metavar="NAME=PORT[:telnet]",
                        help="生の流れ（rtt0〜 / swo / uart / semihosting）を 127.0.0.1:PORT で公開する（繰り返せる。"
                             "例 rtt0=19021:telnet。RTT・SWO の受信は API で始める）")
    parser.add_argument("--stream-log", action="append", default=[], type=parse_stream_log, metavar="NAME=PATH[:time]",
                        help="流れをファイルに書く（繰り返せる。:time なら行の頭に時刻。例 rtt0=rtt0.log:time）")
    parser.add_argument("--new-token", action="store_true", help="保存した合言葉を作り直す（画面は /login で入れ直す）")
    args = parser.parse_args()
    from .. import probe_names
    try:
        probe_names.apply_to_args(args)
    except probe_names.ProbeNameError as e:
        parser.error(str(e))
    from .permissions import PermissionsFileError
    try:
        permissions = prepare_permissions(args.permissions or fprobe_home() / PERMISSIONS_FILE, fprobe_home())
    except PermissionsFileError as e:
        parser.error(str(e))
    # 合言葉は出力に出さない（MCP が起動したデーモンの出力は daemon.log に残る）。場所だけを出す
    token = daemon_token(args.token, fprobe_home(), renew=args.new_token)
    print(f"画面: http://127.0.0.1:{args.listen}/（初めては合言葉を入れる: "
          f"{'--token で渡したもの' if args.token else fprobe_home() / TOKEN_FILE}）", flush=True)
    if permissions.created:
        print(f"許可の表を作った: {permissions.file}（初めての起動。書いてよい: {', '.join(map(str, permissions.write))}。"
              "実行: なし。足すときはこのファイルを直してデーモンを起動し直す）", flush=True)
    else:
        print(f"許可の表: {permissions.file}", flush=True)
    if args.gdb is not None:
        print(f"GDB: target remote 127.0.0.1:{args.gdb}", flush=True)
    for s in args.stream:
        print(f"流れ {s['stream']}: 127.0.0.1:{s['port']}（{s['protocol']}）", flush=True)
    backend = Probe(args.serial, args.port, args.baud, args.eth, bitstreams=args.bitstreams,
                    link_up_wait_s=args.eth_link_up_wait if args.eth else None)
    backend.ui_state_persist(fprobe_home() / "ui_state.json")   # 信号の定義・システム解析の表等を再起動を越えて
    app = create_app(backend, token=token, activity_dir=fprobe_home() / "activity",
                     device=device_of(args.serial, args.port, args.eth), gdb_port=args.gdb,
                     gdb_options=GdbOptions(args.gdb_target, args.gdb_frequency), gdb_role=args.gdb_role,
                     streams=args.stream, stream_logs=args.stream_log, permissions=permissions)
    try:
        uvicorn.run(app, host="0.0.0.0" if args.lan else "127.0.0.1", port=args.listen, log_level="warning")
    finally:
        app.state.daemon.arbiter.close()
        backend.close()


if __name__ == "__main__":
    main()
