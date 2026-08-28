"""WAV の読み取りとチャンネル分離。

★ 録音は `record-from-answer-dual` で取っている。デュアルチャンネルなので
  **左が担当者・右が相手**と決まっている。モノラルなら話者分離を推測に
  頼ることになるが、ここでは物理的に分かれているので確実。
  トーク比率や被り回数（会話メトリクス）が正確に出せるのはこのおかげ。

★ 標準ライブラリの wave だけで扱う。音声処理ライブラリを入れると
  ネイティブ依存が増え、コンテナのビルドが重くなる。やりたいのは
  「2ch を 1ch ずつに分ける」だけなので、それには足りている。
"""

from __future__ import annotations

import io
import struct
import wave
from dataclasses import dataclass


@dataclass(frozen=True)
class AudioChannel:
    """1 話者分の音声。"""

    track: str          # outbound（担当者）/ inbound（相手）
    pcm: bytes          # PCM16 リトルエンディアン
    sample_rate: int

    @property
    def duration_ms(self) -> int:
        return int(len(self.pcm) / 2 / self.sample_rate * 1000)

    def to_wav(self) -> bytes:
        """ASR に渡せる WAV に戻す。"""
        buffer = io.BytesIO()
        with wave.open(buffer, "wb") as out:
            out.setnchannels(1)
            out.setsampwidth(2)
            out.setframerate(self.sample_rate)
            out.writeframes(self.pcm)
        return buffer.getvalue()


class UnsupportedAudio(Exception):
    """扱えない録音。処理をスキップして記録に残す。"""


def split_channels(wav_bytes: bytes) -> list[AudioChannel]:
    """デュアルチャンネルの録音を話者ごとに分ける。

    モノラルの場合は 1 本だけ返す（track は unknown）。話者が分からないので
    会話メトリクスは計算できないが、文字起こしはできる——できることは
    やる、という方針。
    """
    try:
        with wave.open(io.BytesIO(wav_bytes), "rb") as src:
            channels = src.getnchannels()
            width = src.getsampwidth()
            rate = src.getframerate()
            frames = src.readframes(src.getnframes())
    except (wave.Error, EOFError) as exc:
        raise UnsupportedAudio(f"WAV として読めません: {exc}") from exc

    if width != 2:
        # Twilio の録音は 16bit PCM。それ以外が来たら想定外なので明示的に落とす
        raise UnsupportedAudio(f"16bit PCM ではありません（{width * 8}bit）")

    if channels == 1:
        return [AudioChannel(track="unknown", pcm=frames, sample_rate=rate)]
    if channels != 2:
        raise UnsupportedAudio(f"対応していないチャンネル数: {channels}")

    left, right = _deinterleave(frames)
    # ★ Twilio の dual channel は左が発信側（担当者）、右が着信側（相手）
    return [
        AudioChannel(track="outbound", pcm=left, sample_rate=rate),
        AudioChannel(track="inbound", pcm=right, sample_rate=rate),
    ]


def _deinterleave(frames: bytes) -> tuple[bytes, bytes]:
    """L R L R ... の並びを L だけ / R だけに分ける。"""
    count = len(frames) // 4          # 1 フレーム = 2ch * 2byte
    samples = struct.unpack(f"<{count * 2}h", frames[: count * 4])
    left = struct.pack(f"<{count}h", *samples[0::2])
    right = struct.pack(f"<{count}h", *samples[1::2])
    return left, right


def build_dual_wav(left: bytes, right: bytes, sample_rate: int = 8000) -> bytes:
    """テスト用。2 本のモノラル PCM からデュアルチャンネル WAV を作る。

    録音まわりを実 Twilio なしで検証するために要る。
    """
    count = min(len(left), len(right)) // 2
    l_samples = struct.unpack(f"<{count}h", left[: count * 2])
    r_samples = struct.unpack(f"<{count}h", right[: count * 2])
    interleaved = struct.pack(
        f"<{count * 2}h",
        *[s for pair in zip(l_samples, r_samples, strict=True) for s in pair],
    )

    buffer = io.BytesIO()
    with wave.open(buffer, "wb") as out:
        out.setnchannels(2)
        out.setsampwidth(2)
        out.setframerate(sample_rate)
        out.writeframes(interleaved)
    return buffer.getvalue()
