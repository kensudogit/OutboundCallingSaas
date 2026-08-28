"""Twilio の SDK を呼ぶ唯一の場所（原則 1）。

★ このモジュールの外に twilio_client.calls.create(...) を書かない。
  レビューの観点はそれだけでよく、次の grep 1 回で守れているか確認できる。

      grep -rn "calls.create" server/app --include=*.py

  1 件（このファイル）以外がヒットしたら、そこが関門を通らない発信経路。
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import datetime

import asyncpg
from starlette.concurrency import run_in_threadpool

from .. import config
from ..logger import logger
from ..models import CallingWindow, Contact, PlacedCall
from ..repositories import calls as calls_repo
from .gate import CallDecision, can_call, record_block


class CallBlocked(Exception):
    """関門で止まった。API は 403 と decision.reason を返す。"""

    def __init__(self, decision: CallDecision) -> None:
        super().__init__(decision.detail or "発信できません")
        self.decision = decision


class ProviderUnavailable(Exception):
    """Twilio 側の失敗。発信されたかどうか分からない場合を含む。"""

    def __init__(self, message: str, *, uncertain: bool = False) -> None:
        super().__init__(message)
        self.uncertain = uncertain


@dataclass
class Dialer:
    """発信口。テストでは _create_call を差し替える。"""

    account_sid: str = config.TWILIO_ACCOUNT_SID
    auth_token: str = config.TWILIO_AUTH_TOKEN
    caller_id: str = config.TWILIO_CALLER_ID

    def _client(self):
        from twilio.rest import Client

        return Client(self.account_sid, self.auth_token)

    async def _create_call(self, **kwargs):
        """Twilio SDK の同期 I/O をスレッドに逃がす。

        FastAPI の async ハンドラから直接呼ぶとイベントループが止まり、
        発信ボタンを押すたびに全リクエストが数百ミリ秒固まる。
        """
        client = self._client()
        return await run_in_threadpool(client.calls.create, **kwargs)

    async def place_call(
        self,
        conn: asyncpg.Connection,
        *,
        contact: Contact,
        agent_id: str,
        window: CallingWindow,
        now: datetime | None = None,
        require_reservation: bool = True,
    ) -> PlacedCall:
        decision = await can_call(
            conn,
            contact=contact,
            agent_id=agent_id,
            window=window,
            now=now,
            require_reservation=require_reservation,
        )
        if not decision.allowed:
            await record_block(
                conn, contact_id=contact.id, agent_id=agent_id, decision=decision
            )
            logger.warn(
                "発信を関門で停止しました",
                contact_id=contact.id,
                agent_id=agent_id,
                reason=str(decision.reason),
            )
            raise CallBlocked(decision)

        # ★ 先に自分側の call_id を発行してから Twilio を呼ぶ。
        #   call_sid を待ってから行を作ると、その待ち時間が丸ごと事故の窓になる
        #   （発信されたのに自分側に記録がない状態）。
        call_id = str(uuid.uuid4())
        caller_id = contact.assigned_caller_id or self.caller_id

        params: dict[str, object] = {
            "to": contact.phone_e164,
            "from_": caller_id,
            "url": config.public_url(f"/voice/outbound?call_id={call_id}"),
            "status_callback": config.public_url(f"/voice/status?call_id={call_id}"),
            "status_callback_event": ["initiated", "ringing", "answered", "completed"],
            "status_callback_method": "POST",
            "timeout": 25,
        }
        if config.TWILIO_MACHINE_DETECTION:
            params["machine_detection"] = config.TWILIO_MACHINE_DETECTION

        try:
            twilio_call = await self._create_call(**params)
        except Exception as exc:  # noqa: BLE001 — 分類は下の helper が行う
            uncertain = _is_uncertain(exc)
            logger.error(
                "Twilio への発信要求が失敗しました",
                contact_id=contact.id,
                call_id=call_id,
                uncertain=uncertain,
                err=str(exc),
            )
            if uncertain:
                # ★ タイムアウトは「発信されたか分からない」状態。
                #   素朴にリトライすると二重発信になる。行だけ残して人が判断する。
                await calls_repo.record_uncertain(
                    conn, call_id=call_id, contact_id=contact.id, agent_id=agent_id
                )
            raise ProviderUnavailable(str(exc), uncertain=uncertain) from exc

        call = await calls_repo.upsert(
            conn,
            call_id=call_id,
            contact_id=contact.id,
            agent_id=agent_id,
            provider_call_sid=twilio_call.sid,
            status="QUEUED",
            caller_id=caller_id,
        )
        logger.info(
            "発信しました",
            call_id=call["id"],
            provider_call_sid=twilio_call.sid,
            contact_id=contact.id,
            agent_id=agent_id,
            to=contact.phone_e164,
        )
        return PlacedCall(
            id=str(call["id"]),
            provider_call_sid=call["provider_call_sid"],
            contact_id=str(call["contact_id"]),
            agent_id=str(call["agent_id"]),
            status=call["status"],
        )


def _is_uncertain(exc: Exception) -> bool:
    """発信されたか分からない失敗か。

    タイムアウトと 5xx は「届いたかもしれない」。4xx は届いた上で拒否された
    ので確実に発信されていない。判断がつかないなら uncertain に倒す——
    1 件つながらないより、2 回鳴らすほうが取り返しがつかない。
    """
    status = getattr(exc, "status", None)
    if isinstance(status, int):
        return status >= 500
    name = type(exc).__name__.lower()
    return "timeout" in name or "connect" in name


dialer = Dialer()
