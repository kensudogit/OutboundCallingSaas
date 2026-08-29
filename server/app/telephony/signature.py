"""Twilio Webhook の署名検証。

★ SDK の RequestValidator ではなく自前で実装している。アルゴリズムは
  HMAC-SHA1 で固定されていて変わらない一方、SDK のバージョンに
  セキュリティの中心を依存させたくないため。計算は公式実装と完全一致する
  （skill の scripts/verify_twilio_signature.py で相互検証済み）。

署名 = base64( HMAC-SHA1( 完全なURL + パラメータをキー順に key+value 連結, AuthToken ) )
ヘッダ名は X-Twilio-Signature。
"""

from __future__ import annotations

import base64
import hashlib
import hmac

from ..config import TWILIO_AUTH_TOKEN, TWILIO_CONFIGURED, public_url
from ..logger import logger


def compute(url: str, params: dict[str, str], auth_token: str = TWILIO_AUTH_TOKEN) -> str:
    payload = url + "".join(k + str(params[k]) for k in sorted(params))
    digest = hmac.new(auth_token.encode("utf-8"), payload.encode("utf-8"), hashlib.sha1)
    return base64.b64encode(digest.digest()).decode("utf-8")


def is_valid(
    *,
    path_with_query: str,
    params: dict[str, str],
    signature_header: str | None,
    auth_token: str = TWILIO_AUTH_TOKEN,
) -> bool:
    """Twilio からのリクエストであることを検証する。

    ★ URL は request.url から組み立てず、設定値（PUBLIC_BASE_URL）+ パスを使う。
      リバースプロキシの後ろでは request.url が http:// や内部ホスト名になり、
      Twilio が署名に使った https://<公開ドメイン> と一致しない。
      これが「Webhook が全件 403」の原因の第 1 位。

    ★ 署名ヘッダが無いリクエストは通さない。ヘッダ欠落時に検証をスキップする
      実装にすると、誰でも TwiML を引き出せる。正常系のテストでは
      絶対に検出できないので、ここは明示的に書いておく。
    """
    if not signature_header:
        return False

    # ★ Auth Token が空のまま検証を続けると、鍵が「空文字」の HMAC になる。
    #   それは誰でも計算できるので、検証を通したことにしてはいけない。
    #   未設定時は verify_request が先に 503 で止めるが、ここでも塞いでおく。
    if not auth_token:
        return False

    expected = compute(public_url(path_with_query), params, auth_token)
    # 単純な == は使わない（タイミング攻撃）
    return hmac.compare_digest(expected, signature_header)


async def verify_request(request) -> dict[str, str]:
    """FastAPI の依存として使う。検証に失敗したら 403。"""
    from fastapi import HTTPException

    # Twilio 未設定なら検証する鍵が無い。403 ではなく 503 を返す——
    # 403 は「署名が違う」の意味で、URL 不一致の調査に人を向かわせてしまう
    if not TWILIO_CONFIGURED:
        logger.warn(
            "Twilio が未設定のため Webhook を受け付けません",
            path=request.url.path,
            hint="TWILIO_ACCOUNT_SID / TWILIO_AUTH_TOKEN / TWILIO_CALLER_ID を設定する",
        )
        raise HTTPException(status_code=503, detail={"error": "telephony_not_configured"})

    form = await request.form()
    params = {k: str(v) for k, v in form.items()}

    path = request.url.path
    if request.url.query:
        path = f"{path}?{request.url.query}"

    if not is_valid(
        path_with_query=path,
        params=params,
        signature_header=request.headers.get("X-Twilio-Signature"),
    ):
        logger.warn(
            "Twilio 署名が一致しません",
            path=request.url.path,
            has_header=bool(request.headers.get("X-Twilio-Signature")),
            hint="PUBLIC_BASE_URL と Twilio Console の URL が一致しているか確認",
        )
        raise HTTPException(status_code=403)

    return params
