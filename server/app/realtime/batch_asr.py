"""通話後の一括文字起こし。

ストリーミング（asr.py）とは要求が違うので分けてある。

    ストリーミング … 低遅延が最優先。partial を出す。精度は妥協する
    バッチ         … 精度が最優先。遅くてよい。話者は録音のチャンネルで確定

★ 話者はチャンネルで決まっているので、ASR に話者分離をさせない。
  推測させると、担当者と相手が入れ替わった文字起こしが混ざる。
"""

from __future__ import annotations

from dataclasses import dataclass

from ..config import ASR_API_KEY, ASR_LANGUAGE, ASR_PROVIDER
from ..domain.wav import AudioChannel
from ..logger import logger


@dataclass(frozen=True)
class BatchSegment:
    track: str
    started_ms: int
    ended_ms: int
    text: str
    confidence: float | None = None


async def transcribe_channel(channel: AudioChannel) -> list[BatchSegment]:
    """1 話者分の音声を文字起こしする。

    ASR_PROVIDER=null のときは空を返す。文字起こしが無くても録音と
    通話記録は成立する、という優先順位を守るため。
    """
    if ASR_PROVIDER == "null" or not ASR_API_KEY:
        return []
    if ASR_PROVIDER == "deepgram":
        return await _deepgram(channel)

    logger.warn("未実装の ASR プロバイダのため文字起こしを省略します", provider=ASR_PROVIDER)
    return []


async def _deepgram(channel: AudioChannel) -> list[BatchSegment]:
    import httpx

    params = {
        "language": ASR_LANGUAGE,
        "punctuate": "true",
        # 単語ごとのタイムスタンプが要る。会話メトリクスの区間計算に使う
        "utterances": "true",
        "model": "nova-2",
    }

    async with httpx.AsyncClient(timeout=300.0) as client:
        res = await client.post(
            "https://api.deepgram.com/v1/listen",
            params=params,
            headers={
                "Authorization": f"Token {ASR_API_KEY}",
                "Content-Type": "audio/wav",
            },
            content=channel.to_wav(),
        )
        res.raise_for_status()
        body = res.json()

    utterances = body.get("results", {}).get("utterances") or []
    if utterances:
        return [
            BatchSegment(
                track=channel.track,
                started_ms=int(float(u["start"]) * 1000),
                ended_ms=int(float(u["end"]) * 1000),
                text=u["transcript"],
                confidence=u.get("confidence"),
            )
            for u in utterances
            if u.get("transcript")
        ]

    # utterances が無い場合は 1 本のテキストとして返る。区間が取れないので
    # 会話メトリクスは出せないが、文字起こしは残す
    alt = (
        body.get("results", {})
        .get("channels", [{}])[0]
        .get("alternatives", [{}])[0]
    )
    text = alt.get("transcript", "")
    if not text:
        return []
    return [
        BatchSegment(
            track=channel.track,
            started_ms=0,
            ended_ms=channel.duration_ms,
            text=text,
            confidence=alt.get("confidence"),
        )
    ]
