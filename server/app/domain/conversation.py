"""会話の定量化。

★ 純粋関数として書く。DB にも音声にも依存しないので、境界値（同時発話、
  ゼロ長、片方だけ喋る）をテストで固定できる。会話メトリクスは担当者の
  評価に使われる数字なので、計算が曖昧なまま運用に乗せない。

★ 何を測るかの選択そのものが設計。トーク比率と最長独白は「相手の話を
  聞けているか」を見るためのもので、離席時間や打鍵数のような監視目的の
  指標は入れていない。同じデータでも、育成に使うか監視に使うかで
  現場での意味が変わる。
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class Segment:
    """発話区間。文字起こしの 1 セグメントに対応する。"""

    track: str        # outbound（担当者）/ inbound（相手）
    started_ms: int
    ended_ms: int

    @property
    def duration_ms(self) -> int:
        return max(0, self.ended_ms - self.started_ms)


@dataclass(frozen=True)
class ConversationMetrics:
    agent_talk_ms: int
    contact_talk_ms: int
    silence_ms: int
    overlap_ms: int
    agent_turns: int
    longest_monologue_ms: int
    first_response_ms: int | None

    @property
    def talk_ratio(self) -> float | None:
        """担当者が話した割合。

        経験則として、成果の出る架電は 40–50% 程度。70% を超えると
        一方的に喋っている。これを本人に見せるのが育成、並べて管理者に
        見せるのが評価——前者を既定にする。
        """
        total = self.agent_talk_ms + self.contact_talk_ms
        return self.agent_talk_ms / total if total else None


def _merge(intervals: list[tuple[int, int]]) -> list[tuple[int, int]]:
    """重なる区間をまとめる。

    同じ話者の連続した発話が重なって報告されることがある（ASR の
    セグメント境界）。素朴に合計すると同じ時間を二重に数える。
    """
    if not intervals:
        return []
    ordered = sorted(intervals)
    merged = [ordered[0]]
    for start, end in ordered[1:]:
        last_start, last_end = merged[-1]
        if start <= last_end:
            merged[-1] = (last_start, max(last_end, end))
        else:
            merged.append((start, end))
    return merged


def _total(intervals: list[tuple[int, int]]) -> int:
    return sum(end - start for start, end in intervals)


def _intersect(a: list[tuple[int, int]], b: list[tuple[int, int]]) -> int:
    """2 つの区間集合の重なりの合計。被り（同時発話）の計算に使う。"""
    total = 0
    i = j = 0
    while i < len(a) and j < len(b):
        start = max(a[i][0], b[j][0])
        end = min(a[i][1], b[j][1])
        if start < end:
            total += end - start
        if a[i][1] < b[j][1]:
            i += 1
        else:
            j += 1
    return total


def compute(segments: list[Segment], *, call_duration_ms: int | None = None) -> ConversationMetrics:
    """文字起こしのセグメントから会話メトリクスを出す。

    call_duration_ms を渡すと、その長さを基準に沈黙を計算する。渡さない
    場合は最初の発話から最後の発話までを対象にする（前後の無音は数えない）。
    """
    agent = _merge(
        [(s.started_ms, s.ended_ms) for s in segments if s.track == "outbound" and s.duration_ms]
    )
    contact = _merge(
        [(s.started_ms, s.ended_ms) for s in segments if s.track == "inbound" and s.duration_ms]
    )

    agent_talk = _total(agent)
    contact_talk = _total(contact)
    overlap = _intersect(agent, contact)

    # どちらかが話していた時間の和集合
    voiced = _total(_merge(agent + contact))

    if call_duration_ms is not None:
        span = max(0, call_duration_ms)
    elif agent or contact:
        starts = [i[0] for i in agent + contact]
        ends = [i[1] for i in agent + contact]
        span = max(ends) - min(starts)
    else:
        span = 0

    silence = max(0, span - voiced)

    # ★ ターン数は「相手の発話を挟んで担当者が話し始めた回数」。
    #   連続した担当者の発話を 1 ターンと数える
    turns = 0
    previous_track: str | None = None
    for segment in sorted(segments, key=lambda s: s.started_ms):
        if not segment.duration_ms:
            continue
        if segment.track == "outbound" and previous_track != "outbound":
            turns += 1
        previous_track = segment.track

    # ★ 最長連続発話。90 秒を超える独白は「相手を置き去りにした説明」の
    #   代理指標になる。merge 済みなので、間を空けずに続いた塊の最大値
    longest = max((end - start for start, end in agent), default=0)

    # 相手が最初に話し始めるまで。応答直後の沈黙が長いのは不審に思われている合図
    first_response = min((start for start, _ in contact), default=None)

    return ConversationMetrics(
        agent_talk_ms=agent_talk,
        contact_talk_ms=contact_talk,
        silence_ms=silence,
        overlap_ms=overlap,
        agent_turns=turns,
        longest_monologue_ms=longest,
        first_response_ms=first_response,
    )
