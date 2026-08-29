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
from fastapi.responses import HTMLResponse, JSONResponse

from .api import admin, agent_ws, auth, calls, queue, stats, voice_token
from .config import CORS_ORIGIN, TWILIO_CONFIGURED
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

    @app.get("/")
    async def root(request: Request):
        """ブラウザで公開 URL を開いたときに 404 JSON を出さない。

        このサービスは API であって画面ではない。ルート未定義のままだと
        FastAPI 既定の {"detail":"Not Found"} になり、「デプロイ失敗」に見える。
        """
        telephony = "configured" if TWILIO_CONFIGURED else "disabled"
        body = {
            "service": "api",
            "ok": True,
            "telephony": telephony,
            "healthz": "/healthz",
            "docs": "/docs",
        }
        accept = request.headers.get("accept", "")
        if "text/html" in accept:
            status = "有効" if TWILIO_CONFIGURED else "無効（Twilio 未設定）"
            html = f"""<!DOCTYPE html>
<html lang="ja">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>OutboundCallingSaas API</title>
  <style>
    :root {{
      color-scheme: dark;
      font-family: Inter, "Noto Sans JP", system-ui, sans-serif;
      background: #07111f;
      color: #e7edf6;
    }}
    * {{ box-sizing: border-box; }}
    body {{
      min-height: 100vh;
      margin: 0;
      display: grid;
      place-items: center;
      padding: 24px;
      background:
        radial-gradient(circle at 20% 20%, rgba(31, 111, 235, .22), transparent 38%),
        radial-gradient(circle at 80% 80%, rgba(22, 163, 74, .12), transparent 34%),
        #07111f;
    }}
    main {{
      width: min(680px, 100%);
      padding: clamp(28px, 6vw, 52px);
      border: 1px solid rgba(148, 163, 184, .2);
      border-radius: 24px;
      background: rgba(15, 27, 46, .86);
      box-shadow: 0 24px 80px rgba(0, 0, 0, .38);
      backdrop-filter: blur(18px);
    }}
    .eyebrow {{
      margin: 0 0 12px;
      color: #60a5fa;
      font-size: 12px;
      font-weight: 800;
      letter-spacing: .16em;
      text-transform: uppercase;
    }}
    h1 {{
      margin: 0;
      font-size: clamp(30px, 6vw, 48px);
      line-height: 1.1;
      letter-spacing: -.035em;
    }}
    .lead {{
      margin: 18px 0 30px;
      color: #aebbd0;
      line-height: 1.8;
    }}
    .status {{
      display: grid;
      grid-template-columns: repeat(3, 1fr);
      gap: 12px;
      margin: 0;
    }}
    .status > div {{
      min-width: 0;
      padding: 16px;
      border: 1px solid rgba(148, 163, 184, .16);
      border-radius: 14px;
      background: rgba(4, 12, 24, .55);
    }}
    dt {{
      margin-bottom: 7px;
      color: #75859d;
      font-size: 12px;
      font-weight: 700;
    }}
    dd {{
      margin: 0;
      overflow-wrap: anywhere;
      font-size: 14px;
      font-weight: 700;
    }}
    .ok {{ color: #4ade80; }}
    a {{
      color: #93c5fd;
      text-decoration: none;
    }}
    a:hover {{ color: #dbeafe; text-decoration: underline; }}
    @media (max-width: 560px) {{
      .status {{ grid-template-columns: 1fr; }}
    }}
  </style>
</head>
<body>
  <main>
    <p class="eyebrow">Service status</p>
    <h1>OutboundCallingSaas API</h1>
    <p class="lead">
      API サービスは正常に稼働しています。ログイン・発信画面は
      Next.js のフロントサービスから利用してください。
    </p>
    <dl class="status">
      <div>
        <dt>API</dt>
        <dd class="ok">● 稼働中</dd>
      </div>
      <div>
        <dt>死活確認</dt>
        <dd><a href="/healthz">/healthz</a></dd>
      </div>
      <div>
        <dt>電話機能</dt>
        <dd>{status}</dd>
      </div>
    </dl>
    <p class="lead" style="margin-bottom: 0">
      開発者向け: <a href="/docs">API ドキュメントを開く →</a>
    </p>
  </main>
</body>
</html>
"""
            return HTMLResponse(html)
        return body

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
        # WebSocket に JSON を返すと ASGI がエラーをログに出す
        if request.scope.get("type") != "http":
            raise exc
        logger.error("unhandled error", path=request.url.path, err=str(exc))
        return JSONResponse({"error": "internal_error"}, status_code=500)

    return app


app = create_app()
