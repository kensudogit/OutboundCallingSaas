"""担当者向けの WebSocket。

media ワーカーが Redis に publish したものを、ここで購読して画面へ転送する。

★ 直接 WebSocket を張り合わせないのは、どちらかの再起動で全通話の画面が
  死ぬのを避けるため。
★ この WebSocket にも認証と認可を掛ける。call_id を知っていれば誰でも
  他人の通話を盗み聞きできる状態になりやすい。WebSocket の認証は
  忘れられやすく、REST だけ守って満足している実装をよく見る。
"""

from __future__ import annotations

import asyncio

from fastapi import APIRouter, WebSocket, WebSocketDisconnect
from starlette.websockets import WebSocketState

from ..config import REDIS_URL
from ..db.engine import tenant_tx
from ..logger import logger
from .auth import decode_token

router = APIRouter(tags=["realtime"])


async def _reject(ws: WebSocket, code: int) -> None:
    """認証・認可で切る。

    ハンドシェイク前に close だけ送ると、ASGI が
    「websocket.close after http.response.start」相当のエラーをログに出す。
    一度 accept してから閉じる。
    """
    if ws.client_state == WebSocketState.CONNECTING:
        await ws.accept()
    if ws.application_state == WebSocketState.CONNECTED:
        await ws.close(code=code)


@router.websocket("/ws/agent/{call_id}")
async def agent_channel(ws: WebSocket, call_id: str, token: str = "") -> None:
    # ブラウザの WebSocket は任意ヘッダを付けられないのでクエリで受ける。
    # ★ 短命な JWT であること（TTL 2 時間）。URL はログに残りやすい
    try:
        user = decode_token(token)
    except Exception:  # noqa: BLE001
        await _reject(ws, 4401)
        return

    # RLS が効いた接続で引くので、他テナントの通話なら行が見えず 4403 になる
    async with tenant_tx(user.tenant_id) as conn:
        row = await conn.fetchrow(
            "select agent_id from calls where id = $1", call_id
        )
    if row is None:
        await _reject(ws, 4403)
        return
    # 自分の通話か、管理者のモニタリングのみ許可する
    if str(row["agent_id"]) != user.id and user.role not in ("manager", "admin"):
        await _reject(ws, 4403)
        return

    await ws.accept()

    try:
        import redis.asyncio as aioredis

        redis = aioredis.from_url(REDIS_URL, decode_responses=True)
    except Exception as exc:  # noqa: BLE001
        # 文字起こしが出ないだけで通話は続く。画面には停止中と伝える
        logger.warn("Redis に接続できません", err=str(exc))
        await ws.send_json({"type": "transcript_unavailable"})
        await ws.close(code=1011)
        return

    pubsub = redis.pubsub()
    await pubsub.subscribe(f"call:{call_id}")

    try:
        while True:
            message = await pubsub.get_message(ignore_subscribe_messages=True, timeout=30)
            if message is None:
                # 生存確認。切れていればここで例外になる
                await ws.send_json({"type": "ping"})
                continue
            await ws.send_text(message["data"])
    except WebSocketDisconnect:
        pass
    except asyncio.CancelledError:
        raise
    except Exception as exc:  # noqa: BLE001
        logger.warn("担当者チャネルが終了しました", call_id=call_id, err=str(exc))
    finally:
        await pubsub.unsubscribe(f"call:{call_id}")
        await pubsub.aclose()
        await redis.aclose()
