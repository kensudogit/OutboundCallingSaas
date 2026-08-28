"""定期ジョブ。

    python -m app.jobs.maintenance            # 一巡だけ実行
    python -m app.jobs.maintenance --loop 60  # 60 秒ごとに繰り返す

★ どれもアプリの後処理に頼らないための保険。後処理は落ちる。
  予約の解放も録音の削除も、ここで必ず動く形にしておく。
"""

from __future__ import annotations

import argparse
import asyncio

from ..config import AGENT_HEARTBEAT_TIMEOUT_SECONDS
from ..db.engine import admin_tx, close_pool, init_pool
from ..logger import logger
from ..repositories import reservations as reservations_repo


async def expire_reservations() -> dict[str, int]:
    """期限切れの予約と、落ちた担当者の予約を戻す。

    これが動かないと、担当者のブラウザがクラッシュするたびにリストの
    連絡先が 1 件ずつ永久にロックされ、少しずつ枯れていく。
    """
    async with admin_tx() as conn:
        expired = await reservations_repo.expire_stale(conn)
        dead = await reservations_repo.expire_for_dead_agents(
            conn, AGENT_HEARTBEAT_TIMEOUT_SECONDS
        )
    if expired or dead:
        logger.info("予約を解放しました", expired=expired, dead_agents=dead)
    return {"expired": expired, "dead_agents": dead}


async def purge_recordings() -> int:
    """保存期間を過ぎた録音を消す（原則 5）。

    ★ 自社 DB のレコードだけ消してプロバイダに実体が残るのが最も多い漏れ。
      ストレージのオブジェクトを消してから行に deleted_at を立てる。
    """
    from .. import storage

    async with admin_tx() as conn:
        rows = await conn.fetch(
            "select id, storage_key from recordings "
            "where expires_at <= now() and deleted_at is null limit 500"
        )
        deleted = 0
        for row in rows:
            if row["storage_key"] and storage.is_configured():
                try:
                    storage.delete_object(row["storage_key"])
                except Exception as exc:  # noqa: BLE001
                    # 消せなかったものは行を残す。次回また拾う
                    logger.error("録音の削除に失敗しました", id=str(row["id"]), err=str(exc))
                    continue
            await conn.execute(
                "update recordings set deleted_at = now() where id = $1", row["id"]
            )
            deleted += 1

    if deleted:
        logger.info("保存期間を過ぎた録音を削除しました", count=deleted)
    return deleted


async def rollup_daily_stats() -> int:
    """当日と前日の KPI を再計算する。

    前日も入れるのは、深夜をまたいだ通話と遅れて届く statusCallback を
    拾うため。当日分はダッシュボードが calls を直接見るので、ここは
    「過去を固める」役割。
    """
    async with admin_tx() as conn:
        result = await conn.execute(
            """
            insert into daily_agent_stats
              (tenant_id, agent_id, day, attempts, connected, human_connected,
               appointments, refusals, talk_sec_total)
            select c.tenant_id, c.agent_id,
                   (c.started_at at time zone t.calling_timezone)::date,
                   count(*),
                   count(*) filter (where c.answered_at is not null),
                   count(*) filter (where c.answered_by = 'human'),
                   count(*) filter (where d.code = 'appointment'),
                   count(*) filter (where d.triggers_dnc),
                   coalesce(sum(c.duration_sec), 0)
              from calls c
              join tenants t on t.id = c.tenant_id
              left join dispositions d on d.id = c.disposition_id
             where c.started_at >= current_date - interval '1 day'
               and c.agent_id is not null
             group by c.tenant_id, c.agent_id, 3
            on conflict (tenant_id, agent_id, day) do update set
              attempts = excluded.attempts,
              connected = excluded.connected,
              human_connected = excluded.human_connected,
              appointments = excluded.appointments,
              refusals = excluded.refusals,
              talk_sec_total = excluded.talk_sec_total,
              updated_at = now()
            """
        )
    return int(result.rsplit(" ", 1)[-1])


async def run_once() -> None:
    await expire_reservations()
    await purge_recordings()
    await rollup_daily_stats()


async def main() -> None:
    parser = argparse.ArgumentParser(description="定期ジョブ")
    parser.add_argument("--loop", type=int, default=0, help="秒間隔で繰り返す（0 なら 1 回）")
    args = parser.parse_args()

    await init_pool()
    try:
        if args.loop <= 0:
            await run_once()
            return
        while True:
            try:
                await run_once()
            except Exception as exc:  # noqa: BLE001
                # 1 回失敗してもループは止めない。止まると予約が戻らなくなる
                logger.error("定期ジョブが失敗しました", err=str(exc))
            await asyncio.sleep(args.loop)
    finally:
        await close_pool()


if __name__ == "__main__":
    asyncio.run(main())
