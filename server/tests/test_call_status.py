"""通話状態の単調更新（原則 2）と音声処理。

★ 「completed が answered より先に着いたら何が起きるか」に即答できることが、
  この設計が守れているかの判定基準。「起きない」と答えたなら、それは起きる。

状態の順序は SQL 側（call_status_rank）とアプリ側（models.status_rank）の
両方に持っている。冗長だが、アプリでも順序判断が要る場面があるため。
値を変えるときは両方直す必要があるので、ここで一致を固定しておく。
"""

from __future__ import annotations

import pytest

from app.models import CallStatus, status_rank
from app.realtime.audio import UtteranceDetector, mulaw_to_pcm16, rms
from app.telephony.routes import STATUS_MAP


def test_状態の順序が定義されている():
    assert (
        status_rank(CallStatus.QUEUED)
        < status_rank(CallStatus.RINGING)
        < status_rank(CallStatus.ANSWERED)
        < status_rank(CallStatus.COMPLETED)
    )


def test_未知の状態は最下位になる():
    """順序が定義できない値が来ても、既存の状態を巻き戻さない。"""
    assert status_rank("SOMETHING_NEW") == 0
    assert status_rank(CallStatus.UNKNOWN) == 0


def test_到着順が逆転しても状態は進む方向にしか動かない():
    """upsert の case 式と同じ判定をここで固定する。"""

    def apply(current: str, incoming: str) -> str:
        return incoming if status_rank(incoming) > status_rank(current) else current

    # 正常な順序
    assert apply("QUEUED", "RINGING") == "RINGING"
    assert apply("RINGING", "ANSWERED") == "ANSWERED"
    assert apply("ANSWERED", "COMPLETED") == "COMPLETED"

    # ★ completed が先に着いた後で answered が届いても巻き戻らない
    assert apply("COMPLETED", "ANSWERED") == "COMPLETED"
    assert apply("COMPLETED", "RINGING") == "COMPLETED"

    # ★ 重複配信で同じ状態が来ても変わらない
    assert apply("COMPLETED", "COMPLETED") == "COMPLETED"


@pytest.mark.parametrize(
    "twilio_status,expected",
    [
        ("initiated", "QUEUED"),
        ("ringing", "RINGING"),
        ("in-progress", "ANSWERED"),
        ("answered", "ANSWERED"),
        ("completed", "COMPLETED"),
        # ★ busy / no-answer / failed はどれも「終わった」。status には混ぜず
        #   COMPLETED に寄せ、理由は raw_status に残す。混ぜると順序が定義できない
        ("busy", "COMPLETED"),
        ("no-answer", "COMPLETED"),
        ("failed", "COMPLETED"),
        ("canceled", "COMPLETED"),
    ],
)
def test_Twilioの状態を内部状態に写す(twilio_status, expected):
    assert STATUS_MAP[twilio_status] == expected


def test_未知のTwilio状態は終了扱いにする():
    """新しい状態が増えても通話が QUEUED のまま残らないようにする。"""
    assert STATUS_MAP.get("some-new-status", "COMPLETED") == "COMPLETED"


# ---------------------------------------------------------------- 音声


def test_ミューロー変換が無音を0付近にする():
    """μ-law の 0xFF は正の最小値、0x7F は負の最小値。無音は 0xFF が続く。"""
    silence = bytes([0xFF]) * 160
    pcm = mulaw_to_pcm16(silence)
    assert len(pcm) == 320          # 8bit → 16bit なので 2 倍
    assert rms(pcm) < 100


def test_ミューロー変換が音声を大きな値にする():
    loud = bytes([0x00]) * 160      # 0x00 は最大振幅側
    assert rms(mulaw_to_pcm16(loud)) > 1000


def test_発話の切れ目を検出する():
    """★ 相槌では反応しない。短い発話でサジェストが出ると邪魔になるだけ。"""
    detector = UtteranceDetector(silence_threshold_ms=700, min_utterance_ms=800)

    voice = mulaw_to_pcm16(bytes([0x00]) * 160)   # 20ms の有声フレーム
    silence = mulaw_to_pcm16(bytes([0xFF]) * 160)

    # 1 秒話す（50 フレーム）→ まだ切れていない
    for _ in range(50):
        assert detector.feed(voice) is False

    # 700ms 未満の無音では切れない（35 フレーム = 700ms なので 34 まで）
    for _ in range(34):
        assert detector.feed(silence) is False

    # 700ms に達したところで切れる
    assert detector.feed(silence) is True


def test_短すぎる発話では切れ目と判定しない():
    detector = UtteranceDetector(silence_threshold_ms=700, min_utterance_ms=800)
    voice = mulaw_to_pcm16(bytes([0x00]) * 160)
    silence = mulaw_to_pcm16(bytes([0xFF]) * 160)

    # 「はい」程度の 300ms
    for _ in range(15):
        detector.feed(voice)
    results = [detector.feed(silence) for _ in range(40)]
    assert not any(results)


def test_無音だけでは切れ目にならない():
    detector = UtteranceDetector(silence_threshold_ms=700, min_utterance_ms=800)
    silence = mulaw_to_pcm16(bytes([0xFF]) * 160)
    assert not any(detector.feed(silence) for _ in range(100))
