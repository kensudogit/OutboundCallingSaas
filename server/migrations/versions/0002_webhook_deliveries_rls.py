"""webhook_deliveries にテナント分離を入れる

★ 修正する不具合。

  webhook_deliveries は Twilio のコールバックを生の JSON で保持している。
  その payload には From / To の電話番号が入る——つまり個人情報を持つのに、
  tenant_id が無く RLS も掛かっていなかった。アプリの接続ユーザーが
  他テナントの電話番号を全件読める状態だった（原則 4 の違反）。

  受信を記録する経路は tenant_tx の中で動くので、current_tenant_id() で
  埋められる。既存行は provider_call_sid から calls を引いて backfill する。

★ tenant_id を NOT NULL にはしない。自分が発信していない通話（Console の
  テストイベント等）のコールバックは対応する calls が無く、埋められない。
  それらは tenant_id が null のまま残り、RLS の `tenant_id = current_tenant_id()`
  が偽になるのでアプリからは 1 行も見えない——調査用に migrator からだけ
  見える、が正しい落としどころ。

Revision ID: 0002_webhook_rls
Revises: 0001_initial
Create Date: 2026-08-28
"""

from __future__ import annotations

from alembic import op

revision = "0002_webhook_rls"
down_revision = "0001_initial"
branch_labels = None
depends_on = None


UPGRADE = """
alter table webhook_deliveries
  add column if not exists tenant_id uuid references tenants(id) on delete cascade;

-- 既存行の backfill。対応する通話があるものだけ埋まる
update webhook_deliveries d
   set tenant_id = c.tenant_id
  from calls c
 where c.provider_call_sid = d.provider_call_sid
   and d.tenant_id is null;

-- テナント別に引けるようにする（調査でよく使う軸）
create index if not exists webhook_deliveries_tenant
  on webhook_deliveries (tenant_id, received_at desc);

alter table webhook_deliveries enable row level security;
alter table webhook_deliveries force row level security;

drop policy if exists tenant_isolation on webhook_deliveries;
create policy tenant_isolation on webhook_deliveries
  using      (tenant_id = current_tenant_id())
  with check (tenant_id = current_tenant_id());

-- ★ 受信記録は追記のみ。改竄されると障害調査の根拠にならない
revoke update, delete on webhook_deliveries from app_user;
"""

DOWNGRADE = """
drop policy if exists tenant_isolation on webhook_deliveries;
alter table webhook_deliveries disable row level security;
drop index if exists webhook_deliveries_tenant;
alter table webhook_deliveries drop column if exists tenant_id;
grant update, delete on webhook_deliveries to app_user;
"""


def upgrade() -> None:
    op.execute(UPGRADE)


def downgrade() -> None:
    op.execute(DOWNGRADE)
