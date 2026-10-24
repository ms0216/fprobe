"""時刻からコードの位置へ（recording.code_at）: 記録の時刻 t の前後に記録に残っている事実を集める。

仕様: docs/superpowers/specs/2026-10-23-ui-rebuild-design.md §4

- PC のサンプル: サンプラが読んだ DWT_PCSR（stream "sample"、a = 0xE000101C、b = 値）の t の直前（時刻 ≦ t）と直後
  （時刻 > t）の 1 つずつ。それぞれの時刻・t との差（秒）・値・PC・ELF で引いた関数・ファイル・行。
  **行を推定しない**: サンプルとサンプルの間に何を実行したかは記録に無い。直前・直後の事実と、その間の幅だけを返す。
  値 0xFFFFFFFF（コアが止まっている・サンプルできない）と読めなかった語（body の ack）は PC を null にし、理由を付ける。
- ターゲットの状態: t の時点で最後に記録された状態の出来事（stream "target" の kind "state"。デーモンの見張りが
  変化を見た時に入れる。時刻は見た時 = 変わった時刻の上限、不確かさ付き）と、t の前後で止まった出来事（HALTED。
  止まった PC は正確。本文は記録したときのまま）。
- タスク: t を含む RTOS の実行の区間（stream "rtos" の kind "run"）。
- 材料の無いもの（PC のサンプル・ELF・状態の出来事・RTOS の時間軸）は missing に理由の符号で返す（判定はしない）。
"""

from typing import Any

from .profile.dwt import DWT_PCSR, PCSR_INVALID
from .recording import Event, Recording
from .symbols import Symbols

__all__ = ["code_at"]

_PLACE = ("function", "offset", "file", "line", "where")


def code_at(recording: Recording, t: int, symbols: Symbols | None, elf: str | None) -> dict:
    """記録の時刻 t（タイムベース）の前後の事実。symbols は PC を行に引く ELF（無ければ None）、elf はそのパス。"""
    hz = float(recording.meta["timebase_hz"])
    missing: dict[str, dict] = {}

    def seconds(ticks: float) -> float:
        return ticks / hz

    # --- PC のサンプル ---
    before = recording.nearest(t, True, "sample", "word", a=DWT_PCSR)
    after = recording.nearest(t, False, "sample", "word", a=DWT_PCSR)
    if before is None and after is None:
        missing["pc"] = {"reason": "no_samples"}
    if symbols is None:
        missing["elf"] = {"reason": "no_elf"}

    def sample(e: Event | None) -> dict | None:
        if e is None:
            return None
        out: dict[str, Any] = {"t": e.t, "dt_s": seconds(e.t - t), "value": e.b, "pc": None}
        if isinstance(e.body, dict) and "ack" in e.body:
            out.update(reason="read_failed", ack=e.body["ack"])
        elif e.b == PCSR_INVALID:
            out["reason"] = "pcsr_invalid"
        else:
            out["pc"] = e.b
        out.update(_place(symbols, out["pc"]))
        return out

    # --- ターゲットの状態と止まった出来事 ---
    state = recording.nearest(t, True, "target", "state")
    halted = {"state": "HALTED"}
    halt_before = recording.nearest(t, True, "target", "state", body=halted)
    halt_after = recording.nearest(t, False, "target", "state", body=halted)
    if state is None and recording.nearest(t, False, "target", "state") is None:
        missing["state"] = {"reason": "no_state"}

    def target(e: Event | None) -> dict | None:
        if e is None:
            return None
        body = e.body if isinstance(e.body, dict) else {}
        return {"t": e.t, "dt_s": seconds(e.t - t),
                "uncertainty_s": None if e.uncertainty is None else seconds(e.uncertainty), **body}

    # --- タスク ---
    task = None
    run = recording.nearest(t, True, "rtos", "run")
    gap = recording.nearest(t, True, "rtos", "gap")
    if run is not None and run.t_end is not None and t < run.t_end:
        task = {"t0": run.t, "t1": run.t_end, "handle": run.a, **(run.body if isinstance(run.body, dict) else {})}
    elif gap is not None and gap.t_end is not None and t < gap.t_end:
        missing["task"] = {"reason": "gap", "t0": gap.t, "t1": gap.t_end,
                           "detail": (gap.body or {}).get("reason") if isinstance(gap.body, dict) else None}
    elif recording.nearest(t, True, "rtos") is None and recording.nearest(t, False, "rtos") is None:
        missing["task"] = {"reason": "no_rtos"}
    else:
        missing["task"] = {"reason": "no_run"}

    pc_before, pc_after = sample(before), sample(after)
    return {
        "t": t, "timebase_hz": hz, "elf": elf if symbols is not None else None,
        "recorded_elf": recording.meta.get("elf"),
        "pc_before": pc_before, "pc_after": pc_after,
        "pc_gap_s": seconds(after.t - before.t) if before is not None and after is not None else None,
        "state": target(state), "halt_before": target(halt_before), "halt_after": target(halt_after),
        "task": task, "missing": missing,
    }


def _place(symbols: Symbols | None, pc: int | None) -> dict:
    """PC の関数・オフセット・ファイル・行と表示用の where（引けない欄は None）。"""
    if symbols is None or pc is None:
        return dict.fromkeys(_PLACE)
    loc = symbols.describe(pc)
    return {"function": loc.function, "offset": loc.offset, "file": loc.file, "line": loc.line, "where": str(loc)}
