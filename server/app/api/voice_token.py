"""ブラウザソフトフォン用のアクセストークン発行。

★ TwiML App SID と API Key Secret はブラウザに出さない。トークンだけを渡す。
★ identity は推測困難な値にする。連番の社員番号にすると、他人になりすまして
  発信できる余地が生まれる。
★ incoming_allow=False。架電専用なので着信は受けない。
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException

from ..config import (
    TWILIO_ACCOUNT_SID,
    TWILIO_API_KEY_SECRET,
    TWILIO_API_KEY_SID,
    TWILIO_TWIML_APP_SID,
)
from .auth import AuthUser, current_user

router = APIRouter(prefix="/api", tags=["voice"])

TOKEN_TTL_SECONDS = 3600


@router.post("/voice/token")
async def voice_token(user: AuthUser = Depends(current_user)):
    if not (TWILIO_API_KEY_SID and TWILIO_API_KEY_SECRET and TWILIO_TWIML_APP_SID):
        raise HTTPException(
            status_code=501,
            detail={
                "error": "voice_sdk_not_configured",
                "hint": "TWILIO_API_KEY_SID / TWILIO_API_KEY_SECRET / TWILIO_TWIML_APP_SID",
            },
        )

    from twilio.jwt.access_token import AccessToken
    from twilio.jwt.access_token.grants import VoiceGrant

    token = AccessToken(
        TWILIO_ACCOUNT_SID,
        TWILIO_API_KEY_SID,
        TWILIO_API_KEY_SECRET,
        # ユーザー ID は UUID なので推測できない
        identity=f"agent-{user.id}",
        ttl=TOKEN_TTL_SECONDS,
    )
    token.add_grant(
        VoiceGrant(outgoing_application_sid=TWILIO_TWIML_APP_SID, incoming_allow=False)
    )
    return {"token": token.to_jwt(), "ttl": TOKEN_TTL_SECONDS}
