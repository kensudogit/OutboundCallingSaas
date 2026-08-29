"""Twilio の署名検証。

署名は同じアルゴリズムで期待値を自作できるので、フィクスチャを外部から
持ってくる必要がない。加えて、公式実装（twilio.request_validator）と
一致することも確かめる——自前実装を選んだ以上、これは必須。
"""

from __future__ import annotations

import pytest

from app.telephony.signature import compute, is_valid

TOKEN = "test-auth-token-0123456789"
PATH = "/voice/status?call_id=abc"
PARAMS = {
    "CallSid": "CA00000000000000000000000000000001",
    "CallStatus": "completed",
    "From": "+815012345678",
    "To": "+819012345678",
    "CallDuration": "132",
}


def signature_for(path: str = PATH, params: dict[str, str] | None = None) -> str:
    from app.config import public_url

    return compute(public_url(path), params or PARAMS, TOKEN)


def test_正しい署名は通る():
    assert is_valid(
        path_with_query=PATH,
        params=PARAMS,
        signature_header=signature_for(),
        auth_token=TOKEN,
    )


# ★ 最も重要なテスト。「署名が無ければ検証をスキップ」は正常系では検出できない
def test_署名ヘッダが無ければ落ちる():
    assert not is_valid(
        path_with_query=PATH, params=PARAMS, signature_header=None, auth_token=TOKEN
    )
    assert not is_valid(
        path_with_query=PATH, params=PARAMS, signature_header="", auth_token=TOKEN
    )


def test_パラメータが1つ違うと落ちる():
    tampered = {**PARAMS, "CallDuration": "1"}
    assert not is_valid(
        path_with_query=PATH,
        params=tampered,
        signature_header=signature_for(),
        auth_token=TOKEN,
    )


def test_パラメータが欠けると落ちる():
    partial = {k: v for k, v in PARAMS.items() if k != "CallDuration"}
    assert not is_valid(
        path_with_query=PATH,
        params=partial,
        signature_header=signature_for(),
        auth_token=TOKEN,
    )


# ★ 「Webhook が全件 403」の原因の第 1 位。URL の揺れを 1 つずつ確認する
@pytest.mark.parametrize(
    "wrong_path",
    [
        "/voice/status?call_id=abc/",   # 末尾スラッシュ
        "/voice/status",                # クエリが落ちた
        "/voice/status?call_id=xyz",    # クエリの値が違う
        "/voice/Status?call_id=abc",    # 大文字小文字
    ],
)
def test_URLが違うと落ちる(wrong_path):
    assert not is_valid(
        path_with_query=wrong_path,
        params=PARAMS,
        signature_header=signature_for(),
        auth_token=TOKEN,
    )


def test_署名キーが違うと落ちる():
    assert not is_valid(
        path_with_query=PATH,
        params=PARAMS,
        signature_header=signature_for(),
        auth_token="another-token",
    )


# ★ Twilio 未設定でも起動できる（縮退モード）ので、空の鍵で検証が「成功」しない
#   ことを明示的に確かめる。空鍵の HMAC は誰でも計算できるため、ここが通ると
#   認証情報が無い環境で誰でも Webhook を偽造できる
def test_Auth_Tokenが空なら正しい署名でも通らない():
    from app.config import public_url

    forged = compute(public_url(PATH), PARAMS, "")
    assert not is_valid(
        path_with_query=PATH, params=PARAMS, signature_header=forged, auth_token=""
    )


def test_長さの違う署名でも例外にならない():
    assert not is_valid(
        path_with_query=PATH, params=PARAMS, signature_header="short", auth_token=TOKEN
    )


def test_公式実装と一致する():
    """自前実装を選んだ以上、公式と一致することは必ず確かめる。

    SDK のバージョンにセキュリティの中心を依存させない、という判断で
    自前にしている。アルゴリズムがずれていたら意味がない。
    """
    from twilio.request_validator import RequestValidator

    from app.config import public_url

    url = public_url(PATH)
    validator = RequestValidator(TOKEN)
    assert validator.validate(url, PARAMS, compute(url, PARAMS, TOKEN))


def test_マルチバイトを含むパラメータでも一致する():
    params = {**PARAMS, "CallerName": "山田 太郎"}
    assert is_valid(
        path_with_query=PATH,
        params=params,
        signature_header=signature_for(params=params),
        auth_token=TOKEN,
    )
