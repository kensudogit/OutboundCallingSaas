"""実 DB に対する統合検証。

★ ここで確かめるのは、コードではなく **DB が守っているか** の 4 点。
  アプリのロジックを全部消しても成立していなければならない性質なので、
  モックでは検証にならない。

    1. RLS — アプリの WHERE 句を消しても他テナントが見えないか（原則 4）
    2. 予約の排他 — 2 人が同時に取っても同じ相手を掴まないか
    3. 状態の単調更新 — completed が先に着いても巻き戻らないか（原則 2）
    4. DNC の不可逆性 — アプリの接続ユーザーが消せないか

DB が無ければスキップする。CI では docker compose up -d の後に走らせる。

    docker compose up -d
    cd server && python -m app.db.migrate --drop
    python -m pytest tests/test_integration_db.py
"""

from __future__ import annotations

import os
import uuid

import asyncpg
import pytest

APP_DSN = os.environ.get(
    "TEST_APP_DSN", "postgresql://app_user:app_password@localhost:5434/calling"
)
ADMIN_DSN = os.environ.get(
    "TEST_ADMIN_DSN", "postgresql://migrator:migrator_password@localhost:5434/calling"
)


async def _connect(dsn: str):
    try:
        return await asyncpg.connect(dsn, timeout=3)
    except Exception as exc:  # noqa: BLE001
        pytest.skip(f"DB に接続できないためスキップします: {exc}")


@pytest.fixture
async def admin():
    conn = await _connect(ADMIN_DSN)
    try:
        yield conn
    finally:
        await conn.close()


@pytest.fixture
async def app_conn():
    conn = await _connect(APP_DSN)
    try:
        yield conn
    finally:
        await conn.close()


@pytest.fixture
async def two_tenants(admin):
    """A 社と B 社を作り、それぞれに連絡先を 1 件ずつ置く。"""
    # ★ 数字のみ。E.164 の check 制約があるので hex をそのまま使うと弾かれる
    #   （制約が効いている証拠でもある）
    suffix = f"{uuid.uuid4().int % 10**6:06d}"
    data = {}
    for key in ("a", "b"):
        tenant_id = await admin.fetchval(
            "insert into tenants (name, company_name) values ($1, $1) returning id",
            f"tenant-{key}-{suffix}",
        )
        user_id = await admin.fetchval(
            "insert into users (tenant_id, email, display_name, password_hash) "
            "values ($1, $2, $3, 'x:y') returning id",
            tenant_id, f"{key}-{suffix}@example.test", f"agent-{key}",
        )
        list_id = await admin.fetchval(
            "insert into contact_lists (tenant_id, name) values ($1, $2) returning id",
            tenant_id, f"list-{key}",
        )
        contact_id = await admin.fetchval(
            "insert into contacts (tenant_id, list_id, phone_e164, company_name) "
            "values ($1, $2, $3, $4) returning id",
            tenant_id, list_id, f"+8190{suffix}{'1' if key == 'a' else '2'}0",
            f"company-{key}",
        )
        data[key] = {
            "tenant_id": tenant_id, "user_id": user_id,
            "list_id": list_id, "contact_id": contact_id,
        }

    yield data

    for key in ("a", "b"):
        await admin.execute("delete from tenants where id = $1", data[key]["tenant_id"])


# ---------------------------------------------------------------- RLS


async def test_テナントを設定すれば自分の行だけ見える(app_conn, two_tenants):
    async with app_conn.transaction():
        await app_conn.execute(
            "select set_config('app.tenant_id', $1, true)", str(two_tenants["a"]["tenant_id"])
        )
        # ★ WHERE tenant_id を書いていない。それでも A 社の行しか返らない
        rows = await app_conn.fetch("select company_name from contacts")

    names = {r["company_name"] for r in rows}
    assert "company-a" in names
    assert "company-b" not in names


# ★ 設計を評価する視点その 3。ここが落ちたら分離は「していない」
async def test_テナント未設定なら1行も見えない(app_conn, two_tenants):
    """これが正しい失敗の仕方。既定値を持たせると、設定を忘れた接続が
    誰かのデータを読むことになる。
    """
    async with app_conn.transaction():
        rows = await app_conn.fetch("select * from contacts")
    assert rows == []


async def test_他テナントのIDを指定したINSERTは通らない(app_conn, two_tenants):
    """with check が無いと、読み取りは守られても書き込みが素通りする。"""
    with pytest.raises(asyncpg.InsufficientPrivilegeError):
        async with app_conn.transaction():
            await app_conn.execute(
                "select set_config('app.tenant_id', $1, true)",
                str(two_tenants["a"]["tenant_id"]),
            )
            await app_conn.execute(
                "insert into contacts (tenant_id, list_id, phone_e164) values ($1, $2, $3)",
                two_tenants["b"]["tenant_id"], two_tenants["b"]["list_id"], "+819099999999",
            )


async def test_他テナントの行はUPDATEできない(app_conn, two_tenants):
    async with app_conn.transaction():
        await app_conn.execute(
            "select set_config('app.tenant_id', $1, true)", str(two_tenants["a"]["tenant_id"])
        )
        result = await app_conn.execute(
            "update contacts set company_name = 'hacked' where id = $1",
            two_tenants["b"]["contact_id"],
        )
    assert result.endswith(" 0")   # 見えないので 0 行更新


async def test_トランザクションを抜ければ設定は消える(app_conn, two_tenants):
    """SET LOCAL であることの確認。SET だと接続がプールに戻った後も残り、
    次に同じ接続を掴んだ別テナントのリクエストに漏れる。
    """
    async with app_conn.transaction():
        await app_conn.execute(
            "select set_config('app.tenant_id', $1, true)", str(two_tenants["a"]["tenant_id"])
        )
        assert await app_conn.fetch("select 1 from contacts limit 1")

    async with app_conn.transaction():
        assert await app_conn.fetch("select 1 from contacts limit 1") == []


# ---------------------------------------------------------------- 予約の排他


async def test_2人が同時に予約を取っても同じ相手を掴まない(admin, two_tenants):
    """★ 二重発信を DB で止める。アプリのロックでは複数インスタンスで守れない。

    連絡先を 2 件にして、2 つの接続から同時に取りに行く。
    SKIP LOCKED が効いていれば別々の相手が返る。
    """
    tenant_id = two_tenants["a"]["tenant_id"]
    list_id = two_tenants["a"]["list_id"]
    agent_id = two_tenants["a"]["user_id"]

    await admin.execute(
        "insert into contacts (tenant_id, list_id, phone_e164, company_name) "
        "values ($1, $2, '+819011112222', 'second')",
        tenant_id, list_id,
    )

    from app.repositories.reservations import acquire_next

    c1 = await _connect(ADMIN_DSN)
    c2 = await _connect(ADMIN_DSN)
    try:
        tx1, tx2 = c1.transaction(), c2.transaction()
        await tx1.start()
        await tx2.start()
        for conn in (c1, c2):
            await conn.execute("select set_config('app.tenant_id', $1, true)", str(tenant_id))

        r1 = await acquire_next(c1, list_id=list_id, agent_id=agent_id, max_attempts_total=8)
        r2 = await acquire_next(c2, list_id=list_id, agent_id=agent_id, max_attempts_total=8)

        assert r1 is not None and r2 is not None
        # ★ ここが本題。同じ相手を返したら二重発信になる
        assert r1["contact_id"] != r2["contact_id"]

        await tx1.commit()
        await tx2.commit()
    finally:
        await c1.close()
        await c2.close()


async def test_保持中の予約は1連絡先に1件だけ(admin, two_tenants):
    """部分ユニークインデックスの確認。"""
    tenant_id = two_tenants["a"]["tenant_id"]
    contact_id = two_tenants["a"]["contact_id"]
    agent_id = two_tenants["a"]["user_id"]

    insert = (
        "insert into call_reservations (tenant_id, contact_id, agent_id, expires_at) "
        "values ($1, $2, $3, now() + interval '10 minutes')"
    )
    async with admin.transaction():
        await admin.execute("select set_config('app.tenant_id', $1, true)", str(tenant_id))
        await admin.execute(insert, tenant_id, contact_id, agent_id)

    # 2 件目は別トランザクションで試す（失敗するとトランザクションが中断されるため）
    with pytest.raises(asyncpg.UniqueViolationError):
        async with admin.transaction():
            await admin.execute(insert, tenant_id, contact_id, agent_id)


async def test_スキップした相手はすぐには戻ってこない(admin, two_tenants):
    """★ 実行して見つかった不具合の回帰テスト。

    release() は予約を RELEASED にするだけなので、対策が無いと次の
    queue/next で同じ相手が即座に返る。スキップが機能せず、担当者は
    前に進めないまま予約を握ってブラウザを閉じることになる。
    """
    tenant_id = two_tenants["a"]["tenant_id"]
    list_id = two_tenants["a"]["list_id"]
    agent_id = two_tenants["a"]["user_id"]
    other_agent = two_tenants["b"]["user_id"]

    from app.repositories.reservations import acquire_next, release

    async with admin.transaction():
        await admin.execute("select set_config('app.tenant_id', $1, true)", str(tenant_id))

        first = await acquire_next(
            admin, list_id=list_id, agent_id=agent_id, max_attempts_total=8
        )
        assert first is not None
        await release(admin, contact_id=str(first["contact_id"]), agent_id=agent_id)

        # 同じ担当者にはもう出ない（この連絡先しかないので None になる）
        again = await acquire_next(
            admin, list_id=list_id, agent_id=agent_id, max_attempts_total=8
        )
        assert again is None, "スキップした相手が即座に戻ってきている"

    # ★ 別の担当者には出る。特定の相手が誰からも架電されなくなるのを避ける
    async with admin.transaction():
        await admin.execute("select set_config('app.tenant_id', $1, true)", str(tenant_id))
        for_other = await acquire_next(
            admin, list_id=list_id, agent_id=other_agent, max_attempts_total=8
        )
        assert for_other is not None
        assert for_other["contact_id"] == first["contact_id"]


async def test_DNC登録済みの相手はキューに出ない(admin, two_tenants):
    """★ 関門とは別に、候補を絞る段階でも除外する。二重に守る。"""
    tenant_id = two_tenants["a"]["tenant_id"]
    list_id = two_tenants["a"]["list_id"]
    agent_id = two_tenants["a"]["user_id"]

    from app.repositories.reservations import acquire_next

    async with admin.transaction():
        await admin.execute("select set_config('app.tenant_id', $1, true)", str(tenant_id))
        phone = await admin.fetchval(
            "select phone_e164 from contacts where id = $1", two_tenants["a"]["contact_id"]
        )
        await admin.execute(
            "insert into dnc_entries (tenant_id, phone_e164, reason, source) "
            "values ($1, $2, 'refused', 'agent')",
            tenant_id, phone,
        )
        assert await acquire_next(
            admin, list_id=list_id, agent_id=agent_id, max_attempts_total=8
        ) is None


# ---------------------------------------------------------------- 状態の単調更新


async def test_completedが先に着いても状態が巻き戻らない(admin, two_tenants):
    """★ 原則 2 の核心。実運用で普通に起きる順序逆転を DB 側で吸収する。"""
    from app.repositories import calls as calls_repo

    tenant_id = two_tenants["a"]["tenant_id"]
    contact_id = two_tenants["a"]["contact_id"]
    agent_id = two_tenants["a"]["user_id"]
    sid = f"CA{uuid.uuid4().hex}"

    async with admin.transaction():
        await admin.execute("select set_config('app.tenant_id', $1, true)", str(tenant_id))

        # 発信 API のレスポンス
        await calls_repo.upsert(
            admin, provider_call_sid=sid, contact_id=contact_id,
            agent_id=agent_id, status="QUEUED",
        )
        # ★ completed が先に着く
        row = await calls_repo.upsert(
            admin, provider_call_sid=sid, contact_id=contact_id,
            status="COMPLETED", raw_status="completed", duration_sec=95,
        )
        assert row["status"] == "COMPLETED"
        assert row["duration_sec"] == 95

        # 後から answered が届いても巻き戻らない
        row = await calls_repo.upsert(
            admin, provider_call_sid=sid, contact_id=contact_id,
            status="ANSWERED", raw_status="in-progress", answered_by="human",
        )
        assert row["status"] == "COMPLETED", "状態が巻き戻っている"
        assert row["duration_sec"] == 95, "通話時間が失われている"
        assert row["answered_by"] == "human", "後から届いた情報が反映されていない"


async def test_重複配信で通話行が増えない(admin, two_tenants):
    """at-least-once 配信。completed が 3 回届いても 1 行のまま。"""
    from app.repositories import calls as calls_repo

    tenant_id = two_tenants["a"]["tenant_id"]
    contact_id = two_tenants["a"]["contact_id"]
    sid = f"CA{uuid.uuid4().hex}"

    async with admin.transaction():
        await admin.execute("select set_config('app.tenant_id', $1, true)", str(tenant_id))
        for _ in range(3):
            await calls_repo.upsert(
                admin, provider_call_sid=sid, contact_id=contact_id,
                status="COMPLETED", raw_status="completed", duration_sec=60,
            )
        count = await admin.fetchval(
            "select count(*) from calls where provider_call_sid = $1", sid
        )
    assert count == 1


async def test_時刻は最初に届いた値が残る(admin, two_tenants):
    from app.repositories import calls as calls_repo
    from datetime import datetime, timedelta, timezone

    tenant_id = two_tenants["a"]["tenant_id"]
    contact_id = two_tenants["a"]["contact_id"]
    sid = f"CA{uuid.uuid4().hex}"
    first = datetime.now(timezone.utc)
    later = first + timedelta(seconds=30)

    async with admin.transaction():
        await admin.execute("select set_config('app.tenant_id', $1, true)", str(tenant_id))
        await calls_repo.upsert(
            admin, provider_call_sid=sid, contact_id=contact_id,
            status="ANSWERED", answered_at=first,
        )
        row = await calls_repo.upsert(
            admin, provider_call_sid=sid, contact_id=contact_id,
            status="ANSWERED", answered_at=later,   # 重複配信で後から来た値
        )
    assert row["answered_at"] == first, "重複配信で時刻が上書きされている"


# ---------------------------------------------------------------- DNC の不可逆性


async def test_アプリユーザーはDNCを消せない(app_conn, admin, two_tenants):
    """★ 「消せる DNC」は、いつか消される。権限で落としてある。"""
    tenant_id = two_tenants["a"]["tenant_id"]
    async with admin.transaction():
        await admin.execute(
            "insert into dnc_entries (tenant_id, phone_e164, reason, source) "
            "values ($1, '+819088887777', 'refused', 'agent')",
            tenant_id,
        )

    for statement in (
        "delete from dnc_entries where phone_e164 = '+819088887777'",
        "update dnc_entries set reason = 'imported' where phone_e164 = '+819088887777'",
    ):
        with pytest.raises(asyncpg.InsufficientPrivilegeError):
            async with app_conn.transaction():
                await app_conn.execute(
                    "select set_config('app.tenant_id', $1, true)", str(tenant_id)
                )
                await app_conn.execute(statement)


async def test_アプリユーザーは監査ログを消せない(app_conn, two_tenants):
    tenant_id = two_tenants["a"]["tenant_id"]
    with pytest.raises(asyncpg.InsufficientPrivilegeError):
        async with app_conn.transaction():
            await app_conn.execute(
                "select set_config('app.tenant_id', $1, true)", str(tenant_id)
            )
            await app_conn.execute("delete from audit_logs")


async def test_アプリユーザーは通話を消せないが更新はできる(app_conn, two_tenants):
    """calls は upsert で UPDATE するので update は許可、delete は落とす。"""
    tenant_id = two_tenants["a"]["tenant_id"]
    with pytest.raises(asyncpg.InsufficientPrivilegeError):
        async with app_conn.transaction():
            await app_conn.execute(
                "select set_config('app.tenant_id', $1, true)", str(tenant_id)
            )
            await app_conn.execute("delete from calls")

    # update は通る。upsert が UPDATE を使うので落としてはいけない
    async with app_conn.transaction():
        await app_conn.execute("select set_config('app.tenant_id', $1, true)", str(tenant_id))
        await app_conn.execute("update calls set note = 'x' where false")
