"""Media ワーカー（原則 3）。

★ これは web の ASGI アプリとは別の app で、別ポート・別プロセスで動かす。
  Media Streams の WebSocket は 20ms ごとに音声フレームが来るので、
  1 通話で毎秒 50 メッセージ。10 通話同時なら毎秒 500 イベント。
  これを API と同じイベントループに載せると、通話が増えるほど API の
  レスポンスが悪化し、原因が分かりにくい形で表面化する。

  分けておけばスケールの軸も分けられる。API は同時ユーザー数、
  media は同時通話数でスケールする。

起動:
    uvicorn app.realtime.media_app:media_app --port 8001
"""

from __future__ import annotations

import base64
import json
from contextlib import asynccontextmanager

from fastapi import FastAPI, WebSocket, WebSocketDisconnect

from ..logger import logger
from .session import TranscriptionSession

@asynccontextmanager
async def lifespan(_: FastAPI):
    """media ワーカーも同じ DB を触る（通話行の更新・文字起こしの保存）。

    ★ API と同じく、RLS が効かないロールで動いていないかを起動時に確かめる。
      片方だけ検査しても、もう片方から漏れる。
    """
    from ..db.engine import assert_rls_enforced, close_pool, init_pool, pool

    await init_pool()
    async with pool().acquire() as conn:
        await conn.fetchval("select 1")
        await assert_rls_enforced(conn)
    logger.info("media worker started")
    yield
    await close_pool()


media_app = FastAPI(title="media-worker", lifespan=lifespan)


@media_app.get("/healthz")
async def healthz() -> dict[str, bool]:
    return {"ok": True}


@media_app.websocket("/media")
async def media_stream(ws: WebSocket, call_id: str) -> None:
    """Twilio Media Streams の受信。

    メッセージの event は connected / start / media / stop。
    track="both_tracks" を指定しているので inbound（相手）と
    outbound（担当者）が別メッセージで届く。
    """
    await ws.accept()
    session = TranscriptionSession(call_id=call_id)

    try:
        async for raw in ws.iter_text():
            msg = json.loads(raw)
            event = msg.get("event")

            if event == "start":
                start = msg["start"]
                # ★ 3 経路のうちの Media Stream 経路（原則 2）
                await session.open(
                    provider_call_sid=start["callSid"], stream_sid=start["streamSid"]
                )

            elif event == "media":
                media = msg["media"]
                # payload は base64 の μ-law 8kHz、20ms（160 バイト）
                chunk = base64.b64decode(media["payload"])
                await session.feed(
                    track=media.get("track", "inbound"),
                    mulaw=chunk,
                    # ★ ストリーム開始からの経過ms。受信時刻を使うとネットワーク
                    #   遅延が混ざり、後から録音を頭出しできなくなる
                    timestamp_ms=int(media["timestamp"]),
                )

            elif event == "stop":
                break

    except WebSocketDisconnect:
        logger.info("Media Stream が切断されました", call_id=call_id)
    except Exception as exc:  # noqa: BLE001
        # ★ ここで例外が漏れても通話は継続する（<Start><Stream> は分岐なので
        #   WSS が切れても <Dial> に影響しない）。文字起こしを諦めるだけ。
        logger.error("Media Stream の処理に失敗しました", call_id=call_id, err=str(exc))
    finally:
        # ★ 通話が切れたら必ず ASR 接続を閉じる。閉じ忘れると課金が続き、
        #   同時接続数の上限にも当たる
        await session.close()
