"""デモデータの投入。

    python -m app.db.seed
    python -m app.db.seed --reset   # 既存のテナントを消してから入れる

★ スキーマの適用（alembic）とは分けてある。マイグレーションはスキーマの
  変更だけを扱い、データの投入は別物として扱う。混ぜると、本番で
  「マイグレーションを流したら既知パスワードのユーザーができた」が起きる。

★ 既知のパスワードを持つユーザーが作られるので、APP_ENV=production では拒否する。
"""

from __future__ import annotations

import argparse
import asyncio
import sys

import asyncpg

from ..config import APP_ENV, DATABASE_MIGRATOR_URL, DATABASE_URL
from ..security import hash_password

DEMO_TENANT = "デモ株式会社"
DEMO_EMAIL = "agent@example.test"
DEMO_PASSWORD = "demo-password-1234"

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

# ★ 番号は E.164 で投入する。表記が混在すると DNC 照合が漏れる。
#   実発信を試すときは自分の検証済み番号に差し替えること
CONTACTS = [
    ("+819000000001", "株式会社サンプル", "佐藤 一郎"),
    ("+819000000002", "テスト工業株式会社", "鈴木 二郎"),
    ("+819000000003", "サンプル商事株式会社", "高橋 三郎"),
    ("+819000000004", "デモ製作所", "田中 四郎"),
    ("+819000000005", "例示ホールディングス", "渡辺 五郎"),
]


async def _connect() -> asyncpg.Connection:
    url = (DATABASE_MIGRATOR_URL or DATABASE_URL).replace(
        "postgresql+asyncpg://", "postgresql://"
    )
    return await asyncpg.connect(url)


async def seed(reset: bool = False) -> None:
    if APP_ENV == "production":
        print("APP_ENV=production ではデモデータを投入できません", file=sys.stderr)
        raise SystemExit(2)

    conn = await _connect()
    try:
        exists = await conn.fetchval("select 1 from tenants where name = $1", DEMO_TENANT)
        if exists and not reset:
            print(
                f"「{DEMO_TENANT}」は既に存在します。入れ直すには --reset を付けてください",
                file=sys.stderr,
            )
            raise SystemExit(1)
        if exists:
            await conn.execute("delete from tenants where name = $1", DEMO_TENANT)

        async with conn.transaction():
            tenant_id = await conn.fetchval(
                "insert into tenants (name, company_name) values ($1, $1) returning id",
                DEMO_TENANT,
            )
            await conn.fetchval(
                """
                insert into users (tenant_id, email, display_name, password_hash, role)
                values ($1, $2, $3, $4, 'manager') returning id
                """,
                tenant_id, DEMO_EMAIL, "山田 太郎", hash_password(DEMO_PASSWORD),
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
                tenant_id, "2026年1月 新規開拓リスト",
            )
            # ★ DNC を先に入れる。リストを先に入れると、その間の架電が違反になる。
            #   移行手順としても同じ順序を守る
            await conn.execute(
                """
                insert into dnc_entries (tenant_id, phone_e164, reason, source)
                values ($1, '+819000000003', 'refused', 'import')
                """,
                tenant_id,
            )
            await conn.executemany(
                """
                insert into contacts (tenant_id, list_id, phone_e164, company_name, person_name)
                values ($1, $2, $3, $4, $5)
                """,
                [(tenant_id, list_id, *c) for c in CONTACTS],
            )
    finally:
        await conn.close()

    print("")
    print("=" * 70)
    print(" デモデータを投入しました")
    print("=" * 70)
    print(f"  テナント : {DEMO_TENANT} ({tenant_id})")
    print(f"  ログイン : {DEMO_EMAIL} / {DEMO_PASSWORD}")
    print(f"  リスト   : {list_id}（{len(CONTACTS)} 件）")
    print("")
    print("  ★ +819000000003（サンプル商事）は DNC に登録済みです。")
    print("    キューに出てこないこと、直接発信しても 403 になることを")
    print("    最初に確認してください。関門が効いている証拠になります。")
    print("=" * 70)
    print("")


def main() -> None:
    parser = argparse.ArgumentParser(description="デモデータの投入")
    parser.add_argument(
        "--reset", action="store_true", help="同名のテナントを消してから入れ直す"
    )
    args = parser.parse_args()
    asyncio.run(seed(args.reset))


if __name__ == "__main__":
    main()
