"""発信の関門（原則 1）。

★ この関数を通らずに Twilio を呼ぶ経路を作らない。
  DNC・架電可能時間帯・重複・上限・予約・番号の妥当性をここに集約する。

★ 条件をここに足すのは自由だが、ここ以外に足すのは禁止。分散させた瞬間に
  「片方だけ通る経路」が生まれ、それは必ず本番で見つかる。

判定順に意味がある。監査ログに残る理由が変わるため。DNC 登録者に時間外へ
架電しようとしたとき、記録されるべき理由は dnc であって outside_hours ではない。
前者は「かけてはいけない相手」、後者は「今はかけられない」で意味が違う。
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, timedelta
from enum import StrEnum
from zoneinfo import ZoneInfo

import asyncpg

from ..config import DIALING_ENABLED, RECENT_CALL_WINDOW_SECONDS
from ..models import CallingWindow, Contact


class BlockReason(StrEnum):
    """拒否理由。UI の出し分けと監視の両方で使う。

    call_attempts_blocked に記録され、「関門が機能している証跡」になる。
    急増したときはリストの質・時間帯設定・DNC の積み上がりのどれかが動いている。
    """

    DIALING_DISABLED = "dialing_disabled"
    DNC = "dnc"
    OUTSIDE_HOURS = "outside_hours"
    RECENTLY_CALLED = "recently_called"
    DAILY_LIMIT = "daily_limit"
    TOTAL_LIMIT = "total_limit"
    NO_RESERVATION = "no_reservation"
    INVALID_NUMBER = "invalid_number"
    CONTACT_INACTIVE = "contact_inactive"


_MESSAGES: dict[BlockReason, str] = {
    BlockReason.DIALING_DISABLED: "現在発信を停止しています",
    BlockReason.DNC: "架電拒否の登録があります",
    BlockReason.OUTSIDE_HOURS: "架電可能時間外です",
    BlockReason.RECENTLY_CALLED: "直前に発信済みです",
    BlockReason.DAILY_LIMIT: "当日の架電回数の上限に達しています",
    BlockReason.TOTAL_LIMIT: "この連絡先への架電回数の上限に達しています",
    BlockReason.NO_RESERVATION: "この連絡先の予約がありません",
    BlockReason.INVALID_NUMBER: "電話番号が E.164 形式ではありません",
    BlockReason.CONTACT_INACTIVE: "この連絡先は架電対象外です",
}


@dataclass(frozen=True)
class CallDecision:
    """関門の判定結果。

    ★ bool を返さないのは、UI が理由を出し分けられるようにするため。
      「発信できません」としか出せないと、担当者は原因が分からず、
      管理者に聞き、管理者も分からない。
    """

    allowed: bool
    reason: BlockReason | None = None
    detail: str | None = None
    retry_after: datetime | None = None

    @classmethod
    def deny(
        cls, reason: BlockReason, detail: str | None = None, retry_after: datetime | None = None
    ) -> CallDecision:
        return cls(False, reason, detail or _MESSAGES[reason], retry_after)

    @classmethod
    def allow(cls) -> CallDecision:
        return cls(True)


def is_holiday(day: date) -> bool:
    """日本の祝日判定。jpholiday が無い環境では常に False を返す。

    祝日にかけると苦情になりやすく、苦情は DNC 登録につながってリストが痩せる。
    本番では必ず jpholiday を入れる。
    """
    try:
        import jpholiday
    except ImportError:
        return False
    return bool(jpholiday.is_holiday(day))


def next_open(window: CallingWindow, now: datetime) -> datetime:
    """次に架電できる時刻。UI が「18 時以降に再開できます」と言えるようにする。"""
    tz = ZoneInfo(window.timezone)
    candidate = now.astimezone(tz)

    if candidate.time() < window.start and _is_open_day(window, candidate.date()):
        return candidate.replace(
            hour=window.start.hour, minute=window.start.minute, second=0, microsecond=0
        )

    # 翌日以降で最初に開く日を探す。祝日が連続しても 14 日以内には見つかる
    for offset in range(1, 15):
        day = (candidate + timedelta(days=offset)).date()
        if _is_open_day(window, day):
            return datetime.combine(day, window.start, tzinfo=tz)
    return candidate + timedelta(days=1)


def _is_open_day(window: CallingWindow, day: date) -> bool:
    if day.weekday() not in window.weekdays:
        return False
    if window.exclude_holidays and is_holiday(day):
        return False
    return True


def within_window(window: CallingWindow, now: datetime) -> bool:
    local = now.astimezone(ZoneInfo(window.timezone))
    return _is_open_day(window, local.date()) and window.start <= local.time() < window.end


async def can_call(
    conn: asyncpg.Connection,
    *,
    contact: Contact,
    agent_id: str,
    window: CallingWindow,
    now: datetime | None = None,
    require_reservation: bool = True,
) -> CallDecision:
    """発信してよいかを判断する。

    conn は tenant_tx() で得たテナント固定の接続。RLS が効いているので
    ここで tenant_id を WHERE に書く必要はない（書いても害はない）。
    """
    now = now or datetime.now(ZoneInfo(window.timezone))

    # 0. 全体停止フラグ。障害時にアプリを巻き戻さず発信だけ止める
    if not DIALING_ENABLED:
        return CallDecision.deny(BlockReason.DIALING_DISABLED)

    # 1. DNC（特商法・再勧誘の禁止）。他のどの条件よりも先に見る
    dnc = await conn.fetchval(
        """
        select 1 from dnc_entries
         where phone_e164 = $1
           and (tenant_id is null or tenant_id = current_tenant_id())
         limit 1
        """,
        contact.phone_e164,
    )
    if dnc:
        return CallDecision.deny(BlockReason.DNC)

    # 2. 番号の妥当性。投入時に弾いているはずだが、API 経由や手入力の経路が
    #    後から生えるので、発信の直前でも見る
    if not contact.phone_e164.startswith("+") or not contact.phone_e164[1:].isdigit():
        return CallDecision.deny(BlockReason.INVALID_NUMBER, f"番号: {contact.phone_e164}")

    if contact.state != "ACTIVE":
        return CallDecision.deny(
            BlockReason.CONTACT_INACTIVE, f"状態: {contact.state}"
        )

    # 3. 架電可能時間帯。相手側のタイムゾーンで判断する
    contact_window = window.with_timezone(contact.timezone or window.timezone)
    if not within_window(contact_window, now):
        return CallDecision.deny(
            BlockReason.OUTSIDE_HOURS,
            f"架電可能時間外です（{contact_window.start:%H:%M}-{contact_window.end:%H:%M} "
            f"{contact_window.timezone}）",
            retry_after=next_open(contact_window, now),
        )

    # 4. 直前の重複。UI の二度押しとリトライの暴走をここで受け止める
    recent = await conn.fetchval(
        """
        select 1 from calls
         where contact_id = $1
           and created_at > now() - make_interval(secs => $2)
         limit 1
        """,
        contact.id,
        RECENT_CALL_WINDOW_SECONDS,
    )
    if recent:
        return CallDecision.deny(BlockReason.RECENTLY_CALLED)

    # 5. 回数の上限。「つながるまでかける」は法令以前に苦情を生む
    counts = await conn.fetchrow(
        """
        select
          count(*) filter (
            where created_at >= date_trunc('day', now() at time zone $2)
          ) as today,
          count(*) as total
        from calls where contact_id = $1
        """,
        contact.id,
        contact_window.timezone,
    )
    if counts["today"] >= window.max_attempts_per_day:
        return CallDecision.deny(
            BlockReason.DAILY_LIMIT,
            f"当日の上限 {window.max_attempts_per_day} 件に達しています",
            retry_after=next_open(contact_window, now + timedelta(days=1)),
        )
    if counts["total"] >= window.max_attempts_total:
        return CallDecision.deny(
            BlockReason.TOTAL_LIMIT, f"通算の上限 {window.max_attempts_total} 件に達しています"
        )

    # 6. 予約の保持者と一致するか。他人の予約で発信させない
    if require_reservation:
        held = await conn.fetchval(
            """
            select 1 from call_reservations
             where contact_id = $1 and agent_id = $2
               and state = 'HELD' and expires_at > now()
             limit 1
            """,
            contact.id,
            agent_id,
        )
        if not held:
            return CallDecision.deny(BlockReason.NO_RESERVATION)

    return CallDecision.allow()


async def record_block(
    conn: asyncpg.Connection, *, contact_id: str, agent_id: str | None, decision: CallDecision
) -> None:
    """関門で止まった発信を記録する。

    ★ 「関門が実際に機能していることの証跡」であり、監視項目でもある。
      急増は設定ミス（時間帯・タイムゾーン）かリスト品質の劣化を示す。
    """
    await conn.execute(
        """
        insert into call_attempts_blocked
          (tenant_id, contact_id, agent_id, reason, detail)
        values (current_tenant_id(), $1, $2, $3, $4)
        """,
        contact_id,
        agent_id,
        str(decision.reason),
        decision.detail,
    )
