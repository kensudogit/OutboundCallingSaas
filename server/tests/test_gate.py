"""発信の関門（原則 1）。

架電システムで最も壊してはいけない部分。ここが緩むと、断った相手に
もう一度かけることになり、それはロールバックできない。

DB は擬似接続で置き換える。can_call が投げるクエリは 4 種類だけなので、
それぞれの応答を差し替えれば全分岐を通せる。
"""

from __future__ import annotations

from datetime import datetime, time
from zoneinfo import ZoneInfo

import pytest

from app.dialer.gate import BlockReason, can_call, next_open, within_window
from app.models import CallingWindow, Contact

JST = ZoneInfo("Asia/Tokyo")

WINDOW = CallingWindow(
    timezone="Asia/Tokyo",
    start=time(9, 0),
    end=time(20, 0),
    weekdays=frozenset({0, 1, 2, 3, 4}),
    exclude_holidays=False,  # 祝日判定はテストを日付に依存させるので別テストで見る
    max_attempts_per_day=3,
    max_attempts_total=8,
)

CONTACT = Contact(
    id="c1",
    tenant_id="t1",
    list_id="l1",
    phone_e164="+819012345678",
    timezone="Asia/Tokyo",
)


class FakeConn:
    """can_call が投げるクエリだけに応答する擬似接続。

    どの分岐を通したいかを引数で指定する。実 DB を立てないと 1 行も通らない
    テストは、結局ローカルで動かされなくなる。
    """

    def __init__(
        self,
        *,
        in_dnc: bool = False,
        recent: bool = False,
        today: int = 0,
        total: int = 0,
        reserved: bool = True,
    ) -> None:
        self.in_dnc = in_dnc
        self.recent = recent
        self.today = today
        self.total = total
        self.reserved = reserved
        self.queries: list[str] = []

    async def fetchval(self, sql: str, *args):
        self.queries.append(sql)
        if "dnc_entries" in sql:
            return 1 if self.in_dnc else None
        if "call_reservations" in sql:
            return 1 if self.reserved else None
        if "calls" in sql:
            return 1 if self.recent else None
        return None

    async def fetchrow(self, sql: str, *args):
        self.queries.append(sql)
        return {"today": self.today, "total": self.total}

    async def execute(self, sql: str, *args):
        self.queries.append(sql)


BUSINESS_HOURS = datetime(2026, 1, 6, 14, 0, tzinfo=JST)  # 火曜 14:00


async def test_通常時は発信できる():
    decision = await can_call(
        FakeConn(), contact=CONTACT, agent_id="a1", window=WINDOW, now=BUSINESS_HOURS
    )
    assert decision.allowed


# ★ 最も重要なテスト。DNC を素通りする実装は正常系のテストでは検出できない
async def test_DNC登録済みには発信できない():
    conn = FakeConn(in_dnc=True)
    decision = await can_call(
        conn, contact=CONTACT, agent_id="a1", window=WINDOW, now=BUSINESS_HOURS
    )
    assert not decision.allowed
    assert decision.reason is BlockReason.DNC


async def test_DNCは他のどの条件よりも先に判定される():
    """監査ログに残る理由が変わるため順序に意味がある。

    DNC 登録者に時間外へ架電しようとしたとき、記録されるべき理由は dnc であって
    outside_hours ではない。「かけてはいけない相手」と「今はかけられない」は違う。
    """
    conn = FakeConn(in_dnc=True, recent=True, today=99, reserved=False)
    midnight = datetime(2026, 1, 6, 3, 0, tzinfo=JST)
    decision = await can_call(
        conn, contact=CONTACT, agent_id="a1", window=WINDOW, now=midnight
    )
    assert decision.reason is BlockReason.DNC


async def test_架電可能時間外は発信できず次に開く時刻を返す():
    night = datetime(2026, 1, 6, 22, 30, tzinfo=JST)
    decision = await can_call(
        FakeConn(), contact=CONTACT, agent_id="a1", window=WINDOW, now=night
    )
    assert not decision.allowed
    assert decision.reason is BlockReason.OUTSIDE_HOURS
    # ★ retry_after があると UI が「翌 9 時以降に再開できます」と言える
    assert decision.retry_after is not None
    assert decision.retry_after.astimezone(JST).hour == 9


async def test_土日は発信できない():
    saturday = datetime(2026, 1, 10, 14, 0, tzinfo=JST)
    decision = await can_call(
        FakeConn(), contact=CONTACT, agent_id="a1", window=WINDOW, now=saturday
    )
    assert decision.reason is BlockReason.OUTSIDE_HOURS


async def test_相手側のタイムゾーンで判定する():
    """テナントの営業時間ではなく、かける相手の時刻で判断する。"""
    hawaii_contact = Contact(**{**CONTACT.__dict__, "timezone": "Pacific/Honolulu"})
    # 日本の火曜 14:00 はハワイの月曜 19:00。JST 基準なら通るが相手基準でも通る
    decision = await can_call(
        FakeConn(), contact=hawaii_contact, agent_id="a1", window=WINDOW, now=BUSINESS_HOURS
    )
    assert decision.allowed

    # 日本の火曜 9:00 はハワイの月曜 14:00 → 通る
    # 日本の火曜 20:00 はハワイの火曜 1:00 → 相手基準では深夜なので止まる
    jp_evening = datetime(2026, 1, 6, 19, 0, tzinfo=JST)
    decision = await can_call(
        FakeConn(), contact=hawaii_contact, agent_id="a1", window=WINDOW, now=jp_evening
    )
    assert decision.reason is BlockReason.OUTSIDE_HOURS


async def test_直前に発信していれば止まる():
    """UI の二度押しとリトライの暴走をここで受け止める。"""
    conn = FakeConn(recent=True)
    decision = await can_call(
        conn, contact=CONTACT, agent_id="a1", window=WINDOW, now=BUSINESS_HOURS
    )
    assert decision.reason is BlockReason.RECENTLY_CALLED


async def test_当日の上限に達したら止まる():
    conn = FakeConn(today=3)
    decision = await can_call(
        conn, contact=CONTACT, agent_id="a1", window=WINDOW, now=BUSINESS_HOURS
    )
    assert decision.reason is BlockReason.DAILY_LIMIT
    assert decision.retry_after is not None


async def test_通算の上限に達したら止まる():
    """「つながるまでかける」は法令以前に苦情を生む。"""
    conn = FakeConn(today=0, total=8)
    decision = await can_call(
        conn, contact=CONTACT, agent_id="a1", window=WINDOW, now=BUSINESS_HOURS
    )
    assert decision.reason is BlockReason.TOTAL_LIMIT


async def test_予約がなければ発信できない():
    """他人の予約で発信させない。API を直叩きしても通らないこと。"""
    conn = FakeConn(reserved=False)
    decision = await can_call(
        conn, contact=CONTACT, agent_id="a1", window=WINDOW, now=BUSINESS_HOURS
    )
    assert decision.reason is BlockReason.NO_RESERVATION


async def test_E164でない番号は発信できない():
    bad = Contact(**{**CONTACT.__dict__, "phone_e164": "090-1234-5678"})
    decision = await can_call(
        FakeConn(), contact=bad, agent_id="a1", window=WINDOW, now=BUSINESS_HOURS
    )
    assert decision.reason is BlockReason.INVALID_NUMBER


async def test_対象外の連絡先には発信できない():
    exhausted = Contact(**{**CONTACT.__dict__, "state": "EXHAUSTED"})
    decision = await can_call(
        FakeConn(), contact=exhausted, agent_id="a1", window=WINDOW, now=BUSINESS_HOURS
    )
    assert decision.reason is BlockReason.CONTACT_INACTIVE


async def test_発信停止フラグが効く(monkeypatch):
    """障害時にアプリ全体を巻き戻さず発信だけ止められること。"""
    monkeypatch.setattr("app.dialer.gate.DIALING_ENABLED", False)
    decision = await can_call(
        FakeConn(), contact=CONTACT, agent_id="a1", window=WINDOW, now=BUSINESS_HOURS
    )
    assert decision.reason is BlockReason.DIALING_DISABLED


async def test_Twilio未設定なら発信できない(monkeypatch):
    """認証情報が無い状態で起動したとき、発信経路が開いたままにならないこと。

    未設定でも起動を許す縮退モードがあるので、関門側で塞げていないと
    「起動はしたが Twilio に空の SID で発信を試みる」経路が残る。
    """
    monkeypatch.setattr("app.dialer.gate.TWILIO_CONFIGURED", False)
    decision = await can_call(
        FakeConn(), contact=CONTACT, agent_id="a1", window=WINDOW, now=BUSINESS_HOURS
    )
    assert not decision.allowed
    assert decision.reason is BlockReason.TELEPHONY_UNCONFIGURED


async def test_Twilio未設定は発信停止フラグと区別される(monkeypatch):
    """原因が違えば対処も違う。監査ログで一緒くたにしない。"""
    monkeypatch.setattr("app.dialer.gate.TWILIO_CONFIGURED", False)
    monkeypatch.setattr("app.dialer.gate.DIALING_ENABLED", False)
    decision = await can_call(
        FakeConn(), contact=CONTACT, agent_id="a1", window=WINDOW, now=BUSINESS_HOURS
    )
    assert decision.reason is BlockReason.TELEPHONY_UNCONFIGURED


# ---------------------------------------------------------------- 時間帯の計算


@pytest.mark.parametrize(
    "moment,expected",
    [
        (datetime(2026, 1, 6, 8, 59, tzinfo=JST), False),   # 開始直前
        (datetime(2026, 1, 6, 9, 0, tzinfo=JST), True),     # 開始ちょうど
        (datetime(2026, 1, 6, 19, 59, tzinfo=JST), True),   # 終了直前
        (datetime(2026, 1, 6, 20, 0, tzinfo=JST), False),   # 終了ちょうどは含まない
        (datetime(2026, 1, 11, 14, 0, tzinfo=JST), False),  # 日曜
    ],
)
def test_境界時刻(moment, expected):
    assert within_window(WINDOW, moment) is expected


def test_next_openは翌営業日を返す():
    friday_night = datetime(2026, 1, 9, 21, 0, tzinfo=JST)
    nxt = next_open(WINDOW, friday_night).astimezone(JST)
    assert nxt.weekday() == 0  # 月曜
    assert nxt.hour == 9


def test_next_openは同日の開始前なら当日を返す():
    early = datetime(2026, 1, 6, 7, 0, tzinfo=JST)
    nxt = next_open(WINDOW, early).astimezone(JST)
    assert nxt.date() == early.date()
    assert nxt.hour == 9
