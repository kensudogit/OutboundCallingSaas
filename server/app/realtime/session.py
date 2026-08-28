"""1 通話分の文字起こしセッション。

★ 話者ごとに別の ASR ストリームへ流す。1 本にミックスしてから投げると、
  話者分離を推測に頼ることになる。track="both_tracks" で inbound（相手）と
  outbound（担当者）が別々に届くので、そのまま分けて扱う。

★ 文字起こしとサジェストは別タスクとして並行に走らせる。要求が違うため——
  文字起こしは常時・低遅延（合計 400〜900ms）、サジェストは断続的で
  多少遅くてよい（1.5〜2.5s）。同じパイプラインに載せると、LLM の遅さが
  文字起こしを引きずる。
"""

from __future__ import annotations

import asyncio
import json
from dataclasses import dataclass, field

from ..config import MIN_UTTERANCE_MS, REDIS_URL, SILENCE_THRESHOLD_MS
from ..logger import logger
from .asr import ASRResult, create_asr
from .audio import UtteranceDetector, mulaw_to_pcm16


@dataclass
class TranscriptionSession:
    call_id: str

    _asr: dict[str, object] = field(default_factory=dict, init=False)
    _pumps: list[asyncio.Task] = field(default_factory=list, init=False)
    _detector: UtteranceDetector = field(init=False)
    _redis: object | None = field(default=None, init=False)
    _provider_call_sid: str | None = field(default=None, init=False)
    _last_utterance: str = field(default="", init=False)

    def __post_init__(self) -> None:
        self._detector = UtteranceDetector(
            silence_threshold_ms=SILENCE_THRESHOLD_MS, min_utterance_ms=MIN_UTTERANCE_MS
        )

    # ------------------------------------------------------------------ 開始

    async def open(self, *, provider_call_sid: str, stream_sid: str) -> None:
        self._provider_call_sid = provider_call_sid

        # 3 経路のうちの Media Stream 経路を DB に記録する（原則 2）
        from ..db.engine import admin_tx
        from ..repositories import calls as calls_repo

        async with admin_tx() as conn:
            await calls_repo.attach_stream(
                conn, provider_call_sid=provider_call_sid, stream_sid=stream_sid
            )

        self._redis = await self._connect_redis()

        # 話者ごとに 1 本ずつ
        for track in ("inbound", "outbound"):
            asr = create_asr()
            await asr.open()
            self._asr[track] = asr
            self._pumps.append(asyncio.create_task(self._pump(track, asr)))

        logger.info("文字起こしを開始しました", call_id=self.call_id, stream_sid=stream_sid)

    async def _connect_redis(self):
        try:
            import redis.asyncio as aioredis

            return aioredis.from_url(REDIS_URL, decode_responses=True)
        except Exception as exc:  # noqa: BLE001
            # Redis が無くても通話と録音は成立する。画面への配信だけ諦める
            logger.warn("Redis に接続できません。画面への配信を無効にします", err=str(exc))
            return None

    # ------------------------------------------------------------------ 音声

    async def feed(self, *, track: str, mulaw: bytes, timestamp_ms: int) -> None:
        pcm = mulaw_to_pcm16(mulaw)

        asr = self._asr.get(track)
        if asr is not None:
            await asr.feed(pcm, timestamp_ms)  # type: ignore[attr-defined]

        # 発話の切れ目は相手側だけを見る
        if track == "inbound" and self._detector.feed(pcm) and self._last_utterance:
            asyncio.create_task(self._suggest(self._last_utterance))

    # ------------------------------------------------------------------ 配信

    async def _pump(self, track: str, asr) -> None:
        """ASR の結果を担当者の画面へ流す。

        media ワーカーから直接 WebSocket を張らず Redis を挟むのは、
        どちらかの再起動で全通話の画面が死なないようにするため。
        """
        try:
            async for result in asr.results():
                if track == "inbound" and result.is_final:
                    self._last_utterance = result.text

                await self._publish(
                    {
                        "type": "transcript",
                        "track": track,
                        "text": result.text,
                        "is_final": result.is_final,
                        "started_ms": result.started_ms,
                        "ended_ms": result.ended_ms,
                    }
                )
                if result.is_final:
                    await self._persist(track, result)
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001
            logger.error("文字起こしの配信に失敗しました", call_id=self.call_id, err=str(exc))

    async def _publish(self, message: dict) -> None:
        if self._redis is None:
            return
        try:
            await self._redis.publish(f"call:{self.call_id}", json.dumps(message))
        except Exception as exc:  # noqa: BLE001
            logger.warn("Redis への配信に失敗しました", call_id=self.call_id, err=str(exc))

    async def _persist(self, track: str, result: ASRResult) -> None:
        """final だけ保存する。source='realtime' として、後のバッチ版と併存させる。

        リアルタイム版は精度でバッチ版に劣るが、「その時点で担当者が何を見て
        いたか」の記録として、サジェストの品質評価に要る。
        """
        from ..config import RECORDING_RETENTION_DAYS
        from ..db.engine import admin_tx

        try:
            async with admin_tx() as conn:
                await conn.execute(
                    """
                    insert into transcript_segments
                      (tenant_id, call_id, source, track, started_ms, ended_ms,
                       text, confidence, expires_at)
                    select tenant_id, id, 'realtime', $2, $3, $4, $5, $6,
                           now() + make_interval(days => $7)
                      from calls where id = $1
                    """,
                    self.call_id,
                    track,
                    result.started_ms,
                    result.ended_ms,
                    result.text,
                    result.confidence,
                    RECORDING_RETENTION_DAYS,
                )
        except Exception as exc:  # noqa: BLE001
            logger.error("文字起こしの保存に失敗しました", call_id=self.call_id, err=str(exc))

    # ------------------------------------------------------------------ 提案

    async def _suggest(self, utterance: str) -> None:
        """相手の発話が切れたタイミングで切り返し候補を出す。

        ★ 短く出す。担当者は会話中で、画面を読む余裕は 1〜2 秒しかない。
          長い提案は読まれず、読まれないものは価値がない。
        """
        from .suggest import suggest_reply

        try:
            text = await suggest_reply(self.call_id, utterance)
        except Exception as exc:  # noqa: BLE001
            logger.warn("サジェストの生成に失敗しました", call_id=self.call_id, err=str(exc))
            return
        if not text:
            return

        await self._publish({"type": "suggestion", "text": text})

    # ------------------------------------------------------------------ 終了

    async def close(self) -> None:
        for task in self._pumps:
            task.cancel()
        self._pumps.clear()

        for asr in self._asr.values():
            try:
                await asr.close()  # type: ignore[attr-defined]
            except Exception as exc:  # noqa: BLE001
                logger.warn("ASR の切断に失敗しました", err=str(exc))
        self._asr.clear()

        if self._redis is not None:
            await self._publish({"type": "stream_ended"})
            try:
                await self._redis.aclose()
            except Exception:  # noqa: BLE001, S110
                pass
            self._redis = None

        logger.info("文字起こしを終了しました", call_id=self.call_id)
