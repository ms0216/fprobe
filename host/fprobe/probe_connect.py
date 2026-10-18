"""Probe の道具: 接続の手順の上書きとロック解除（U30）。J-Link Script Files・LinkServer のスクリプト・pyOCD の user script、
STM32CubeProgrammer のオプションバイト、保護の解除に当たる。

Probe（fprobe.mcp_server）に混ぜて使う mixin。Probe の _lock・target・target_disconnect と、LowLevelTools の
_raw_dp（未接続の DP）を使う。機器に触るのは必ず _lock の中。

- 接続の手順: target_connect の script（"builtin:<名前>" かファイル）を pyOCD の user_script に渡す。ここでは
  script_check（実行せずに検査）と script_list（内蔵の手順）。
- オプションバイト: STM32F4・L4・G4・G0（fprobe.stm32_option_bytes）。部品は DBGMCU の DEV_ID で判別する。
- 解除（target_unlock）: STM32 は RDP を Level 0 に（接続して。全消去）、nRF52 は CTRL-AP の ERASEALL（未接続で。全消去）。
  どちらも confirm_mass_erase が無ければ何もせず断る。
"""

import locale
import tempfile
import time
from pathlib import Path

from pyocd.core.target import Target

from . import stm32_option_bytes as ob
from .connect_scripts import builtin_scripts, check_script
from .coresight_id import identify_part
from .raw_dap import DapError

__all__ = ["ConnectTools", "NRF_CTRL_AP_IDR"]

NRF_CTRL_AP_IDR = 0x02880000
_CTRL_RESET, _CTRL_ERASEALL, _CTRL_ERASEALLSTATUS, _CTRL_APPROTECTSTATUS = 0x00, 0x04, 0x08, 0x0C
_MASS_ERASE_REFUSAL = ("全消去を伴う（Flash のプログラムもデータも消える）: よければ confirm_mass_erase=true で。"
                       "何もしていない")


class ConnectTools:
    """接続の手順の上書きとロック解除（Probe の mixin）。"""

    # --- 接続の手順 ---------------------------------------------------------------------------

    def script_check(self, script: str) -> dict:
        """接続の手順のスクリプト（pyOCD のユーザスクリプト。"builtin:<名前>" かファイル）を実行せずに調べる:
        定義された手順（will_connect / did_init_target / will_reset / unlock_device 等）、名前・引数の誤り、構文の誤り。"""
        return check_script(script).as_dict()

    def script_list(self) -> dict:
        """内蔵の接続の手順（target_connect の script に "builtin:<名前>" で渡す）と、pyOCD の手順の名前の一覧。"""
        from .connect_scripts import HOOKS
        return {"builtin": builtin_scripts(),
                "hooks": [{"name": n, "params": h["params"], "summary": h["summary"]} for n, h in HOOKS.items()]}

    def _connect_script(self, script: str) -> tuple[str, dict]:
        check = check_script(script)
        if check.error:
            raise ValueError(f"スクリプト {script!r} に構文の誤り: {check.error}")
        return _for_pyocd(Path(check.path)), check.as_dict()

    # --- オプションバイト --------------------------------------------------------------------------

    def _stm32_family(self):
        target = self.target
        part = identify_part(target.read32)
        if part is None:
            raise ob.OptionBytesError("部品を判別できない（STM32 の DBGMCU の IDCODE が読めない）")
        return target, part, ob.family_for(part)

    def option_bytes_read(self) -> dict:
        """STM32 のオプションバイト（RDP・BOR・ウォッチドッグ・リセット・ブート・書き込み保護）を名前で読む。変えない。"""
        with self._lock:
            target, part, family = self._stm32_family()
            return {"part": part.as_dict(), **ob.decode(family, target.read32(family.option_address))}

    def option_bytes_write(self, fields: dict[str, int], confirm_mass_erase: bool = False,
                           confirm_protect: bool = False, reset: bool = True, timeout_s: float = 60.0) -> dict:
        """STM32 のオプションバイトを名前で書く（fields 例 {"nWRP": 0xFF, "IWDG_SW": 1}。書かないビットは保つ）。
        RDP を Level 1 → 0（全消去）には confirm_mass_erase、Level 0 → 1 には confirm_protect が要る。Level 2 は書かない。
        コアを止めてから書く。reset: F4 は書いた後でリセットして止める、L4 / G4 / G0 は OBL_LAUNCH で反映
        （ターゲットがリセットされ切断する: target_connect し直す）。"""
        with self._lock:
            target, part, family = self._stm32_family()
            before = target.read32(family.option_address)
            new, notes = ob.plan_write(family, before, dict(fields), confirm_mass_erase, confirm_protect)
            if target.get_state() != Target.State.HALTED:
                target.halt()
                notes.append("コアを止めた")
            launch = reset and family.style == "optr"
            log = ob.program(target, family, new, timeout_s=timeout_s, launch=launch)
            out = {"part": part.as_dict(), "before": ob.decode(family, before), "notes": notes, "log": log,
                   "reconnect_required": launch}
            if launch:
                out["planned"] = ob.decode(family, new)
                try:
                    self.target_disconnect()
                except Exception:
                    self._debug = None
                notes.append("OBL_LAUNCH でターゲットがリセットされた: target_connect で接続し直す")
                return out
            after = target.read32(family.option_address)
            out["after"] = ob.decode(family, after)
            if reset:
                target.reset_and_halt()
                notes.append("リセットして止めた（新しい値はリセットで有効）")
            elif family.style == "optr":
                notes.append("反映は OBL_LAUNCH か次の電源投入")
            return out

    # --- 解除 ---------------------------------------------------------------------------------------

    def target_unlock(self, confirm_mass_erase: bool = False, reset: bool = True, timeout_s: float = 60.0) -> dict:
        """保護を全消去で解除する。接続中: STM32 の RDP を Level 0 に（option_bytes_write と同じ手順）。未接続: nRF52 の
        CTRL-AP の ERASEALL（APPROTECT の解除）。confirm_mass_erase が無ければ何もせず断る。"""
        with self._lock:
            if self._debug is not None:
                target, part, family = self._stm32_family()
                current = target.read32(family.option_address)
                level = ob.rdp_level(family.field("RDP").get(current))
                if level == 0:
                    return {"kind": "stm32_rdp", "already_unlocked": True, "part": part.as_dict(),
                            "option_bytes": ob.decode(family, current)}
                if not confirm_mass_erase:
                    raise ValueError(f"{part.name} の RDP Level {level} を解除する: {_MASS_ERASE_REFUSAL}")
                out = self.option_bytes_write({"RDP": ob.RDP_LEVEL0}, confirm_mass_erase=True, reset=reset,
                                              timeout_s=timeout_s)
                return {"kind": "stm32_rdp", "already_unlocked": False, **out}
            return self._unlock_nrf(confirm_mass_erase, timeout_s)

    def _unlock_nrf(self, confirm_mass_erase: bool, timeout_s: float) -> dict:
        dap = self._raw_dp()
        ctrl_ap = next((ap for ap in range(4) if self._read_ap_quiet(dap, ap, 0xFC) == NRF_CTRL_AP_IDR), None)
        if ctrl_ap is None:
            raise RuntimeError("未接続で解除できるのは nRF52 の CTRL-AP だけ: STM32 は target_connect（connect_mode "
                               "under-reset か attach）してから target_unlock")
        protected = dap.read_ap(ctrl_ap, _CTRL_APPROTECTSTATUS) & 1 == 0
        if not protected:
            return {"kind": "nrf_ctrl_ap", "already_unlocked": True, "ap": ctrl_ap}
        if not confirm_mass_erase:
            raise ValueError(f"nRF の APPROTECT を解除する: {_MASS_ERASE_REFUSAL}")
        dap.write_ap(ctrl_ap, _CTRL_ERASEALL, 1)
        deadline = time.monotonic() + timeout_s
        while dap.read_ap(ctrl_ap, _CTRL_ERASEALLSTATUS) & 1:
            if time.monotonic() >= deadline:
                raise RuntimeError(f"ERASEALL が {timeout_s} 秒で終わらない")
            time.sleep(0.05)
        dap.write_ap(ctrl_ap, _CTRL_RESET, 1)
        dap.write_ap(ctrl_ap, _CTRL_RESET, 0)
        unlocked = dap.read_ap(ctrl_ap, _CTRL_APPROTECTSTATUS) & 1 == 1
        return {"kind": "nrf_ctrl_ap", "already_unlocked": False, "ap": ctrl_ap, "erased": True, "unlocked": unlocked,
                "notes": ["全消去してリセットした" + ("" if unlocked else "。APPROTECT がまだ有効: UICR を書かずに電源を入れ直す")]}

    @staticmethod
    def _read_ap_quiet(dap, ap: int, register: int) -> int | None:
        try:
            return dap.read_ap(ap, register)
        except DapError:
            try:
                dap.clear_errors()
            except DapError:
                pass
            return None


def _for_pyocd(path: Path) -> str:
    """pyOCD は user_script を OS の既定の文字コードで開く（Windows では cp932）。UTF-8 の日本語のコメントがあると
    読めない（実機で UnicodeDecodeError。2026-10-18）。既定の文字コードで読めなければ、その文字コードで書き直した
    写しを渡す（表せない文字はバックスラッシュの形にする。コメントの外の文字列は ASCII にしておくこと）。"""
    encoding = locale.getpreferredencoding(False)
    data = path.read_bytes()
    try:
        data.decode(encoding)
        return str(path)
    except UnicodeDecodeError:
        pass
    text = data.decode("utf-8")
    copy = Path(tempfile.gettempdir()) / "fprobe_connect_scripts" / path.name
    copy.parent.mkdir(parents=True, exist_ok=True)
    copy.write_bytes(text.encode(encoding, errors="backslashreplace"))
    return str(copy)
