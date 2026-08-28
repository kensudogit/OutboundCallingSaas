"""DNC — 最も重要なテーブル（Phase 7 の中核）。

他のテーブルと扱いが違う点が 3 つある。

★ 削除させない。マイグレーションで app_user から UPDATE / DELETE を落としてある。
  誤って消すと再架電に直結し、消えた事実も残らない。
★ 重複登録を許す。担当者が「拒否」を 2 回押しても on conflict do nothing で通す。
  エラーにすると、UI がエラーを握り潰したときに登録されない。
★ テナント跨ぎの共有は既定で行わない。A 社への勧誘を断った人が B 社の対象から
  外れる必要はなく、勝手に共有すると個人情報の第三者提供になる。
"""

from __future__ import annotations

import asyncpg


async def contains(conn: asyncpg.Connection, phone_e164: str) -> bool:
    """発信の関門から呼ばれる照合。完全一致で引く。

    ★ だから contacts.phone_e164 は E.164 に正規化して投入する。
      '090-1234-5678' と '+819012345678' が混在すると照合が漏れ、
      漏れた結果が「断った相手への再架電」になる。
    """
    return bool(
        await conn.fetchval(
            """
            select 1 from dnc_entries
             where phone_e164 = $1
               and (tenant_id is null
                    or tenant_id = current_tenant_id())
             limit 1
            """,
            phone_e164,
        )
    )


async def add(
    conn: asyncpg.Connection,
    *,
    phone_e164: str,
    reason: str,
    source: str,
    source_call_id: str | None = None,
    created_by: str | None = None,
) -> None:
    await conn.execute(
        """
        insert into dnc_entries
          (tenant_id, phone_e164, reason, source, source_call_id, created_by)
        values (current_tenant_id(), $1, $2, $3, $4, $5)
        on conflict do nothing
        """,
        phone_e164,
        reason,
        source,
        source_call_id,
        created_by,
    )


async def import_many(
    conn: asyncpg.Connection, phones: list[str], *, created_by: str | None = None
) -> int:
    """既存システムからの移行。

    ★ 移行手順では、リストより先に DNC を入れる。順序を逆にすると、
      その間の架電が全部違反になる。
    """
    result = await conn.executemany(
        """
        insert into dnc_entries (tenant_id, phone_e164, reason, source, created_by)
        values (current_tenant_id(), $1, 'imported', 'import', $2)
        on conflict do nothing
        """,
        [(p, created_by) for p in phones],
    )
    return len(phones) if result is None else len(phones)


async def find_by_phone(conn: asyncpg.Connection, phone_e164: str) -> list[asyncpg.Record]:
    """本人からの開示請求に応じるための経路（個人情報保護法）。

    電話番号を起点に辿れる設計になっているかを、この関数が書けるかで確認できる。
    """
    return await conn.fetch(
        "select * from dnc_entries where phone_e164 = $1 order by created_at", phone_e164
    )
