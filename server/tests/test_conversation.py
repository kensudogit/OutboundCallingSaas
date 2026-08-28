"""会話メトリクスの計算。

★ この数字は担当者の評価に使われる。計算が曖昧なまま運用に乗せない。
  特にトーク比率は「一方的に喋っている」の判定に直結するので、
  同時発話や重複セグメントの扱いを境界値で固定しておく。
"""

from __future__ import annotations

from app.domain.conversation import Segment, compute

A = "outbound"  # 担当者
C = "inbound"   # 相手


def test_基本的な会話():
    metrics = compute([
        Segment(A, 0, 3000),        # 担当者 3 秒
        Segment(C, 3200, 8200),     # 相手 5 秒
        Segment(A, 8500, 10500),    # 担当者 2 秒
    ], call_duration_ms=11000)

    assert metrics.agent_talk_ms == 5000
    assert metrics.contact_talk_ms == 5000
    assert metrics.overlap_ms == 0
    assert metrics.agent_turns == 2
    assert metrics.talk_ratio == 0.5


def test_トーク比率():
    """★ 経験則として 40-50% が良く、70% を超えると一方的。"""
    metrics = compute([Segment(A, 0, 7000), Segment(C, 7000, 10000)])
    assert metrics.talk_ratio == 0.7


def test_誰も話していなければ比率は求められない():
    """0 除算で落とさない。分母 0 を 0% と表示するか「—」にするかは画面の判断。"""
    assert compute([]).talk_ratio is None
    assert compute([Segment(A, 100, 100)]).talk_ratio is None


def test_同時発話は被りとして数える():
    """相手の話を遮った回数の代理指標。多いと印象が悪い。"""
    metrics = compute([
        Segment(C, 0, 5000),
        Segment(A, 3000, 6000),   # 2 秒重なる
    ])
    assert metrics.overlap_ms == 2000


def test_同じ話者の重なるセグメントを二重に数えない():
    """★ ASR のセグメント境界で、同じ話者の発話が重なって報告されることがある。
    素朴に合計すると同じ時間を 2 回数え、トーク比率が壊れる。
    """
    metrics = compute([
        Segment(A, 0, 3000),
        Segment(A, 2000, 5000),   # 1 秒重なっている
    ])
    assert metrics.agent_talk_ms == 5000   # 3000 + 3000 = 6000 ではない


def test_最長連続発話は間を空けない塊の最大():
    """90 秒を超える独白は「相手を置き去りにした説明」の代理指標。"""
    metrics = compute([
        Segment(A, 0, 20_000),
        Segment(A, 20_000, 115_000),   # 連続しているので 1 つの塊
        Segment(C, 116_000, 117_000),
        Segment(A, 118_000, 120_000),
    ])
    assert metrics.longest_monologue_ms == 115_000


def test_間が空けば別の塊になる():
    metrics = compute([
        Segment(A, 0, 5000),
        Segment(A, 9000, 12_000),   # 4 秒空いている
    ])
    assert metrics.longest_monologue_ms == 5000


def test_ターン数は連続した発話を1つと数える():
    metrics = compute([
        Segment(A, 0, 1000),
        Segment(A, 1000, 2000),    # 続き。ターンは増えない
        Segment(C, 2000, 3000),
        Segment(A, 3000, 4000),    # 2 ターン目
    ])
    assert metrics.agent_turns == 2


def test_沈黙は通話時間から発話の和集合を引く():
    metrics = compute(
        [Segment(A, 0, 2000), Segment(C, 5000, 7000)],
        call_duration_ms=10_000,
    )
    # 10 秒のうち話していたのは 4 秒
    assert metrics.silence_ms == 6000


def test_通話時間を渡さなければ前後の無音は数えない():
    metrics = compute([Segment(A, 10_000, 12_000), Segment(C, 15_000, 16_000)])
    # 10 秒〜16 秒の 6 秒間のうち、話していた 3 秒を引いた 3 秒
    assert metrics.silence_ms == 3000


def test_同時発話があっても沈黙は二重に引かれない():
    metrics = compute(
        [Segment(A, 0, 5000), Segment(C, 3000, 8000)],
        call_duration_ms=10_000,
    )
    # 和集合は 0-8000 の 8 秒。沈黙は 2 秒
    assert metrics.silence_ms == 2000
    assert metrics.overlap_ms == 2000


def test_相手が最初に話し始めるまでの時間():
    """応答直後の沈黙が長いのは、不審に思われている合図。"""
    metrics = compute([Segment(A, 0, 3000), Segment(C, 8000, 10_000)])
    assert metrics.first_response_ms == 8000


def test_相手が一度も話さなければNone():
    """留守電に吹き込んだ通話など。0 と区別できるようにする。"""
    metrics = compute([Segment(A, 0, 20_000)])
    assert metrics.first_response_ms is None
    assert metrics.contact_talk_ms == 0
    assert metrics.talk_ratio == 1.0


def test_長さゼロのセグメントは無視する():
    metrics = compute([Segment(A, 1000, 1000), Segment(C, 2000, 4000)])
    assert metrics.agent_talk_ms == 0
    assert metrics.agent_turns == 0


def test_順序が入れ替わって渡されても結果は同じ():
    """DB から order by 無しで来ても壊れないこと。"""
    segments = [Segment(C, 3200, 8200), Segment(A, 0, 3000), Segment(A, 8500, 10_500)]
    assert compute(segments) == compute(sorted(segments, key=lambda s: s.started_ms))
