"""Twilio からのコールバック（Phase 4）。

このルーターは 3 つの入口を持ち、いずれも署名検証を通る。

    POST /voice/outbound   TwiML 要求。<Start><Stream> + <Dial> を返す
    POST /voice/status     ringing / answered / completed
    POST /voice/recording   録音の準備完了

★ 状態は upsert で単調更新する（原則 2）。到着順の逆転と重複配信は
  「起きるかもしれない」ではなく「起きる」。
"""

from __future__ import annotations

import json
from datetime import datetime, timezone

from fastapi import APIRouter, Request, Response
from twilio.twiml.voice_response import Dial, Start, VoiceResponse

from ..config import TWILIO_CALLER_ID, public_url, public_wss
from ..db.engine import admin_tx
from ..logger import logger
from ..repositories import calls as calls_repo
from .signature import verify_request

router = APIRouter(tags=["telephony"])

# CallStatus → 内部の状態。busy / no-answer / failed はどれも「終わった」なので
# COMPLETED に寄せ、理由は raw_status に残す。status に混ぜると順序が定義できない。
STATUS_MAP = {
    "queued": "QUEUED",
    "initiated": "QUEUED",
    "ringing": "RINGING",
    "in-progress": "ANSWERED",
    "answered": "ANSWERED",
    "completed": "COMPLETED",
    "busy": "COMPLETED",
    "no-answer": "COMPLETED",
    "failed": "COMPLETED",
    "canceled": "COMPLETED",
}


async def _tenant_conn_for_call(sid: str):
    """コールバックにはテナントが乗っていないので、call_sid から引く。

    ★ ここだけは RLS を迂回して引く必要がある（テナントが未確定のため）。
      引けたら以降はそのテナントに固定する。
    """
    async with admin_tx() as conn:
        row = await conn.fetchrow(
            "select tenant_id from calls where provider_call_sid = $1", sid
        )
    return str(row["tenant_id"]) if row else None


@router.post("/voice/outbound")
async def outbound_twiml(request: Request, call_id: str) -> Response:
    """発信時の TwiML。相手にダイヤルし、音声を分岐させる。"""
    await verify_request(request)

    response = VoiceResponse()

    # ★ <Start><Stream> は音声を「分岐」する。通話をブロックしないので、
    #   ASR が落ちていても通話自体は成立する。<Connect><Stream> は双方向で
    #   通話を占有するため、文字起こし用途では使わない。
    start = Start()
    start.stream(url=public_wss(f"/media?call_id={call_id}"), track="both_tracks")
    response.append(start)

    async with admin_tx() as conn:
        row = await conn.fetchrow(
            "select c.phone_e164, ca.caller_id from calls ca "
            "join contacts c on c.id = ca.contact_id where ca.id = $1",
            call_id,
        )

    if row is None:
        # TwiML 要求に対応する通話が無い。発信 API のレスポンスより先に
        # ここへ来ることは設計上ありえないので、握り潰さず切断する
        logger.error("TwiML 要求に対応する通話がありません", call_id=call_id)
        response.hangup()
        return Response(content=str(response), media_type="application/xml")

    dial = Dial(
        caller_id=row["caller_id"] or TWILIO_CALLER_ID,
        # デュアルチャンネル。左が担当者・右が相手と決まるので、
        # トーク比率や被り回数を推測でなく実測できる
        record="record-from-answer-dual",
        recording_status_callback=public_url(f"/voice/recording?call_id={call_id}"),
        recording_status_callback_event="completed",
        timeout=25,
        # ★ 相手が出るまで担当者に呼び出し音を聞かせる。外すと担当者には
        #   即座に接続音が鳴り、「つながったのに相手が黙っている」と誤解する
        answer_on_bridge=True,
    )
    dial.number(row["phone_e164"])
    response.append(dial)

    return Response(content=str(response), media_type="application/xml")


@router.post("/voice/status")
async def call_status(request: Request, call_id: str | None = None) -> Response:
    """通話の状態変化。到着順は保証されない。"""
    params = await verify_request(request)
    sid = params["CallSid"]
    raw_status = params.get("CallStatus", "")
    status = STATUS_MAP.get(raw_status, "COMPLETED")

    tenant_id = await _tenant_conn_for_call(sid)
    if tenant_id is None:
        # 自分が発信していない通話。Console からのテスト等。
        # ★ 500 を返すと Twilio が再送し続けるので 204 で受け流す
        logger.info("未知の CallSid のコールバック", provider_call_sid=sid, status=raw_status)
        return Response(status_code=204)

    duration = params.get("CallDuration")

    # 時刻はサーバー時刻で埋める。upsert 側の coalesce により最初に届いた値だけが
    # 残るので、重複配信で上書きされない
    now = datetime.now(timezone.utc)
    timestamps: dict[str, object] = {}
    if status in ("QUEUED", "RINGING"):
        timestamps["started_at"] = now
    elif status == "ANSWERED":
        timestamps["answered_at"] = now
    elif status == "COMPLETED":
        timestamps["ended_at"] = now

    from ..db.engine import tenant_tx

    async with tenant_tx(tenant_id) as conn:
        await calls_repo.record_delivery(
            conn, provider_call_sid=sid, event_type=raw_status, payload=json.dumps(params)
        )
        row = await calls_repo.upsert_from_callback(
            conn,
            provider_call_sid=sid,
            call_id=call_id,
            status=status,
            raw_status=raw_status,
            # ★ ended_at - answered_at で自分で引き算しない。Twilio の値を採る
            duration_sec=int(duration) if duration else None,
            answered_by=params.get("AnsweredBy"),
            timestamps=timestamps,
        )

    logger.info(
        "通話状態を更新しました",
        call_id=str(row["id"]),
        provider_call_sid=sid,
        raw_status=raw_status,
        status=row["status"],
    )
    # ★ 200/204 を返す。500 を返すと Twilio が再送し、それは重複配信として届く。
    #   処理の失敗は自前のジョブでリトライしたほうが制御しやすい
    return Response(status_code=204)


@router.post("/voice/recording")
async def recording_ready(request: Request, call_id: str) -> Response:
    params = await verify_request(request)
    sid = params["CallSid"]

    tenant_id = await _tenant_conn_for_call(sid)
    if tenant_id is None:
        return Response(status_code=204)

    from ..config import RECORDING_RETENTION_DAYS
    from ..db.engine import tenant_tx

    async with tenant_tx(tenant_id) as conn:
        await conn.execute(
            """
            insert into recordings
              (tenant_id, call_id, provider_recording_sid, provider_url,
               duration_sec, channels, expires_at)
            values (current_tenant_id(), $1, $2, $3, $4, $5,
                    now() + make_interval(days => $6))
            on conflict (tenant_id, provider_recording_sid) do nothing
            """,
            call_id,
            params["RecordingSid"],
            params.get("RecordingUrl"),
            int(params.get("RecordingDuration") or 0),
            int(params.get("RecordingChannels") or 1),
            RECORDING_RETENTION_DAYS,
        )

    # 全文の文字起こしと要約は非同期で。ここで待つと Twilio がタイムアウトする
    logger.info("録音を記録しました", call_id=call_id, provider_call_sid=sid)
    return Response(status_code=204)
