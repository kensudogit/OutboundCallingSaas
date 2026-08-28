"""通話レコード（原則 2）。

1 本の通話に 3 つの経路が非同期に触りに来る。到着順は保証されない。

    発信 API のレスポンス  … call_sid, contact_id, agent_id
    statusCallback         … call_sid, status, duration, answered_by
    Media Stream           … call_sid, stream_sid

★ 書き込みはすべて upsert。どの経路が最初に着いても行ができ、後続は
  その行を更新する。
★ 状態は「進む方向にしか動かさない」。completed が answered より先に着いても
  巻き戻らない。
"""

from __future__ import annotations

import asyncpg

# 状態は call_status_rank() で単調更新する。時刻は最初に届いた値を残す
# （重複配信で上書きしない）。duration だけは逆で、後から来た確定値を採る。
_UPSERT = """
insert into calls (
  id, tenant_id, contact_id, agent_id, reservation_id,
  provider_call_sid, status, raw_status, answered_by, caller_id,
  started_at, answered_at, ended_at, duration_sec
)
values (
  coalesce($1::uuid, gen_random_uuid()),
  current_tenant_id(),
  $2, $3, $4, $5, $6, $7, $8, $9, $10, $11, $12, $13
)
on conflict (tenant_id, provider_call_sid)
do update set
  status = case
             when call_status_rank(excluded.status) > call_status_rank(calls.status)
             then excluded.status else calls.status
           end,
  raw_status   = coalesce(excluded.raw_status,  calls.raw_status),
  answered_by  = coalesce(excluded.answered_by, calls.answered_by),
  agent_id     = coalesce(calls.agent_id,       excluded.agent_id),
  started_at   = coalesce(calls.started_at,     excluded.started_at),
  answered_at  = coalesce(calls.answered_at,    excluded.answered_at),
  ended_at     = coalesce(calls.ended_at,       excluded.ended_at),
  duration_sec = coalesce(excluded.duration_sec, calls.duration_sec),
  updated_at   = now()
returning *
"""


async def upsert(
    conn: asyncpg.Connection,
    *,
    provider_call_sid: str,
    call_id: str | None = None,
    contact_id: str | None = None,
    agent_id: str | None = None,
    reservation_id: str | None = None,
    status: str = "QUEUED",
    raw_status: str | None = None,
    answered_by: str | None = None,
    caller_id: str | None = None,
    started_at=None,
    answered_at=None,
    ended_at=None,
    duration_sec: int | None = None,
) -> asyncpg.Record:
    return await conn.fetchrow(
        _UPSERT,
        call_id,
        contact_id,
        agent_id,
        reservation_id,
        provider_call_sid,
        status,
        raw_status,
        answered_by,
        caller_id,
        started_at,
        answered_at,
        ended_at,
        duration_sec,
    )


async def upsert_from_callback(
    conn: asyncpg.Connection,
    *,
    provider_call_sid: str,
    call_id: str | None,
    status: str,
    raw_status: str,
    duration_sec: int | None,
    answered_by: str | None,
    timestamps: dict[str, object],
) -> asyncpg.Record:
    """statusCallback からの更新。

    contact_id を渡さないのは、この経路が知らないから。upsert 側で
    coalesce しているので、先に届いても後から埋まる。ただし contact_id は
    NOT NULL なので、statusCallback が「本当に最初」に来ることはない
    （発信 API のレスポンスを先に書いているため）。万一そうなったら
    外部キー違反で落ち、それは検知したい事象なので握り潰さない。
    """
    return await upsert(
        conn,
        call_id=call_id,
        provider_call_sid=provider_call_sid,
        status=status,
        raw_status=raw_status,
        answered_by=answered_by,
        duration_sec=duration_sec,
        **timestamps,  # type: ignore[arg-type]
    )


async def attach_stream(
    conn: asyncpg.Connection, *, provider_call_sid: str, stream_sid: str
) -> None:
    """Media Stream 経路。音声が始まったことを記録する。

    ここも upsert 相当だが、通話行が無い状態で stream だけ来ることは
    設計上ありえない（TwiML を返す時点で行がある）ので update にする。
    0 行更新なら経路の前提が崩れているので、ログに残す。
    """
    updated = await conn.execute(
        "update calls set provider_stream_sid = $2, updated_at = now() "
        "where provider_call_sid = $1",
        provider_call_sid,
        stream_sid,
    )
    if updated.endswith(" 0"):
        from ..logger import logger

        logger.warn(
            "Media Stream に対応する通話行がありません",
            provider_call_sid=provider_call_sid,
            stream_sid=stream_sid,
        )


async def record_uncertain(
    conn: asyncpg.Connection, *, call_id: str, contact_id: str, agent_id: str
) -> None:
    """発信されたか分からない状態を残す（Twilio がタイムアウトした場合）。

    provider_call_sid が無いので、call_id を仮の SID として入れる。
    後から Calls API で照会して突き合わせる運用のための行。
    """
    await conn.execute(
        """
        insert into calls
          (id, tenant_id, contact_id, agent_id, provider_call_sid, status, raw_status)
        values ($1, current_tenant_id(), $2, $3,
                'UNKNOWN:' || $1::text, 'UNKNOWN', 'provider_timeout')
        on conflict (tenant_id, provider_call_sid) do nothing
        """,
        call_id,
        contact_id,
        agent_id,
    )


async def get(conn: asyncpg.Connection, call_id: str) -> asyncpg.Record | None:
    return await conn.fetchrow("select * from calls where id = $1", call_id)


async def get_by_sid(conn: asyncpg.Connection, sid: str) -> asyncpg.Record | None:
    return await conn.fetchrow("select * from calls where provider_call_sid = $1", sid)


async def set_disposition(
    conn: asyncpg.Connection, *, call_id: str, disposition_code: str, note: str | None
) -> asyncpg.Record:
    """結果を登録する。

    ★ 2 回目は 0 行更新になり、呼び出し側が 409 を返せる。
      これがプログレッシブダイヤルの「次の発信」を 1 回に限る土台
      （プロバイダのイベントではなく、この明示的なアクションをトリガーにする）。
    """
    row = await conn.fetchrow(
        """
        update calls c
           set disposition_id = d.id,
               disposition_at = now(),
               note = coalesce($3, c.note),
               updated_at = now()
          from dispositions d
         where c.id = $1
           and d.code = $2
           and d.tenant_id = current_tenant_id()
           and c.disposition_id is null      -- ★ 既に登録済みなら 0 行
        returning c.*, d.code as disposition_code, d.triggers_dnc, d.retry_after_minutes
        """,
        call_id,
        disposition_code,
        note,
    )
    return row


async def record_delivery(
    conn: asyncpg.Connection, *, provider_call_sid: str, event_type: str, payload: str
) -> None:
    """コールバックの受信そのものを記録する。

    処理は upsert で冪等なので重複しても壊れないが、「実際に何回来たか」を
    後から確認できるようにしておくと障害調査が早い。
    """
    await conn.execute(
        "insert into webhook_deliveries (provider_call_sid, event_type, payload) "
        "values ($1, $2, $3::jsonb)",
        provider_call_sid,
        event_type,
        payload,
    )
