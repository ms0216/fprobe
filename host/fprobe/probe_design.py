"""Probe の道具: Primer の構成（ビットストリーム）を知る・切り替える（B22）。

Probe（fprobe.probe）が API の名前を委ねるサービス（fprobe.probe_service。ホストから使う名前は NEEDS）。Probe の _lock・session・close・_debug と、接続の設定（_serial・
_port・_eth）を使う。構成の対応と書き込みは fprobe.bitstreams。

- probe_design_get: 今のデザイン・線と機能の有無・切り替えられる構成（ビットストリームの場所と有無）。
- probe_design_switch: 構成を明示して書き替える（ターゲット未接続のときだけ）。
- _ensure_design(protocol): target_connect・JTAG の低レベルの道具が、要る線の構成でなければ書き替える
  （自動の書き替え。FPROBE_AUTO_SWITCH=0 なら誤り）。ロックを持たずに呼ぶこと（書き替えは Probe を閉じる）。

書き替え: Probe を閉じる（FPGA が入れ替わるのでターゲット・ロジアナ・流れはすべて切れる）→ fprobe_gw program →
USB の再列挙を待って開き直す → ケイパビリティの design を確かめる。

- recover_link(error): USB が切れた誤り（Primer がつながり直した）なら、Probe を閉じて開き直し、利用者に返す説明を
  返す（B28）。デーモンが機器の誤りのたびに呼ぶ。ビットストリームの版（ケイパビリティの build。U49）が前と違えば
  別の版と言い、ビルド済みの最新（<ビットストリームの置き場所>/<デザイン>/inputs.sha256）かも書く。版の無い古い構成
  では変わった機能（事実）だけを書き、原因は推測しない。
"""

import contextlib
import errno
import time
from collections.abc import Callable

import usb.core

from . import bitstreams as bs
from .probe_service import HostView

__all__ = ["DesignService", "REENUMERATE_S", "link_lost"]

REENUMERATE_S = 30.0                    # 書き込みの後、USB の fprobe が現れて CONTROL が応答するまでの上限
_POLL_S = 0.5
_LOST_ERRNOS = {errno.EIO, errno.ENODEV}   # libusb の IO・NO_DEVICE（待ちすぎ・STALL は切断ではない）


def link_lost(error: BaseException | None) -> bool:
    """USB が切れた誤りか（pyOCD 等に包まれていても、原因をたどって見る）。"""
    seen = set()
    while error is not None and id(error) not in seen:
        seen.add(id(error))
        if isinstance(error, usb.core.USBError) and not isinstance(error, usb.core.USBTimeoutError)                 and error.errno in _LOST_ERRNOS:
            return True
        error = error.__cause__ or error.__context__
    return False


class DesignService:
    """構成の取得・切り替え（Probe のサービス）。"""

    API = ("probe_design_get", "probe_design_switch", "recover_link")
    # ほかの領域（Probe の委譲）・試験に出す名前
    SHARED = ("_ensure_design", "_reenumerate_s", "_switch_design", "_switchable")
    # ホスト（Probe の核と、ほかの領域）から使う名前
    NEEDS = ("_debug", "_eth", "_lock", "_port", "_recorder", "_release_device", "_require_dap_free",
             "_restore_outputs", "_session", "recording_stop", "session")

    def __init__(self, host, bitstreams: str | None = None, *, program: Callable = bs.program,
                 sleep: Callable[[float], None] = time.sleep, reenumerate_s: float = REENUMERATE_S):
        self._host = HostView(host, self.NEEDS, type(self).__name__)
        self._bitstreams = bitstreams           # ビットストリームの置き場所（None なら FPROBE_BITSTREAMS か既定）
        self._program_bitstream = program
        self._sleep = sleep
        self._reenumerate_s = reenumerate_s

    # --- 道具 ---------------------------------------------------------------------------------

    def probe_design_get(self) -> dict:
        """Primer の構成（ビットストリーム）: 今のデザイン・線（SWD / JTAG）と機能（サンプラ・故障注入・専用の入力・
        ロジアナの流し続けにイベントも載せる la_stream_events）の有無・切り替え
        られる構成（線・ビットストリームの場所と有無）・自動の書き替えの有無。target_connect は要る線の構成でなければ
        自動で書き替える（JTAG は jtag_chain か Cortex-A・ARM11 のターゲット）。"""
        with self._host._lock:
            caps = self._host.session.device.capabilities()
        return {
            "design": caps.design, "board": caps.board,
            "features": {"swd": True, "jtag": caps.jtag, "sampler": caps.sampler, "inject": caps.inject,
                         "la_inputs": sum(c.kind == "input" for c in caps.la_layout),
                         "la_stream_events": bool(caps.la_stream_events)},
            "switchable": self._switchable(caps),
            "auto_switch": bs.auto_switch_enabled(),
            "designs": [{"design": design, "protocols": bs.protocols_of(design),
                         "for": [f for f, d in bs.FEATURES.items() if d == design],
                         "bitstream": str(bs.bitstream_path(design, self._bitstreams)),
                         "available": bs.bitstream_path(design, self._bitstreams).is_file(),
                         "current": design == caps.design}
                        for design in dict.fromkeys([*bs.PROTOCOLS.values(), *bs.FEATURES.values()])],
        }

    def probe_design_switch(self, design: str | None = None, protocol: str | None = None) -> dict:
        """Primer の構成を書き替える（ターゲット未接続のときだけ）。design（"fprobe" = 標準: SWD・サンプラ・故障注入、
        "fprobe_jtag" = JTAG と SWD、"fprobe_la" = SWD とロジアナの専用の入力 6 本（J14）・ロジアナの流し続けにイベントも
        載せる（サンプラ・故障注入なし））か
        protocol（"swd" / "jtag"）で指定する。今と同じなら何もしない。書き替えると
        ロジアナ・記録・流れの状態も消える。"""
        if (design is None) == (protocol is None):
            raise ValueError("design か protocol のどちらか 1 つを指定する")
        wanted = design if design is not None else bs.design_for(protocol)
        if wanted not in bs.SWITCHABLE:
            raise ValueError(f"切り替えられる構成は {sorted(bs.SWITCHABLE)}: {wanted!r}")
        with self._host._lock:
            if self._host._debug is not None:
                raise RuntimeError("ターゲットに接続中: target_disconnect してから書き替える")
            caps = self._host.session.device.capabilities()
        if caps.design == wanted:
            return {"switched": False, "design": wanted}
        if not self._switchable(caps):
            raise bs.DesignSwitchError(f"今のデザイン {caps.design!r}（{caps.board}）は書き替えの対象でない")
        return {"switched": True, **self._switch_design(caps.design, wanted)}

    # --- 自動の書き替え -----------------------------------------------------------------------------

    def _ensure_design(self, protocol: str) -> dict | None:
        """protocol の線を使えるよう、要れば構成を書き替える。書き替えたらその内容、要らなければ None。"""
        with self._host._lock:
            caps = self._host.session.device.capabilities()
        wanted = bs.wanted_design(caps, protocol)
        if wanted is None:
            return None
        if not self._switchable(caps):
            if protocol == "jtag" and not caps.jtag:
                raise bs.DesignSwitchError(f"デザイン {caps.design!r} に JTAG が無く、この接続では書き替えられない")
            return None
        if not bs.auto_switch_enabled():
            if protocol == "jtag" and not caps.jtag:
                raise bs.DesignSwitchError(
                    f"今の構成 {caps.design!r} に JTAG が無い。自動の書き替えは切ってある（{bs.ENV_AUTO}=0）: "
                    f"probe_design_switch(protocol=\"jtag\") で書き替える")
            return None                         # SWD はどちらの構成にもある（標準でないだけ）
        return self._switch_design(caps.design, wanted)

    def _switchable(self, caps) -> bool:
        """書き替えてよいか: Primer の USB の製品デザイン（組の中）で、USB でつないでいる。"""
        return caps.design in bs.SWITCHABLE and not self._host._port and not self._host._eth

    def _switch_design(self, before: str, design: str) -> dict:
        self._host._require_dap_free("構成の書き替え")
        path = bs.bitstream_path(design, self._bitstreams)
        if not path.is_file():
            raise bs.DesignSwitchError(
                f"構成 {design!r} のビットストリーム {path} が無い（python -m fprobe_gw build {design} で作る。"
                f"置き場所は {bs.ENV_ROOT} か --bitstreams）")
        started = time.monotonic()
        # ビットストリームを書くとタイムベースが 0 に戻る（time-accuracy §14.1）: 記録は前の時代で終える（混ぜない）
        stopped = self._end_recording("design_switch", before=before, after=design)
        released = self._host._release_device()                 # FPGA が入れ替わる: ターゲット・ロジアナ・流れ・USB を閉じる（失敗した段は事実として返す）
        self._program_bitstream(path)
        caps = self._reopen("書き込みの後")
        if caps.design != design:
            raise bs.DesignSwitchError(f"書き込んだが、デザインが {caps.design!r}（{design!r} のはず）: {path}")
        outputs = self._host._restore_outputs()       # 出力に使っていた端子を取り戻す（B29）
        return {"before": before, "after": design, "bitstream": str(path),
                "seconds": round(time.monotonic() - started, 1), "outputs": outputs, "recording_stopped": stopped,
                "release_failures": released}

    def _end_recording(self, reason: str, **facts) -> str | None:
        """記録中なら理由を残して記録を止める（止めた記録の名前。記録していなければ None）。"""
        recorder = self._host._recorder
        if recorder is None:
            return None
        recorder.end(reason, **facts)
        return self._host.recording_stop()["recording"]

    def recover_link(self, error: BaseException) -> dict | None:
        """error が USB の切断なら、閉じて開き直し、{"message": 説明, "changed": {機能: [前, 後]}, "version": 版の比較,
        "outputs": 出力の端子の取り戻し（XtrigOutputService.restore_outputs）} を返す（違えば None）。
        changed は前と変わった機能（版を除く。前が分からなければ空）。version は {"before", "after": 版の説明,
        "changed": 版が違う（どちらかの版が分からなければ None）, "latest_build": 今の版がビルド済みの最新か（分からなければ
        None）}。現れなければ DesignSwitchError。"""
        if self._host._port or self._host._eth or not link_lost(error):
            return None
        before = self._host._session.device.last_capabilities if self._host._session is not None else None
        # USB が切れた間の出来事は失われ、FPGA が立ち上がり直したか（タイムベースが 0 に戻ったか）も開き直すまで分からない:
        # 記録はここで終える（time-accuracy §14.1）。開き直した後のタイムベースと比べた事実を応答に返す
        recorder = self._host._recorder
        last_ticks = getattr(recorder, "_last_ticks", None)
        stopped = None
        if recorder is not None:
            with contextlib.suppress(Exception):           # 切れた機器の最後の取り込みは失敗してよい（記録は閉じる）
                stopped = self._end_recording("link_lost", last_ticks=last_ticks)
        self._host._release_device()                 # 切れた機器への後始末は失敗しうる（どの段も試し、失敗は返す。ここでは使わない）
        if self._host._session is not None:          # close が途中で失敗したとき
            with contextlib.suppress(Exception):
                self._host._session.close()
            self._host._session = None
        self._host._debug = None                     # ponytail: 切れた口の pyOCD のセッションは捨てる（閉じられない）
        after = self._reopen("USB が切れた後")
        note = ("Primer の USB が切れたので、つなぎ直した。ターゲットの接続・ロジアナ・流れは"
                "切れたので、target_connect からやり直す。")
        outputs = self._host._restore_outputs()       # 端子が普段の機能でターゲットを駆動する前に取り戻す（B29）
        if outputs["failed"]:
            note += f" 出力に使っていた端子を取り戻せなかった（{'; '.join(outputs['failed'].values())}）: 配線を確かめる。"
        changed = _changes(before, after)
        version = self._version_change(before, after)
        if version["changed"]:
            note += f" FPGA の版が変わった（前 {version['before']} → 今 {version['after']}）: 別のビットストリームで立ち上がった。"
            if version["latest_build"]:
                note += "今の版はビルド済みの最新と同じ。"
            else:
                note += "開発中の版なら python -m fprobe_gw program で書き直す。"
        elif version["changed"] is None and changed:
            listed = ", ".join(f"{k}: {a} → {b}" for k, (a, b) in changed.items())
            note += (f" FPGA の機能が前と変わった（{listed}）。版を読めないので、どのビットストリームかは分からない。"
                     "開発中の版なら python -m fprobe_gw program で書き直す。")
        recording = None
        if stopped is not None:
            restarted = None
            with contextlib.suppress(Exception):
                with self._host._lock:
                    ticks = self._host.session.device.time()
                restarted = None if last_ticks is None else ticks < last_ticks
            recording = {"name": stopped, "timebase_restarted": restarted}
            note += (f" 記録 {stopped} は切れた所で終えた（切れた間の出来事は無い。続けるなら recording_start で新しく）。")
        return {"message": note, "changed": changed, "version": version, "outputs": outputs,
                "recording_stopped": recording}

    def _version_change(self, before, after) -> dict:
        """版の比較（U49）。"""
        old, new = getattr(before, "build", None), getattr(after, "build", None)
        built = bs.built_id(after.design, self._bitstreams) if new is not None else None
        return {"before": old.describe() if old is not None else None,
                "after": new.describe() if new is not None else None,
                "changed": None if old is None or new is None else old.id != new.id,
                "latest_build": None if new is None or built is None else new.id == built}

    def _reopen(self, after: str):
        """USB の再列挙を待ち、開き直してケイパビリティを返す。after は誤りの文言（いつの後か）。"""
        deadline = time.monotonic() + self._reenumerate_s
        while True:
            try:
                with self._host._lock:
                    return self._host.session.device.capabilities()
            except Exception as e:              # まだ現れない・応答しない（libusb・タイムアウト等）
                if self._host._session is not None:
                    try:
                        self._host._session.close()
                    except Exception:
                        pass
                    self._host._session = None
                if time.monotonic() >= deadline:
                    raise bs.DesignSwitchError(
                        f"{after} {self._reenumerate_s:.0f} 秒たっても fprobe が USB に現れない: {e}") from e
                self._sleep(_POLL_S)


def _changes(before, after) -> dict:
    """ケイパビリティの違い {名前: [前, 後]}。前が分からなければ空。"""
    if before is None:
        return {}
    a, b = _features(before), _features(after)
    return {k: [a.get(k), b.get(k)] for k in sorted(a.keys() | b.keys()) if a.get(k) != b.get(k)}


def _features(caps) -> dict:
    """機能の比較に使う値（版 build は別に比べる）。"""
    return {k: v for k, v in vars(caps).items() if not k.startswith("_") and k != "build"}
