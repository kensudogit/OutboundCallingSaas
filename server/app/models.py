"""ドメインの値オブジェクト。

DB の行をそのまま持ち回らず、関門やダイヤラーが必要とする形だけを定義する。
ここに置いてあるものは DB にも Twilio にも依存しないので、単体テストが
インフラなしで書ける。
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import datetime, time
from enum import StrEnum

from .config import CALLING_DEFAULTS


class CallStatus(StrEnum):
    """通話の状態。

    ★ 順序が定義できることが重要（原則 2）。コールバックの到着順が逆転しても
      状態を巻き戻さないために、rank で比較する。
      busy / no-answer / failed は「終わった」なので COMPLETED に寄せ、
      理由は raw_status に別途残す。ここに混ぜると順序が定義できなくなる。
    """

    QUEUED = "QUEUED"
    RINGING = "RINGING"
    ANSWERED = "ANSWERED"
    COMPLETED = "COMPLETED"
    UNKNOWN = "UNKNOWN"


_RANK: dict[str, int] = {
    CallStatus.QUEUED: 1,
    CallStatus.RINGING: 2,
    CallStatus.ANSWERED: 3,
    CallStatus.COMPLETED: 4,
}


def status_rank(status: str) -> int:
    """DB 側の call_status_rank() と同じ順序（0001_initial_schema）。冗長だが、
    アプリ側でも順序判断が要る場面（イベントの取捨）があるため。
    値を変えるときは必ず両方直す。
    """
    return _RANK.get(status, 0)


class AgentState(StrEnum):
    OFFLINE = "OFFLINE"
    READY = "READY"
    RESERVED = "RESERVED"
    DIALING = "DIALING"
    TALKING = "TALKING"
    WRAP_UP = "WRAP_UP"


class DialMode(StrEnum):
    PREVIEW = "PREVIEW"
    PROGRESSIVE = "PROGRESSIVE"


@dataclass(frozen=True)
class Contact:
    id: str
    tenant_id: str
    list_id: str
    phone_e164: str
    company_name: str | None = None
    person_name: str | None = None
    timezone: str | None = None
    state: str = "ACTIVE"
    assigned_caller_id: str | None = None


@dataclass(frozen=True)
class CallingWindow:
    """架電可能時間帯。テナント設定から作る。

    設定でオフにできる項目とできない項目を分けてある。DNC の照合は
    そもそもこのクラスに現れない——無効にできる作りにしないため。
    """

    timezone: str = CALLING_DEFAULTS.timezone
    start: time = CALLING_DEFAULTS.start
    end: time = CALLING_DEFAULTS.end
    weekdays: frozenset[int] = CALLING_DEFAULTS.weekdays
    exclude_holidays: bool = CALLING_DEFAULTS.exclude_holidays
    max_attempts_per_day: int = CALLING_DEFAULTS.max_attempts_per_day
    max_attempts_total: int = CALLING_DEFAULTS.max_attempts_total

    def with_timezone(self, tz: str) -> "CallingWindow":
        """相手側のタイムゾーンで判断するための差し替え。"""
        return replace(self, timezone=tz)

    @classmethod
    def from_row(cls, row) -> "CallingWindow":
        return cls(
            timezone=row["calling_timezone"],
            start=row["calling_hours_start"],
            end=row["calling_hours_end"],
            # PostgreSQL の int[] は ISO の 1=月曜。Python の weekday() は 0=月曜
            weekdays=frozenset(d - 1 for d in row["calling_weekdays"]),
            exclude_holidays=row["exclude_holidays"],
            max_attempts_per_day=row["max_attempts_per_day"],
            max_attempts_total=row["max_attempts_total"],
        )


@dataclass(frozen=True)
class Reservation:
    id: str
    contact_id: str
    agent_id: str
    expires_at: datetime


@dataclass(frozen=True)
class PlacedCall:
    id: str
    provider_call_sid: str
    contact_id: str
    agent_id: str
    status: str
