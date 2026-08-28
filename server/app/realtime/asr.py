"""ストリーミング ASR の抽象。

★ ASR は乗り換える前提の部品。精度も価格も動くので、実装を差し替えられる
  形にしておく。ここが具体的なプロバイダに直結していると、乗り換えのたびに
  media ワーカー全体を書き直すことになる。

★ partial（暫定）と final（確定）を区別する。partial は画面に薄く出して
  随時書き換える。DB に保存するのは final だけ——partial を保存すると
  1 発話あたり数十行が積み上がる。

既定は NullASR。ASR_PROVIDER=null で、文字起こしなしでも通話とダイヤラーの
検証ができる状態にしてある（AI が止まっても電話はかけられる、が正しい壊れ方）。
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from dataclasses import dataclass
from typing import Protocol

from ..config import ASR_API_KEY, ASR_LANGUAGE, ASR_PROVIDER, ASR_SAMPLE_RATE
from ..logger import logger


@dataclass(frozen=True)
class ASRResult:
    text: str
    is_final: bool
    started_ms: int
    ended_ms: int
    confidence: float | None = None


class StreamingASR(Protocol):
    async def open(self) -> None: ...
    async def feed(self, pcm: bytes, timestamp_ms: int) -> None: ...
    def results(self) -> AsyncIterator[ASRResult]: ...
    async def close(self) -> None: ...


class NullASR:
    """何もしない実装。ASR を用意せずに全体を動かすための既定。"""

    def __init__(self, **_: object) -> None:
        self._queue: asyncio.Queue[ASRResult | None] = asyncio.Queue()

    async def open(self) -> None:
        return None

    async def feed(self, pcm: bytes, timestamp_ms: int) -> None:
        return None

    async def results(self) -> AsyncIterator[ASRResult]:
        while True:
            item = await self._queue.get()
            if item is None:
                return
            yield item

    async def close(self) -> None:
        await self._queue.put(None)


class DeepgramASR:
    """Deepgram のストリーミング実装。

    実運用でリアルタイム支援をやるならクラウドのストリーミング ASR を選ぶ。
    Whisper は区間を切って投げる形になるため、区切り位置で単語が切れて
    精度が落ち、遅延も読めない。
    """

    def __init__(self, *, api_key: str, language: str, sample_rate: int) -> None:
        self._api_key = api_key
        self._language = language
        self._sample_rate = sample_rate
        self._ws = None
        self._queue: asyncio.Queue[ASRResult | None] = asyncio.Queue()
        self._reader: asyncio.Task | None = None

    def _url(self) -> str:
        return (
            "wss://api.deepgram.com/v1/listen"
            f"?encoding=linear16&sample_rate={self._sample_rate}&channels=1"
            f"&language={self._language}&interim_results=true&punctuate=true"
        )

    async def open(self) -> None:
        import websockets

        self._ws = await websockets.connect(
            self._url(), additional_headers={"Authorization": f"Token {self._api_key}"}
        )
        self._reader = asyncio.create_task(self._read_loop())

    async def _read_loop(self) -> None:
        import json

        try:
            async for raw in self._ws:  # type: ignore[union-attr]
                msg = json.loads(raw)
                alt = (msg.get("channel") or {}).get("alternatives", [{}])[0]
                text = alt.get("transcript", "")
                if not text:
                    continue
                start = float(msg.get("start", 0.0))
                duration = float(msg.get("duration", 0.0))
                await self._queue.put(
                    ASRResult(
                        text=text,
                        is_final=bool(msg.get("is_final")),
                        started_ms=int(start * 1000),
                        ended_ms=int((start + duration) * 1000),
                        confidence=alt.get("confidence"),
                    )
                )
        except Exception as exc:  # noqa: BLE001
            # ★ ASR が落ちても通話は続く。ここで例外を伝播させて
            #   Media Stream ごと落とさない
            logger.error("ASR の受信ループが終了しました", err=str(exc))
        finally:
            await self._queue.put(None)

    async def feed(self, pcm: bytes, timestamp_ms: int) -> None:
        if self._ws is not None:
            await self._ws.send(pcm)

    async def results(self) -> AsyncIterator[ASRResult]:
        while True:
            item = await self._queue.get()
            if item is None:
                return
            yield item

    async def close(self) -> None:
        if self._ws is not None:
            try:
                await self._ws.close()
            finally:
                self._ws = None
        if self._reader is not None:
            self._reader.cancel()
            self._reader = None


def create_asr() -> StreamingASR:
    """設定からプロバイダを選ぶ。ここが唯一の分岐点。"""
    if ASR_PROVIDER == "deepgram":
        return DeepgramASR(
            api_key=ASR_API_KEY, language=ASR_LANGUAGE, sample_rate=ASR_SAMPLE_RATE
        )
    if ASR_PROVIDER != "null":
        logger.warn(
            "未実装の ASR プロバイダのため文字起こしを無効にします",
            provider=ASR_PROVIDER,
        )
    return NullASR()
