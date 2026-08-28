-- ============================================================================
-- ロールの作成。postgres の初回起動時に自動実行される。
--
-- ★ 2 つのロールを分けるのが RLS の前提（原則 4）。
--
--   app_user  … アプリの接続ユーザー。RLS が「効く」側。
--                BYPASSRLS を絶対に付けない。
--   migrator  … マイグレーションと定期ジョブ。RLS を迂回する。
--                アプリのリクエスト処理からは使わない。
--
-- 1 つのロールで済ませると、RLS を書いても素通りしてしまい、
-- 「テナント分離しているつもり」の状態になる。
-- ============================================================================

create role app_user login password 'app_password';

-- migrator は BYPASSRLS を持つ。スキーマ適用時にポリシーに阻まれないため
create role migrator login password 'migrator_password' bypassrls;

-- 拡張（pgcrypto / citext）の作成に必要。どちらも trusted extension なので、
-- データベースへの CREATE 権限があれば非スーパーユーザーでも入れられる
grant create on database calling to migrator;
grant all on schema public to migrator;

grant usage on schema public to app_user;

-- migrator が今後作るテーブルの権限を app_user に自動で渡す。
-- ★ これが無いと、スキーマを流すたびに grant を手で打つことになり、
--   打ち忘れた日に本番でアプリだけが動かなくなる
alter default privileges for role migrator in schema public
  grant select, insert, update on tables to app_user;
alter default privileges for role migrator in schema public
  grant usage, select on sequences to app_user;
