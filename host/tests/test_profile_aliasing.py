"""PC サンプリングのエイリアシングの検出: 例外トレースで測った本当の負荷と、サンプルの割合（信頼区間）が食い違えば
知らせる。数字は 2026-10-18 の実機（rtt_demo、SysTick 1 kHz・17 サイクル、サンプルの間隔 2048 サイクル）。"""

from fprobe.profile.aliasing import aliasing_notes

SYSTICK = {"exception": 15, "name": "SysTick", "load": 0.00106}


def test_a_handler_seen_far_more_often_than_its_load_is_reported():
    rows = [{"exception": 15, "name": "SysTick", "samples": 63, "fraction": 0.00804, "ci": [0.0063, 0.0103]}]
    notes = aliasing_notes(rows, [SYSTICK], total=7834)
    # 原因（周期の噛み合い）は断定しない。例外トレースの負荷が、サンプルの信頼区間の外にある事実だけ
    assert len(notes) == 1 and "SysTick" in notes[0] and "信頼区間" in notes[0] and notes[0].endswith("の外") and "エイリアシング" not in notes[0]


def test_a_handler_never_sampled_despite_its_load_is_reported():
    # 0 回のときも同じ基準（0 / 7834 の信頼区間の上端 0.05 % < 0.106 %）。「期待 4.6 回以上」の経験則は使わない
    assert len(aliasing_notes([], [SYSTICK], total=7834)) == 1


def test_agreement_is_quiet():
    rows = [{"exception": 15, "name": "SysTick", "samples": 9, "fraction": 0.00115, "ci": [0.0006, 0.0022]}]
    assert aliasing_notes(rows, [SYSTICK], total=7834) == []
    assert aliasing_notes([], [SYSTICK], total=500) == []                          # 0 / 500 の区間の上端 0.76 % の中
    assert len(aliasing_notes([], [SYSTICK], total=500, confidence=0.5)) == 1     # 信頼度は使う側が渡す
