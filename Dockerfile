# ============================================================================
# サーバー（FastAPI）のイメージ。
#
# ★ 1 つのイメージで 3 つのプロセスを動かす。原則 3 により、音声の WebSocket は
#   API と同じプロセスに載せられない（Media Streams は 1 通話あたり毎秒 50
#   メッセージ。API と同じイベントループに載せると通話が増えるほど API が遅くなる）。
#
#     docker run <image> api      API（既定）
#     docker run <image> media    音声ワーカー。★ 必ず別サービスとして動かす
#     docker run <image> jobs     定期ジョブ。予約の解放・録音の削除・集計
#     docker run <image> migrate  スキーマ適用。リリース時に一度だけ
#
#   イメージを分けないのは、依存もコードも同じで、分けると「片方だけ古い」が
#   起きるため。プロセスの分離はデプロイ側（サービス定義）で行う。
#
# ★ 起動時にマイグレーションを流さない。複数インスタンスが同時に上がると
#   同じマイグレーションを並行実行することになる。リリースコマンドとして
#   1 回だけ実行する（下の「デプロイ」を参照）。
#
# ★ フロント（Next.js）は別イメージ。web/Dockerfile を使う。
#   Python から Server Components は配信できないので、同居させられない。
# ============================================================================

# ---------------------------------------------------------------- builder

FROM python:3.12-slim AS builder

ENV PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /build

# 依存の解決だけを先に行い、レイヤをキャッシュさせる。
# app/ を先に COPY するとコード変更のたびに全依存を入れ直すことになる
COPY server/pyproject.toml ./
COPY server/app ./app

RUN python -m venv /opt/venv \
 && /opt/venv/bin/pip install --upgrade pip setuptools wheel \
 && /opt/venv/bin/pip install ".[storage,llm]"

# ---------------------------------------------------------------- runtime

FROM python:3.12-slim AS runtime

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PATH="/opt/venv/bin:$PATH" \
    APP_ENV=production

# ★ 非 root で動かす。録音（個人情報）を扱うコンテナなので、
#   侵入されたときにできることを減らす
RUN groupadd --system app && useradd --system --gid app --home /srv app

COPY --from=builder /opt/venv /opt/venv

WORKDIR /srv

# alembic は cwd の alembic.ini と migrations/ を見る
COPY server/alembic.ini ./
COPY server/migrations ./migrations
COPY server/app ./app
COPY docker-entrypoint.sh /usr/local/bin/entrypoint

RUN chmod +x /usr/local/bin/entrypoint && chown -R app:app /srv

USER app

EXPOSE 8000

# ★ 「プロセスが生きている」ではなく「DB に繋がる」で判定する。
#   前者だと DB 断でもロードバランサに入れられ続ける。
#   media ワーカーにも /healthz があるので、同じ設定で両方に効く
HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
  CMD python -c "import os,urllib.request,sys; \
port=os.environ.get('PORT','8000'); \
sys.exit(0 if urllib.request.urlopen(f'http://127.0.0.1:{port}/healthz', timeout=4).status==200 else 1)"

ENTRYPOINT ["entrypoint"]
CMD ["api"]
