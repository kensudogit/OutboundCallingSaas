"""Alembic の実行環境。

★ 接続は必ず migrator ロール（BYPASSRLS）で行う。アプリの接続ユーザーで
  流すと、RLS のポリシーに阻まれてマイグレーション自体が中途半端に失敗する。
  失敗の仕方が分かりにくいので、ここで明示的に切り替える。

★ autogenerate は使えない。SQLAlchemy のモデルを持たず、DDL を生の SQL で
  書いているため（RLS ポリシー・部分ユニークインデックス・不変関数は
  SQLAlchemy のスキーマ表現では書けないものが多い）。
  target_metadata を None にしてあるので、autogenerate は空の差分を出す。
  リビジョンは `alembic revision -m "..."` で作り、SQL を手で書く。
"""

from __future__ import annotations

import sys
from logging.config import fileConfig
from pathlib import Path

from alembic import context
from sqlalchemy import engine_from_config, pool

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.config import DATABASE_MIGRATOR_URL, DATABASE_URL  # noqa: E402

config = context.config

if config.config_file_name is not None:
    fileConfig(config.config_file_name)

# ★ モデルを持たないので autogenerate は無効。詳細は上の docstring
target_metadata = None


def _sync_url() -> str:
    """同期ドライバ（psycopg）用の URL に直す。

    アプリは asyncpg で動くが、Alembic は同期で回すほうが素直
    （マイグレーション中に並行性は要らない）。
    """
    url = DATABASE_MIGRATOR_URL or DATABASE_URL
    if not (DATABASE_MIGRATOR_URL or "").strip():
        # ★ 落とす。アプリのロールで流すと RLS に阻まれて中途半端に失敗する
        raise RuntimeError(
            "DATABASE_MIGRATOR_URL が未設定です。"
            "BYPASSRLS を持つロール（migrator）の接続文字列を設定してください"
        )
    return (
        url.replace("postgresql+asyncpg://", "postgresql+psycopg://")
        .replace("postgresql://", "postgresql+psycopg://")
    )


def run_migrations_offline() -> None:
    """SQL を出力するだけのモード（本番の適用を DBA に渡す場合など）。"""
    context.configure(
        url=_sync_url(),
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
    )
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    section = config.get_section(config.config_ini_section, {})
    section["sqlalchemy.url"] = _sync_url()

    connectable = engine_from_config(section, prefix="sqlalchemy.", poolclass=pool.NullPool)

    with connectable.connect() as connection:
        context.configure(
            connection=connection,
            target_metadata=target_metadata,
            # ★ 1 トランザクションで流す。途中で失敗したら全部戻る。
            #   部分適用された状態を残さないため
            transaction_per_migration=False,
        )
        with context.begin_transaction():
            context.run_migrations()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
