"""PC サンプリングのエイリアシングの検出: 例外トレースで測った本当の負荷と、サンプルの割合（信頼区間）が食い違えば
知らせる。数字は 2026-10-18 の実機（rtt_demo、SysTick 1 kHz・17 サイクル、サンプルの間隔 2048 サイクル）。"""

from fprobe.profile.aliasing import aliasing_notes

SYSTICK = {"exception": 15, "name": "SysTick", "load": 0.00106}


def test_a_handler_seen_far_more_often_than_its_load_is_reported():
    rows = [{"exception": 15, "name": "SysTick", "samples": 63, "fraction": 0.00804, "ci": [0.0063, 0.0103]}]
    notes = aliasing_notes(rows, [SYSTICK], total=7834)
    assert len(notes) == 1 and "SysTick" in notes[0] and "エイリアシング" in notes[0]


def test_a_handler_never_sampled_despite_its_load_is_reported():
    assert len(aliasing_notes([], [SYSTICK], total=7834)) == 1                    # 期待 8 回 → 0 回


def test_agreement_is_quiet():
    rows = [{"exception": 15, "name": "SysTick", "samples": 9, "fraction": 0.00115, "ci": [0.0006, 0.0022]}]
    assert aliasing_notes(rows, [SYSTICK], total=7834) == []
    assert aliasing_notes([], [SYSTICK], total=500) == []                          # 期待 0.5 回: 0 回でも自然
