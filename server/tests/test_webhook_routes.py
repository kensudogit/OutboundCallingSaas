"""Twilio コールバックの経路を、アプリ全体を通して検証する。

単体の署名テスト（test_signature.py）は関数を直接呼ぶが、ここでは
実際の HTTP リクエストとして通す。ルーティング・依存解決・フォームの
パースまで含めて「署名が無いリクエストは DB に触る前に落ちる」ことを見る。

★ この検証が重要なのは、署名検証が「1 箇所でも抜けると全部無意味」だから。
  ルートを追加したときに verify_request を書き忘れる、という間違いは
  関数単体のテストでは絶対に検出できない。
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from app.app import create_app
from app.config import public_url
from app.telephony.signature import compute

TOKEN = "test-auth-token-0123456789"

FORM = {
    "AccountSid": "ACtest00000000000000000000000000",
    "CallSid": "CA00000000000000000000000000000001",
    "CallStatus": "completed",
    "From": "+815012345678",
    "To": "+819012345678",
    "CallDuration": "132",
}


@pytest.fixture
def client(monkeypatch):
    # lifespan は DB に繋ぐので、TestClient のコンテキストには入らず
    # アプリだけを組み立てて叩く
    app = create_app()
    return TestClient(app, raise_server_exceptions=False)


def signed_headers(path: str, form: dict[str, str]) -> dict[str, str]:
    return {"X-Twilio-Signature": compute(public_url(path), form, TOKEN)}


def test_ルートはブラウザ向けにHTMLを返す(client):
    """公開 URL を開いたときに FastAPI 既定の 404 JSON を出さない。"""
    res = client.get("/", headers={"Accept": "text/html"})
    assert res.status_code == 200
    assert "text/html" in res.headers["content-type"]
    assert "OutboundCallingSaas API" in res.text
    assert "/healthz" in res.text


def test_ルートはJSONでも同じ状態を返す(client):
    res = client.get("/", headers={"Accept": "application/json"})
    assert res.status_code == 200
    body = res.json()
    assert body["service"] == "api"
    assert body["ok"] is True
    assert body["healthz"] == "/healthz"
    assert body["telephony"] in ("configured", "disabled")


# ---------------------------------------------------------------- 署名なし

# ★ 最も重要な検証。署名検証を書き忘れたルートがあると、誰でも TwiML を
#   引き出せるうえ、偽の通話イベントを流し込める
@pytest.mark.parametrize(
    "path",
    [
        "/voice/outbound?call_id=00000000-0000-4000-8000-000000000001",
        "/voice/status?call_id=00000000-0000-4000-8000-000000000001",
        "/voice/recording?call_id=00000000-0000-4000-8000-000000000001",
    ],
)
def test_署名がなければ403(client, path):
    res = client.post(path, data=FORM)
    assert res.status_code == 403


@pytest.mark.parametrize(
    "path",
    [
        "/voice/outbound?call_id=00000000-0000-4000-8000-000000000001",
        "/voice/status?call_id=00000000-0000-4000-8000-000000000001",
        "/voice/recording?call_id=00000000-0000-4000-8000-000000000001",
    ],
)
def test_署名が誤っていれば403(client, path):
    res = client.post(path, data=FORM, headers={"X-Twilio-Signature": "AAAAAAAAAAAAAAAAAAAAAAAAAAA="})
    assert res.status_code == 403


def test_ボディを改竄すると403(client):
    path = "/voice/status?call_id=00000000-0000-4000-8000-000000000001"
    headers = signed_headers(path, FORM)
    tampered = {**FORM, "CallDuration": "1"}
    assert client.post(path, data=tampered, headers=headers).status_code == 403


def test_クエリを変えると403(client):
    """署名対象の URL にはクエリも含まれる。"""
    path = "/voice/status?call_id=00000000-0000-4000-8000-000000000001"
    headers = signed_headers(path, FORM)
    other = "/voice/status?call_id=00000000-0000-4000-8000-000000000002"
    assert client.post(other, data=FORM, headers=headers).status_code == 403


# ------------------------------------------------------------ Twilio 未設定

# ★ 縮退モード（Twilio 未設定でも起動する）で、Webhook の入口が
#   「空の Auth Token で検証する」状態にならないことを通しで確認する
@pytest.mark.parametrize(
    "path",
    [
        "/voice/outbound?call_id=00000000-0000-4000-8000-000000000001",
        "/voice/status?call_id=00000000-0000-4000-8000-000000000001",
        "/voice/recording?call_id=00000000-0000-4000-8000-000000000001",
    ],
)
def test_Twilio未設定なら正しい署名でも503(client, monkeypatch, path):
    monkeypatch.setattr("app.telephony.signature.TWILIO_CONFIGURED", False)
    res = client.post(path, data=FORM, headers=signed_headers(path, FORM))
    assert res.status_code == 503


def test_Twilio未設定ならDBに触らない(client, monkeypatch):
    """403 と同様、検証を通る前に副作用を起こさない。"""
    touched = []

    async def spy(sid: str):
        touched.append(sid)
        return None

    monkeypatch.setattr("app.telephony.signature.TWILIO_CONFIGURED", False)
    monkeypatch.setattr("app.telephony.routes._tenant_conn_for_call", spy)

    path = "/voice/status?call_id=00000000-0000-4000-8000-000000000001"
    client.post(path, data=FORM, headers=signed_headers(path, FORM))
    assert touched == []


# ---------------------------------------------------------------- 署名あり


def test_正しい署名なら通り未知のCallSidは204で受け流す(client, monkeypatch):
    """自分が発信していない通話（Console のテスト等）が届いても 500 にしない。

    ★ 500 を返すと Twilio が指数バックオフで再送し続ける。
    """
    # DB を見に行く関数だけ差し替える。署名検証はそのまま通す
    async def no_tenant(_sid: str):
        return None

    monkeypatch.setattr("app.telephony.routes._tenant_conn_for_call", no_tenant)

    path = "/voice/status?call_id=00000000-0000-4000-8000-000000000001"
    res = client.post(path, data=FORM, headers=signed_headers(path, FORM))
    assert res.status_code == 204


def test_署名がなければDBに一切触らない(client, monkeypatch):
    """関門と同じ発想。検証を通る前に副作用を起こさない。"""
    touched = []

    async def spy(sid: str):
        touched.append(sid)
        return None

    monkeypatch.setattr("app.telephony.routes._tenant_conn_for_call", spy)

    client.post("/voice/status?call_id=x", data=FORM)
    assert touched == []


# ---------------------------------------------------------------- WebSocket


@pytest.mark.parametrize(
    "url",
    [
        "/ws/agent/00000000-0000-4000-8000-000000000001",              # トークンなし
        "/ws/agent/00000000-0000-4000-8000-000000000001?token=garbage",  # 不正なトークン
        "/ws/agent/abc?token=",                                          # 空
    ],
)
def test_担当者チャネルは認証を通らないと接続できない(client, url):
    """★ WebSocket の認証は忘れられやすい。REST だけ守って満足している
    実装が多く、call_id を知っていれば他人の通話を聞ける状態になりやすい。

    close code 4401 で閉じられることを確認する。これはルートが存在し、
    かつ認証で弾かれたことの両方を意味する（ルートが無ければ別の失敗になる）。
    """
    with client.websocket_connect(url) as websocket:
        message = websocket.receive()
    assert message["type"] == "websocket.close"
    assert message["code"] == 4401
