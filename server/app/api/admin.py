"""管理画面の API。

★ リストの一括操作は管理者権限に限る。担当者の画面から数万件が動くと
  事故が大きい。

★ 取り込みは「全件検証してから 1 トランザクション」。途中で落ちて
  「1000 件中 380 件だけ入った」状態を作ると、再取り込みで重複するか、
  どこから再開するか分からなくなる。

★ テナント設定には「変更できるもの」と「できないもの」がある。
  DNC の照合を無効にする口は作らない。「一時的に外したい」という要望は
  必ず来るが、外せる作りにした時点で、外したまま運用される日が来る。
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field

from ..db.engine import tenant_tx
from ..domain.phone import parse_contacts_csv, parse_phone_list
from ..logger import logger
from ..models import CallingWindow
from .auth import AuthUser, current_manager

router = APIRouter(prefix="/api/admin", tags=["admin"])

# 1 回の取り込みの上限。これを超えるならファイルを分けてもらう。
# 無制限にすると、1 リクエストが数分掛かって途中で切れる
MAX_IMPORT_ROWS = 50_000


# ---------------------------------------------------------------- 設定


class TenantSettings(BaseModel):
    company_name: str = Field(min_length=1)
    calling_timezone: str
    calling_hours_start: str
    calling_hours_end: str
    calling_weekdays: list[int]
    exclude_holidays: bool
    max_attempts_per_day: int = Field(ge=1, le=10)
    max_attempts_total: int = Field(ge=1, le=50)
    auto_dial_delay_sec: int = Field(ge=0, le=60)
    # ★ 無期限にはできない。上限は DB の check 制約と揃える
    recording_retention_days: int = Field(ge=1, le=3650)
    ai_features_enabled: bool


@router.get("/settings")
async def get_settings(user: AuthUser = Depends(current_manager)):
    async with tenant_tx(user.tenant_id) as conn:
        row = await conn.fetchrow("select * from tenants where id = current_tenant_id()")
    if row is None:
        raise HTTPException(status_code=404, detail="tenant_not_found")

    return {
        "settings": {
            "company_name": row["company_name"],
            "calling_timezone": row["calling_timezone"],
            "calling_hours_start": row["calling_hours_start"].strftime("%H:%M"),
            "calling_hours_end": row["calling_hours_end"].strftime("%H:%M"),
            "calling_weekdays": list(row["calling_weekdays"]),
            "exclude_holidays": row["exclude_holidays"],
            "max_attempts_per_day": row["max_attempts_per_day"],
            "max_attempts_total": row["max_attempts_total"],
            "auto_dial_delay_sec": row["auto_dial_delay_sec"],
            "recording_retention_days": row["recording_retention_days"],
            "ai_features_enabled": row["ai_features_enabled"],
        },
        # ★ 画面に「変えられないもの」を明示する。設定項目が無いことに
        #   気付かず探し回るより、理由付きで見せるほうがよい
        "immutable": [
            {
                "label": "DNC（架電拒否）の照合",
                "value": "常に有効",
                "reason": "無効にできる作りにすると、外したまま運用される日が来るため",
            },
            {
                "label": "録音の保存期間",
                "value": "無期限にはできない（最長 3650 日）",
                "reason": "消す仕組みが無いと、消してよいか判断できない量が溜まるため",
            },
        ],
    }


@router.put("/settings")
async def update_settings(body: TenantSettings, user: AuthUser = Depends(current_manager)):
    if not (1 <= min(body.calling_weekdays, default=1) and max(body.calling_weekdays, default=7) <= 7):
        raise HTTPException(status_code=400, detail="calling_weekdays は 1(月)〜7(日)")
    if not body.calling_weekdays:
        raise HTTPException(status_code=400, detail="架電する曜日を 1 つ以上選んでください")

    try:
        start = _parse_time(body.calling_hours_start)
        end = _parse_time(body.calling_hours_end)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    if start >= end:
        raise HTTPException(status_code=400, detail="開始時刻が終了時刻以降になっています")

    async with tenant_tx(user.tenant_id) as conn:
        await conn.execute(
            """
            update tenants set
              company_name = $1, calling_timezone = $2,
              calling_hours_start = $3, calling_hours_end = $4,
              calling_weekdays = $5, exclude_holidays = $6,
              max_attempts_per_day = $7, max_attempts_total = $8,
              auto_dial_delay_sec = $9, recording_retention_days = $10,
              ai_features_enabled = $11
            where id = current_tenant_id()
            """,
            body.company_name, body.calling_timezone, start, end,
            body.calling_weekdays, body.exclude_holidays,
            body.max_attempts_per_day, body.max_attempts_total,
            body.auto_dial_delay_sec, body.recording_retention_days,
            body.ai_features_enabled,
        )
        await _audit(conn, user, "settings.updated", detail={"company_name": body.company_name})

    logger.info("テナント設定を更新しました", tenant_id=user.tenant_id, actor=user.id)
    return {"status": "ok"}


def _parse_time(text: str):
    from datetime import time

    hh, _, mm = text.partition(":")
    try:
        return time(int(hh), int(mm))
    except ValueError as exc:
        raise ValueError(f"HH:MM 形式で指定してください: {text}") from exc


# ---------------------------------------------------------------- リスト


class CreateList(BaseModel):
    name: str = Field(min_length=1, max_length=200)


class ImportContacts(BaseModel):
    csv: str
    # 検証だけ行って結果を返す。取り込む前に何件弾かれるか見せるため
    dry_run: bool = False


@router.get("/lists")
async def list_lists(user: AuthUser = Depends(current_manager)):
    async with tenant_tx(user.tenant_id) as conn:
        rows = await conn.fetch(
            """
            select l.id, l.name, l.is_active, l.created_at,
                   count(c.*) filter (where c.state = 'ACTIVE')     as active_contacts,
                   count(c.*) filter (where c.state = 'EXHAUSTED')  as exhausted_contacts,
                   count(c.*)                                       as total_contacts
              from contact_lists l
              left join contacts c on c.list_id = l.id
             group by l.id
             order by l.created_at desc
            """
        )
    return [dict(r) | {"id": str(r["id"])} for r in rows]


@router.post("/lists", status_code=201)
async def create_list(body: CreateList, user: AuthUser = Depends(current_manager)):
    async with tenant_tx(user.tenant_id) as conn:
        list_id = await conn.fetchval(
            "insert into contact_lists (tenant_id, name) "
            "values (current_tenant_id(), $1) returning id",
            body.name,
        )
        await _audit(conn, user, "list.created", target_id=list_id, detail={"name": body.name})
    return {"id": str(list_id), "name": body.name}


@router.post("/lists/{list_id}/contacts")
async def import_contacts(
    list_id: str, body: ImportContacts, user: AuthUser = Depends(current_manager)
):
    """CSV から連絡先を取り込む。

    ★ 全件検証してから 1 トランザクションで入れる。1 件でも不正なら
      何も入れない。部分的に入った状態は、再取り込みで重複を生む。
    """
    result = parse_contacts_csv(body.csv)

    if len(result.accepted) > MAX_IMPORT_ROWS:
        raise HTTPException(
            status_code=413,
            detail=f"1 回の取り込みは {MAX_IMPORT_ROWS} 件までです。ファイルを分けてください",
        )

    summary = {
        "accepted": len(result.accepted),
        "rejected": [
            {"line": r.line, "raw": r.raw, "reason": r.reason} for r in result.rejected[:100]
        ],
        "rejected_total": len(result.rejected),
    }

    if body.dry_run:
        return {"status": "dry_run", **summary}

    # ★ 1 件でも弾かれたら取り込まない。「380 件だけ入った」を作らない
    if result.rejected:
        raise HTTPException(
            status_code=422,
            detail={
                "error": "invalid_rows",
                "message": "不正な行があるため取り込みを中止しました",
                **summary,
            },
        )
    if not result.accepted:
        raise HTTPException(status_code=422, detail="取り込める行がありません")

    async with tenant_tx(user.tenant_id) as conn:
        owned = await conn.fetchval("select 1 from contact_lists where id = $1", list_id)
        if not owned:
            raise HTTPException(status_code=404, detail="list_not_found")

        # ★ 既に DNC にある番号は最初から入れない。入れてしまうと、
        #   キューに出ないだけの「永久に消化されない行」が積み上がる
        blocked = await conn.fetch(
            """
            select phone_e164 from dnc_entries
             where phone_e164 = any($1::text[])
               and (tenant_id is null or tenant_id = current_tenant_id())
            """,
            [r.phone_e164 for r in result.accepted],
        )
        blocked_set = {r["phone_e164"] for r in blocked}
        rows = [r for r in result.accepted if r.phone_e164 not in blocked_set]

        inserted = await conn.fetch(
            """
            insert into contacts
              (tenant_id, list_id, phone_e164, company_name, person_name, department)
            select current_tenant_id(), $1, u.phone, u.company, u.person, u.dept
              from unnest($2::text[], $3::text[], $4::text[], $5::text[])
                     as u(phone, company, person, dept)
            returning id
            """,
            list_id,
            [r.phone_e164 for r in rows],
            [r.company_name for r in rows],
            [r.person_name for r in rows],
            [r.department for r in rows],
        )
        await _audit(
            conn, user, "contacts.imported", target_id=list_id,
            detail={"inserted": len(inserted), "skipped_dnc": len(blocked_set)},
        )

    logger.info(
        "連絡先を取り込みました",
        list_id=list_id, inserted=len(inserted), skipped_dnc=len(blocked_set),
    )
    return {
        "status": "ok",
        "inserted": len(inserted),
        "skipped_dnc": len(blocked_set),
        **summary,
    }


# ---------------------------------------------------------------- DNC


class ImportDnc(BaseModel):
    phones: str
    dry_run: bool = False


@router.get("/dnc")
async def list_dnc(limit: int = 100, user: AuthUser = Depends(current_manager)):
    async with tenant_tx(user.tenant_id) as conn:
        total = await conn.fetchval("select count(*) from dnc_entries")
        rows = await conn.fetch(
            "select phone_e164, reason, source, created_at from dnc_entries "
            "order by created_at desc limit $1",
            min(limit, 500),
        )
    return {"total": total, "entries": [dict(r) for r in rows]}


@router.post("/dnc/import")
async def import_dnc(body: ImportDnc, user: AuthUser = Depends(current_manager)):
    """DNC の一括取り込み。

    ★ 移行では**リストより先に DNC を入れる**。順序を逆にすると、その間の
      架電が全部違反になる。画面にもその順序を出している。

    ★ 連絡先の取り込みと違い、1 件不正でも他は入れる。DNC は「入れ過ぎても
      害がない」側なので、全部止めるほうが危険。
    """
    result = parse_phone_list(body.phones)

    summary = {
        "accepted": len(result.accepted),
        "rejected": [
            {"line": r.line, "raw": r.raw, "reason": r.reason} for r in result.rejected[:100]
        ],
        "rejected_total": len(result.rejected),
    }
    if body.dry_run:
        return {"status": "dry_run", **summary}
    if not result.accepted:
        raise HTTPException(status_code=422, detail={"error": "no_valid_rows", **summary})

    async with tenant_tx(user.tenant_id) as conn:
        await conn.executemany(
            """
            insert into dnc_entries (tenant_id, phone_e164, reason, source, created_by)
            values (current_tenant_id(), $1, 'imported', 'import', $2)
            on conflict do nothing
            """,
            [(r.phone_e164, user.id) for r in result.accepted],
        )
        # 取り込んだ番号が既存のリストにあれば、対象から外す
        deactivated = await conn.fetchval(
            """
            with hit as (
              update contacts set state = 'ARCHIVED'
               where phone_e164 = any($1::text[]) and state = 'ACTIVE'
              returning 1
            )
            select count(*) from hit
            """,
            [r.phone_e164 for r in result.accepted],
        )
        await _audit(
            conn, user, "dnc.imported",
            detail={"accepted": len(result.accepted), "archived_contacts": deactivated},
        )

    logger.info(
        "DNC を取り込みました",
        accepted=len(result.accepted), archived_contacts=deactivated,
    )
    return {"status": "ok", "archived_contacts": deactivated, **summary}


# ---------------------------------------------------------------- 監査


@router.get("/audit")
async def audit_logs(limit: int = 100, user: AuthUser = Depends(current_manager)):
    """監査ログ。関門で止まった件数もここから見える。"""
    async with tenant_tx(user.tenant_id) as conn:
        logs = await conn.fetch(
            """
            select a.action, a.target_type, a.target_id, a.detail, a.created_at,
                   u.display_name as actor
              from audit_logs a
              left join users u on u.id = a.actor_id
             order by a.created_at desc limit $1
            """,
            min(limit, 500),
        )
        # ★ 「関門が機能している証跡」。急増は設定ミスかリスト品質の劣化を示す
        blocked = await conn.fetch(
            """
            select reason, count(*) as count
              from call_attempts_blocked
             where created_at >= now() - interval '7 days'
             group by reason order by count desc
            """
        )
    return {
        "logs": [dict(r) | {"target_id": str(r["target_id"]) if r["target_id"] else None}
                 for r in logs],
        "blocked_last_7d": [dict(r) for r in blocked],
    }


async def _audit(conn, user: AuthUser, action: str, *, target_id=None, detail=None) -> None:
    import json

    await conn.execute(
        """
        insert into audit_logs (tenant_id, actor_id, action, target_type, target_id, detail)
        values (current_tenant_id(), $1, $2, $3, $4, $5::jsonb)
        """,
        user.id, action, action.split(".")[0], target_id, json.dumps(detail or {}),
    )


# ---------------------------------------------------------------- 参考


@router.get("/calling-window")
async def calling_window(user: AuthUser = Depends(current_manager)):
    """現在の設定で「今かけられるか」を返す。設定変更の確認用。"""
    from datetime import datetime
    from zoneinfo import ZoneInfo

    from ..dialer.gate import next_open, within_window

    async with tenant_tx(user.tenant_id) as conn:
        row = await conn.fetchrow("select * from tenants where id = current_tenant_id()")
    window = CallingWindow.from_row(row)
    now = datetime.now(ZoneInfo(window.timezone))

    return {
        "now": now.isoformat(),
        "can_call_now": within_window(window, now),
        "next_open": next_open(window, now).isoformat(),
    }
