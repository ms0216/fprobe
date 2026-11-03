"""プラグイン: Zynq-7000（PYNQ-Z2 の Cortex-A9 × 2）の CTI の番号と番地。

本体（`fprobe.linux_halt`）には CTI のボード固有の値を持たせない。このモジュールがそれを渡すだけ。

    from fprobe.plugins import zynq_cti
    cross = zynq_cti.cross_halt(zynq_cti.ZYNQ7000, io, apb_offset=..., ack_timeout_polls=...)     # io = fprobe.linux_halt.cti.ApbIo(dap, ap)
    cores.use_cross_halt(cross, request_skew_upper_bound_ns=..., event_skew_upper_bound_ns=..., event_trigin=zynq_cti.ZYNQ7000.dbgack_in)

事実の出どころ（PYNQ-Z2 の実機、2026-10-28。Vivado の xsdb 経由。2026-10-29 に OpenOCD 経由でこのモジュールでも止めた。下の段落）:
  * コアごとの CTI が 0xF8898000（コア 0）・0xF8899000（コア 1）。4 チャンネル・8 トリガー。リセット直後は CONTROL = 0、全部ゲート
    開放（GATE = 0xF）、INEN/OUTEN = 0。
  * トリガー出力 0 がコアの外部デバッグ要求（EDBGRQ。MOE = 4）。チャンネルを**立てたまま**（CTIAPPSET）にすると両方が止まる。
    1 クロックの脈（CTIAPPPULSE）は止めない。よって halt_mode は "level"。
  * トリガー入力 0 が「そのコアがデバッグ状態に入った」合図。入力 0 → チャンネル 0 → 出力 0 で、コア 0 が止まるとコア 1 も止まる
    （2026-10-29、OpenOCD 経由、linux-awareness-trial の tools/pynq_trigin_route.py）:
      - コア 0 がブレークポイントで止まったとき 10 回中 10 回、DBGDRCR の停止要求で止めたとき 10 回中 10 回。入力 1 では 10 回中 0 回。
      - 入力 5 では、どちらの止め方でも連動しなかった。10-28 の「入力 5 = DBGACK」は誤りだった。
      - 連動したコア 1 の MOE は 0（4 ではない）。理由は分かっていない。
      - 止まっている間も TRIGINSTATUS に入力 0 は現れない（0x10 のまま）。読み取りでは、この入力を見つけられない。
    入力 4 は常に High（割り当てない）。
  * トリガー出力 1 は再開しない。再開はコアごとの DBGDRCR の再開要求で行う。
  * 止めた 2 コアの時間差の上限は 68 ns（xsdb 経由の測定。docs/fprobe-integration-design.md の 11.10 に相当）。**上限の値はここに持たず、
    呼び出し側が `use_cross_halt` に渡す。**
  * 2026-10-29、このモジュールのコード（OpenOCD 経由）での測定。各コアが Global Timer（162.5 MHz、1 刻み 6.15 ns）を r4 に読み続ける
    方式で、root で実行した（linux-awareness-trial の testapp/lxskew2.c、tools/skew_fine.py）:
      - こちらから止める（チャンネル 0 を立てたまま）: 2 回の測定（40 回ずつ）で、中央値 12 ns、最大 49 ns、上限 74 ns。
        測定の刻み（Global Timer 6.15 ns、1 回の読み出し 18.5 ns）が下限なので、真の差はこれ以下かもしれない。
      - コア 0 がブレークポイントで止まり、入力 0 でコア 1 を止める: コア 1 は毎回 209〜252 ns 遅れた（上限 277 ns）。
        コア 0 を DBGDRCR の要求で止めても同じ（中央値 234 ns）で、ブレークポイントの照合は遅れに寄与しない。遅れは「デバッグ状態に入る →
        DBGACK → CTI → CTM → もう一方の CTI → 停止要求」の直列の連なりの長さ。SLCR の DBG_CLK_CTRL（デバッグ用クロック）を 50〜125 MHz に変えても
        234 ns（209〜252 ns）は変わらず、同時要求の差も変わらなかったので、デバッグ用クロックが原因ではない。どの回路のクロックが決めているかは未特定。
      - 比べると、OpenOCD がコアを 1 つずつ止めた場合は 1.6〜1.9 ms だった。

番地は AHB-AP（xsdb の APU）から見たシステム番地。APB-AP（AP 1）から見た番地は、2026-10-29 に PYNQ 自身の USB-JTAG と
OpenOCD で ROM テーブルを読んで確かめた: CTI は 0x80098000・0x80099000（部品番号 0x906）。つまり apb_offset = 0x80098000 − 0xF8898000。
この経路で、このモジュールと fprobe.linux_halt.cti を使って 2 コアを止めた（43 回中 43 回、両コアとも MOE = 4。記録は
linux-awareness-trial の docs/pynq-fprobe-code-findings.md）。
**未確認**: fprobe（Primer）の PJTAG 経由。同じ DAP なので同じ見え方のはずだが、確かめていないので apb_offset は呼び出し側が渡す（既定なし）。
"""
from dataclasses import dataclass

from ..linux_halt.cti import CrossHalt, Cti

__all__ = ["CtiBoard", "ZYNQ7000", "cross_halt"]


@dataclass(frozen=True)
class CtiBoard:
    name: str
    cti_bases: tuple[int, ...]            # コア番号の順
    halt_out: int                         # コアの停止要求につながるトリガー出力
    restart_out: int
    halt_chan: int                        # 停止要求に使うチャンネル
    restart_chan: int
    dbgack_in: int                        # コアがデバッグ状態に入ったことを CTI に知らせるトリガー入力
    halt_mode: str                        # "pulse" か "level"
    apb_view_offset: int                  # APB-AP から見た番地 = システム番地 + この値（ROM テーブルで確かめた値）

    def apb_offset_for(self, discovery: str) -> int:
        """DAP の見え方に応じた、io から見た番地の足し方。discovery は `ArmACores.discovery`（"apb_rom_table" か "ahb_system_map"）。"""
        if discovery == "apb_rom_table":
            return self.apb_view_offset
        if discovery == "ahb_system_map":
            return 0
        raise ValueError("discovery が分からない: %r" % (discovery,))


ZYNQ7000 = CtiBoard("zynq-7000 (PYNQ-Z2, Cortex-A9 x2)", (0xF8898000, 0xF8899000), halt_out=0, restart_out=1,
                    halt_chan=0, restart_chan=1, dbgack_in=0, halt_mode="level",
                    apb_view_offset=0x80098000 - 0xF8898000)


def cross_halt(board: CtiBoard, io, *, apb_offset: int, ack_timeout_polls: int) -> CrossHalt:
    """board の CTI を io（read32/write32）で動かす CrossHalt。apb_offset: io から見た番地 = システム番地 + apb_offset（必須）。
    ack_timeout_polls: 停止要求の確認応答（INTACK）が消えるのを何回まで見るか（必須。既定なし）。"""
    ctis = [Cti(io, base + apb_offset, "cti%d" % i, halt_out=board.halt_out, restart_out=board.restart_out,
            halt_chan=board.halt_chan, restart_chan=board.restart_chan)
            for i, base in enumerate(board.cti_bases)]
    return CrossHalt(ctis, halt_mode=board.halt_mode, ack_timeout_polls=ack_timeout_polls)
