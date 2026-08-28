"""管理 API の統合検証（実 DB）。

★ ここで確かめたいのは 4 点。どれも「部分的に入った」「入れてはいけない
  ものが入った」を防ぐためのもので、繋いで動かさないと確認できない。

    1. 一括操作が管理者権限に限られているか
    2. 不正な行が 1 件でもあれば連絡先を 1 件も入れないか
    3. DNC にある番号をリストに入れないか
    4. DNC を取り込んだら既存リストの該当行を対象外にするか
"""

from __future__ import annotations

import os
import uuid

import asyncpg
import pytest
from fastapi.testclient import TestClient

from app.api.auth import AuthUser, issue_token
from app.app import create_app

ADMIN_DSN = os.environ.get(
    "TEST_ADMIN_DSN", "postgresql://migrator:migrator_password@localhost:5434/calling"
)


async def _connect():
    try:
        return await asyncpg.connect(ADMIN_DSN, timeout=3)
    except Exception as exc:  # noqa: BLE001
        pytest.skip(f"DB に接続できないためスキップします: {exc}")


@pytest.fixture
async def admin_conn():
    conn = await _connect()
    try:
        yield conn
    finally:
        await conn.close()


@pytest.fixture
async def tenant(admin_conn):
    suffix = f"{uuid.uuid4().int % 10**6:06d}"
    tenant_id = await admin_conn.fetchval(
        "insert into tenants (name, company_name) values ($1, $1) returning id", f"t-{suffix}"
    )
    manager_id = await admin_conn.fetchval(
        "insert into users (tenant_id, email, display_name, password_hash, role) "
        "values ($1, $2, 'マネージャー', 'x:y', 'manager') returning id",
        tenant_id, f"m-{suffix}@example.test",
    )
    agent_id = await admin_conn.fetchval(
        "insert into users (tenant_id, email, display_name, password_hash, role) "
        "values ($1, $2, '担当者', 'x:y', 'agent') returning id",
        tenant_id, f"a-{suffix}@example.test",
    )
    list_id = await admin_conn.fetchval(
        "insert into contact_lists (tenant_id, name) values ($1, 'テストリスト') returning id",
        tenant_id,
    )

    yield {
        "tenant_id": str(tenant_id),
        "manager_id": str(manager_id),
        "agent_id": str(agent_id),
        "list_id": str(list_id),
    }
    await admin_conn.execute("delete from tenants where id = $1", tenant_id)


@pytest.fixture
def client():
    """★ lifespan の中で接続プールを作らせる。

    asyncpg の接続は「作られたイベントループ」に紐づく。TestClient は自分の
    ループで ASGI アプリを回すので、pytest 側のループでプールを作ると
    ConnectionDoesNotExistError になる。`with` に入れて lifespan を走らせ、
    プールを TestClient のループで作らせるのが正しい。
    """
    with TestClient(create_app(), raise_server_exceptions=False) as test_client:
        yield test_client


def headers(tenant, role: str = "manager") -> dict[str, str]:
    user = AuthUser(
        id=tenant[f"{role}_id"], tenant_id=tenant["tenant_id"],
        email="x@example.test", role=role,
    )
    return {"Authorization": f"Bearer {issue_token(user)}"}


# ---------------------------------------------------------------- 権限


# ★ 担当者の画面から数万件が動くと事故が大きい
@pytest.mark.parametrize(
    "method,path",
    [
        ("GET", "/api/admin/settings"),
        ("GET", "/api/admin/lists"),
        ("POST", "/api/admin/lists"),
        ("GET", "/api/admin/dnc"),
        ("POST", "/api/admin/dnc/import"),
        ("GET", "/api/admin/audit"),
    ],
)
def test_担当者は管理APIを使えない(client, tenant, method, path):
    res = client.request(method, path, headers=headers(tenant, "agent"), json={})
    assert res.status_code == 403


def test_認証なしでは使えない(client):
    assert client.get("/api/admin/settings").status_code == 401


# ---------------------------------------------------------------- 設定


def test_設定を取得すると変えられない項目も返る(client, tenant):
    res = client.get("/api/admin/settings", headers=headers(tenant))
    assert res.status_code == 200
    body = res.json()

    assert body["settings"]["calling_hours_start"] == "09:00"
    # ★ 設定項目が無いことに気付かず探し回るより、理由付きで見せる
    labels = [i["label"] for i in body["immutable"]]
    assert any("DNC" in label for label in labels)


def test_設定を更新できる(client, tenant):
    current = client.get("/api/admin/settings", headers=headers(tenant)).json()["settings"]
    current["calling_hours_end"] = "18:30"
    current["max_attempts_per_day"] = 2

    res = client.put("/api/admin/settings", headers=headers(tenant), json=current)
    assert res.status_code == 200

    updated = client.get("/api/admin/settings", headers=headers(tenant)).json()["settings"]
    assert updated["calling_hours_end"] == "18:30"
    assert updated["max_attempts_per_day"] == 2


def test_開始が終了以降なら拒否する(client, tenant):
    current = client.get("/api/admin/settings", headers=headers(tenant)).json()["settings"]
    current["calling_hours_start"] = "21:00"
    current["calling_hours_end"] = "09:00"
    assert client.put("/api/admin/settings", headers=headers(tenant), json=current).status_code == 400


# ★ 「消す仕組みが無い」状態を設定から作れないようにする
def test_録音の保存期間を無期限にはできない(client, tenant):
    current = client.get("/api/admin/settings", headers=headers(tenant)).json()["settings"]
    current["recording_retention_days"] = 0
    assert client.put("/api/admin/settings", headers=headers(tenant), json=current).status_code == 422

    current["recording_retention_days"] = 99999
    assert client.put("/api/admin/settings", headers=headers(tenant), json=current).status_code == 422


def test_架電する曜日を空にはできない(client, tenant):
    current = client.get("/api/admin/settings", headers=headers(tenant)).json()["settings"]
    current["calling_weekdays"] = []
    assert client.put("/api/admin/settings", headers=headers(tenant), json=current).status_code == 400


# ---------------------------------------------------------------- 取り込み


def test_連絡先を取り込める(client, tenant):
    res = client.post(
        f"/api/admin/lists/{tenant['list_id']}/contacts",
        headers=headers(tenant),
        json={"csv": "電話番号,会社名\n090-1111-2222,株式会社サンプル\n03-1234-5678,テスト商事\n"},
    )
    assert res.status_code == 200, res.text
    assert res.json()["inserted"] == 2


# ★ 「1000 件中 380 件だけ入った」を作らない
async def test_不正な行が1件でもあれば1件も入れない(client, tenant, admin_conn):
    res = client.post(
        f"/api/admin/lists/{tenant['list_id']}/contacts",
        headers=headers(tenant),
        json={"csv": "phone\n09011112222\nabc\n09033334444\n"},
    )
    assert res.status_code == 422
    detail = res.json()["detail"]
    assert detail["rejected"][0]["line"] == 3

    count = await admin_conn.fetchval(
        "select count(*) from contacts where list_id = $1", uuid.UUID(tenant["list_id"])
    )
    assert count == 0, "部分的に取り込まれている"


def test_dry_runでは取り込まない(client, tenant):
    res = client.post(
        f"/api/admin/lists/{tenant['list_id']}/contacts",
        headers=headers(tenant),
        json={"csv": "phone\n09011112222\nabc\n", "dry_run": True},
    )
    assert res.status_code == 200
    body = res.json()
    assert body["status"] == "dry_run"
    assert body["accepted"] == 1
    assert body["rejected_total"] == 1


# ★ 入れてしまうと、キューに出ないだけの「永久に消化されない行」が積み上がる
async def test_DNCにある番号はリストに入れない(client, tenant, admin_conn):
    await admin_conn.execute(
        "insert into dnc_entries (tenant_id, phone_e164, reason, source) "
        "values ($1, '+819011112222', 'refused', 'agent')",
        uuid.UUID(tenant["tenant_id"]),
    )

    res = client.post(
        f"/api/admin/lists/{tenant['list_id']}/contacts",
        headers=headers(tenant),
        json={"csv": "phone\n09011112222\n09033334444\n"},
    )
    assert res.status_code == 200
    assert res.json() == {**res.json(), "inserted": 1, "skipped_dnc": 1}

    rows = await admin_conn.fetch(
        "select phone_e164 from contacts where list_id = $1", uuid.UUID(tenant["list_id"])
    )
    assert [r["phone_e164"] for r in rows] == ["+819033334444"]


def test_他テナントのリストには入れられない(client, tenant):
    res = client.post(
        f"/api/admin/lists/{uuid.uuid4()}/contacts",
        headers=headers(tenant),
        json={"csv": "phone\n09011112222\n"},
    )
    assert res.status_code == 404


# ---------------------------------------------------------------- DNC


async def test_DNCを取り込むと既存の連絡先を対象外にする(client, tenant, admin_conn):
    """★ 取り込んだだけでリストに残っていると、関門で毎回止まる行になる。"""
    await admin_conn.execute(
        "insert into contacts (tenant_id, list_id, phone_e164) values ($1, $2, $3)",
        uuid.UUID(tenant["tenant_id"]), uuid.UUID(tenant["list_id"]), "+819011112222",
    )

    res = client.post(
        "/api/admin/dnc/import",
        headers=headers(tenant),
        json={"phones": "090-1111-2222\n"},
    )
    assert res.status_code == 200
    assert res.json()["archived_contacts"] == 1

    state = await admin_conn.fetchval(
        "select state from contacts where phone_e164 = '+819011112222' and tenant_id = $1",
        uuid.UUID(tenant["tenant_id"]),
    )
    assert state == "ARCHIVED"


# ★ 連絡先と違い、DNC は 1 件不正でも他は入れる。入れ過ぎても害がない側
def test_DNCは不正な行があっても他を取り込む(client, tenant):
    res = client.post(
        "/api/admin/dnc/import",
        headers=headers(tenant),
        json={"phones": "09011112222\nabc\n03-1234-5678\n"},
    )
    assert res.status_code == 200
    body = res.json()
    assert body["accepted"] == 2
    assert body["rejected_total"] == 1


def test_DNCの一覧が引ける(client, tenant):
    client.post(
        "/api/admin/dnc/import", headers=headers(tenant), json={"phones": "09011112222\n"}
    )
    body = client.get("/api/admin/dnc", headers=headers(tenant)).json()
    assert body["total"] == 1
    assert body["entries"][0]["phone_e164"] == "+819011112222"


# ---------------------------------------------------------------- 監査


def test_一括操作は監査ログに残る(client, tenant):
    client.post(
        f"/api/admin/lists/{tenant['list_id']}/contacts",
        headers=headers(tenant),
        json={"csv": "phone\n09011112222\n"},
    )
    body = client.get("/api/admin/audit", headers=headers(tenant)).json()
    actions = [entry["action"] for entry in body["logs"]]
    assert "contacts.imported" in actions


def test_関門で止まった件数が見える(client, tenant):
    """★ 「関門が機能している証跡」。急増は設定ミスかリスト品質の劣化を示す。"""
    body = client.get("/api/admin/audit", headers=headers(tenant)).json()
    assert "blocked_last_7d" in body


def test_今かけられるかを確認できる(client, tenant):
    body = client.get("/api/admin/calling-window", headers=headers(tenant)).json()
    assert isinstance(body["can_call_now"], bool)
    assert body["next_open"]
