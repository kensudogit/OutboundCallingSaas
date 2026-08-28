"""接続プールとテナントスコープ（原則 4）。

★ RLS は「接続にテナントを設定してから使う」ことで効く。設定を忘れた接続からは
  1 行も見えないのが正しい失敗の仕方で、そのために current_setting(..., true) が
  NULL を返すようにしてある（migrations/versions/0001_initial_schema.py 参照）。

★ SET LOCAL であることが重要。SET だと接続がプールに戻った後も設定が残り、
  次に同じ接続を掴んだ別テナントのリクエストに漏れる。
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

import asyncpg

from ..config import DATABASE_MIGRATOR_URL, DATABASE_URL
from ..logger import logger

_pool: asyncpg.Pool | None = None


def _dsn(url: str) -> str:
    """SQLAlchemy 形式の URL が来ても asyncpg が読める形に落とす。"""
    return url.replace("postgresql+asyncpg://", "postgresql://")


async def init_pool() -> asyncpg.Pool:
    global _pool
    if _pool is None:
        _pool = await asyncpg.create_pool(
            _dsn(DATABASE_URL),
            min_size=2,
            max_size=10,
            command_timeout=10,
            # 金額と違い、ここでの型変換の主対象は uuid と timestamptz。
            # asyncpg は既定で適切に返すので追加のコーデックは要らない
        )
        logger.info("database pool initialized")
    return _pool


async def close_pool() -> None:
    global _pool
    if _pool is not None:
        await _pool.close()
        _pool = None


def pool() -> asyncpg.Pool:
    if _pool is None:
        raise RuntimeError("プールが初期化されていません。init_pool() を先に呼んでください")
    return _pool


@asynccontextmanager
async def tenant_tx(tenant_id: str) -> AsyncIterator[asyncpg.Connection]:
    """テナントを固定したトランザクション。

    RLS を通す唯一の入口。アプリのクエリはすべてここで得た接続を使う。
    トランザクションが終われば set_config も消える。
    """
    async with pool().acquire() as conn:
        async with conn.transaction():
            # ★ 第 3 引数 true が is_local。SET LOCAL と同義で、バインドできる
            await conn.execute("select set_config('app.tenant_id', $1, true)", str(tenant_id))
            yield conn


@asynccontextmanager
async def admin_tx() -> AsyncIterator[asyncpg.Connection]:
    """RLS を迂回する接続（マイグレーション・定期ジョブ用）。

    ★ BYPASSRLS を持つロールで接続する。アプリのリクエスト処理からは使わない。
      使いたくなったら、それは RLS の設計を間違えている合図。
    """
    url = DATABASE_MIGRATOR_URL or DATABASE_URL
    conn = await asyncpg.connect(_dsn(url))
    try:
        async with conn.transaction():
            yield conn
    finally:
        await conn.close()
