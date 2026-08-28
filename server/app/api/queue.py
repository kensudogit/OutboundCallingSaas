"""架電キューと発信の API（Phase 3）。

プレビューダイヤルの導線:

    GET  /api/queue/next   予約を取り、連絡先と履歴を返す（RESERVED）
    POST /api/calls        place_call（DIALING）
    POST /api/queue/skip   予約を解放して次へ

★ 予約を取るのは「表示した瞬間」であって「発信ボタンを押した瞬間」ではない。
  押したときに取ると、2 人が同じ相手を見ている状態が起きて片方が徒労になる。
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from ..db.engine import tenant_tx
from ..dialer.dialer import CallBlocked, ProviderUnavailable, dialer
from ..models import AgentState, CallingWindow, Contact
from ..repositories import agents as agents_repo
from ..repositories import reservations as reservations_repo
from .auth import AuthUser, current_user

router = APIRouter(prefix="/api", tags=["queue"])


class NextContact(BaseModel):
    contact_id: str
    phone_e164: str
    company_name: str | None
    person_name: str | None
    reservation_expires_at: str
    previous_calls: list[dict]


class PlaceCallRequest(BaseModel):
    contact_id: str


class SkipRequest(BaseModel):
    contact_id: str


async def _window(conn) -> CallingWindow:
    row = await conn.fetchrow(
        "select * from tenants where id = current_tenant_id()"
    )
    if row is None:
        raise HTTPException(status_code=403, detail="tenant_not_found")
    return CallingWindow.from_row(row)


@router.get("/lists")
async def contact_lists(user: AuthUser = Depends(current_user)):
    """架電リストの一覧。RLS が効いているので自テナントのものだけが返る。"""
    async with tenant_tx(user.tenant_id) as conn:
        rows = await conn.fetch(
            """
            select l.id, l.name,
                   count(c.*) filter (where c.state = 'ACTIVE') as active_contacts
              from contact_lists l
              left join contacts c on c.list_id = l.id
             where l.is_active
             group by l.id, l.name
             order by l.created_at desc
            """
        )
    return [
        {"id": str(r["id"]), "name": r["name"], "active_contacts": r["active_contacts"]}
        for r in rows
    ]


@router.post("/heartbeat", status_code=204)
async def heartbeat(user: AuthUser = Depends(current_user)):
    """担当者の生存通知。

    ★ 途切れると定期ジョブが OFFLINE に落とし、保持中の予約を解放する。
      これが唯一の「担当者が生きている証拠」で、ブラウザの beforeunload には
      頼らない（防げるかもしれない、程度のもの）。
    """
    async with tenant_tx(user.tenant_id) as conn:
        await agents_repo.heartbeat(conn, agent_id=user.id)


@router.get("/queue/next", response_model=NextContact | None)
async def next_contact(list_id: str, user: AuthUser = Depends(current_user)):
    async with tenant_tx(user.tenant_id) as conn:
        window = await _window(conn)
        reservation = await reservations_repo.acquire_next(
            conn,
            list_id=list_id,
            agent_id=user.id,
            max_attempts_total=window.max_attempts_total,
        )
        if reservation is None:
            await agents_repo.set_state(conn, agent_id=user.id, state=AgentState.READY)
            return None

        contact = await conn.fetchrow(
            "select * from contacts where id = $1", reservation["contact_id"]
        )
        history = await conn.fetch(
            """
            select ca.started_at, ca.raw_status, d.label as disposition, ca.note
              from calls ca
              left join dispositions d on d.id = ca.disposition_id
             where ca.contact_id = $1
             order by ca.created_at desc limit 5
            """,
            reservation["contact_id"],
        )
        await agents_repo.set_state(
            conn, agent_id=user.id, state=AgentState.RESERVED, list_id=list_id
        )

    return NextContact(
        contact_id=str(contact["id"]),
        phone_e164=contact["phone_e164"],
        company_name=contact["company_name"],
        person_name=contact["person_name"],
        reservation_expires_at=reservation["expires_at"].isoformat(),
        previous_calls=[dict(r) for r in history],
    )


@router.post("/queue/skip", status_code=204)
async def skip(body: SkipRequest, user: AuthUser = Depends(current_user)):
    """かけたくない相手を処理できる経路。

    これが無いと、担当者は予約を握ったままブラウザを閉じる。
    """
    async with tenant_tx(user.tenant_id) as conn:
        await reservations_repo.release(conn, contact_id=body.contact_id, agent_id=user.id)
        await agents_repo.set_state(conn, agent_id=user.id, state=AgentState.READY)


@router.post("/calls")
async def place_call(body: PlaceCallRequest, user: AuthUser = Depends(current_user)):
    """発信。関門（can_call）を通るのはこの下の dialer.place_call の中。

    ★ ここで先にチェックを書き足さない。書き足した瞬間に、関門が
      2 箇所に分かれる。条件を増やしたいなら gate.py に足す。
    """
    async with tenant_tx(user.tenant_id) as conn:
        window = await _window(conn)
        row = await conn.fetchrow("select * from contacts where id = $1", body.contact_id)
        if row is None:
            raise HTTPException(status_code=404, detail="contact_not_found")

        contact = Contact(
            id=str(row["id"]),
            tenant_id=str(row["tenant_id"]),
            list_id=str(row["list_id"]),
            phone_e164=row["phone_e164"],
            company_name=row["company_name"],
            person_name=row["person_name"],
            timezone=row["timezone"],
            state=row["state"],
            assigned_caller_id=row["assigned_caller_id"],
        )

        try:
            placed = await dialer.place_call(
                conn, contact=contact, agent_id=user.id, window=window
            )
        except CallBlocked as exc:
            # 理由を返すことで UI が出し分けられる。「発信できません」だけだと
            # 担当者は原因が分からず、管理者に聞き、管理者も分からない
            raise HTTPException(
                status_code=403,
                detail={
                    "error": "call_blocked",
                    "reason": str(exc.decision.reason),
                    "detail": exc.decision.detail,
                    "retry_after": exc.decision.retry_after.isoformat()
                    if exc.decision.retry_after
                    else None,
                },
            ) from exc
        except ProviderUnavailable as exc:
            # uncertain なら発信されたかもしれない。UI には「確認中」と出させる
            raise HTTPException(
                status_code=503 if exc.uncertain else 502,
                detail={
                    "error": "provider_unavailable",
                    "uncertain": exc.uncertain,
                },
            ) from exc

        await agents_repo.set_state(conn, agent_id=user.id, state=AgentState.DIALING)

    return {"call_id": placed.id, "status": placed.status}
