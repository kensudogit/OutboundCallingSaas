"""担当者のセッションと状態機械。

    OFFLINE ──ログイン──► READY ──予約──► RESERVED ──発信──► DIALING
                            ▲                                    │応答
                            │                                    ▼
                            └────結果登録──── WRAP_UP ◄──終了── TALKING

★ ハートビートが途切れたら OFFLINE に落とし、保持中の予約も解放する
  （repositories/reservations.expire_for_dead_agents）。これをやらないと、
  帰宅した担当者の予約が翌朝までリストを塞ぐ。
"""

from __future__ import annotations

import asyncpg

from ..models import AgentState, DialMode


async def get_session(conn: asyncpg.Connection, *, agent_id: str) -> asyncpg.Record | None:
    return await conn.fetchrow("select * from agent_sessions where agent_id = $1", agent_id)


async def set_state(
    conn: asyncpg.Connection,
    *,
    agent_id: str,
    state: AgentState,
    list_id: str | None = None,
    mode: DialMode | None = None,
) -> None:
    await conn.execute(
        """
        insert into agent_sessions (agent_id, tenant_id, state, mode, list_id, updated_at)
        values ($1, current_tenant_id(), $2,
                coalesce($4, 'PREVIEW'), $3, now())
        on conflict (agent_id) do update set
          state      = excluded.state,
          mode       = coalesce($4, agent_sessions.mode),
          list_id    = coalesce($3, agent_sessions.list_id),
          updated_at = now()
        """,
        agent_id,
        str(state),
        list_id,
        str(mode) if mode else None,
    )


async def heartbeat(conn: asyncpg.Connection, *, agent_id: str) -> None:
    """定期的に呼ばれる生存通知。

    ★ これが唯一の「担当者が生きている証拠」。ブラウザの beforeunload には
      頼らない（防げるかもしれない、程度のもの）。
    """
    await conn.execute(
        "update agent_sessions set updated_at = now() where agent_id = $1", agent_id
    )


async def set_mode(
    conn: asyncpg.Connection, *, agent_id: str, mode: DialMode, list_id: str
) -> None:
    await conn.execute(
        """
        insert into agent_sessions (agent_id, tenant_id, state, mode, list_id)
        values ($1, current_tenant_id(), 'READY', $2, $3)
        on conflict (agent_id) do update set
          mode = excluded.mode, list_id = excluded.list_id,
          stop_requested = false, updated_at = now()
        """,
        agent_id,
        str(mode),
        list_id,
    )


async def request_stop(conn: asyncpg.Connection, *, agent_id: str) -> None:
    """プログレッシブの自動発信を止める。

    次の結果登録の時点で次を取りに行かなくなる。通話中に押しても
    その通話は最後まで続く。
    """
    await conn.execute(
        "update agent_sessions set stop_requested = true, updated_at = now() "
        "where agent_id = $1",
        agent_id,
    )
