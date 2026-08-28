#!/bin/sh
# ============================================================================
# プロセスの振り分け。
#
# ★ 改行は LF でなければならない。CRLF のままコンテナに入ると
#   "no such file or directory" で落ち、原因が改行だと気付くまで時間を溶かす。
#   .gitattributes で LF に固定してある。
#
# ★ exec で置き換える。シェルを親に残すと SIGTERM がアプリに届かず、
#   デプロイのたびに通話が強制切断される（graceful shutdown が効かない）。
# ============================================================================
set -e

PORT="${PORT:-8000}"
MEDIA_PORT="${MEDIA_PORT:-8001}"

case "${1:-api}" in
  api)
    exec uvicorn app.app:app --host 0.0.0.0 --port "$PORT" \
      --proxy-headers --forwarded-allow-ips='*'
    ;;

  media)
    # ★ API とは別サービスとして動かす（原則 3）。同じプロセスに載せると
    #   通話が増えるほど API のレスポンスが悪化する。
    #   スケールの軸も違う（API は同時ユーザー数、media は同時通話数）
    exec uvicorn app.realtime.media_app:media_app --host 0.0.0.0 --port "$MEDIA_PORT" \
      --proxy-headers --forwarded-allow-ips='*'
    ;;

  jobs)
    # ★ これを動かさないと、担当者のブラウザが落ちるたびに予約が残って
    #   リストが少しずつ枯れる。録音も消えない
    exec python -m app.jobs.maintenance --loop "${JOB_INTERVAL_SECONDS:-60}"
    ;;

  migrate)
    # ★ リリース時に一度だけ。コンテナ起動時に流さない
    #   （複数インスタンスが同時に上がると並行実行になる）
    exec python -m alembic upgrade head
    ;;

  seed)
    # デモデータ。APP_ENV=production では seed 側が拒否する
    exec python -m app.db.seed "${@:-}"
    ;;

  *)
    # 任意のコマンドを実行できるようにしておく（調査用）
    exec "$@"
    ;;
esac
