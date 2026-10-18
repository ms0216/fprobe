"""命令トレース（ETM）の解読。層ごとに分ける（docs/reports/2026-10-19-etm-investigation.md §3.1）:

    バイト列（トレース端子・トレース FIFO・ファイル）
      → tpiu        TPIU のフォーマッタの枠を外す（16 バイトのフレーム → トレース ID ごとのバイト列）
      → etmv4 等    パケットの解読 → 共通の要素（elements: アドレス・アトム・例外・時刻…）
      → reconstruct プログラムの像（ELF か実機のメモリ）と合わせて、実行した命令の流れを再構成
      → 応用        命令の一覧・事後の命令履歴（fprobe.probe_etm の trace.etm.*）

プロトコルは `decoder_for(name)` で選ぶ。再構成（reconstruct）は共通の要素だけを見るので、ETMv3.5 の解読器を
足しても同じ口に載る。CoreSight の部品（ETMv4・ATB ファンネル・TPIU）の設定は coresight。
"""

from .elements import Element
from .etmv4 import Etmv4Config, Etmv4Decoder

__all__ = ["PROTOCOLS", "Element", "Etmv4Config", "Etmv4Decoder", "decoder_for"]

PROTOCOLS = ("etmv4",)


def decoder_for(protocol: str, config: Etmv4Config | None = None):
    """プロトコルの名前から解読器（decode(bytes) -> list[Element] を持つもの）。"""
    if protocol == "etmv4":
        return Etmv4Decoder(config or Etmv4Config())
    if protocol in ("etmv3", "etmv3.5"):
        # 共通の要素（elements）に落とす形で足す（I-sync → Address、P-header → Atoms、分岐アドレスの例外 →
        # ExceptionEntry）。Cortex-M3/M4 の実機（STM32F4 ディスカバリ等）で確かめる段で実装する
        raise NotImplementedError("ETMv3.5 の解読はまだ無い（ETMv4 のみ）")
    raise ValueError(f"知らないプロトコル: {protocol!r}（{', '.join(PROTOCOLS)}）")
