"""音声フォーマットの変換と、発話の切れ目の判定。

Twilio が送るのは μ-law（G.711u）8kHz モノラル。ASR は PCM16 を要求する
ことが多いので変換する。

★ サンプリングレートは上げない。8kHz を 16kHz にしても情報は増えない。
  ASR 側が 8kHz を受け付けるならそのまま渡すのが速く、精度も変わらない。

★ audioop は Python 3.13 で標準ライブラリから削除された。ここでは変換表を
  自前で持つことで、Python のバージョンに依存しないようにしている
  （3.13 に上げた日に文字起こしだけが静かに死ぬ、を避ける）。
"""

from __future__ import annotations

import struct
from dataclasses import dataclass, field

# μ-law → PCM16 の変換表。256 通りしかないので起動時に作って引くだけにする
_BIAS = 0x84
_CLIP = 32635


def _decode_sample(mu: int) -> int:
    mu = ~mu & 0xFF
    sign = mu & 0x80
    exponent = (mu >> 4) & 0x07
    mantissa = mu & 0x0F
    sample = ((mantissa << 3) + _BIAS) << exponent
    sample -= _BIAS
    return -sample if sign else sample


_MULAW_TABLE: tuple[int, ...] = tuple(_decode_sample(i) for i in range(256))


def mulaw_to_pcm16(chunk: bytes) -> bytes:
    """μ-law 8kHz → PCM16 リトルエンディアン 8kHz。"""
    return struct.pack(f"<{len(chunk)}h", *(_MULAW_TABLE[b] for b in chunk))


def rms(pcm: bytes) -> float:
    """音量の代理指標。無音検出に使う。"""
    if not pcm:
        return 0.0
    samples = struct.unpack(f"<{len(pcm) // 2}h", pcm[: len(pcm) // 2 * 2])
    return (sum(s * s for s in samples) / len(samples)) ** 0.5


# 8kHz PCM16 での経験的な閾値。環境ノイズが多い現場では上げる
VOICE_RMS_THRESHOLD = 500.0


@dataclass
class UtteranceDetector:
    """発話の切れ目を検出する。

    ★ ASR の final だけに頼ると、プロバイダによっては数秒待たされる。
      無音を自前で見ると反応が安定する。

    ★ 相手（inbound）の発話にだけ反応する。担当者が話している間に新しい提案が
      出てくると読めず、邪魔になるだけ。
    """

    silence_threshold_ms: int
    min_utterance_ms: int
    frame_ms: int = 20

    _voiced_ms: int = field(default=0, init=False)
    _silence_ms: int = field(default=0, init=False)
    _in_speech: bool = field(default=False, init=False)

    def feed(self, pcm: bytes) -> bool:
        """1 フレーム分を渡す。発話が終わったと判断したら True。"""
        is_voice = rms(pcm) >= VOICE_RMS_THRESHOLD

        if is_voice:
            self._voiced_ms += self.frame_ms
            self._silence_ms = 0
            self._in_speech = True
            return False

        if not self._in_speech:
            return False

        self._silence_ms += self.frame_ms
        if self._silence_ms < self.silence_threshold_ms:
            return False

        # 発話が切れた。短すぎる相槌では反応しない
        ended = self._voiced_ms >= self.min_utterance_ms
        self._reset()
        return ended

    def _reset(self) -> None:
        self._voiced_ms = 0
        self._silence_ms = 0
        self._in_speech = False
