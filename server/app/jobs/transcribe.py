"""通話後の全文文字起こし・要約・会話メトリクス。

    python -m app.jobs.transcribe

★ リアルタイム版（source='realtime'）とは別に source='batch' で保存し、
  両方を残す。精度はバッチが上だが、リアルタイム版は「その時点で担当者が
  何を見ていたか」の記録として、サジェストの品質評価に要る。

★ 状態を transcription_jobs で持つ。ASR も LLM も外部サービスなので落ちる。
  途中で失敗しても、次回の実行が同じ録音を拾い直せるようにする。

★ AI が止まっても通話はできる、が正しい壊れ方。このジョブが動かなくても
  発信・録音・結果登録は成立する。
"""

from __future__ import annotations

import argparse
import asyncio

from .. import storage
from ..config import ASR_PROVIDER, LLM_API_KEY, LLM_MODEL, RECORDING_RETENTION_DAYS
from ..db.engine import admin_tx, close_pool, init_pool
from ..domain import conversation
from ..domain.wav import UnsupportedAudio, split_channels
from ..logger import logger
from ..realtime.batch_asr import BatchSegment, transcribe_channel

BATCH_SIZE = 10
MAX_ATTEMPTS = 3


async def _claim(limit: int) -> list:
    """処理対象を確保する。

    ★ FOR UPDATE SKIP LOCKED で、ジョブを複数並べても同じ録音を
      2 回処理しない。発信の予約と同じ考え方。
    """
    async with admin_tx() as conn:
        return await conn.fetch(
            """
            with pending as (
              select r.id
                from recordings r
                left join transcription_jobs j on j.recording_id = r.id
               where r.storage_key is not null
                 and r.deleted_at is null
                 and (j.state is null
                      or (j.state = 'FAILED' and j.attempts < $2))
               order by r.created_at
               limit $1
               for update of r skip locked
            )
            insert into transcription_jobs (recording_id, tenant_id, state, attempts)
            select pending.id, r.tenant_id, 'RUNNING', 1
              from pending join recordings r on r.id = pending.id
            on conflict (recording_id) do update set
              state = 'RUNNING',
              attempts = transcription_jobs.attempts + 1,
              updated_at = now()
            returning recording_id, tenant_id
            """,
            limit,
            MAX_ATTEMPTS,
        )


async def _finish(recording_id, state: str, error: str | None = None) -> None:
    """処理結果を記録する。

    ★ update ではなく upsert。通常は _claim が行を作ってから呼ばれるが、
      作られていない経路（再処理・手動実行）で状態が黙って捨てられると、
      SKIPPED にしたはずの録音を延々と拾い直すことになる。
    """
    async with admin_tx() as conn:
        await conn.execute(
            """
            insert into transcription_jobs (recording_id, tenant_id, state, last_error, attempts)
            select r.id, r.tenant_id, $2, $3, 1 from recordings r where r.id = $1
            on conflict (recording_id) do update set
              state = excluded.state, last_error = excluded.last_error, updated_at = now()
            """,
            recording_id, state, error,
        )


async def process_one(recording_id, tenant_id) -> str:
    """1 件の録音を処理する。戻り値は最終状態。"""
    async with admin_tx() as conn:
        rec = await conn.fetchrow(
            "select r.*, c.id as call_ref, c.duration_sec "
            "from recordings r join calls c on c.id = r.call_id where r.id = $1",
            recording_id,
        )
    if rec is None:
        return "SKIPPED"

    try:
        audio = storage.backend().get(rec["storage_key"])
    except Exception as exc:  # noqa: BLE001
        logger.error("録音を読めません", recording_id=str(recording_id), err=str(exc))
        return "FAILED"

    try:
        channels = split_channels(audio)
    except UnsupportedAudio as exc:
        # 再試行しても直らない。SKIPPED にして拾い直さない
        logger.warn("扱えない録音のためスキップします", recording_id=str(recording_id), err=str(exc))
        await _finish(recording_id, "SKIPPED", str(exc))
        return "SKIPPED"

    segments: list[BatchSegment] = []
    for channel in channels:
        try:
            segments.extend(await transcribe_channel(channel))
        except Exception as exc:  # noqa: BLE001
            logger.error(
                "文字起こしに失敗しました",
                recording_id=str(recording_id), track=channel.track, err=str(exc),
            )
            return "FAILED"

    if not segments:
        # ASR 未設定（ASR_PROVIDER=null）や完全な無音。処理自体は成功
        logger.info("文字起こし結果が空でした", recording_id=str(recording_id))
        return "DONE"

    await _store_segments(rec["call_id"], tenant_id, segments)
    await _store_metrics(rec["call_id"], tenant_id, segments, rec["duration_sec"])
    await _store_summary(rec["call_id"], tenant_id, segments)
    return "DONE"


async def _store_segments(call_id, tenant_id, segments: list[BatchSegment]) -> None:
    """確定版を入れ替える。

    ★ 冪等にするため、先に同じ source の行を消してから入れる。
      ジョブが 2 回走っても文字起こしが二重にならない。
    """
    async with admin_tx() as conn:
        async with conn.transaction():
            await conn.execute(
                "delete from transcript_segments where call_id = $1 and source = 'batch'",
                call_id,
            )
            await conn.executemany(
                """
                insert into transcript_segments
                  (tenant_id, call_id, source, track, started_ms, ended_ms,
                   text, confidence, expires_at)
                values ($1, $2, 'batch', $3, $4, $5, $6, $7,
                        now() + make_interval(days => $8))
                """,
                [
                    (tenant_id, call_id, s.track, s.started_ms, s.ended_ms,
                     s.text, s.confidence, RECORDING_RETENTION_DAYS)
                    for s in segments
                ],
            )
    logger.info("全文文字起こしを保存しました", call_id=str(call_id), segments=len(segments))


async def _store_metrics(
    call_id, tenant_id, segments: list[BatchSegment], duration_sec: int | None
) -> None:
    """会話メトリクスを 1 回だけ計算して保存する。

    ★ 通話一覧を開くたびにセグメントから計算し直すと重くなる。
    """
    tracks = {s.track for s in segments}
    if not {"inbound", "outbound"} & tracks or "unknown" in tracks:
        # モノラル録音では話者が分からないので計算しない。
        # 不確かな数字を出すより、出さないほうがよい
        logger.info("話者が分離できないため会話メトリクスを省略します", call_id=str(call_id))
        return

    metrics = conversation.compute(
        [conversation.Segment(s.track, s.started_ms, s.ended_ms) for s in segments],
        call_duration_ms=duration_sec * 1000 if duration_sec else None,
    )

    async with admin_tx() as conn:
        await conn.execute(
            """
            insert into call_conversation_metrics
              (call_id, tenant_id, agent_talk_ms, contact_talk_ms, silence_ms,
               overlap_ms, agent_turns, longest_monologue_ms, first_response_ms)
            values ($1, $2, $3, $4, $5, $6, $7, $8, $9)
            on conflict (call_id) do update set
              agent_talk_ms = excluded.agent_talk_ms,
              contact_talk_ms = excluded.contact_talk_ms,
              silence_ms = excluded.silence_ms,
              overlap_ms = excluded.overlap_ms,
              agent_turns = excluded.agent_turns,
              longest_monologue_ms = excluded.longest_monologue_ms,
              first_response_ms = excluded.first_response_ms,
              computed_at = now()
            """,
            call_id, tenant_id,
            metrics.agent_talk_ms, metrics.contact_talk_ms, metrics.silence_ms,
            metrics.overlap_ms, metrics.agent_turns, metrics.longest_monologue_ms,
            metrics.first_response_ms,
        )
    logger.info(
        "会話メトリクスを保存しました",
        call_id=str(call_id),
        talk_ratio=round(metrics.talk_ratio, 3) if metrics.talk_ratio else None,
    )


_SUMMARY_SYSTEM = """通話の文字起こしから、営業担当者が後で読む要約を作ってください。

制約:
- 3 行以内。長いと読まれません
- 相手が言ったことだけを書く。推測や評価を混ぜない
- 最後に「次アクション」を 1 行。決まっていなければ「なし」と書く
"""


async def _store_summary(call_id, tenant_id, segments: list[BatchSegment]) -> None:
    if not LLM_API_KEY:
        return

    transcript = "\n".join(
        f"{'担当者' if s.track == 'outbound' else '相手'}: {s.text}"
        for s in sorted(segments, key=lambda s: s.started_ms)
    )

    try:
        from anthropic import AsyncAnthropic

        client = AsyncAnthropic(api_key=LLM_API_KEY)
        message = await client.messages.create(
            model=LLM_MODEL,
            max_tokens=400,
            system=_SUMMARY_SYSTEM,
            messages=[{"role": "user", "content": transcript[:20000]}],
        )
    except Exception as exc:  # noqa: BLE001
        # 要約が無くても文字起こしは残る。ジョブ全体は失敗にしない
        logger.warn("要約の生成に失敗しました", call_id=str(call_id), err=str(exc))
        return

    text = "".join(b.text for b in message.content if b.type == "text").strip()
    if not text:
        return

    summary, _, next_action = text.rpartition("次アクション")
    async with admin_tx() as conn:
        await conn.execute(
            """
            insert into call_summaries
              (call_id, tenant_id, summary, next_action, model, expires_at)
            values ($1, $2, $3, $4, $5, now() + make_interval(days => $6))
            on conflict (call_id) do update set
              summary = excluded.summary, next_action = excluded.next_action,
              model = excluded.model, created_at = now()
            """,
            call_id, tenant_id,
            (summary or text).strip(),
            next_action.lstrip(":： ").strip() or None,
            LLM_MODEL,
            RECORDING_RETENTION_DAYS,
        )
    logger.info("要約を保存しました", call_id=str(call_id))


async def run_once(*, limit: int = BATCH_SIZE) -> dict[str, int]:
    claimed = await _claim(limit)
    result = {"done": 0, "failed": 0, "skipped": 0}

    for row in claimed:
        try:
            state = await process_one(row["recording_id"], row["tenant_id"])
        except Exception as exc:  # noqa: BLE001
            logger.error(
                "文字起こしジョブが例外で終了しました",
                recording_id=str(row["recording_id"]), err=str(exc),
            )
            await _finish(row["recording_id"], "FAILED", str(exc))
            result["failed"] += 1
            continue

        if state == "SKIPPED":
            result["skipped"] += 1
            continue
        await _finish(row["recording_id"], state)
        result["done" if state == "DONE" else "failed"] += 1

    return result


async def main() -> None:
    parser = argparse.ArgumentParser(description="通話後の全文文字起こしと要約")
    parser.add_argument("--limit", type=int, default=BATCH_SIZE)
    parser.add_argument("--loop", type=int, default=0)
    args = parser.parse_args()

    if ASR_PROVIDER == "null":
        logger.warn("ASR_PROVIDER=null のため文字起こしは行われません（メトリクスも出ません）")

    await init_pool()
    try:
        if args.loop <= 0:
            print(await run_once(limit=args.limit))
            return
        while True:
            try:
                await run_once(limit=args.limit)
            except Exception as exc:  # noqa: BLE001
                logger.error("文字起こしジョブが失敗しました", err=str(exc))
            await asyncio.sleep(args.loop)
    finally:
        await close_pool()


if __name__ == "__main__":
    asyncio.run(main())
