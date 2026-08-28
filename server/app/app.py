"""web ワーカーの ASGI アプリ。

★ Media Streams の WebSocket はここに載せない（原則 3）。
  音声は app.realtime.media_app を別ポート・別プロセスで動かす。
  ここに載せると、通話が増えるほど API のレスポンスが悪化する。

起動:
    uvicorn app.app:app --port 8000                       # API
    uvicorn app.realtime.media_app:media_app --port 8001  # 音声
"""

from __future__ import annotations

from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

from .api import admin, agent_ws, auth, calls, queue, stats, voice_token
from .config import CORS_ORIGIN
from .db.engine import assert_rls_enforced, close_pool, init_pool, pool
from .logger import logger
from .telephony import routes as telephony_routes


@asynccontextmanager
async def lifespan(_: FastAPI):
    await init_pool()
    # DB に繋がらないまま起動して、発信のときに初めて気付くのを避ける
    async with pool().acquire() as conn:
        await conn.fetchval("select 1")
        # ★ 接続ロールが RLS を素通りしないことを確かめる。
        #   ここを通さないと「動くけれど守られていない」本番ができる
        await assert_rls_enforced(conn)
    logger.info("server started")
    yield
    await close_pool()


def create_app() -> FastAPI:
    app = FastAPI(title="OutboundCallingSaas", lifespan=lifespan)

    app.add_middleware(
        CORSMiddleware,
        allow_origins=[CORS_ORIGIN],
        allow_credentials=False,
        allow_methods=["*"],
        allow_headers=["*"],
    )

    @app.get("/healthz")
    async def healthz():
        # 「プロセスが生きている」ではなく「DB に繋がる」で判定する。
        # 前者だとDB断でもロードバランサに入れられ続ける
        try:
            async with pool().acquire() as conn:
                await conn.fetchval("select 1")
        except Exception:  # noqa: BLE001
            return JSONResponse({"ok": False, "error": "database_unavailable"}, status_code=503)
        return {"ok": True}

    # Twilio からのコールバック（署名検証あり）
    app.include_router(telephony_routes.router)

    # 通常の JSON API
    app.include_router(auth.router)
    app.include_router(admin.router)
    app.include_router(queue.router)
    app.include_router(calls.router)
    app.include_router(voice_token.router)
    app.include_router(stats.router)
    app.include_router(agent_ws.router)

    @app.exception_handler(Exception)
    async def unhandled(request: Request, exc: Exception):
        logger.error("unhandled error", path=request.url.path, err=str(exc))
        return JSONResponse({"error": "internal_error"}, status_code=500)

    return app


app = create_app()
