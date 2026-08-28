"""KPI（Phase 8）。

★ 数と質を必ず並べて返す。「架電数」だけを返す API を作ると、画面が
  それだけを大きく出す。指標の設計が組織の行動を変えるので、
  API の形の時点で並べておく。

★ 割合はサーバーで割らず、分子と分母を返す。分母 0 の扱い（0% と出すか
  「—」にするか）を SQL に埋めると変えにくく、加重平均も取り直せない。
"""

from __future__ import annotations

from datetime import date, timedelta

from fastapi import APIRouter, Depends

from ..db.engine import tenant_tx
from .auth import AuthUser, current_manager, current_user

router = APIRouter(prefix="/api", tags=["stats"])

_AGENT_DAILY = """
select
  count(*)                                                     as attempts,
  count(*) filter (where answered_at is not null)              as connected,
  count(*) filter (where answered_by = 'human')                as human_connected,
  count(*) filter (where d.code = 'appointment')               as appointments,
  count(*) filter (where d.triggers_dnc)                       as refusals,
  -- ★ 応答した通話だけの平均。不応答を混ぜると意味のない数字になる
  coalesce(avg(c.duration_sec) filter (where c.answered_at is not null), 0)::int
                                                               as avg_talk_sec
from calls c
left join dispositions d on d.id = c.disposition_id
where c.agent_id = $1
  and c.started_at >= $2 and c.started_at < $3
"""


@router.get("/stats/me")
async def my_stats(user: AuthUser = Depends(current_user)):
    """担当者向け。★ 自分の数字だけ。

    他人との比較を担当者画面に置くと、良い効果より悪い効果が大きい。
    目標に対する進捗として見せる。
    """
    today = date.today()
    async with tenant_tx(user.tenant_id) as conn:
        row = await conn.fetchrow(_AGENT_DAILY, user.id, today, today + timedelta(days=1))
        remaining = await conn.fetchval(
            """
            select count(*) from contacts c
             where c.state = 'ACTIVE'
               and not exists (select 1 from dnc_entries d
                                where d.phone_e164 = c.phone_e164
                                  and (d.tenant_id is null
                                       or d.tenant_id = current_tenant_id()))
            """
        )
    return {**dict(row), "remaining_contacts": remaining}


@router.get("/stats/team")
async def team_stats(days: int = 7, user: AuthUser = Depends(current_manager)):
    """マネージャー向け。数と質を並べる。

    ★ ランキング表示は既定でオフ。ここは順序を付けずに返し、
      並べ替えるかどうかは画面側とテナント設定に委ねる。
    """
    since = date.today() - timedelta(days=days)
    async with tenant_tx(user.tenant_id) as conn:
        rows = await conn.fetch(
            """
            select
              u.id as agent_id, u.display_name,
              count(c.*)                                        as attempts,
              count(c.*) filter (where c.answered_by = 'human')  as human_connected,
              count(c.*) filter (where d.code = 'appointment')   as appointments,
              count(c.*) filter (where d.triggers_dnc)           as refusals,
              coalesce(avg(c.duration_sec)
                       filter (where c.answered_at is not null), 0)::int as avg_talk_sec,
              coalesce(avg(m.agent_talk_ms::numeric
                           / nullif(m.agent_talk_ms + m.contact_talk_ms, 0)), 0) as talk_ratio
            from users u
            left join calls c on c.agent_id = u.id and c.started_at >= $1
            left join dispositions d on d.id = c.disposition_id
            left join call_conversation_metrics m on m.call_id = c.id
            where u.is_active
            group by u.id, u.display_name
            order by u.display_name
            """,
            since,
        )
    return [dict(r) for r in rows]


@router.get("/stats/silence")
async def silence_check(user: AuthUser = Depends(current_manager)):
    """★ 「数字が出ないこと」への監視。

    エラー率だけを見ていると、認証情報の失効やキューの枯渇で架電数が 0 に
    なった障害を見逃す。絶対値の閾値だと営業日と休日で誤検知するので、
    前週同時刻と比べる。
    """
    async with tenant_tx(user.tenant_id) as conn:
        row = await conn.fetchrow(
            """
            with recent as (
              select count(*)::numeric as n from calls
               where started_at >= now() - interval '1 hour'
            ),
            baseline as (
              select avg(w.cnt) as n
                from generate_series(1, 4) as g(week)
                cross join lateral (
                  select count(*)::numeric as cnt from calls
                   where started_at >= now() - interval '1 hour'
                                     - (g.week * interval '7 days')
                     and started_at <  now() - (g.week * interval '7 days')
                ) as w
            ),
            blocked as (
              select count(*)::int as n from call_attempts_blocked
               where created_at >= now() - interval '1 hour'
            )
            select recent.n as current_hour, baseline.n as typical_hour,
                   blocked.n as blocked_last_hour,
                   -- 基準が小さいうちは鳴らさない（深夜・立ち上げ直後の誤検知を避ける）
                   (baseline.n >= 10 and recent.n < baseline.n * 0.5) as should_alert
              from recent, baseline, blocked
            """
        )
    return dict(row)
