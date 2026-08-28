-- ============================================================================
-- 架電特化型SaaS スキーマ
--
-- 設計の要点は references/data-model.md にある。ここで押さえているのは 4 点。
--
--   1. 電話番号は E.164 の 1 列だけ。DNC 照合は完全一致で行うため
--   2. calls は provider_call_sid で一意。3 経路が非同期に upsert する
--   3. 予約は部分ユニークインデックスで排他し、期限で自動解放する
--   4. 全テーブルで RLS。アプリの WHERE 句に分離を依存させない
--
-- 実行前に:
--   create role app_user login password '...';
--   create role migrator login password '...' bypassrls;
-- アプリは app_user で、マイグレーションと管理バッチは migrator で接続する。
-- ============================================================================

create extension if not exists pgcrypto;   -- gen_random_uuid()
create extension if not exists citext;     -- 大文字小文字を区別しないメールアドレス

-- ============================================================================
-- テナントの取り出し。
--
-- ★ nullif が要る。SET LOCAL のトランザクションを抜けた後、設定値は NULL では
--   なく「空文字」になる。素の current_setting(...)::uuid だと ''::uuid で
--   例外になり、「0 行が返る」ではなく 500 エラーになってしまう。
--   データは漏れないが、接続を使い回したかどうかで挙動が変わるのは良くない。
--
--   nullif を挟むと、未設定でも使い回しでも一貫して NULL になり、
--   NULL との比較は偽なので 1 行も見えない。これが正しい失敗の仕方。
-- ============================================================================
create or replace function current_tenant_id() returns uuid
  language sql stable parallel safe as $$
  select nullif(current_setting('app.tenant_id', true), '')::uuid;
$$;


-- ---------------------------------------------------------------- tenants

create table tenants (
  id          uuid primary key default gen_random_uuid(),
  name        text not null,
  -- 法令要件の設定。DNC の照合だけは設定で無効にできない（列を作らない）
  calling_timezone       text    not null default 'Asia/Tokyo',
  calling_hours_start    time    not null default '09:00',
  calling_hours_end      time    not null default '20:00',
  calling_weekdays       int[]   not null default '{1,2,3,4,5}',
  exclude_holidays       boolean not null default true,
  max_attempts_per_day   integer not null default 3,
  max_attempts_total     integer not null default 8,
  auto_dial_delay_sec    integer not null default 3,
  -- 冒頭の明示（特商法 第16条）。空にできないことをアプリ側で強制する
  company_name           text    not null,
  recording_retention_days integer not null default 365
    check (recording_retention_days between 1 and 3650),   -- 無期限にはできない
  ai_features_enabled    boolean not null default true,
  created_at  timestamptz not null default now()
);

-- ---------------------------------------------------------------- users

create table users (
  id            uuid primary key default gen_random_uuid(),
  tenant_id     uuid not null references tenants(id) on delete cascade,
  email         citext not null,
  display_name  text not null,
  password_hash text not null,
  role          text not null default 'agent'
    check (role in ('agent', 'manager', 'admin')),
  is_active     boolean not null default true,
  created_at    timestamptz not null default now(),
  unique (tenant_id, email)
);

-- ---------------------------------------------------------------- lists / contacts

create table contact_lists (
  id          uuid primary key default gen_random_uuid(),
  tenant_id   uuid not null references tenants(id) on delete cascade,
  name        text not null,
  is_active   boolean not null default true,
  created_at  timestamptz not null default now()
);

create table contacts (
  id            uuid primary key default gen_random_uuid(),
  tenant_id     uuid not null references tenants(id) on delete cascade,
  list_id       uuid not null references contact_lists(id) on delete cascade,
  -- ★ E.164 のみ。'090-1234-5678' のような表記は投入時に正規化して弾く
  phone_e164    text not null check (phone_e164 ~ '^\+[1-9][0-9]{6,14}$'),
  company_name  text,
  person_name   text,
  department    text,
  -- 相手側のタイムゾーン。国内のみなら既定のままでよい
  timezone      text not null default 'Asia/Tokyo',
  -- 同じ相手には同じ番号からかける（references/telephony.md）
  assigned_caller_id text,
  priority      integer not null default 0,
  state         text not null default 'ACTIVE'
    check (state in ('ACTIVE', 'EXHAUSTED', 'INVALID', 'ARCHIVED')),
  attributes    jsonb not null default '{}',
  created_at    timestamptz not null default now()
);

-- ★ 架電済みフラグを持たない。最終架電は calls から導出する。
--   フラグにすると、リスト再利用時のリセット漏れで永久に対象外の行が生まれる

-- ---------------------------------------------------------------- DNC

create table dnc_entries (
  id             uuid primary key default gen_random_uuid(),
  -- null 可。テナント共通の全社 DNC を持つ場合に使う（既定では使わない）
  tenant_id      uuid references tenants(id) on delete cascade,
  phone_e164     text not null check (phone_e164 ~ '^\+[1-9][0-9]{6,14}$'),
  reason         text not null
    check (reason in ('refused', 'do_not_call', 'complaint', 'imported', 'system')),
  source         text not null check (source in ('agent', 'import', 'api', 'system')),
  source_call_id uuid,
  created_by     uuid references users(id),
  created_at     timestamptz not null default now()
);

-- テナント別と全社共通で別々に一意にする（null は unique で重複扱いされないため）
create unique index dnc_entries_tenant_uniq
  on dnc_entries (tenant_id, phone_e164) where tenant_id is not null;
create unique index dnc_entries_global_uniq
  on dnc_entries (phone_e164) where tenant_id is null;

create index dnc_entries_lookup on dnc_entries (tenant_id, phone_e164);

-- ★ 消せないテーブルにする（権限の設定は末尾の grant の「後」で行う。
--   先に revoke しても、後続の一括 grant に上書きされてしまう）

-- ---------------------------------------------------------------- dispositions

create table dispositions (
  id          uuid primary key default gen_random_uuid(),
  tenant_id   uuid not null references tenants(id) on delete cascade,
  code        text not null,
  label       text not null,
  -- true なら結果登録と同時に DNC へ登録する（references/compliance.md）
  triggers_dnc boolean not null default false,
  -- 再架電の可否と間隔
  retry_after_minutes integer,
  sort_order  integer not null default 0,
  unique (tenant_id, code)
);

-- ---------------------------------------------------------------- reservations

create table call_reservations (
  id          uuid primary key default gen_random_uuid(),
  tenant_id   uuid not null references tenants(id) on delete cascade,
  contact_id  uuid not null references contacts(id) on delete cascade,
  agent_id    uuid not null references users(id),
  state       text not null default 'HELD'
    check (state in ('HELD', 'CONSUMED', 'RELEASED', 'EXPIRED')),
  expires_at  timestamptz not null,
  created_at  timestamptz not null default now()
);

-- ★ 二重発信を DB で止める。1 連絡先につき HELD は同時に 1 件だけ
create unique index call_reservations_one_active
  on call_reservations (contact_id) where state = 'HELD';

create index call_reservations_expiry on call_reservations (state, expires_at)
  where state = 'HELD';

-- ---------------------------------------------------------------- calls

-- 状態の順序。到着順が逆転しても巻き戻らないようにするための不変関数
create or replace function call_status_rank(status text) returns integer
  language sql immutable parallel safe as $$
  select case status
           when 'QUEUED'   then 1
           when 'RINGING'  then 2
           when 'ANSWERED' then 3
           when 'COMPLETED' then 4
           else 0
         end;
$$;

create table calls (
  id                uuid primary key default gen_random_uuid(),
  tenant_id         uuid not null references tenants(id) on delete cascade,
  contact_id        uuid not null references contacts(id),
  agent_id          uuid references users(id),
  reservation_id    uuid references call_reservations(id),
  -- ★ 3 経路（API 応答 / statusCallback / Media Stream）がこの列で同じ行に集まる
  provider_call_sid text not null,
  provider_stream_sid text,
  status            text not null default 'QUEUED'
    check (status in ('QUEUED', 'RINGING', 'ANSWERED', 'COMPLETED', 'UNKNOWN')),
  -- busy / no-answer / failed / canceled はここに残す。status には混ぜない
  raw_status        text,
  answered_by       text,
  caller_id         text,
  started_at        timestamptz,
  answered_at       timestamptz,
  ended_at          timestamptz,
  -- ★ ended_at - answered_at で計算しない。プロバイダの値をそのまま採る
  duration_sec      integer,
  disposition_id    uuid references dispositions(id),
  disposition_at    timestamptz,
  note              text,
  created_at        timestamptz not null default now(),
  updated_at        timestamptz not null default now(),
  unique (tenant_id, provider_call_sid)
);

create index calls_tenant_time on calls (tenant_id, started_at desc);
create index calls_contact on calls (contact_id, started_at desc);
create index calls_agent_day on calls (agent_id, started_at desc);

-- 関門で止まった発信。「関門が機能している証跡」であり監視項目でもある
create table call_attempts_blocked (
  id          uuid primary key default gen_random_uuid(),
  tenant_id   uuid not null references tenants(id) on delete cascade,
  contact_id  uuid not null references contacts(id),
  agent_id    uuid references users(id),
  reason      text not null,
  detail      text,
  created_at  timestamptz not null default now()
);

create index call_attempts_blocked_time on call_attempts_blocked (tenant_id, created_at desc);

-- 重複配信の実態を追えるようにしておく（処理自体は upsert で冪等）
create table webhook_deliveries (
  id              bigserial primary key,
  provider_call_sid text not null,
  event_type      text not null,
  payload         jsonb not null,
  received_at     timestamptz not null default now()
);

create index webhook_deliveries_call on webhook_deliveries (provider_call_sid, received_at);

-- ---------------------------------------------------------------- recordings

create table recordings (
  id                     uuid primary key default gen_random_uuid(),
  tenant_id              uuid not null references tenants(id) on delete cascade,
  call_id                uuid not null references calls(id) on delete cascade,
  provider_recording_sid text not null,
  provider_url           text,
  -- 自社ストレージへコピーした後のキー。プロバイダ側は削除する
  storage_key            text,
  duration_sec           integer,
  channels               integer not null default 1,
  -- ★ 保存期間は行に持つ。「消す仕組み」を最初に作る
  expires_at             timestamptz not null,
  deleted_at             timestamptz,
  created_at             timestamptz not null default now(),
  unique (tenant_id, provider_recording_sid)
);

create index recordings_expiry on recordings (expires_at) where deleted_at is null;

-- ---------------------------------------------------------------- transcripts

create table transcript_segments (
  id          bigserial primary key,
  tenant_id   uuid not null references tenants(id) on delete cascade,
  call_id     uuid not null references calls(id) on delete cascade,
  -- realtime: 通話中の暫定版 / batch: 録音からの確定版。両方残す
  source      text not null check (source in ('realtime', 'batch')),
  track       text not null check (track in ('inbound', 'outbound')),
  -- ★ ストリーム開始からの経過ms。受信時刻を使うと録音と頭出しがずれる
  started_ms  integer not null,
  ended_ms    integer not null,
  text        text not null,
  confidence  real,
  expires_at  timestamptz not null,
  created_at  timestamptz not null default now()
);

create index transcript_segments_call on transcript_segments (call_id, source, started_ms);

create table call_suggestions (
  id          bigserial primary key,
  tenant_id   uuid not null references tenants(id) on delete cascade,
  call_id     uuid not null references calls(id) on delete cascade,
  trigger_text text not null,      -- サジェストのきっかけになった相手の発話
  suggestion  text not null,
  shown_at    timestamptz not null default now(),
  used        boolean not null default false   -- 採否。プロンプト改善の材料になる
);

create table call_conversation_metrics (
  call_id              uuid primary key references calls(id) on delete cascade,
  tenant_id            uuid not null references tenants(id) on delete cascade,
  agent_talk_ms        integer not null,
  contact_talk_ms      integer not null,
  silence_ms           integer not null,
  overlap_ms           integer not null,
  agent_turns          integer not null,
  longest_monologue_ms integer not null,
  first_response_ms    integer,
  computed_at          timestamptz not null default now()
);

-- ---------------------------------------------------------------- sessions / rollup

create table agent_sessions (
  agent_id    uuid primary key references users(id) on delete cascade,
  tenant_id   uuid not null references tenants(id) on delete cascade,
  state       text not null default 'OFFLINE'
    check (state in ('OFFLINE', 'READY', 'RESERVED', 'DIALING', 'TALKING', 'WRAP_UP')),
  mode        text not null default 'PREVIEW' check (mode in ('PREVIEW', 'PROGRESSIVE')),
  list_id     uuid references contact_lists(id),
  stop_requested boolean not null default false,
  -- ★ ハートビート。途切れたら OFFLINE に落とし、保持中の予約を解放する
  updated_at  timestamptz not null default now()
);

create index agent_sessions_heartbeat on agent_sessions (updated_at)
  where state <> 'OFFLINE';

create table daily_agent_stats (
  tenant_id       uuid not null references tenants(id) on delete cascade,
  agent_id        uuid not null references users(id) on delete cascade,
  day             date not null,
  attempts        integer not null default 0,
  connected       integer not null default 0,
  human_connected integer not null default 0,
  appointments    integer not null default 0,
  refusals        integer not null default 0,
  talk_sec_total  integer not null default 0,
  updated_at      timestamptz not null default now(),
  primary key (tenant_id, agent_id, day)
);

-- ---------------------------------------------------------------- audit

create table audit_logs (
  id          bigserial primary key,
  tenant_id   uuid not null references tenants(id) on delete cascade,
  actor_id    uuid references users(id),
  action      text not null,   -- call.placed / call.blocked / dnc.added / recording.listen ...
  target_type text,
  target_id   uuid,
  detail      jsonb not null default '{}',
  created_at  timestamptz not null default now()
);

create index audit_logs_tenant_time on audit_logs (tenant_id, created_at desc);

-- ============================================================================
-- Row Level Security
--
-- ★ force を付けないとテーブル所有者が素通りする。
-- ★ with check がないと他テナントの tenant_id を指定した INSERT が通る。
-- ★ current_setting(..., true) にすると未設定時に NULL となり 1 行も見えない。
--   これが正しい失敗の仕方で、既定値を持たせると忘れた接続が他人のデータを読む。
-- ============================================================================

do $$
declare t text;
begin
  foreach t in array array[
    'users', 'contact_lists', 'contacts', 'dispositions', 'call_reservations',
    'calls', 'call_attempts_blocked', 'recordings', 'transcript_segments',
    'call_suggestions', 'call_conversation_metrics', 'agent_sessions',
    'daily_agent_stats', 'audit_logs'
  ]
  loop
    execute format('alter table %I enable row level security', t);
    execute format('alter table %I force row level security', t);
    execute format($p$
      create policy tenant_isolation on %I
        using      (tenant_id = current_tenant_id())
        with check (tenant_id = current_tenant_id())
    $p$, t);
  end loop;
end $$;

-- dnc_entries だけは全社共通行（tenant_id is null）の読み取りを許す
alter table dnc_entries enable row level security;
alter table dnc_entries force row level security;

create policy dnc_read on dnc_entries for select
  using (tenant_id is null
         or tenant_id = current_tenant_id());

create policy dnc_insert on dnc_entries for insert
  with check (tenant_id = current_tenant_id());

-- tenants 自身はアプリからは自テナントのみ
alter table tenants enable row level security;
alter table tenants force row level security;
create policy tenant_self on tenants
  using (id = current_tenant_id());

grant select, insert, update on all tables in schema public to app_user;
grant usage, select on all sequences in schema public to app_user;

-- ★ 追記のみのテーブルは、一括 grant の「後」で権限を落とす。
--   順序を逆にすると grant に上書きされ、何も守っていない状態になる。
--   dnc_entries を消せる作りにした時点で、いつか消される。
revoke update, delete on dnc_entries from app_user;
revoke update, delete on audit_logs  from app_user;
-- ★ calls は upsert で UPDATE するため revoke しない。DELETE だけ落とす
revoke delete on calls from app_user;
