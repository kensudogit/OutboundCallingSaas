-- ============================================================================
-- マネージド Postgres 向けのロール作成（Fly Postgres / RDS / Supabase など）。
--
-- ★ ローカルの docker-compose は db/init/00-roles.sql が初回起動時に走るので
--   これは不要。マネージド DB では初期化スクリプトを差し込めないため、
--   一度だけ手で流す。
--
-- ★ これを流さないまま DATABASE_URL にマネージド DB の既定ロール
--   （多くの場合 superuser か所有者）を指すと、RLS が「書かれているのに
--   1 行も効かない」状態になる。アプリは正常に動くので気付けない。
--   起動時の assert_rls_enforced() がこれを検出して止める。
--
-- 使い方（Fly Postgres の例）:
--
--   fly postgres connect -a <postgres-app> -d <database>
--   \i db/bootstrap-roles.sql
--
--   あるいは
--   psql "$ADMIN_DATABASE_URL" -f db/bootstrap-roles.sql
--
-- ★ パスワードは必ず変更すること。ここに書いてある値は使わない。
-- ============================================================================

\set app_password 'CHANGE_ME_app'
\set migrator_password 'CHANGE_ME_migrator'

-- ---------------------------------------------------------------- ロール

-- アプリの接続ユーザー。RLS が「効く」側。
-- ★ BYPASSRLS を絶対に付けない。付けた時点でテナント分離が無くなる
do $$
begin
  if not exists (select 1 from pg_roles where rolname = 'app_user') then
    create role app_user login;
  end if;
end $$;

-- マイグレーションと定期ジョブ。RLS を迂回する。
-- ★ アプリのリクエスト処理からは使わない
do $$
begin
  if not exists (select 1 from pg_roles where rolname = 'migrator') then
    create role migrator login bypassrls;
  end if;
end $$;

alter role app_user  with password :'app_password';
alter role migrator  with password :'migrator_password' bypassrls;

-- ---------------------------------------------------------------- 権限

-- 拡張（pgcrypto / citext）の作成に必要。どちらも trusted extension なので、
-- データベースへの CREATE 権限があれば非スーパーユーザーでも入れられる
grant create on database current_database() to migrator;
grant all    on schema public to migrator;
grant usage  on schema public to app_user;

-- migrator が今後作るテーブルの権限を app_user に自動で渡す。
-- ★ これが無いと、マイグレーションを流すたびに grant を手で打つことになり、
--   打ち忘れた日に本番でアプリだけが動かなくなる
alter default privileges for role migrator in schema public
  grant select, insert, update on tables to app_user;
alter default privileges for role migrator in schema public
  grant usage, select on sequences to app_user;

-- 既にテーブルがある場合（後からロールを作ったとき）の穴埋め
grant select, insert, update on all tables    in schema public to app_user;
grant usage,  select          on all sequences in schema public to app_user;

-- ★ 追記のみのテーブルは権限を落とす。マイグレーションの末尾と同じ内容だが、
--   後からロールを作った場合は上の一括 grant で戻ってしまうので、ここでも落とす
do $$
begin
  if exists (select 1 from pg_tables where schemaname = 'public'
              and tablename = 'dnc_entries') then
    revoke update, delete on dnc_entries        from app_user;
    revoke update, delete on audit_logs         from app_user;
    revoke update, delete on webhook_deliveries from app_user;
    revoke delete          on calls             from app_user;
  end if;
end $$;

-- ---------------------------------------------------------------- 確認

-- ★ app_user が RLS を素通りしないことを確かめる。
--   ここが t だと、ポリシーを書いても意味がない
select rolname, rolsuper, rolbypassrls,
       case when rolsuper or rolbypassrls
            then '★ RLS が効きません。この設定では使えません'
            else 'OK'
       end as verdict
  from pg_roles
 where rolname in ('app_user', 'migrator')
 order by rolname;
