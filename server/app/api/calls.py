"""通話の結果登録と、プログレッシブの次の一手（Phase 3）。

★ プログレッシブダイヤルの発信トリガーは「担当者の結果登録」に置く。
  Twilio の completed イベントに置くと、at-least-once 配信の重複がそのまま
  二重発信になる。イベントは重複するが、結果登録は 1 回しか成功しない
  （disposition_id が既に埋まっていたら 0 行更新 → 409）。
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from ..config import AUTO_DIAL_DELAY_SEC
from ..db.engine import tenant_tx
from ..logger import logger
from ..models import AgentState, CallingWindow, DialMode
from ..repositories import agents as agents_repo
from ..repositories import calls as calls_repo
from ..repositories import dnc as dnc_repo
from ..repositories import reservations as reservations_repo
from .auth import AuthUser, current_user

router = APIRouter(prefix="/api", tags=["calls"])


class DispositionRequest(BaseModel):
    disposition_code: str
    note: str | None = None


@router.post("/calls/{call_id}/disposition")
async def register_disposition(
    call_id: str, body: DispositionRequest, user: AuthUser = Depends(current_user)
):
    async with tenant_tx(user.tenant_id) as conn:
        row = await calls_repo.set_disposition(
            conn, call_id=call_id, disposition_code=body.disposition_code, note=body.note
        )
        if row is None:
            # 既に登録済み、または未知の結果コード。ここで 409 を返すことが
            # 「次の発信が 1 回しか起きない」ことの担保になっている
            existing = await calls_repo.get(conn, call_id)
            raise HTTPException(
                status_code=409 if existing else 404,
                detail="already_dispositioned" if existing else "call_not_found",
            )

        # ★ 拒否系の結果は DNC 登録と不可分にする。担当者の追加操作を挟むと
        #   通話直後の忙しさで忘れられ、それが再架電に直結する
        if row["triggers_dnc"]:
            await dnc_repo.add(
                conn,
                phone_e164=await conn.fetchval(
                    "select phone_e164 from contacts where id = $1", row["contact_id"]
                ),
                reason="refused",
                source="agent",
                source_call_id=call_id,
                created_by=user.id,
            )
            logger.info("拒否によりDNCへ登録しました", call_id=call_id, agent_id=user.id)

        await reservations_repo.consume(conn, contact_id=str(row["contact_id"]))

        session = await agents_repo.get_session(conn, agent_id=user.id)
        await agents_repo.set_state(conn, agent_id=user.id, state=AgentState.READY)

        # プレビューならここで終わり。担当者が自分で次を取りに行く
        if session is None or session["mode"] != DialMode.PROGRESSIVE or session["stop_requested"]:
            return {"status": "ok", "next": None}

        tenant = await conn.fetchrow(
            "select * from tenants where id = current_tenant_id()"
        )
        window = CallingWindow.from_row(tenant)
        nxt = await reservations_repo.acquire_next(
            conn,
            list_id=str(session["list_id"]),
            agent_id=user.id,
            max_attempts_total=window.max_attempts_total,
        )
        if nxt is None:
            return {"status": "ok", "next": None, "queue_empty": True}

        await agents_repo.set_state(conn, agent_id=user.id, state=AgentState.RESERVED)
        contact = await conn.fetchrow(
            "select id, phone_e164, company_name, person_name from contacts where id = $1",
            nxt["contact_id"],
        )

    # ★ 自動発信には間隔を挟み、キャンセルできるようにする。
    #   結果登録の直後に鳴らすと、担当者が息を継げない
    return {
        "status": "ok",
        "next": {
            "contact_id": str(contact["id"]),
            "phone_e164": contact["phone_e164"],
            "company_name": contact["company_name"],
            "person_name": contact["person_name"],
            "delay_sec": AUTO_DIAL_DELAY_SEC,
        },
    }


@router.get("/calls/{call_id}")
async def get_call(call_id: str, user: AuthUser = Depends(current_user)):
    async with tenant_tx(user.tenant_id) as conn:
        row = await calls_repo.get(conn, call_id)
        if row is None:
            raise HTTPException(status_code=404, detail="call_not_found")
        segments = await conn.fetch(
            "select track, source, started_ms, text from transcript_segments "
            "where call_id = $1 order by source, started_ms",
            call_id,
        )
    return {"call": dict(row), "transcript": [dict(s) for s in segments]}


@router.get("/calls/{call_id}/recording")
async def get_recording(call_id: str, user: AuthUser = Depends(current_user)):
    """録音を聴くための一時 URL を発行する。

    ★ Twilio の Recording URL をそのままフロントに渡さない。自社の
      アクセス制御を通さずに配られる URL が存在するのは良くない。
    ★ 監査ログを必ず残す（誰がどの通話を聴いたか）。
    """
    async with tenant_tx(user.tenant_id) as conn:
        rec = await conn.fetchrow(
            "select * from recordings where call_id = $1 and deleted_at is null", call_id
        )
        if rec is None:
            raise HTTPException(status_code=404, detail="recording_not_found")

        await conn.execute(
            """
            insert into audit_logs (tenant_id, actor_id, action, target_type, target_id)
            values (current_tenant_id(), $1,
                    'recording.listen', 'call', $2)
            """,
            user.id,
            call_id,
        )

    # 自社ストレージへのコピーが済んでいればそちらの署名付き URL を返す。
    # 未実装の間は 501 を返し、Twilio の URL を素通しさせない
    if not rec["storage_key"]:
        raise HTTPException(
            status_code=501,
            detail="recording_storage_not_configured",
        )
    from ..storage import presigned_url

    return {"url": presigned_url(rec["storage_key"], expires_in=300)}
