"""スキーマの適用とデモデータの投入。

    python -m app.db.migrate                # スキーマを流す
    python -m app.db.migrate --drop         # 作り直す（開発用）
    python -m app.db.migrate --seed         # デモデータも入れる

★ RLS を迂回できるロール（BYPASSRLS）で接続する。アプリの接続ユーザーで
  流すと、テーブルは作れてもポリシーの検証ができない。

★ --drop は APP_ENV=production では拒否する。架電 SaaS の DB には他社の
  顧客リストと通話録音が入っているので、事故の代償が大きい。
"""

from __future__ import annotations

import argparse
import asyncio
import pathlib
import sys

import asyncpg

from ..config import APP_ENV, DATABASE_MIGRATOR_URL, DATABASE_URL
from ..logger import logger
from ..security import hash_password

SCHEMA = pathlib.Path(__file__).with_name("schema.sql")

DROP = """
drop table if exists audit_logs, daily_agent_stats, agent_sessions,
  call_conversation_metrics, call_suggestions, transcript_segments, recordings,
  webhook_deliveries, call_attempts_blocked, calls, call_reservations,
  dispositions, dnc_entries, contacts, contact_lists, users, tenants cascade;
drop function if exists call_status_rank(text);
"""

# 結果コード。triggers_dnc が true のものは、登録と同時に DNC に入る。
# ★ 「拒否」を選んだら自動で DNC。担当者の追加操作を挟むと、通話直後の
#   忙しさで忘れられ、それが再架電に直結する。
DISPOSITIONS = [
    ("appointment", "アポ獲得", False, None, 10),
    ("callback", "再架電希望", False, 1440, 20),
    ("not_interested", "興味なし", False, None, 30),
    ("no_authority", "決裁権なし", False, None, 40),
    ("no_answer", "不応答", False, 120, 50),
    ("busy", "話中", False, 30, 60),
    ("machine", "留守番電話", False, 1440, 70),
    ("wrong_number", "番号違い", False, None, 80),
    ("refused", "架電拒否（DNC登録）", True, None, 90),
    ("complaint", "苦情（DNC登録）", True, None, 100),
]


async def _connect() -> asyncpg.Connection:
    url = (DATABASE_MIGRATOR_URL or DATABASE_URL).replace("postgresql+asyncpg://", "postgresql://")
    return await asyncpg.connect(url)


async def migrate(drop: bool, seed: bool) -> None:
    if drop and APP_ENV == "production":
        print("APP_ENV=production では --drop を実行できません", file=sys.stderr)
        raise SystemExit(2)

    conn = await _connect()
    try:
        if drop:
            logger.warn("既存のテーブルを削除します", app_env=APP_ENV)
            await conn.execute(DROP)

        await conn.execute(SCHEMA.read_text(encoding="utf-8"))
        logger.info("スキーマを適用しました")

        if seed:
            await _seed(conn)
    finally:
        await conn.close()


async def _seed(conn: asyncpg.Connection) -> None:
    """デモデータ。★ 既知のパスワードのユーザーが作られるので本番で流さない。"""
    if APP_ENV == "production":
        print("APP_ENV=production では --seed を実行できません", file=sys.stderr)
        raise SystemExit(2)

    tenant_id = await conn.fetchval(
        "insert into tenants (name, company_name) values ($1, $2) returning id",
        "デモ株式会社",
        "デモ株式会社",
    )

    password = "demo-password-1234"
    agent_id = await conn.fetchval(
        """
        insert into users (tenant_id, email, display_name, password_hash, role)
        values ($1, $2, $3, $4, 'manager') returning id
        """,
        tenant_id,
        "agent@example.test",
        "山田 太郎",
        hash_password(password),
    )

    await conn.executemany(
        """
        insert into dispositions (tenant_id, code, label, triggers_dnc,
                                  retry_after_minutes, sort_order)
        values ($1, $2, $3, $4, $5, $6)
        """,
        [(tenant_id, *d) for d in DISPOSITIONS],
    )

    list_id = await conn.fetchval(
        "insert into contact_lists (tenant_id, name) values ($1, $2) returning id",
        tenant_id,
        "2026年1月 新規開拓リスト",
    )

    # ★ 番号は E.164 で投入する。表記が混在すると DNC 照合が漏れる。
    #   +81 90-0000-xxxx は日本の「使用されない番号」帯ではないので、
    #   実発信を試すときは自分の検証済み番号に差し替えること
    contacts = [
        ("+819000000001", "株式会社サンプル", "佐藤 一郎"),
        ("+819000000002", "テスト工業株式会社", "鈴木 二郎"),
        ("+819000000003", "サンプル商事株式会社", "高橋 三郎"),
        ("+819000000004", "デモ製作所", "田中 四郎"),
        ("+819000000005", "例示ホールディングス", "渡辺 五郎"),
    ]
    await conn.executemany(
        """
        insert into contacts (tenant_id, list_id, phone_e164, company_name, person_name)
        values ($1, $2, $3, $4, $5)
        """,
        [(tenant_id, list_id, *c) for c in contacts],
    )

    # DNC に 1 件入れておく。関門が効いていることを最初に確認できる
    await conn.execute(
        """
        insert into dnc_entries (tenant_id, phone_e164, reason, source)
        values ($1, '+819000000003', 'refused', 'import')
        """,
        tenant_id,
    )

    print("")
    print("=" * 70)
    print(" デモデータを投入しました")
    print("=" * 70)
    print(f"  テナント : デモ株式会社 ({tenant_id})")
    print(f"  ログイン : agent@example.test / {password}")
    print(f"  リスト   : {list_id}（5 件）")
    print("")
    print("  ★ +819000000003（サンプル商事）は DNC に登録済みです。")
    print("    キューに出てこないこと、直接発信しても 403 になることを")
    print("    最初に確認してください。関門が効いている証拠になります。")
    print("=" * 70)
    print("")


def main() -> None:
    parser = argparse.ArgumentParser(description="スキーマ適用")
    parser.add_argument("--drop", action="store_true", help="既存テーブルを削除してから作る")
    parser.add_argument("--seed", action="store_true", help="デモデータを投入する")
    args = parser.parse_args()
    asyncio.run(migrate(args.drop, args.seed))


if __name__ == "__main__":
    main()
