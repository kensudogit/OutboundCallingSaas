"""テスト共通のセットアップ。

★ config.py は import 時に環境変数を検証して落ちる実装なので、
  テストでは値を先に入れておく。これをしないと、config を import する
  モジュールを一切テストできなくなる。

★ 実 DB と Twilio には触らない。架電システムのテストで実際に電話をかけると
  検証にならない（相手が必要で、繰り返せず、課金される）。
"""

from __future__ import annotations

import os

os.environ.setdefault("APP_ENV", "test")
os.environ.setdefault("PUBLIC_BASE_URL", "https://example.test")
os.environ.setdefault("PUBLIC_WSS_URL", "wss://example.test")
# ★ 実 DB を使うテスト（*_db.py）はアプリのプール経由で繋ぐので、
#   ここが実在する DSN でないと接続できない。DB が無い環境では
#   各テストの fixture が skip するので、値が入っていても害はない。
os.environ.setdefault(
    "DATABASE_URL", "postgresql://app_user:app_password@localhost:5434/calling"
)
os.environ.setdefault(
    "DATABASE_MIGRATOR_URL", "postgresql://migrator:migrator_password@localhost:5434/calling"
)
os.environ.setdefault("JWT_SECRET", "test-jwt-secret-do-not-use-outside-tests")
os.environ.setdefault("TWILIO_ACCOUNT_SID", "ACtest00000000000000000000000000")
os.environ.setdefault("TWILIO_AUTH_TOKEN", "test-auth-token-0123456789")
os.environ.setdefault("TWILIO_CALLER_ID", "+815012345678")
os.environ.setdefault("ASR_PROVIDER", "null")

import pytest  # noqa: E402


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"
