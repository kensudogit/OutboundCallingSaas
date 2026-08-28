"""予約 — 二重発信を DB で止める。

「未架電の先頭 1 件を取る」を素直に書くと、2 人の担当者が同時に押したときに
同じ行を取る。アプリのロックでは複数インスタンス構成で守れないので、
DB の行ロック（FOR UPDATE SKIP LOCKED）で直列化する。

★ 予約を取ってから解放するまでが 1 件の架電。途中でブラウザが落ちても
  expires_at で対象に戻る。解放をアプリの後処理だけに任せない——後処理は落ちる。
"""

from __future__ import annotations

import asyncpg

from ..config import RESERVATION_TTL_SECONDS

# 同じ担当者がスキップした相手を再び出すまでの時間。
# 短すぎるとスキップが効かず、長すぎるとリストが痩せる
SKIP_COOLDOWN_SECONDS = 12 * 3600

# DNC と保持中の予約を除外して 1 件取り、同じトランザクションで予約を作る。
# ★ ここの DNC 除外は候補を絞るためのもので、発信直前の関門を置き換えない。
#   予約から発信までの間に拒否の申し出が入ることがあるので、チェックは 2 回通す。
_ACQUIRE = """
with picked as (
  select c.id, c.phone_e164
    from contacts c
   where c.list_id = $1
     and c.state = 'ACTIVE'
     and not exists (
       select 1 from dnc_entries d
        where d.phone_e164 = c.phone_e164
          and (d.tenant_id is null
               or d.tenant_id = current_tenant_id())
     )
     and not exists (
       select 1 from call_reservations r
        where r.contact_id = c.id and r.state = 'HELD' and r.expires_at > now()
     )
     -- ★ 同じ担当者が直前にスキップした相手は、しばらく出さない。
     --   これが無いとスキップした瞬間に同じ相手が戻ってきて、スキップが
     --   機能しない（担当者は前に進めず、予約を握ったままブラウザを閉じる）。
     --   別の担当者には出る。特定の相手が誰からも架電されなくなるのを避けるため
     and not exists (
       select 1 from call_reservations r2
        where r2.contact_id = c.id
          and r2.agent_id = $2
          and r2.state = 'RELEASED'
          and r2.created_at > now() - make_interval(secs => $5)
     )
     and (
       select count(*) from calls ca where ca.contact_id = c.id
     ) < $3
   order by c.priority desc, c.created_at
   limit 1
   for update of c skip locked          -- ★ ここが直列化の要
)
insert into call_reservations (tenant_id, contact_id, agent_id, expires_at)
select current_tenant_id(),
       picked.id, $2, now() + make_interval(secs => $4)
  from picked
returning *
"""


async def acquire_next(
    conn: asyncpg.Connection,
    *,
    list_id: str,
    agent_id: str,
    max_attempts_total: int,
    ttl_seconds: int = RESERVATION_TTL_SECONDS,
    skip_cooldown_seconds: int = SKIP_COOLDOWN_SECONDS,
) -> asyncpg.Record | None:
    """次の架電対象を排他的に確保する。取れなければ None（リストが尽きた）。"""
    return await conn.fetchrow(
        _ACQUIRE, list_id, agent_id, max_attempts_total, ttl_seconds, skip_cooldown_seconds
    )


async def release(conn: asyncpg.Connection, *, contact_id: str, agent_id: str) -> None:
    """スキップ。担当者が「かけたくない相手」を処理できる経路。

    これが無いと、担当者は予約を握ったままブラウザを閉じ、リストが少しずつ枯れる。
    """
    await conn.execute(
        "update call_reservations set state = 'RELEASED' "
        "where contact_id = $1 and agent_id = $2 and state = 'HELD'",
        contact_id,
        agent_id,
    )


async def consume(conn: asyncpg.Connection, *, contact_id: str) -> None:
    """架電が完結した。結果登録のタイミングで呼ぶ。"""
    await conn.execute(
        "update call_reservations set state = 'CONSUMED' "
        "where contact_id = $1 and state = 'HELD'",
        contact_id,
    )


async def held_by(
    conn: asyncpg.Connection, *, contact_id: str, agent_id: str
) -> bool:
    return bool(
        await conn.fetchval(
            "select 1 from call_reservations "
            "where contact_id = $1 and agent_id = $2 and state = 'HELD' and expires_at > now()",
            contact_id,
            agent_id,
        )
    )


async def expire_stale(conn: asyncpg.Connection) -> int:
    """期限切れの予約を戻す（定期ジョブ）。

    ★ アプリの後処理に頼らない。担当者のブラウザがクラッシュしても、
      席を立ったままでも、ここで必ず戻る。
    """
    result = await conn.execute(
        "update call_reservations set state = 'EXPIRED' "
        "where state = 'HELD' and expires_at <= now()"
    )
    return int(result.rsplit(" ", 1)[-1])


async def expire_for_dead_agents(conn: asyncpg.Connection, timeout_seconds: int) -> int:
    """ハートビートが途切れた担当者を落とし、保持中の予約を戻す。

    これをやらないと、帰宅した担当者の予約が翌朝までリストを塞ぐ。
    """
    result = await conn.execute(
        """
        with dead as (
          update agent_sessions set state = 'OFFLINE'
           where state <> 'OFFLINE'
             and updated_at < now() - make_interval(secs => $1)
          returning agent_id
        )
        update call_reservations r set state = 'EXPIRED'
          from dead
         where r.agent_id = dead.agent_id and r.state = 'HELD'
        """,
        timeout_seconds,
    )
    return int(result.rsplit(" ", 1)[-1])
